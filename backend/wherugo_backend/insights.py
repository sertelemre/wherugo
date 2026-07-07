"""metrics_bundle builder (input to wherugo_ai, CONTRACTS section 5) + optional
loading of the wherugo_ai package. The ai package is developed independently;
if it is not installed the briefing/assistant endpoints return 501."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import metrics
from .models import Store, Zone


@dataclass
class AIRuntime:
    briefing: Any
    assistant: Any
    provider: Any
    provider_name: str


def load_ai() -> AIRuntime | None:
    """Import wherugo_ai lazily; None when unavailable.

    Contract (CONTRACTS section 5):
        briefing.generate(metrics_bundle, store_name, date, provider) -> .text_md/.metric_refs
        assistant.answer(question, metrics_bundle, provider) -> .answer_md/.metrics_used
    Provider selection is delegated to wherugo_ai (env WHERUGO_LLM_PROVIDER)."""
    try:
        ai_pkg = importlib.import_module("wherugo_ai")
        briefing_mod = getattr(ai_pkg, "briefing", None) or importlib.import_module("wherugo_ai.briefing")
        assistant_mod = getattr(ai_pkg, "assistant", None) or importlib.import_module("wherugo_ai.assistant")
    except ImportError:
        return None

    provider = None
    for factory in ("get_provider", "make_provider", "provider_from_env"):
        fn = getattr(ai_pkg, factory, None)
        if callable(fn):
            try:
                provider = fn()
                break
            except Exception:
                provider = None
    if provider is None:
        try:
            providers_mod = importlib.import_module("wherugo_ai.providers")
            fn = getattr(providers_mod, "get_provider", None)
            if callable(fn):
                provider = fn()
            else:
                mock_cls = getattr(providers_mod, "MockProvider", None)
                provider = mock_cls() if mock_cls else None
        except ImportError:
            provider = None
    if provider is None:
        return None
    return AIRuntime(briefing=briefing_mod, assistant=assistant_mod,
                     provider=provider, provider_name=type(provider).__name__)


def build_metrics_bundle(session: Session, store_id: int,
                         t_from: datetime, t_to: datetime) -> dict[str, Any]:
    """Footfall/dwell/queue/funnel/gap summaries — every numeric claim in an
    AI briefing must reference a key of this dict."""
    store = session.get(Store, store_id)
    zones = session.scalars(select(Zone).where(Zone.store_id == store_id)).all()

    ff = metrics.footfall(session, store_id, t_from, t_to, "1h")
    occ = metrics.occupancy(session, store_id, t_from, t_to, "1h")
    conv = metrics.conversion(session, store_id, t_from, t_to, "1d")
    fun = metrics.funnel(session, store_id, t_from, t_to)
    gaps = metrics.coverage_gaps(session, store_id, t_from, t_to)

    dwell_by_zone: dict[str, Any] = {}
    for z in zones:
        if z.zone_type == "entrance":
            continue
        d = metrics.dwell_stats(session, store_id, z.id, t_from, t_to)
        if d["visits"] > 0:
            dwell_by_zone[z.name] = {
                "zone_id": z.id, "zone_type": z.zone_type,
                "p50_sec": d["stats"].get("p50"), "p95_sec": d["stats"].get("p95"),
                "visits": d["visits"], "draw_rate": d["draw_rate"],
            }

    queues: dict[str, Any] = {}
    for z in zones:
        if z.zone_type != "queue":
            continue
        q = metrics.queue_live(session, store_id, z.id, t_to)
        if q["latest"]:
            queues[z.name] = {
                "zone_id": z.id,
                "latest_queue_len": q["latest"]["queue_len"],
                "latest_est_wait_sec": q["latest"]["est_wait_sec"],
                "alert": q["alert"],
            }

    peak_hour = None
    if ff["series"]:
        best = max(ff["series"], key=lambda p: p["value"])
        if best["value"] > 0:
            peak_hour = {"ts": best["ts"], "footfall": best["value"]}

    return {
        "store": {"id": store_id, "name": store.name if store else str(store_id)},
        "window": {"from": t_from.isoformat() + "Z", "to": t_to.isoformat() + "Z"},
        "footfall": {"total": ff["total"], "peak_hour": peak_hour},
        "occupancy": {"peak": occ["total"]},
        "conversion": {"rate": conv["total"]},
        "dwell": dwell_by_zone,
        "queues": queues,
        "funnel": {s["name"]: s["value"] for s in fun["steps"]},
        "coverage_gaps": {"count": len(gaps["gaps"]), "total_minutes": gaps["total_minutes"]},
    }
