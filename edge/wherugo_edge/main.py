"""CLI: wherugo-edge --config <yaml> [--speed N] [--duration SN] [--seed N] [--dry-run]."""
from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import privacy
from .config import ConfigError, load_config
from .events import Enveloper
from .publisher import Publisher
from .sources.simulate import run_simulation


def build_arg_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="wherugo-edge",
        description="WherUGo kenar ajanı: sentetik/gerçek kaynak -> zone olayları -> backend",
    )
    ap.add_argument("--config", required=True, help="YAML config yolu (örn. deploy/edge-demo.yaml)")
    ap.add_argument("--source", choices=["simulate", "video"], default="simulate")
    ap.add_argument("--speed", type=float, default=1.0, help="sim hız çarpanı (sim-sn / duvar-sn; 0 = tam hız)")
    ap.add_argument("--duration", type=float, default=3600.0, help="simüle edilecek süre (sim saniyesi)")
    ap.add_argument("--seed", type=int, default=None, help="determinizm için RNG tohumu")
    ap.add_argument("--dry-run", action="store_true", help="HTTP yok; olayları stdout'a JSON satırları olarak yaz")
    ap.add_argument(
        "--start",
        default=None,
        help=(
            "sim başlangıç zamanı (RFC3339). Varsayılan: --speed 1 ise şimdi; "
            "--speed != 1 ise şimdi - --duration, yani sim geçmişten şimdiye doğru "
            "akar ve olay zamanları geleceğe damgalanmaz. track_id tabanı da bu "
            "başlangıçtan türetilir (aynı seed + aynı --start → aynı olaylar)"
        ),
    )
    ap.add_argument("--spool", default=None, help="SQLite spool yolu (vars: config publisher.spool_path)")
    ap.add_argument("--video-source", default=None, help="--source video için RTSP URL / dosya yolu")
    ap.add_argument(
        "--backend-url",
        default=None,
        help="config'teki backend_url'i geçersiz kıl (vars: env WHERUGO_BACKEND_URL, sonra config)",
    )
    return ap


def _parse_start(raw: Optional[str]) -> Optional[datetime]:
    if raw is None:
        return None
    dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _install_sigterm_handler() -> None:
    """SIGTERM'i KeyboardInterrupt'a çevirir: docker stop / make demo-stop /
    `kill <pid>` yollarında da finally blokları (publisher flush+close) çalışır."""

    def _raise_interrupt(_signum, _frame):  # pragma: no cover - sinyal bağlamı
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _raise_interrupt)
    except ValueError:
        pass  # ana thread dışında (örn. test host'u): sinyal kurulamaz, sorun değil


def _run_video(cfg, args) -> int:
    from .sources.video import open_source  # [cv] yoksa anlaşılır ImportError

    if not args.video_source:
        print("hata: --source video için --video-source (RTSP URL / dosya) gerekli", file=sys.stderr)
        return 2
    from .zones import ZoneEngine

    src = open_source(cfg, args.video_source)
    ep = cfg.engine
    engine = ZoneEngine(
        cfg.zones,
        hysteresis_samples=ep.hysteresis_samples,
        dwell_threshold_sec=ep.dwell_threshold_sec,
        interaction_dwell_sec=ep.interaction_dwell_sec,
        queue_interval_sec=ep.queue_interval_sec,
        service_time_sec=ep.service_time_sec,
    )
    publisher = None if args.dry_run else Publisher(cfg, spool_path=args.spool)
    enveloper = Enveloper(cfg.tenant_id, cfg.store_id, cfg.device_id) if args.dry_run else None

    started = time.monotonic()
    try:
        _video_loop(src, engine, publisher, enveloper, args, started)
    except KeyboardInterrupt:
        pass  # SIGINT/SIGTERM: temiz kapanış (finally flush+close yapar)
    finally:
        if publisher is not None:
            publisher.flush()
            publisher.close()
    return 0


def _video_loop(src, engine, publisher, enveloper, args, started: float) -> None:
    from .events import Quality, TrackUpdate

    for sample in src.samples():
        events = [
            TrackUpdate(
                event_time=sample.ts,
                camera_id=sample.camera_id,
                track_id=sample.track_id,
                x_m=sample.x_m,
                y_m=sample.y_m,
                sigma_cm=sample.sigma_cm,
                is_staff=sample.is_staff,
                quality=Quality(conf=sample.conf, coverage_ok=True),
            )
        ]
        events.extend(engine.process(sample))
        events.extend(engine.tick(sample.ts))
        for ev in events:
            if publisher is not None:
                publisher.publish(ev)
            else:
                wire = enveloper.wrap(ev)
                privacy.assert_wire_safe(wire)
                print(json.dumps(wire, ensure_ascii=False))
        if publisher is not None:
            publisher.flush()
        if args.duration and time.monotonic() - started >= args.duration:
            break


def main(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _install_sigterm_handler()
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config hatası: {exc}", file=sys.stderr)
        return 2

    backend_override = args.backend_url or os.environ.get("WHERUGO_BACKEND_URL")
    if backend_override:
        cfg.backend_url = backend_override.rstrip("/")

    if args.source == "video":
        return _run_video(cfg, args)

    try:
        start_time = _parse_start(args.start)
    except ValueError as exc:
        print(f"--start ayrıştırılamadı: {exc}", file=sys.stderr)
        return 2

    if start_time is None and args.speed != 1.0:
        # Hızlandırılmış (veya tam hız) modda sim saati duvar saatinden hızlı
        # akar; "şimdi"den başlatmak olayları geleceğe damgalar ve to=now
        # pencereli sorgularda görünmez kılar. Geçmişten başlat: sim
        # [now - duration, now] aralığında şimdiye doğru akar.
        start_time = datetime.now(timezone.utc) - timedelta(seconds=float(args.duration))

    gen = run_simulation(cfg, seed=args.seed, duration_sec=args.duration, start_time=start_time)
    sleep_per_step = (1.0 / args.speed) if args.speed and args.speed > 0 else 0.0

    if args.dry_run:
        rng = random.Random(args.seed) if args.seed is not None else None
        enveloper = Enveloper(cfg.tenant_id, cfg.store_id, cfg.device_id, rng=rng)
        for _, events in gen:
            for ev in events:
                wire = enveloper.wrap(ev)
                privacy.assert_wire_safe(wire)
                print(json.dumps(wire, ensure_ascii=False))
            if sleep_per_step:
                time.sleep(sleep_per_step)
        return 0

    publisher = Publisher(cfg, spool_path=args.spool)
    flush_every = max(1, int(cfg.publisher.flush_interval_sec * max(1.0, args.speed or 1.0)))
    step_i = 0
    try:
        for _, events in gen:
            for ev in events:
                publisher.publish(ev)
            step_i += 1
            if publisher.unflushed >= publisher.batch_size or step_i % flush_every == 0:
                publisher.flush()
            if sleep_per_step:
                time.sleep(sleep_per_step)
    except KeyboardInterrupt:
        pass
    finally:
        publisher.flush()
        publisher.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
