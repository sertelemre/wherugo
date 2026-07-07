"""Analytics Q&A assistant over a metrics bundle (CONTRACTS.md §5).

Mock mode routes on Turkish/English question keywords (queue / dwell /
footfall / conversion / coverage) and answers ONLY from the bundle; unknown
questions or missing data yield an explicit "cannot answer" — no fabrication.
Bundle schema: see :mod:`wherugo_ai.briefing` module docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .briefing import _fmt, _num, _peak_hour, extract_refs, ref_exists
from .providers import LLMProvider, MockProvider, get_provider

_SYSTEM_PROMPT = (
    "Sen bir perakende analitik asistanısın. Soruları Türkçe ve kısa yanıtlarsın.\n"
    "KURALLAR:\n"
    "1) YALNIZCA verilen metrik paketindeki sayıları kullan; pakette olmayan bir "
    "bilgi sorulursa 'bu veriyle yanıtlayamıyorum' de. Tahmin veya uydurma yok.\n"
    "2) Her sayısal iddianın yanına köşeli parantez içinde kaynak metrik anahtarını "
    "yaz; ör. [footfall_total], [queue.max_len], [zones.<bölge adı>.dwell_p50]."
)

_CANT_ANSWER = (
    "Bu soruyu eldeki metrik paketiyle yanıtlayamıyorum; ilgili veri pakette yok. "
    "Tahmin ya da uydurma yapmıyorum."
)

_QUEUE_KW = ("kuyruk", "bekleme", "kasa", "queue", "terk")
_COVERAGE_KW = ("kapsam", "boşluk", "bosluk", "coverage", "gap", "veri kalite")
_FUNNEL_KW = ("dönüşüm", "donusum", "conversion", "funnel", "huni", "satın", "satin", "satış", "satis")
_ZONE_KW = ("dwell", "bölge", "bolge", "zone", "reyon", "kalış", "kalis", "çekim", "cekim")
_FOOTFALL_KW = ("footfall", "ziyaret", "giriş", "giris", "trafik", "kaç kişi", "kac kisi", "yoğun", "yogun")


@dataclass(frozen=True)
class AnswerResult:
    answer_md: str
    metrics_used: list[str] = field(default_factory=list)


def _result(bundle: dict, text: str) -> AnswerResult:
    used = [r for r in extract_refs(text) if ref_exists(bundle, r)]
    return AnswerResult(answer_md=text, metrics_used=used)


def _cant(bundle: dict) -> AnswerResult:
    return AnswerResult(answer_md=_CANT_ANSWER, metrics_used=[])


def _answer_queue(bundle: dict) -> AnswerResult:
    q = bundle.get("queue")
    if not isinstance(q, dict):
        return _cant(bundle)
    parts: list[str] = []
    if _num(q.get("max_len")):
        parts.append(f"Kuyruk bugün en fazla **{_fmt(q['max_len'])}** kişiye ulaştı [queue.max_len].")
    if _num(q.get("avg_wait_sec")):
        parts.append(f"Ortalama bekleme {_fmt(q['avg_wait_sec'])} sn oldu [queue.avg_wait_sec].")
    if _num(q.get("abandons")):
        if q["abandons"] > 0:
            parts.append(f"{_fmt(q['abandons'])} müşteri kuyruğu terk etti [queue.abandons].")
        else:
            parts.append("Kuyruk terki gözlenmedi [queue.abandons].")
    if not parts:
        return _cant(bundle)
    return _result(bundle, " ".join(parts))


def _answer_coverage(bundle: dict) -> AnswerResult:
    gap = bundle.get("coverage_gap_min")
    if not _num(gap):
        return _cant(bundle)
    if gap > 0:
        text = (
            f"Bugün toplam {_fmt(gap)} dakikalık kapsam boşluğu kaydedildi "
            "[coverage_gap_min]; bu pencerelerdeki metrikler eksik sayım içerebilir."
        )
    else:
        text = "Kapsam boşluğu kaydedilmedi [coverage_gap_min]."
    return _result(bundle, text)


def _answer_funnel(bundle: dict) -> AnswerResult:
    f = bundle.get("funnel")
    if not isinstance(f, dict):
        return _cant(bundle)
    steps = [
        ("entered", "giren"),
        ("engaged", "bölgeyle ilgilenen"),
        ("interacted", "ürünle etkileşen"),
        ("transactions", "satın alan"),
    ]
    parts = [f"{_fmt(f[k])} {label} [funnel.{k}]" for k, label in steps if _num(f.get(k))]
    if not parts:
        return _cant(bundle)
    text = "Dönüşüm hunisi: " + " → ".join(parts) + "."
    if _num(f.get("entered")) and f["entered"] > 0 and _num(f.get("transactions")):
        rate = f["transactions"] / f["entered"] * 100
        text += f" Genel dönüşüm oranı %{_fmt(rate)} [funnel.entered][funnel.transactions]."
    return _result(bundle, text)


def _answer_zones(bundle: dict) -> AnswerResult:
    zones = [
        z
        for z in bundle.get("zones") or []
        if isinstance(z, dict) and z.get("name") and _num(z.get("dwell_p50"))
    ]
    if not zones:
        return _cant(bundle)
    ranked = sorted(zones, key=lambda z: (-float(z["dwell_p50"]), str(z["name"])))
    lines = ["Bölge kalış süreleri (medyan):"]
    for z in ranked[:3]:
        extra = ""
        if _num(z.get("visits")):
            extra = f", {_fmt(z['visits'])} ziyaret [zones.{z['name']}.visits]"
        lines.append(f"- **{z['name']}**: {_fmt(z['dwell_p50'])} sn{extra} [zones.{z['name']}.dwell_p50]")
    best, worst = ranked[0], ranked[-1]
    if best is not worst:
        lines.append(
            f"En uzun kalış **{best['name']}**, en kısa kalış **{worst['name']}** bölgesinde."
        )
    return _result(bundle, "\n".join(lines))


def _answer_footfall(bundle: dict) -> AnswerResult:
    parts: list[str] = []
    if _num(bundle.get("footfall_total")):
        parts.append(
            f"Bugün toplam **{_fmt(bundle['footfall_total'])}** ziyaretçi girişi ölçüldü [footfall_total]."
        )
    peak = _peak_hour(bundle.get("footfall_by_hour"))
    if peak:
        parts.append(f"En yoğun saat **{peak[0]}** ({_fmt(peak[1])} giriş) [footfall_by_hour].")
    if not parts:
        return _cant(bundle)
    return _result(bundle, " ".join(parts))


def _mock_answer(question: str, bundle: dict) -> AnswerResult:
    q = question.casefold()
    if any(kw in q for kw in _QUEUE_KW):
        return _answer_queue(bundle)
    if any(kw in q for kw in _COVERAGE_KW):
        return _answer_coverage(bundle)
    if any(kw in q for kw in _FUNNEL_KW):
        return _answer_funnel(bundle)
    if any(kw in q for kw in _ZONE_KW):
        return _answer_zones(bundle)
    if any(kw in q for kw in _FOOTFALL_KW):
        return _answer_footfall(bundle)
    return _cant(bundle)


def answer(
    question: str,
    metrics_bundle: dict,
    provider: LLMProvider | None = None,
) -> AnswerResult:
    """Answer an analytics question strictly from the metrics bundle."""
    if provider is None:
        provider = get_provider()
    bundle = metrics_bundle or {}
    if isinstance(provider, MockProvider):
        return _mock_answer(question, bundle)
    user = (
        f"Soru: {question}\n\n"
        "Metrik paketi (JSON):\n"
        f"{json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True)}"
    )
    text = provider.complete(_SYSTEM_PROMPT, user)
    return _result(bundle, text)
