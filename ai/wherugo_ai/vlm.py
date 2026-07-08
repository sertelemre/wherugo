"""VLM clip-judge: interface, real OpenAI-compat client and mock
(CONTRACTS.md §5 + v2 §15, docs/03 §3.2).

Real path — MiMo-VL-7B on on-prem vLLM (OpenAI-compatible vision chat):

    pip install vllm
    vllm serve XiaomiMiMo/MiMo-VL-7B-RL-2508 \\
        --host 0.0.0.0 --port 8002 \\
        --limit-mm-per-prompt '{"image": 8}' \\
        --max-model-len 32768

    export WHERUGO_VLM_BASE_URL=http://<gpu-host>:8002/v1
    export WHERUGO_VLM_MODEL=XiaomiMiMo/MiMo-VL-7B-RL-2508   # optional (default)
    export WHERUGO_VLM_API_KEY=...                           # optional

``--limit-mm-per-prompt`` must allow at least :data:`MAX_FRAMES` (8) images
per request because :meth:`OpenAICompatVLM.judge_clip` sends up to 8 frames
(older vLLM releases use the ``--limit-mm-per-prompt image=8`` syntax).

The edge writes 8 jpeg frames per interaction under ``clips/<date>/evt-<seq>/``
(LOCAL disk only, 72-hour TTL — CONTRACTS.md v2 §15); ``judge_clip`` accepts
that directory or a single jpg. The verdict is a *suggestion* stored in
``interaction_detected.vlm_verdict/vlm_conf`` — never ground truth without
human approval. Clips never leave the store network or reach commercial APIs.
Without ``WHERUGO_VLM_BASE_URL``, :func:`get_vlm` returns :class:`MockVLM`.
"""

from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .providers import ProviderError, _chat_completions_url, _post_json

DEFAULT_VLM_MODEL = "XiaomiMiMo/MiMo-VL-7B-RL-2508"
DEFAULT_VLM_TIMEOUT = 30.0
MAX_FRAMES = 8

VLM_SYSTEM_PROMPT = (
    "Sen bir mağaza içi davranış hakemisin. Sana bir perakende mağazasının "
    "analitik kamerasından alınmış, zaman sırasına dizilmiş kareler ve olay "
    "bağlamı verilecek. Görevin karelerdeki müşteri-ürün etkileşimini "
    "sınıflandırmak (ör. pickup, putback, interaction_candidate, "
    "queue_abandon, no_interaction) ve kararını kısa bir Türkçe gerekçeyle "
    "açıklamaktır. Kararın bir öneridir; emin değilsen conf değerini düşük "
    "tut. Yanıtı YALNIZCA şu şemada geçerli bir JSON nesnesi olarak ver, "
    "JSON dışında hiçbir metin yazma:\n"
    '{"label": "<etiket>", "conf": <0.0-1.0 arası sayı>, '
    '"rationale": "<kısa Türkçe gerekçe>"}'
)


@dataclass(frozen=True)
class VLMVerdict:
    label: str
    conf: float
    rationale: str


@runtime_checkable
class VLMJudge(Protocol):
    def judge_clip(self, clip_path: str, context: dict) -> VLMVerdict: ...


# --- frame collection / sampling -------------------------------------------

def _collect_frame_paths(clip_path: str) -> list[Path]:
    """clip_path is either a directory of frame-*.jpg files or a single jpg."""
    p = Path(clip_path)
    if p.is_dir():
        frames = sorted(
            f for f in p.iterdir()
            if f.is_file() and f.suffix.lower() in (".jpg", ".jpeg")
        )
        if not frames:
            raise ProviderError(f"no jpeg frames found in clip directory: {clip_path}")
        return frames
    if p.is_file():
        return [p]
    raise ProviderError(f"clip path does not exist: {clip_path}")


