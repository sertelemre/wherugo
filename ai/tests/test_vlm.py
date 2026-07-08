import base64
import json
from urllib.error import URLError

import pytest

from wherugo_ai import providers
from wherugo_ai.providers import ProviderError
from wherugo_ai.vlm import (
    DEFAULT_VLM_MODEL,
    MAX_FRAMES,
    MockVLM,
    OpenAICompatVLM,
    VLMJudge,
    VLMVerdict,
    _parse_verdict,
    _sample_evenly,
    get_vlm,
)

# Gerçek, geçerli 1x1 piksel JPEG (SOI ffd8 ... EOI ffd9, 160 bayt) —
# test fixture'ları ağa/diske bağımlı olmadan gerçek jpg baytları üretir.
JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRof"
    "Hh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAAB"
    "AAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AVN//2Q=="
)

VERDICT_JSON = '{"label": "pickup", "conf": 0.83, "rationale": "Ürün rafa geri konmadı."}'


def _jpeg_bytes(i: int) -> bytes:
    """Kare başına ayırt edilebilir jpg baytları (geçerli JPEG + iz baytı)."""
    return JPEG_1PX + b"#%03d" % i


def _write_clip_dir(tmp_path, n, name="clip"):
    clip = tmp_path / name
    clip.mkdir()
    # Ters sırada yaz: sıralamanın dosya adına göre yapıldığını da doğrular.
    for i in reversed(range(n)):
        (clip / f"frame-{i:03d}.jpg").write_bytes(_jpeg_bytes(i))
    return clip


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _chat_payload(content):
    return {"choices": [{"message": {"content": content}}]}


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


def _image_bytes(parts):
    """user content'teki image_url parçalarını ham jpg baytlarına çöz."""
    images = [p for p in parts if p["type"] == "image_url"]
    out = []
    for p in images:
        url = p["image_url"]["url"]
        assert url.startswith("data:image/jpeg;base64,")
        out.append(base64.b64decode(url.removeprefix("data:image/jpeg;base64,")))
    return out


# --- mevcut stub davranışı (korunur) ---

def test_mock_vlm_satisfies_protocol():
    assert isinstance(MockVLM(), VLMJudge)


def test_mock_vlm_verdict_shape():
    v = MockVLM().judge_clip("/clips/c1.mp4", {"event_type": "queue_abandon"})
    assert isinstance(v, VLMVerdict)
    assert v.label == "queue_abandon_unverified"
    assert v.conf == 0.0
    assert "MockVLM" in v.rationale


def test_mock_vlm_empty_context():
    v = MockVLM().judge_clip("/clips/c2.mp4", {})
    assert v.label == "unknown_unverified"
    assert MockVLM().judge_clip("/clips/c2.mp4", None) == v


# --- OpenAICompatVLM: istek gövdesi (ağa çıkmadan) ---

def test_vlm_satisfies_protocol():
    assert isinstance(OpenAICompatVLM("http://x", "m"), VLMJudge)


def test_vlm_request_body_directory(monkeypatch, tmp_path):
    clip = _write_clip_dir(tmp_path, 3)
    captured = _capture_urlopen(monkeypatch, _chat_payload(VERDICT_JSON))
    vlm = OpenAICompatVLM(
        base_url="http://gpu:8002/v1", model="XiaomiMiMo/MiMo-VL-7B-RL-2508",
        api_key="sk-vlm", timeout=17.0,
    )
    verdict = vlm.judge_clip(str(clip), {"event_type": "interaction_candidate", "zone": "Aksesuar"})

    assert verdict == VLMVerdict("pickup", 0.83, "Ürün rafa geri konmadı.")
    assert captured["url"] == "http://gpu:8002/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 17.0
    assert captured["headers"]["authorization"] == "Bearer sk-vlm"
    assert captured["headers"]["content-type"] == "application/json"

    body = captured["body"]
    assert body["model"] == "XiaomiMiMo/MiMo-VL-7B-RL-2508"
    system, user = body["messages"]
    assert system["role"] == "system"
    assert "JSON" in system["content"] and "conf" in system["content"]
    assert user["role"] == "user"
    parts = user["content"]
    # İlk parça metin bağlamı, olay bağlamını içerir.
    assert parts[0]["type"] == "text"
    assert "interaction_candidate" in parts[0]["text"]
    assert "Aksesuar" in parts[0]["text"]
    # 3 kare → 3 image_url, dosya adına göre sıralı, data URI önekli.
    assert _image_bytes(parts) == [_jpeg_bytes(0), _jpeg_bytes(1), _jpeg_bytes(2)]


