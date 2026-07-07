"""VLM clip-judge interface + MVP stub (CONTRACTS.md §5, docs/03 §3.2).

Real integration note — MiMo-VL-7B-RL-2508 on on-prem vLLM:
The production judge posts a 5-15 s clip (sampled at 2 FPS, max 256 frames)
to a vLLM server exposing the OpenAI-compatible /v1/chat/completions endpoint
(same wire protocol as :class:`wherugo_ai.providers.OpenAICompatProvider`,
plus video/image content parts). Low-latency classification runs the
``/no_think`` profile; anomaly explanation uses thinking mode. The verdict is
structured English JSON and is stored as a *suggestion* in ``vlm_verdict`` —
never ground truth without human approval. Clips carry a 72-hour TTL and
never leave the store network or reach commercial APIs. Real integration
requires the Jetson clip pipeline, so the MVP ships only this interface and
:class:`MockVLM`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class VLMVerdict:
    label: str
    conf: float
    rationale: str


@runtime_checkable
class VLMJudge(Protocol):
    def judge_clip(self, clip_path: str, context: dict) -> VLMVerdict: ...


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