def _sample_evenly(items: list, k: int) -> list:
    """At most k items, evenly spaced, always keeping first and last."""
    n = len(items)
    if n <= k:
        return list(items)
    if k == 1:
        return [items[0]]
    return [items[(i * (n - 1)) // (k - 1)] for i in range(k)]


# --- response parsing guardrails --------------------------------------------

def _clamp_conf(value: Any) -> float:
    try:
        conf = float(value)
    except (TypeError, ValueError):
        return 0.0
    if conf != conf:  # NaN
        return 0.0
    return max(0.0, min(1.0, conf))


def _try_parse_json_object(text: str) -> dict | None:
    """Parse the whole text as JSON, else the outermost {...} slice (covers
    code fences and chatty prefixes). None if neither yields a dict."""
    candidates = [text.strip()]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            data = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _parse_verdict(text: str) -> VLMVerdict:
    """JSON-parse guardrail: valid JSON → verdict; broken JSON → salvage the
    label from the raw text; otherwise label='unparseable', conf=0.0 and the
    first 200 chars of the raw text as rationale."""
    data = _try_parse_json_object(text)
    if data is not None and data.get("label"):
        return VLMVerdict(
            label=str(data["label"]),
            conf=_clamp_conf(data.get("conf")),
            rationale=str(data.get("rationale") or "").strip(),
        )
    match = re.search(r'["\']?label["\']?\s*[:=]\s*["\']([^"\']+)["\']', text)
    if match is None:
        match = re.search(r"\blabel\b\s*[:=]\s*([A-Za-z_][\w.-]*)", text)
    if match:
        conf_match = re.search(
            r'["\']?conf(?:idence)?["\']?\s*[:=]\s*([0-9]*\.?[0-9]+)', text
        )
        rationale_match = re.search(
            r'["\']?rationale["\']?\s*[:=]\s*["\']([^"\']+)["\']', text
        )
        return VLMVerdict(
            label=match.group(1).strip(),
            conf=_clamp_conf(conf_match.group(1)) if conf_match else 0.0,
            rationale=(
                rationale_match.group(1).strip()
                if rationale_match
                else text.strip()[:200]
            ),
        )
    return VLMVerdict(label="unparseable", conf=0.0, rationale=text.strip()[:200])


def _extract_content_text(data: dict) -> str:
    """choices[0].message.content — plain string or content-parts list."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"unexpected chat/completions body: {str(data)[:300]}") from exc
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        if texts:
            return "".join(texts)
    raise ProviderError(f"unexpected content type: {type(content).__name__}")


# --- implementations ---------------------------------------------------------

class OpenAICompatVLM:
    """POST {base_url}/v1/chat/completions with vision content parts —
    MiMo-VL-7B on vLLM or any OpenAI-compatible vision server.

    Frames are read from disk, base64-encoded as ``data:image/jpeg;base64,...``
    image_url parts (at most :data:`MAX_FRAMES`, evenly sampled, sorted by
    filename). Network errors are wrapped in :class:`ProviderError` via the
    shared :func:`wherugo_ai.providers._post_json` helper.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = DEFAULT_VLM_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.name = f"openai-vlm:{model}"

    def judge_clip(self, clip_path: str, context: dict) -> VLMVerdict:
        frames = _sample_evenly(_collect_frame_paths(clip_path), MAX_FRAMES)
        content: list[dict[str, Any]] = [
            {"type": "text", "text": self._context_text(context, len(frames))}
        ]
        for frame in frames:
            encoded = base64.b64encode(frame.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                }
            )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": VLM_SYSTEM_PROMPT},
                {"role": "user", "content": content},
            ],
            "temperature": 0.2,
            "max_tokens": 512,
        }
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = _post_json(_chat_completions_url(self.base_url), body, headers, self.timeout)
        return _parse_verdict(_extract_content_text(data))

    @staticmethod
    def _context_text(context: dict, frame_count: int) -> str:
        ctx_json = json.dumps(context or {}, ensure_ascii=False, sort_keys=True)
        return (
            f"Olay bağlamı (JSON): {ctx_json}\n"
            f"Aşağıda olay anına ait {frame_count} kare zaman sırasıyla verilmiştir."
        )


class MockVLM:
    """Deterministic stub: never inspects the clip, returns conf=0.0 so the
    analytics layer can never mistake it for a real verdict."""

    name = "mock-vlm"

    def judge_clip(self, clip_path: str, context: dict) -> VLMVerdict:
        event_type = (context or {}).get("event_type", "unknown")
        return VLMVerdict(
            label=f"{event_type}_unverified",
            conf=0.0,
            rationale=(
                "MockVLM stub yanıtı: klip incelenmedi. Gerçek hakemlik "
                "MiMo-VL-7B vLLM entegrasyonu (Jetson klip altyapısı) gerektirir."
            ),
        )


def get_vlm() -> VLMJudge:
    """Env factory (CONTRACTS.md v2 §15): WHERUGO_VLM_BASE_URL set →
    :class:`OpenAICompatVLM` (WHERUGO_VLM_MODEL default
    ``XiaomiMiMo/MiMo-VL-7B-RL-2508``, WHERUGO_VLM_API_KEY optional);
    otherwise :class:`MockVLM`."""
    base_url = os.environ.get("WHERUGO_VLM_BASE_URL", "").strip()
    if not base_url:
        return MockVLM()
    return OpenAICompatVLM(
        base_url=base_url,
        model=os.environ.get("WHERUGO_VLM_MODEL", DEFAULT_VLM_MODEL),
        api_key=os.environ.get("WHERUGO_VLM_API_KEY") or None,
    )