def test_vlm_single_file_clip_and_no_auth(monkeypatch, tmp_path):
    jpg = tmp_path / "evt-42.jpg"
    jpg.write_bytes(JPEG_1PX)
    captured = _capture_urlopen(monkeypatch, _chat_payload(VERDICT_JSON))
    vlm = OpenAICompatVLM(base_url="http://gpu:8002", model="m")  # api_key yok
    vlm.judge_clip(str(jpg), {})

    assert captured["url"] == "http://gpu:8002/v1/chat/completions"
    assert "authorization" not in captured["headers"]
    parts = captured["body"]["messages"][1]["content"]
    assert _image_bytes(parts) == [JPEG_1PX]


def test_vlm_caps_at_8_frames_evenly_sampled(monkeypatch, tmp_path):
    clip = _write_clip_dir(tmp_path, 20)
    captured = _capture_urlopen(monkeypatch, _chat_payload(VERDICT_JSON))
    OpenAICompatVLM("http://x", "m").judge_clip(str(clip), {"event_type": "x"})

    parts = captured["body"]["messages"][1]["content"]
    images = _image_bytes(parts)
    assert len(images) == MAX_FRAMES == 8
    # Eşit aralıklı örnekleme: i*(n-1)//(k-1), ilk ve son kare her zaman dahil.
    expected = [(i * 19) // 7 for i in range(8)]
    assert expected == [0, 2, 5, 8, 10, 13, 16, 19]
    assert images == [_jpeg_bytes(i) for i in expected]


def test_sample_evenly_small_lists_untouched():
    assert _sample_evenly([1, 2, 3], 8) == [1, 2, 3]
    assert _sample_evenly(list(range(8)), 8) == list(range(8))
    assert _sample_evenly(list(range(9)), 8)[0] == 0
    assert _sample_evenly(list(range(9)), 8)[-1] == 8


def test_vlm_missing_clip_and_empty_dir_raise_before_network(monkeypatch, tmp_path):
    def boom(*a, **k):  # ağa asla çıkılmamalı
        raise AssertionError("urlopen called")

    monkeypatch.setattr(providers, "urlopen", boom)
    vlm = OpenAICompatVLM("http://x", "m")
    with pytest.raises(ProviderError, match="does not exist"):
        vlm.judge_clip(str(tmp_path / "yok"), {})
    empty = tmp_path / "bos"
    empty.mkdir()
    with pytest.raises(ProviderError, match="no jpeg frames"):
        vlm.judge_clip(str(empty), {})


# --- yanıt parse korkulukları ---

def test_vlm_parses_json_in_code_fence(monkeypatch, tmp_path):
    jpg = tmp_path / "f.jpg"
    jpg.write_bytes(JPEG_1PX)
    content = "İşte kararım:\n```json\n" + VERDICT_JSON + "\n```"
    _capture_urlopen(monkeypatch, _chat_payload(content))
    v = OpenAICompatVLM("http://x", "m").judge_clip(str(jpg), {})
    assert v.label == "pickup"
    assert v.conf == 0.83


def test_parse_verdict_clamps_conf():
    assert _parse_verdict('{"label": "a", "conf": 1.7, "rationale": "r"}').conf == 1.0
    assert _parse_verdict('{"label": "a", "conf": -3, "rationale": "r"}').conf == 0.0
    assert _parse_verdict('{"label": "a", "conf": "çok", "rationale": "r"}').conf == 0.0


def test_parse_verdict_broken_json_salvages_label():
    v = _parse_verdict('{"label": "putback", "conf": 0.6, "rationale": "kes')
    assert v.label == "putback"
    assert v.conf == 0.6


def test_parse_verdict_unparseable_truncates_rationale():
    raw = "model tamamen serbest metin döndürdü " * 20
    v = _parse_verdict(raw)
    assert v.label == "unparseable"
    assert v.conf == 0.0
    assert v.rationale == raw.strip()[:200]
    assert len(v.rationale) == 200


def test_vlm_content_parts_response_and_unparseable(monkeypatch, tmp_path):
    jpg = tmp_path / "f.jpg"
    jpg.write_bytes(JPEG_1PX)
    # content, content-parts listesi olarak da gelebilir.
    _capture_urlopen(
        monkeypatch, _chat_payload([{"type": "text", "text": "sadece metin"}])
    )
    v = OpenAICompatVLM("http://x", "m").judge_clip(str(jpg), {})
    assert v == VLMVerdict("unparseable", 0.0, "sadece metin")


# --- ağ hataları ProviderError'a sarılır ---

def test_vlm_timeout_wrapped(monkeypatch, tmp_path):
    jpg = tmp_path / "f.jpg"
    jpg.write_bytes(JPEG_1PX)
    monkeypatch.setattr(
        providers, "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(TimeoutError("timed out")),
    )
    with pytest.raises(ProviderError, match="network error"):
        OpenAICompatVLM("http://x", "m").judge_clip(str(jpg), {})


def test_vlm_unreachable_wrapped(monkeypatch, tmp_path):
    jpg = tmp_path / "f.jpg"
    jpg.write_bytes(JPEG_1PX)
    monkeypatch.setattr(
        providers, "urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(URLError("bağlantı yok")),
    )
    with pytest.raises(ProviderError, match="cannot reach"):
        OpenAICompatVLM("http://x", "m").judge_clip(str(jpg), {})


def test_vlm_malformed_completion_body_wrapped(monkeypatch, tmp_path):
    jpg = tmp_path / "f.jpg"
    jpg.write_bytes(JPEG_1PX)
    _capture_urlopen(monkeypatch, {"error": "no choices"})
    with pytest.raises(ProviderError, match="unexpected"):
        OpenAICompatVLM("http://x", "m").judge_clip(str(jpg), {})


# --- get_vlm env fabrikası ---

def test_get_vlm_default_is_mock(monkeypatch):
    monkeypatch.delenv("WHERUGO_VLM_BASE_URL", raising=False)
    assert isinstance(get_vlm(), MockVLM)


def test_get_vlm_blank_base_url_is_mock(monkeypatch):
    monkeypatch.setenv("WHERUGO_VLM_BASE_URL", "   ")
    assert isinstance(get_vlm(), MockVLM)


def test_get_vlm_openai_compat_from_env(monkeypatch):
    monkeypatch.setenv("WHERUGO_VLM_BASE_URL", "http://gpu:8002/v1")
    monkeypatch.delenv("WHERUGO_VLM_MODEL", raising=False)
    monkeypatch.delenv("WHERUGO_VLM_API_KEY", raising=False)
    vlm = get_vlm()
    assert isinstance(vlm, OpenAICompatVLM)
    assert vlm.base_url == "http://gpu:8002/v1"
    assert vlm.model == DEFAULT_VLM_MODEL == "XiaomiMiMo/MiMo-VL-7B-RL-2508"
    assert vlm.api_key is None


def test_get_vlm_model_and_key_from_env(monkeypatch):
    monkeypatch.setenv("WHERUGO_VLM_BASE_URL", "http://gpu:8002")
    monkeypatch.setenv("WHERUGO_VLM_MODEL", "ozel/model")
    monkeypatch.setenv("WHERUGO_VLM_API_KEY", "sk-vlm-test")
    vlm = get_vlm()
    assert isinstance(vlm, OpenAICompatVLM)
    assert vlm.model == "ozel/model"
    assert vlm.api_key == "sk-vlm-test"
