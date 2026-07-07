"""metrics_bundle builder (input to wherugo_ai, CONTRACTS section 5) + optional
loading of the wherugo_ai package. The ai package is developed independently;
if it is not installed the briefing/assistant endpoints return 501."""
from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import metrics
from .models import QueueSample, Store, Zone


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
    """Bundle in the exact shape wherugo_ai documents (ai/wherugo_ai/briefing.py
    docstring): footfall_total, footfall_by_hour, zones[], queue{}, funnel{},
    coverage_gap_min. Every numeric claim in an AI briefing must reference a
    key of this dict."""
    store = session.get(Store, store_id)
    zones = session.scalars(select(Zone).where(Zone.store_id == store_id)).all()

    ff = metrics.footfall(session, store_id, t_from, t_to, "1h")
    conv = metrics.conversion(session, store_id, t_from, t_to, "1d")
    fun = metrics.funnel(session, store_id, t_from, t_to)
    gaps = metrics.coverage_gaps(session, store_id, t_from, t_to)

    footfall_by_hour: dict[str, int] = {}
    for pt in ff["series"]:
        hour = pt["ts"][11:13]
        footfall_by_hour[hour] = footfall_by_hour.get(hour, 0) + int(pt["value"])

    zones_out: list[dict[str, Any]] = []
    for z in zones:
        if z.zone_type == "entrance":
            continue
        d = metrics.dwell_stats(session, store_id, z.id, t_from, t_to)
        if d["visits"] > 0:
            zones_out.append({
                "name": z.name, "zone_type": z.zone_type,
                "visits": d["visits"],
                "dwell_p50": d["stats"].get("p50"), "dwell_p95": d["stats"].get("p95"),
                "draw_rate": d["draw_rate"],
            })

    q_row = session.execute(
        select(func.max(QueueSample.queue_len),
               func.avg(QueueSample.est_wait_sec),
               func.sum(QueueSample.abandons))
        .where(QueueSample.store_id == store_id,
               QueueSample.ts >= t_from,
               QueueSample.ts <= t_to)
    ).one()
    queue = None
    if q_row[0] is not None:
        queue = {
            "max_len": int(q_row[0]),
            "avg_wait_sec": round(float(q_row[1] or 0.0), 1),
            "abandons": int(q_row[2] or 0),
        }

    fun_map = {st["name"]: st["value"] for st in fun["steps"]}
    bundle: dict[str, Any] = {
        "store": {"id": store_id, "name": store.name if store else str(store_id)},
        "window": {"from": t_from.isoformat() + "Z", "to": t_to.isoformat() + "Z"},
        "footfall_total": int(ff["total"]),
        "footfall_by_hour": footfall_by_hour,
        "zones": zones_out,
        "funnel": {
            "entered": int(fun_map.get("entered", 0)),
            "engaged": int(fun_map.get("visited_zone", 0)),
            "interacted": int(fun_map.get("interacted", 0)),
            "transactions": int(fun_map.get("purchased", 0)),
        },
        "coverage_gap_min": gaps["total_minutes"],
    }
    if queue is not None:
        bundle["queue"] = queue
    if conv.get("has_data") and conv.get("total"):
        bundle["conversion_rate"] = conv["total"]
    return bundle
