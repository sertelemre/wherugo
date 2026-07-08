"""wherugo_ai — provider-agnostic LLM/VLM layer for WherUGo.

Standalone library: imports no other wherugo package; inputs are plain
dicts/strings (CONTRACTS.md §1, §5).
"""

from .assistant import AnswerResult, answer
from .briefing import BriefingResult, generate
from .providers import (
    AnthropicProvider,
    LLMProvider,
    MockProvider,
    OpenAICompatProvider,
    ProviderError,
    get_provider,
)
from .vlm import MockVLM, OpenAICompatVLM, VLMJudge, VLMVerdict, get_vlm

__version__ = "0.1.0"

__all__ = [
    "AnswerResult",
    "AnthropicProvider",
    "BriefingResult",
    "LLMProvider",
    "MockProvider",
    "MockVLM",
    "OpenAICompatProvider",
    "OpenAICompatVLM",
    "ProviderError",
    "VLMJudge",
    "VLMVerdict",
    "answer",
    "generate",
    "get_provider",
    "get_vlm",
    "__version__",
]
