from wherugo_ai import MockProvider, answer


def _ask(question, bundle):
    return answer(question, bundle, provider=MockProvider())


def test_queue_question(sample_bundle):
    r = _ask("Kasadaki kuyruk durumu nasıldı?", sample_bundle)
    assert "**7**" in r.answer_md
    assert "queue.max_len" in r.metrics_used
    assert "queue.avg_wait_sec" in r.metrics_used
    assert "queue.abandons" in r.metrics_used


def test_footfall_question(sample_bundle):
    r = _ask("Bugün kaç ziyaretçi geldi?", sample_bundle)
    assert "**312**" in r.answer_md
    assert "footfall_total" in r.metrics_used


def test_peak_hour_question(sample_bundle):
    r = _ask("En yoğun saat hangisiydi?", sample_bundle)
    assert "17:00" in r.answer_md
    assert "footfall_by_hour" in r.metrics_used


def test_zone_dwell_question(sample_bundle):
    r = _ask("Hangi bölgede kalış süresi en yüksekti?", sample_bundle)
    assert "Kadın Giyim" in r.answer_md
    assert "zones.Kadın Giyim.dwell_p50" in r.metrics_used


def test_conversion_question(sample_bundle):
    r = _ask("Dönüşüm oranı nedir?", sample_bundle)
    assert "%19.6" in r.answer_md  # 61/312
    assert "funnel.entered" in r.metrics_used
    assert "funnel.transactions" in r.metrics_used


def test_coverage_question(sample_bundle):
    r = _ask("Kapsam boşluğu var mıydı?", sample_bundle)
    assert "12.5" in r.answer_md
    assert r.metrics_used == ["coverage_gap_min"]


def test_unknown_question_refuses(sample_bundle):
    r = _ask("Yarın hava nasıl olacak?", sample_bundle)
    assert "yanıtlayamıyorum" in r.answer_md
    assert r.metrics_used == []


def test_known_topic_missing_data_refuses(sample_bundle):
    no_queue = {k: v for k, v in sample_bundle.items() if k != "queue"}
    r = _ask("Kuyrukta bekleme ne kadardı?", no_queue)
    assert "yanıtlayamıyorum" in r.answer_md
    assert r.metrics_used == []


def test_deterministic(sample_bundle):
    a = _ask("Kuyruk nasıldı?", sample_bundle)
    b = _ask("Kuyruk nasıldı?", sample_bundle)
    assert a == b


def test_real_provider_filters_unresolvable_refs(sample_bundle):
    class FakeProvider:
        name = "fake:model"

        def complete(self, system, user, *, json_mode=False):
            return "312 ziyaretçi [footfall_total], uydurma sayı [made_up_metric]."

    r = answer("Kaç ziyaretçi?", sample_bundle, provider=FakeProvider())
    assert r.metrics_used == ["footfall_total"]
