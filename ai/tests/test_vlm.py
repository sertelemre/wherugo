from wherugo_ai.vlm import MockVLM, VLMJudge, VLMVerdict


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
