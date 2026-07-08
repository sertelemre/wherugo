"""CLI davranışları: hızlandırılmış modda geçmişe damgalama + SIGTERM temiz kapanış."""
import json
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from wherugo_edge.main import main
from wherugo_edge.publisher import SpoolStore

DEMO_YAML = Path(__file__).resolve().parents[2] / "deploy" / "edge-demo.yaml"


def _wires(out: str):
    return [json.loads(line) for line in out.strip().splitlines() if line.strip()]


def _ts(wire) -> datetime:
    return datetime.fromisoformat(wire["envelope"]["event_time"].replace("Z", "+00:00"))


def test_accelerated_dry_run_stamps_events_in_past(capsys):
    """--start verilmemiş + speed != 1 → sim [now - duration, now] aralığında akar;
    olay zamanları GELECEĞE damgalanmaz (dashboard to=now pencereleri görür)."""
    before = datetime.now(timezone.utc)
    rc = main([
        "--config", str(DEMO_YAML), "--dry-run",
        "--duration", "60", "--speed", "0", "--seed", "7",
    ])
    after = datetime.now(timezone.utc)
    assert rc == 0
    wires = _wires(capsys.readouterr().out)
    assert wires  # en az kuyruk ölçümleri (+31 sn) üretilmiş olmalı
    times = [_ts(w) for w in wires]
    assert max(times) <= after + timedelta(seconds=1)  # gelecek yok
    assert min(times) >= before - timedelta(seconds=61)  # [now-duration, now] içinde


def test_explicit_start_still_honored(capsys):
    rc = main([
        "--config", str(DEMO_YAML), "--dry-run",
        "--duration", "40", "--speed", "0", "--seed", "7",
        "--start", "2026-01-02T03:04:05Z",
    ])
    assert rc == 0
    wires = _wires(capsys.readouterr().out)
    assert wires
    start = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    for w in wires:
        assert start < _ts(w) <= start + timedelta(seconds=40)


def test_sigterm_is_clean_shutdown_without_event_loss(tmp_path):
    """SIGTERM (make demo-stop / docker stop yolu) → finally flush+close çalışır:
    süreç 0 ile çıkar, spool ile seq sayacı tutarlı kalır (kayıp/sahte gap yok)."""
    spool = tmp_path / "spool.sqlite"
    proc = subprocess.Popen(
        [
            sys.executable, "-c",
            "import sys; from wherugo_edge.main import main; sys.exit(main(sys.argv[1:]))",
            "--config", str(DEMO_YAML), "--source", "simulate",
            "--speed", "50", "--duration", "3600", "--seed", "3",
            "--start", "2026-07-07T10:00:00Z",
            "--spool", str(spool),
            "--backend-url", "http://127.0.0.1:9",  # kapalı port: her POST hızla düşer
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Olaylar publish anında spool'a indiği için birkaç kaydı bekle.
        deadline = time.time() + 30
        seen = 0
        while time.time() < deadline and seen < 5:
            if spool.exists():
                try:
                    reader = SpoolStore(spool)
                    seen = reader.count()
                    reader.close()
                except Exception:
                    seen = 0
            time.sleep(0.2)
        assert seen >= 5, "simülatör zamanında olay üretmedi"
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert rc == 0, proc.stderr.read()  # 143 değil: temiz kapanış

    store = SpoolStore(spool)
    n = store.count()
    seqs = [seq for seq, _ in store.load(1_000_000)]
    next_seq = store.next_seq()
    store.close()
    assert n >= seen  # backend kapalıyken hiçbir olay atılmadı
    assert seqs == list(range(1, n + 1))  # delik yok
    assert next_seq == n + 1  # sayaç ile spool tutarlı → backend'de sahte seq_gap oluşmaz
