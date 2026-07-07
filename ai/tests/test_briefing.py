import copy

from wherugo_ai import MockProvider, generate
from wherugo_ai.briefing import extract_refs, ref_exists


def test_mock_briefing_deterministic(sample_bundle):
    a = generate(sample_bundle, "Demo Mağaza", "2026-07-06", provider=MockProvider())
    b = generate(copy.deepcopy(sample_bundle), "Demo Mağaza", "2026-07-06", provider=MockProvider())
    assert a.text_md == b.text_md
    assert a.metric_refs == b.metric_refs
    assert a.provider_name == "mock"


def test_mock_briefing_content(sample_bundle):
    r = generate(sample_bundle, "Demo Mağaza", "2026-07-06", provider=MockProvider())
    text = r.text_md
    assert text.startswith("# Demo Mağaza — Günlük Brifing (2026-07-06)")
    assert "**312**" in text                      # footfall
    assert "17:00" in text                        # peak hour
    assert "Kadın Giyim" in text                  # best dwell zone
    assert "kuyruğu terk etti" in text            # abandons
    assert "kapsam boşluğu" in text               # coverage warning
    assert "## Önerilen Aksiyonlar" in text
    assert "1. " in text and "2. " in text        # at least 2 actions


def test_metric_refs_resolve_in_bundle(sample_bundle):
    r = generate(sample_bundle, "Demo Mağaza", "2026-07-06", provider=MockProvider())
    assert r.metric_refs, "full bundle must yield metric refs"
    for ref in r.metric_refs:
        assert ref_exists(sample_bundle, ref), f"unresolvable ref: {ref}"
    # refs listed == refs present in the text
    assert r.metric_refs == extract_refs(r.text_md)
    assert "footfall_total" in r.metric_refs
    assert "queue.max_len" in r.metric_refs
    assert "zones.Kadın Giyim.dwell_p50" in r.metric_refs


def test_empty_bundle_does_not_crash():
    r = generate({}, "Boş Mağaza", "2026-07-06", provider=MockProvider())
    assert r.text_md.startswith("# Boş Mağaza")
    assert "yeterli metrik verisi bulunmuyor" in r.text_md
    assert r.metric_refs == []

    r2 = generate(None, "Boş Mağaza", "2026-07-06", provider=MockProvider())
    assert r2.text_md == r.text_md


def test_partial_bundle_skips_missing_sections(sample_bundle):
    only_footfall = {"footfall_total": 42}
    r = generate(only_footfall, "M", "2026-07-06", provider=MockProvider())
    assert "**42**" in r.text_md
    assert "## Kuyruk" not in r.text_md
    assert "## Dönüşüm Hunisi" not in r.text_md
    assert "## Veri Kalitesi" not in r.text_md
    for ref in r.metric_refs:
        assert ref_exists(only_footfall, ref)

    no_queue = {k: v for k, v in sample_bundle.items() if k != "queue"}
    r2 = generate(no_queue, "M", "2026-07-06", provider=MockProvider())
    assert "## Kuyruk" not in r2.text_md
    assert "## Bölge Performansı" in r2.text_md


def test_malformed_fields_tolerated():
    bundle = {
        "footfall_total": "çok",          # wrong type -> skipped
        "footfall_by_hour": "yok",        # wrong type -> skipped
        "zones": [{"visits": 5}, "bozuk", {"name": "A", "dwell_p50": "x"}],
        "queue": {"max_len": None},
        "coverage_gap_min": 0,
    }
    r = generate(bundle, "M", "2026-07-06", provider=MockProvider())
    assert "Kapsam boşluğu yaşanmadı" in r.text_md


def test_real_provider_path_uses_complete(sample_bundle):
    class FakeProvider:
        name = "fake:model"

        def __init__(self):
            self.calls = []

        def complete(self, system, user, *, json_mode=False):
            self.calls.append((system, user, json_mode))
            return "Bugün 312 ziyaretçi geldi [footfall_total]."

    fake = FakeProvider()
    r = generate(sample_bundle, "Demo", "2026-07-06", provider=fake)
    assert r.provider_name == "fake:model"
    assert r.metric_refs == ["footfall_total"]
    (system, user, json_mode) = fake.calls[0]
    assert "metrik" in system.lower()
    assert "312" in user and "Demo" in user and "2026-07-06" in user
    assert json_mode is False
