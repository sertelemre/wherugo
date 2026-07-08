import http.client
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from wherugo_ai import providers
from wherugo_ai.providers import (
    AnthropicProvider,
    LLMProvider,
    MockProvider,
    OpenAICompatProvider,
    ProviderError,
    get_provider,
)


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _capture_urlopen(monkeypatch, payload):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = {k.lower(): v for k, v in req.header_items()}
        captured["timeout"] = timeout
        return FakeResponse(payload)

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    return captured


# --- get_provider / env selection ---

def test_get_provider_default_is_mock(monkeypatch):
    monkeypatch.delenv("WHERUGO_LLM_PROVIDER", raising=False)
    assert isinstance(get_provider(), MockProvider)


def test_get_provider_openai_from_env(monkeypatch):
    monkeypatch.setenv("WHERUGO_LLM_PROVIDER", "openai")
    monkeypatch.setenv("WHERUGO_LLM_BASE_URL", "http://vllm:8001/v1")
    monkeypatch.setenv("WHERUGO_LLM_MODEL", "XiaomiMiMo/MiMo-7B-RL")
    monkeypatch.setenv("WHERUGO_LLM_API_KEY", "sk-test")
    p = get_provider()
    assert isinstance(p, OpenAICompatProvider)
    assert p.base_url == "http://vllm:8001/v1"
    assert p.model == "XiaomiMiMo/MiMo-7B-RL"
    assert p.api_key == "sk-test"


