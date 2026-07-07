"""Store-and-forward publisher: bellek kuyruğu + SQLite spool (CONTRACTS §0/§6).

Backend erişilebilirse HTTP batch POST /v1/ingest/events; değilse SQLite
spool'a biriktirir, sonraki flush'ta sırayla boşaltır (at-least-once).
seq_no cihaz başına monoton ve spool'da kalıcıdır.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

import requests

from . import privacy
from .config import EdgeConfig
from .events import Enveloper, Event

INGEST_PATH = "/v1/ingest/events"
MAX_BATCH = 500  # CONTRACTS §2: <=500 olay/batch


class SpoolStore:
    """SQLite spool: gönderilemeyen olaylar + kalıcı seq_no sayacı."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS spool (seq_no INTEGER PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._conn.commit()

    def next_seq(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key = 'next_seq'").fetchone()
        return int(row[0]) if row else 1

    def set_next_seq(self, value: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('next_seq', ?)", (str(int(value)),)
        )
        self._conn.commit()

    def append_many(self, wires: list[dict[str, Any]]) -> None:
        self._conn.executemany(
            "INSERT OR REPLACE INTO spool (seq_no, payload) VALUES (?, ?)",
            [(int(w["envelope"]["seq_no"]), json.dumps(w, ensure_ascii=False)) for w in wires],
        )
        self._conn.commit()

    def load(self, limit: int) -> list[tuple[int, dict[str, Any]]]:
        rows = self._conn.execute(
            "SELECT seq_no, payload FROM spool ORDER BY seq_no LIMIT ?", (int(limit),)
        ).fetchall()
        return [(int(seq), json.loads(payload)) for seq, payload in rows]

    def delete(self, seq_nos: list[int]) -> None:
        self._conn.executemany("DELETE FROM spool WHERE seq_no = ?", [(int(s),) for s in seq_nos])
        self._conn.commit()

    def count(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM spool").fetchone()[0])

    def close(self) -> None:
        self._conn.close()


class Publisher:
    """Olayları zarflar, kuyruğa alır, backend'e gönderir; olmadı spool'lar."""

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
        self.store = SpoolStore(spool_path or config.publisher.spool_path)
        self.enveloper = Enveloper(
            config.tenant_id,
            config.store_id,
            config.device_id,
            next_seq=self.store.next_seq(),
        )
        self.buffer: list[dict[str, Any]] = []

    def publish(self, event: Event) -> dict[str, Any]:
        """Olayı zarflar ve bellek kuyruğuna ekler; seq sayacını kalıcılaştırır."""
        wire = self.enveloper.wrap(event)
        privacy.assert_wire_safe(wire)
        self.store.set_next_seq(self.enveloper.next_seq)
        self.buffer.append(wire)
        return wire

    def _post(self, wires: list[dict[str, Any]]) -> bool:
        try:
            resp = requests.post(
                self.url,
                json={"events": wires},
                headers={"Authorization": f"Bearer {self.cfg.auth_token}"},
                timeout=self.timeout_sec,
            )
            return resp.status_code == 200
        except requests.RequestException:
            return False

    def _spool_buffer(self) -> int:
        n = len(self.buffer)
        if n:
            self.store.append_many(self.buffer)
            self.buffer.clear()
        return n

    def flush(self) -> dict[str, int]:
        """Önce spool'u (eski seq'ler), sonra bellek kuyruğunu sırayla gönderir."""
        sent = 0
        spooled = 0
        while True:
            batch = self.store.load(self.batch_size)
            if not batch:
                break
            if self._post([w for _, w in batch]):
                self.store.delete([s for s, _ in batch])
                sent += len(batch)
            else:
                spooled += self._spool_buffer()
                return {"sent": sent, "spooled": spooled, "pending_spool": self.store.count()}
        while self.buffer:
            batch_w = self.buffer[: self.batch_size]
            if self._post(batch_w):
                del self.buffer[: len(batch_w)]
                sent += len(batch_w)
            else:
                spooled += self._spool_buffer()
                break
        return {"sent": sent, "spooled": spooled, "pending_spool": self.store.count()}

    def close(self) -> None:
        """Gönderilememiş bellek kuyruğunu diske indirir ve bağlantıyı kapatır."""
        self._spool_buffer()
        self.store.close()
