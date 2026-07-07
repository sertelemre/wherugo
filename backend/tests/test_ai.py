"""AI layer integration: wherugo_ai is optional — endpoints return 501 without it,
and delegate to it (mocked here) when present."""
import sys
import types
from types import SimpleNamespace

from helpers import AUTH


def test_briefing_501_when_ai_missing(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "wherugo_ai", None)  # forces ImportError
    resp = client.get("/v1/stores/1/briefing", headers=AUTH)
    assert resp.status_code == 501
    assert "wherugo_ai" in resp.json()["detail"]


def test_assistant_501_when_ai_missing(client, monkeypatch):
    monkeypatch.setitem(sys.modules, "wherugo_ai", None)
    resp = client.post("/v1/stores/1/assistant", headers=AUTH,
                       json={"question": "Dün kaç kişi geldi?"})
    assert resp.status_code == 501


def _install_fake_ai(monkeypatch, calls):
    class MockProvider:
        pass

    briefing_mod = types.ModuleType("wherugo_ai.briefing")

    def generate(metrics_bundle, store_name, date, provider):
        calls.append(("generate", metrics_bundle, store_name, date, provider))
        return SimpleNamespace(text_md=f"# {store_name} — {date} brifingi",
                               metric_refs=["footfall.total", "funnel.entered"])

    briefing_mod.generate = generate

    assistant_mod = types.ModuleType("wherugo_ai.assistant")

    def answer(question, metrics_bundle, provider):
        calls.append(("answer", question, metrics_bundle, provider))
        return SimpleNamespace(answer_md="**Cevap:** 42", metrics_used=["footfall.total"])

    assistant_mod.answer = answer

    pkg = types.ModuleType("wherugo_ai")
    pkg.briefing = briefing_mod
    pkg.assistant = assistant_mod
    pkg.get_provider = lambda: MockProvider()

    monkeypatch.setitem(sys.modules, "wherugo_ai", pkg)
    monkeypatch.setitem(sys.modules, "wherugo_ai.briefing", briefing_mod)
    monkeypatch.setitem(sys.modules, "wherugo_ai.assistant", assistant_mod)


def test_briefing_generated_and_cached(client, monkeypatch):
    calls = []
    _install_fake_ai(monkeypatch, calls)

    resp = client.get("/v1/stores/1/briefing", headers=AUTH, params={"date": "2026-07-07"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "Demo Mağaza" in body["text_md"]
    assert body["provider"] == "MockProvider"
    assert body["metric_refs"] == ["footfall.total", "funnel.entered"]
    assert len([c for c in calls if c[0] == "generate"]) == 1

    # metrics_bundle must follow the schema documented in ai/wherugo_ai/briefing.py
    bundle = calls[0][1]
    for key in ("store", "window", "footfall_total", "footfall_by_hour",
                "zones", "funnel", "coverage_gap_min"):
        assert key in bundle
    assert set(bundle["funnel"]) == {"entered", "engaged", "interacted", "transactions"}

    # second call: served from DB, no new generate call
    resp2 = client.get("/v1/stores/1/briefing", headers=AUTH, params={"date": "2026-07-07"})
    assert resp2.status_code == 200
    assert resp2.json()["text_md"] == body["text_md"]
    assert len([c for c in calls if c[0] == "generate"]) == 1


def test_assistant_answer(client, monkeypatch):
    calls = []
    _install_fake_ai(monkeypatch, calls)
    resp = client.post("/v1/stores/1/assistant", headers=AUTH,
                       json={"question": "Bugün en yoğun saat hangisiydi?"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["answer_md"] == "**Cevap:** 42"
    assert body["metrics_used"] == ["footfall.total"]
    assert calls[0][1] == "Bugün en yoğun saat hangisiydi?"