def test_get_provider_anthropic_from_env(monkeypatch):
    monkeypatch.setenv("WHERUGO_LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("WHERUGO_LLM_API_KEY", "sk-ant-test")
    monkeypatch.delenv("WHERUGO_LLM_MODEL", raising=False)
    p = get_provider()
    assert isinstance(p, AnthropicProvider)
    assert p.model == "claude-haiku-4-5-20251001"
    assert p.api_key == "sk-ant-test"


def test_get_provider_unknown_raises(monkeypatch):
    monkeypatch.setenv("WHERUGO_LLM_PROVIDER", "gemini")
    with pytest.raises(ProviderError):
        get_provider()


def test_providers_satisfy_protocol():
    assert isinstance(MockProvider(), LLMProvider)
    assert isinstance(OpenAICompatProvider("http://x", "m"), LLMProvider)
    assert isinstance(AnthropicProvider(api_key="k"), LLMProvider)


# --- OpenAICompatProvider request building (no network) ---

def test_openai_compat_request_body(monkeypatch):
    captured = _capture_urlopen(
        monkeypatch, {"choices": [{"message": {"content": "tamam"}}]}
    )
    p = OpenAICompatProvider(
        base_url="http://localhost:8001/v1", model="XiaomiMiMo/MiMo-7B-RL",
        api_key="sk-x", timeout=12.0,
    )
    out = p.complete("sistem talimatı", "kullanıcı sorusu", json_mode=True)

    assert out == "tamam"
    assert captured["url"] == "http://localhost:8001/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 12.0
    body = captured["body"]
    assert body["model"] == "XiaomiMiMo/MiMo-7B-RL"
    assert body["messages"] == [
        {"role": "system", "content": "sistem talimatı"},
        {"role": "user", "content": "kullanıcı sorusu"},
    ]
    assert body["response_format"] == {"type": "json_object"}
    assert captured["headers"]["authorization"] == "Bearer sk-x"
    assert captured["headers"]["content-type"] == "application/json"


def test_openai_compat_appends_v1_and_no_auth_header(monkeypatch):
    captured = _capture_urlopen(
        monkeypatch, {"choices": [{"message": {"content": "ok"}}]}
    )
    p = OpenAICompatProvider(base_url="http://lmstudio:1234/", model="local")
    out = p.complete("s", "u")
    assert out == "ok"
    assert captured["url"] == "http://lmstudio:1234/v1/chat/completions"
    assert "authorization" not in captured["headers"]
    assert "response_format" not in captured["body"]


def test_openai_compat_http_error_wrapped(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise HTTPError(req.full_url, 500, "Internal", {}, io.BytesIO(b"patladi"))

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    p = OpenAICompatProvider(base_url="http://x", model="m")
    with pytest.raises(ProviderError, match="HTTP 500"):
        p.complete("s", "u")


def test_openai_compat_network_error_wrapped(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise URLError("bağlantı yok")

    monkeypatch.setattr(providers, "urlopen", fake_urlopen)
    p = OpenAICompatProvider(base_url="http://x", model="m")
    with pytest.raises(ProviderError, match="cannot reach"):
        p.complete("s", "u")


class FailingReadResponse:
    """Bağlantı kuruldu ama gövde okunurken hata çıktı senaryosu (C22)."""

    def __init__(self, exc):
        self._exc = exc

    def read(self):
        raise self._exc

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen_failing_read(monkeypatch, exc):
    monkeypatch.setattr(
        providers, "urlopen", lambda req, timeout=None: FailingReadResponse(exc)
    )


def test_openai_compat_timeout_during_read_wrapped(monkeypatch):
    _urlopen_failing_read(monkeypatch, TimeoutError("timed out"))
    p = OpenAICompatProvider(base_url="http://x", model="m")
    with pytest.raises(ProviderError, match="network error while reading"):
        p.complete("s", "u")


def test_openai_compat_incomplete_read_wrapped(monkeypatch):
    _urlopen_failing_read(monkeypatch, http.client.IncompleteRead(b"kismi"))
    p = OpenAICompatProvider(base_url="http://x", model="m")
    with pytest.raises(ProviderError, match="network error while reading"):
        p.complete("s", "u")


def test_anthropic_timeout_during_read_wrapped(monkeypatch):
    _urlopen_failing_read(monkeypatch, TimeoutError("timed out"))
    p = AnthropicProvider(api_key="k")
    with pytest.raises(ProviderError, match="network error while reading"):
        p.complete("s", "u")


def test_anthropic_connection_reset_during_read_wrapped(monkeypatch):
    _urlopen_failing_read(monkeypatch, ConnectionResetError("reset"))
    p = AnthropicProvider(api_key="k")
    with pytest.raises(ProviderError, match="network error while reading"):
        p.complete("s", "u")


def test_openai_compat_malformed_body_wrapped(monkeypatch):
    _capture_urlopen(monkeypatch, {"error": "no choices"})
    p = OpenAICompatProvider(base_url="http://x", model="m")
    with pytest.raises(ProviderError, match="unexpected"):
        p.complete("s", "u")


# --- AnthropicProvider request building (no network) ---

def test_anthropic_request_body(monkeypatch):
    captured = _capture_urlopen(
        monkeypatch,
        {"content": [{"type": "text", "text": "merhaba"}], "stop_reason": "end_turn"},
    )
    p = AnthropicProvider(api_key="sk-ant-x")
    out = p.complete("sistem", "soru")

    assert out == "merhaba"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "sk-ant-x"
    assert captured["headers"]["anthropic-version"] == "2023-06-01"
    body = captured["body"]
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["system"] == "sistem"
    assert body["messages"] == [{"role": "user", "content": "soru"}]
    assert body["max_tokens"] > 0


def test_anthropic_json_mode_appends_instruction(monkeypatch):
    captured = _capture_urlopen(
        monkeypatch, {"content": [{"type": "text", "text": "{}"}]}
    )
    p = AnthropicProvider(api_key="k")
    p.complete("sistem", "soru", json_mode=True)
    assert "JSON" in captured["body"]["system"]


def test_anthropic_missing_key_raises_before_network(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def boom(*a, **k):  # network must never be touched
        raise AssertionError("urlopen called")

    monkeypatch.setattr(providers, "urlopen", boom)
    p = AnthropicProvider()
    with pytest.raises(ProviderError, match="API key"):
        p.complete("s", "u")


# --- MockProvider ---

def test_mock_provider_deterministic():
    m = MockProvider()
    assert m.complete("abc", "de") == m.complete("abc", "de")
    parsed = json.loads(m.complete("abc", "de", json_mode=True))
    assert parsed["provider"] == "mock"
