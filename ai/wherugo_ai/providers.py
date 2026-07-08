"""Provider-agnostic LLM access layer (CONTRACTS.md §5).

Implementations use only the standard library (urllib) for HTTP; the official
anthropic/openai SDKs are an OPTIONAL extra and are never imported here.

Provider selection via environment (see :func:`get_provider`):
  WHERUGO_LLM_PROVIDER = mock | openai | anthropic   (default: mock)
  WHERUGO_LLM_BASE_URL   (openai-compat endpoint, e.g. MiMo vLLM / OpenRouter / LM Studio)
  WHERUGO_LLM_MODEL
  WHERUGO_LLM_API_KEY    (anthropic also falls back to ANTHROPIC_API_KEY)
"""

from __future__ import annotations

import http.client
import json
import os
from typing import Any, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT = 60.0
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_OPENAI_MODEL = "XiaomiMiMo/MiMo-7B-RL"


class ProviderError(RuntimeError):
    """Raised for configuration, network or malformed-response errors."""


@runtime_checkable
class LLMProvider(Protocol):
    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str: ...


def _post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    payload = json.dumps(body).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **headers}
    req = Request(url, data=payload, headers=all_headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = ""
        raise ProviderError(f"HTTP {exc.code} from {url}: {detail or exc.reason}") from exc
    except URLError as exc:
        raise ProviderError(f"cannot reach {url}: {exc.reason}") from exc
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        # Gövde okuma sırasındaki timeout / kesik yanıt (IncompleteRead) /
        # bağlantı kopması da ProviderError olarak sarılır; ham TimeoutError
        # veya HTTPException çağırana sızmaz. (TimeoutError, OSError'ın alt
        # sınıfı olsa da açıkça listelenir; HTTPError/URLError yukarıda ele
        # alındığı için buraya düşmez.)
        raise ProviderError(f"network error while reading response from {url}: {exc!r}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"non-JSON response from {url}: {raw[:200]!r}") from exc


class OpenAICompatProvider:
    """POST {base_url}/v1/chat/completions — MiMo-7B/MiMo-VL on vLLM,
    OpenRouter, LM Studio and any other OpenAI-compatible server."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.name = f"openai:{model}"

    def _endpoint(self) -> str:
        if self.base_url.endswith("/v1"):
            return self.base_url + "/chat/completions"
        return self.base_url + "/v1/chat/completions"

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = _post_json(self._endpoint(), body, headers, self.timeout)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"unexpected chat/completions body: {str(data)[:300]}") from exc
        if not isinstance(content, str):
            raise ProviderError(f"unexpected content type: {type(content).__name__}")
        return content


class AnthropicProvider:
    """Anthropic Messages API over stdlib HTTP (no SDK). Needs ANTHROPIC_API_KEY
    (or an explicit api_key / WHERUGO_LLM_API_KEY via get_provider)."""

    def __init__(
        self,
        model: str = DEFAULT_ANTHROPIC_MODEL,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_tokens: int = 2048,
    ) -> None:
        self.model = model
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout = timeout
        self.max_tokens = max_tokens
        self.name = f"anthropic:{model}"

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if not self.api_key:
            raise ProviderError(
                "AnthropicProvider requires an API key (set ANTHROPIC_API_KEY or WHERUGO_LLM_API_KEY)"
            )
        if json_mode:
            system = system + "\nYanıtı yalnızca geçerli JSON olarak ver; JSON dışında hiçbir metin yazma."
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        data = _post_json(ANTHROPIC_API_URL, body, headers, self.timeout)
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"unexpected messages body: {str(data)[:300]}")
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        if not texts:
            raise ProviderError(f"no text block in response (stop_reason={data.get('stop_reason')!r})")
        return "".join(texts)


class MockProvider:
    """Deterministic, key-free provider. briefing/assistant detect it and use
    their template paths; complete() itself returns a fixed Turkish stub."""

    name = "mock"

    def complete(self, system: str, user: str, *, json_mode: bool = False) -> str:
        if json_mode:
            return json.dumps(
                {"provider": "mock", "system_chars": len(system), "user_chars": len(user)},
                ensure_ascii=False,
                sort_keys=True,
            )
        return (
            "Bu yanıt MockProvider tarafından üretildi (API anahtarı gerektirmez). "
            f"Sistem talimatı {len(system)} karakter, kullanıcı girdisi {len(user)} karakter."
        )


def get_provider() -> LLMProvider:
    """Build a provider from WHERUGO_LLM_* environment variables (default: mock)."""
    kind = os.environ.get("WHERUGO_LLM_PROVIDER", "mock").strip().lower()
    if kind in ("", "mock"):
        return MockProvider()
    if kind == "openai":
        return OpenAICompatProvider(
            base_url=os.environ.get("WHERUGO_LLM_BASE_URL", "http://localhost:8001"),
            model=os.environ.get("WHERUGO_LLM_MODEL", DEFAULT_OPENAI_MODEL),
            api_key=os.environ.get("WHERUGO_LLM_API_KEY") or None,
        )
    if kind == "anthropic":
        return AnthropicProvider(
            model=os.environ.get("WHERUGO_LLM_MODEL", DEFAULT_ANTHROPIC_MODEL),
            api_key=os.environ.get("WHERUGO_LLM_API_KEY") or None,
        )
    raise ProviderError(
        f"unknown WHERUGO_LLM_PROVIDER={kind!r} (expected: mock | openai | anthropic)"
    )
