"""Store-and-forward publisher: SQLite spool önce, sonra HTTP (CONTRACTS §0/§6).

At-least-once garantisi: publish() olayı seq_no sayacıyla AYNI SQLite
transaction'ında spool'a yazar — ani çökmede (kill -9, elektrik kesintisi)
yayınlanmış hiçbir olay kaybolmaz; yeniden başlayan Publisher spool'dan
gönderir (çift gönderim backend'de event_id ile idempotent). flush() spool'dan
okur, backend 200 dönünce siler; RAM'de yalnız gönderim-içi geçici batch yaşar.
seq_no cihaz başına monoton ve spool'da kalıcıdır.

Dayanıklılık sınırları:
- Spool en fazla ``publisher.spool_max_events`` kayıt tutar (vars. 200k);
  aşılırsa en eski kayıtlar silinir ve stderr'e uyarı yazılır (backend eksik
  seq'leri coverage_gap olarak belgeler).
- Kalıcı 4xx yanıtları (400/413/422) ağ hatasından ayrılır: o batch spool'dan
  düşürülür ve stderr'e loglanır — bozuk batch sonsuz yeniden deneme
  döngüsüne giremez. Diğer hatalar (ağ, 5xx, 401...) geçici sayılır ve
  olaylar spool'da bekler.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

import requests

from . import privacy
from .config import EdgeConfig
from .events import Enveloper, Event

INGEST_PATH = "/v1/ingest/events"
MAX_BATCH = 500  # CONTRACTS §2: <=500 olay/batch
DEFAULT_MAX_SPOOL_EVENTS = 200_000
# Yeniden denemenin asla düzeltemeyeceği istemci hataları: batch düşürülür.
PERMANENT_REJECT_STATUSES = frozenset({400, 413, 422})


class SpoolStore:
    """SQLite spool: olaylar + kalıcı seq_no sayacı (tek transaction'da).

    max_events > 0 ise kayıt sayısı sınırlanır: sınır aşıldığında en eski
    (en düşük seq_no'lu) kayıtlar silinir ve stderr'e uyarı yazılır.
    """

    def __init__(self, path: str | Path, *, max_events: int = DEFAULT_MAX_SPOOL_EVENTS) -> None:
        self.path = str(path)
        self.max_events = max(0, int(max_events))
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS spool (seq_no INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.commit()
        self._count = self._recount()

    def _recount(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0])

    def next_seq(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'next_seq'").fetchone()
        return int(row[0]) if row else 1

    def set_next_seq(self, value: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('next_seq', ?)", (str(int(value)),)
        )
        self._conn.commit()

    def append_many(self, wires: list[dict[str, Any]], *, next_seq: Optional[int] = None) -> None:
        """Olayları spool'a yazar; next_seq verilirse sayacı AYNI transaction'da günceller.

        Olay + sayaç birlikte commit edilir: arada çökülürse ikisi tutarlı kalır
        (sayaç ilerlemiş ama olay kaybolmuş durumu oluşamaz).
        """
        if not wires and next_seq is None:
            return
        self._conn.executemany(
            "INSERT OR REPLACE INTO spool (seq_no, payload) VALUES (?, ?)",
            [(int(w["envelope"]["seq_no"]), json.dumps(w, ensure_ascii=False)) for w in wires],
        )
        if next_seq is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('next_seq', ?)",
                (str(int(next_seq)),),
            )
        self._count += len(wires)
        dropped = 0
        if self.max_events > 0 and self._count > self.max_events:
            dropped = self._count - self.max_events
            self._conn.execute(
                "DELETE FROM spool WHERE seq_no IN "
                "(SELECT seq_no FROM spool ORDER BY seq_no LIMIT ?)",
                (dropped,),
            )
        self._conn.commit()
        if dropped:
            self._count = self._recount()
            print(
                f"uyarı: spool sınırı ({self.max_events} kayıt) aşıldı; en eski {dropped} olay "
                "silindi (backend eksik seq'leri coverage_gap olarak kaydeder)",
                file=sys.stderr,
            )

    def load(self, limit: int) -> list[tuple[int, dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT seq_no, payload FROM spool ORDER BY seq_no LIMIT ?", (int(limit),)
        ).fetchall()
        return [(int(seq), json.loads(payload)) for seq, payload in rows]

    def delete(self, seq_nos: list[int]) -> None:
        self._conn.executemany("DELETE FROM spool WHERE seq_no = ?", [(int(s),) for s in seq_nos])
        self._conn.commit()
        self._count = self._recount()

    def count(self) -> int:
        return self._count

    def close(self) -> None:
        self._conn.close()


class Publisher:
    """Olayları zarflar, spool'a kalıcılaştırır, backend'e sırayla gönderir."""

    def __init__(
        self,
        config: EdgeConfig,
        *,
        spool_path: Optional[str | Path] = None,
        timeout_sec: float = 5.0,
    ) -> None:
        self.cfg = config
        self.timeout_sec = timeout_sec
        self.url = config.backend_url.rstrip("/") + INGEST_PATH
        self.batch_size = min(MAX_BATCH, max(1, config.publisher.batch_size))
        self.store = SpoolStore(
            spool_path or config.publisher.spool_path,
            max_events=config.publisher.spool_max_events,
        )
        self.enveloper = Enveloper(
            config.tenant_id,
            config.store_id,
            config.device_id,
            next_seq=self.store.next_seq(),
        )
        self.unflushed = 0  # son flush'tan beri publish edilen olay sayısı
        self._last_transient: Optional[str] = None  # tekrarlanan uyarıyı sustur

    def publish(self, event: Event) -> dict[str, Any]:
        """Olayı zarflar ve spool'a YAZAR (seq sayacıyla aynı transaction'da kalıcı).

        Ani çökmede olay kaybolmaz: bir sonraki Publisher spool'dan gönderir.
        """
        wire = self.enveloper.wrap(event)
        privacy.assert_wire_safe(wire)
        self.store.append_many([wire], next_seq=self.enveloper.next_seq)
        self.unflushed += 1
        return wire

    def _post(self, wires: list[dict[str, Any]]) -> Optional[int]:
        """HTTP durum kodunu döndürür; ağ hatasında None (geçici, tekrar denenir)."""
        try:
            resp = requests.post(
                self.url,
                json={"events": wires},
                headers={"Authorization": f"Bearer {self.cfg.auth_token}"},
                timeout=self.timeout_sec,
            )
            return resp.status_code
        except requests.RequestException:
            return None

    def flush(self) -> dict[str, int]:
        """Spool'u seq sırasıyla gönderir; 200'de siler, kalıcı 4xx'te düşürür.

        Geçici hatada (ağ / 5xx / auth) olaylar spool'da kalır ve bir sonraki
        flush'ta yeniden denenir (at-least-once).
        """
        sent = 0
        dropped = 0
        while True:
            batch = self.store.load(self.batch_size)
            if not batch:
                break
            seqs = [s for s, _ in batch]
            status = self._post([w for _, w in batch])
            if status == 200:
                self.store.delete(seqs)
                sent += len(batch)
            elif status in PERMANENT_REJECT_STATUSES:
                # Yeniden deneme bu batch'i asla geçiremez: düşür ve devam et.
                self.store.delete(seqs)
                dropped += len(batch)
                print(
                    f"uyarı: backend {status} (kalıcı red) döndü; {len(batch)} olay "
                    f"(seq {seqs[0]}-{seqs[-1]}) spool'dan düşürüldü",
                    file=sys.stderr,
                )
            else:
                # Geçici hata: spool'da beklet, sonraki flush'ta dene. Aynı hata
                # üst üste tekrar ederse yalnız ilkinde uyar (stderr spam'i yok).
                reason = "ağ hatası" if status is None else f"HTTP {status}"
                if reason != self._last_transient:
                    print(
                        f"uyarı: ingest gönderimi başarısız ({reason}); "
                        f"{self.store.count()} olay spool'da bekliyor, yeniden denenecek",
                        file=sys.stderr,
                    )
                    self._last_transient = reason
                break
        if sent:
            self._last_transient = None
        self.unflushed = 0
        return {"sent": sent, "dropped": dropped, "pending_spool": self.store.count()}

    def close(self) -> None:
        """Spool bağlantısını kapatır (olaylar publish() anında zaten diskte)."""
        self.store.close()
