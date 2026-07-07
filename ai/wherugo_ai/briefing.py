"""Daily Turkish executive briefing from a metrics bundle (CONTRACTS.md §5).

``metrics_bundle`` is a flexible plain dict built by the backend
(``backend/wherugo_backend/insights.py``). Expected keys — every one of them
optional; sentences whose data is missing are simply skipped:

  footfall_total    : int                     — total visitor count for the day
  footfall_by_hour  : {"09": 18, ...}         — hour -> count map, or a list of
                                                {"hour": ..., "count": ...} items
  zones             : [{"name": str, "visits": int,
                        "dwell_p50": float(sec), "draw_rate": float(0-1)}, ...]
  queue             : {"max_len": int, "avg_wait_sec": float, "abandons": int}
  funnel            : {"entered": int, "engaged": int,
                       "interacted": int, "transactions": int}
  coverage_gap_min  : float                   — total coverage-gap minutes
  prev_day          : {"footfall_total": int, ...}  — optional day-over-day bridge

Metric references: every numeric claim in the output carries a bracketed
reference such as ``[footfall_total]`` or ``[zones.Kasa.dwell_p50]``. The
dotted path resolves against the bundle; list segments are matched by the
items' ``name`` field (zone names therefore must not contain dots).

Hallucination guardrails: with a real provider the system prompt forbids any
number not present in the bundle; with :class:`MockProvider` the text is a
pure deterministic template over the bundle — no model call at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .providers import LLMProvider, MockProvider, get_provider

_MISSING = object()
_REF_RE = re.compile(r"\[([^\[\]\n]+)\]")

_SYSTEM_PROMPT = (
    "Sen deneyimli bir perakende mağaza analistisin. Mağaza yöneticisi için "
    "Türkçe, kısa ve profesyonel bir günlük brifing yazarsın.\n"
    "KURALLAR:\n"
    "1) YALNIZCA verilen metrik paketindeki sayıları kullan; pakette olmayan "
    "hiçbir sayı, oran veya tahmin yazma.\n"
    "2) Her sayısal iddianın hemen yanına köşeli parantez içinde kaynak metrik "
    "anahtarını yaz; ör. \"312 ziyaretçi [footfall_total]\". İç içe alanlar için "
    "nokta gösterimi kullan: [queue.max_len], [zones.<bölge adı>.dwell_p50].\n"
    "3) Bir alan pakette yoksa o konuya hiç değinme.\n"
    "4) Çıktı Markdown olsun: başlık, kısa bölümler ve 2-3 somut aksiyon önerisi."
)


@dataclass(frozen=True)
class BriefingResult:
    text_md: str
    metric_refs: list[str] = field(default_factory=list)
    provider_name: str = "mock"


def resolve_ref(bundle: dict, ref: str):
    """Resolve a dotted metric reference against the bundle; _MISSING if absent."""
    node = bundle
    for part in ref.split("."):
        if isinstance(node, dict):
            if part in node:
                node = node[part]
                continue
            return _MISSING
        if isinstance(node, list):
            match = next(
                (item for item in node if isinstance(item, dict) and item.get("name") == part),
                _MISSING,
            )
            if match is _MISSING:
                return _MISSING
            node = match
            continue
        return _MISSING
    return node


def ref_exists(bundle: dict, ref: str) -> bool:
    return resolve_ref(bundle, ref) is not _MISSING


def extract_refs(text: str) -> list[str]:
    """Unique bracketed references in order of first appearance (markdown links skipped)."""
    refs: list[str] = []
    for m in _REF_RE.finditer(text):
        if text[m.end() : m.end() + 1] == "(":  # [text](url) markdown link
            continue
        ref = m.group(1).strip()
        if ref and ref not in refs:
            refs.append(ref)
    return refs


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _fmt(x) -> str:
    if isinstance(x, float):
        if x == int(x):
            return str(int(x))
        return f"{x:.1f}"
    return str(x)


def _peak_hour(fbh):
    pairs = []
    if isinstance(fbh, dict):
        pairs = [(h, c) for h, c in fbh.items() if _num(c)]
    elif isinstance(fbh, list):
        for item in fbh:
            if isinstance(item, dict) and "hour" in item:
                c = item.get("count", item.get("value"))
                if _num(c):
                    pairs.append((item["hour"], c))
    if not pairs:
        return None
    hour, count = max(pairs, key=lambda p: p[1])
    label = str(hour)
    if label.isdigit():
        label = f"{int(label):02d}:00"
    return label, count


def _mock_briefing(bundle: dict, store_name: str, date: str) -> str:
    sections: list[tuple[str, list[str]]] = []

    # --- Ziyaretçi trafiği ---
    traffic: list[str] = []
    ft = bundle.get("footfall_total")
    if _num(ft):
        traffic.append(f"Mağaza toplam **{_fmt(ft)}** ziyaretçi ağırladı [footfall_total].")
    peak = _peak_hour(bundle.get("footfall_by_hour"))
    if peak:
        traffic.append(
            f"En yoğun saat **{peak[0]}** oldu ({_fmt(peak[1])} giriş) [footfall_by_hour]."
        )
    prev = bundle.get("prev_day")
    if _num(ft) and isinstance(prev, dict) and _num(prev.get("footfall_total")) and prev["footfall_total"] > 0:
        change = (ft - prev["footfall_total"]) / prev["footfall_total"] * 100
        direction = "arttı" if change >= 0 else "azaldı"
        traffic.append(
            f"Ziyaretçi sayısı önceki güne göre %{_fmt(abs(change))} {direction} [prev_day.footfall_total]."
        )
    if traffic:
        sections.append(("## Ziyaretçi Trafiği", traffic))

    # --- Bölge performansı ---
    zone_lines: list[str] = []
    zones = [z for z in bundle.get("zones") or [] if isinstance(z, dict) and z.get("name")]
    with_dwell = [z for z in zones if _num(z.get("dwell_p50"))]
    if with_dwell:
        best = max(with_dwell, key=lambda z: z["dwell_p50"])
        worst = min(with_dwell, key=lambda z: z["dwell_p50"])
        zone_lines.append(
            f"En uzun kalış **{best['name']}** bölgesinde ölçüldü "
            f"(medyan {_fmt(best['dwell_p50'])} sn) [zones.{best['name']}.dwell_p50]."
        )
        if worst is not best:
            zone_lines.append(
                f"En kısa kalış **{worst['name']}** bölgesinde "
                f"({_fmt(worst['dwell_p50'])} sn) [zones.{worst['name']}.dwell_p50]."
            )
    with_draw = [z for z in zones if _num(z.get("draw_rate"))]
    if with_draw:
        top = max(with_draw, key=lambda z: z["draw_rate"])
        zone_lines.append(
            f"En yüksek çekim oranı **{top['name']}** bölgesinde "
            f"(%{_fmt(top['draw_rate'] * 100)}) [zones.{top['name']}.draw_rate]."
        )
    if zone_lines:
        sections.append(("## Bölge Performansı", zone_lines))

    # --- Kuyruk ---
    q = bundle.get("queue")
    queue_lines: list[str] = []
    if isinstance(q, dict):
        if _num(q.get("max_len")):
            queue_lines.append(
                f"Kasa kuyruğu en fazla **{_fmt(q['max_len'])}** kişiye ulaştı [queue.max_len]."
            )
        if _num(q.get("avg_wait_sec")):
            queue_lines.append(
                f"Ortalama bekleme süresi {_fmt(q['avg_wait_sec'])} sn oldu [queue.avg_wait_sec]."
            )
        if _num(q.get("abandons")):
            if q["abandons"] > 0:
                queue_lines.append(
                    f"**{_fmt(q['abandons'])}** müşteri kuyruğu terk etti [queue.abandons]."
                )
            else:
                queue_lines.append("Kuyruk terki gözlenmedi [queue.abandons].")
    if queue_lines:
        sections.append(("## Kuyruk", queue_lines))

    # --- Dönüşüm hunisi ---
    f = bundle.get("funnel")
    funnel_lines: list[str] = []
    if isinstance(f, dict):
        steps = [
            ("entered", "giren"),
            ("engaged", "bölgeyle ilgilenen"),
            ("interacted", "ürünle etkileşen"),
            ("transactions", "satın alan"),
        ]
        parts = [
            f"{_fmt(f[key])} {label} [funnel.{key}]"
            for key, label in steps
            if _num(f.get(key))
        ]
        if parts:
            funnel_lines.append("Huni: " + " → ".join(parts) + ".")
        if _num(f.get("entered")) and f["entered"] > 0 and _num(f.get("transactions")):
            rate = f["transactions"] / f["entered"] * 100
            funnel_lines.append(
                f"Genel dönüşüm oranı %{_fmt(rate)} [funnel.entered][funnel.transactions]."
            )
    if funnel_lines:
        sections.append(("## Dönüşüm Hunisi", funnel_lines))

    # --- Veri kalitesi ---
    gap = bundle.get("coverage_gap_min")
    quality_lines: list[str] = []
    if _num(gap):
        if gap > 0:
            quality_lines.append(
                f"Uyarı: bugün toplam {_fmt(gap)} dakikalık kapsam boşluğu yaşandı "
                "[coverage_gap_min]; bu pencerelerdeki metrikler eksik sayım içerebilir."
            )
        else:
            quality_lines.append("Kapsam boşluğu yaşanmadı [coverage_gap_min].")
    if quality_lines:
        sections.append(("## Veri Kalitesi", quality_lines))

    # --- Aksiyon önerileri ---
    actions: list[str] = []
    if isinstance(q, dict) and (
        (_num(q.get("max_len")) and q["max_len"] >= 5)
        or (_num(q.get("avg_wait_sec")) and q["avg_wait_sec"] >= 300)
    ):
        if _num(q.get("max_len")):
            actions.append(
                f"Yoğun saatlerde ek kasa açın; kuyruk {_fmt(q['max_len'])} kişiye "
                "kadar uzadı [queue.max_len]."
            )
        else:
            actions.append(
                f"Yoğun saatlerde ek kasa açın; ortalama bekleme {_fmt(q['avg_wait_sec'])} sn "
                "[queue.avg_wait_sec]."
            )
    if with_dwell:
        worst = min(with_dwell, key=lambda z: z["dwell_p50"])
        best = max(with_dwell, key=lambda z: z["dwell_p50"])
        if worst is not best:
            actions.append(
                f"**{worst['name']}** bölgesinin yerleşimini ve ürün sunumunu gözden geçirin; "
                f"medyan kalış yalnızca {_fmt(worst['dwell_p50'])} sn [zones.{worst['name']}.dwell_p50]."
            )
    if peak:
        actions.append(
            f"Personel planını **{peak[0]}** yoğunluğuna göre ayarlayın [footfall_by_hour]."
        )
    if _num(gap) and gap > 0:
        actions.append(
            "Kenar cihaz bağlantısını kontrol ettirin; kapsam boşluğu veri kalitesini "
            "düşürüyor [coverage_gap_min]."
        )
    if actions:
        sections.append(("## Önerilen Aksiyonlar", actions[:3]))

    out = [f"# {store_name} — Günlük Brifing ({date})"]
    if not sections:
        out += ["", "Bu tarih için yeterli metrik verisi bulunmuyor; brifing üretilemedi."]
        return "\n".join(out)
    for title, lines in sections:
        out += ["", title, ""]
        if title == "## Önerilen Aksiyonlar":
            out += [f"{i}. {line}" for i, line in enumerate(lines, 1)]
        else:
            out.append(" ".join(lines))
    return "\n".join(out)


def _user_prompt(bundle: dict, store_name: str, date: str) -> str:
    return (
        f"Mağaza: {store_name}\n"
        f"Tarih: {date}\n"
        "Metrik paketi (JSON):\n"
        f"{json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True)}\n\n"
        "Bu paketten günlük yönetici brifingini yaz."
    )


def generate(
    metrics_bundle: dict,
    store_name: str,
    date: str,
    provider: LLMProvider | None = None,
) -> BriefingResult:
    """Generate the daily briefing. Deterministic template on MockProvider;
    guard-railed LLM call otherwise."""
    if provider is None:
        provider = get_provider()
    bundle = metrics_bundle or {}
    if isinstance(provider, MockProvider):
        text = _mock_briefing(bundle, store_name, date)
    else:
        text = provider.complete(_SYSTEM_PROMPT, _user_prompt(bundle, store_name, date))
    return BriefingResult(
        text_md=text,
        metric_refs=extract_refs(text),
        provider_name=getattr(provider, "name", type(provider).__name__),
    )
