"""Zone motoru: enter/exit, dwell/pass_by, histerezis, interaction_candidate."""
from datetime import datetime, timedelta, timezone

from wherugo_edge.config import ZoneDef
from wherugo_edge.zones import TrackSample, ZoneEngine, point_in_polygon

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)

SHELF = ZoneDef(id=31, name="raf", zone_type="shelf", polygon=[(0, 0), (4, 0), (4, 4), (0, 4)])


def s(track_id, sec, x, y, **kw):
    return TrackSample(track_id=track_id, ts=T0 + timedelta(seconds=sec), x_m=x, y_m=y, **kw)


def feed(engine, samples):
    events = []
    for smp in samples:
        events.extend(engine.process(smp))
    return events


def by_type(events, t):
    return [e for e in events if e.TYPE == t]


def test_point_in_polygon_basic():
    poly = [(0.0, 0.0), (4.0, 0.0), (4.0, 4.0), (0.0, 4.0)]
    assert point_in_polygon(2, 2, poly)
    assert not point_in_polygon(5, 2, poly)
    assert not point_in_polygon(-0.1, 2, poly)


def test_enter_dwell_interaction_exit():
    eng = ZoneEngine([SHELF])
    samples = [s(1, 0, -1, 2)] + [s(1, i, 2, 2) for i in range(1, 12)] + [
        s(1, 12, 6, 2), s(1, 13, 7, 2),
    ]
    events = feed(eng, samples)

    enters = by_type(events, "zone_enter")
    assert len(enters) == 1
    assert enters[0].zone_id == 31
    assert enters[0].event_time == T0 + timedelta(seconds=1)  # ilk içerideki örnek
    assert enters[0].zone_type == "shelf"

    inters = by_type(events, "interaction_detected")
    assert len(inters) == 1  # ziyaret başına bir kez
    assert inters[0].duration_sec >= 8.0
    assert inters[0].interaction == "interaction_candidate"
    assert 0.5 <= inters[0].quality.conf <= 0.7  # ~0.6

    exits = by_type(events, "zone_exit")
    assert len(exits) == 1
    ex = exits[0]
    assert ex.event_time == T0 + timedelta(seconds=12)  # ilk dışarıdaki örnek
    assert abs(ex.dwell_sec - 11.0) < 1e-6
    assert ex.classification == "dwell"


def test_pass_by_classification():
    eng = ZoneEngine([SHELF])
    samples = [s(2, 0, -1, 2), s(2, 1, 1, 2), s(2, 2, 2, 2), s(2, 3, 3, 2),
               s(2, 4, 5, 2), s(2, 5, 6, 2)]
    events = feed(eng, samples)
    exits = by_type(events, "zone_exit")
    assert len(exits) == 1
    assert exits[0].classification == "pass_by"  # 3 sn < 5 sn eşik
    assert by_type(events, "interaction_detected") == []


def test_hysteresis_single_sample_blip_no_enter():
    eng = ZoneEngine([SHELF])
    events = feed(eng, [s(3, 0, -1, 2), s(3, 1, 2, 2), s(3, 2, -1, 2), s(3, 3, -1, 2)])
    assert events == []


def test_hysteresis_single_sample_blip_no_exit():
    eng = ZoneEngine([SHELF])
    samples = [s(4, i, 2, 2) for i in range(0, 6)]  # içeride
    samples.append(s(4, 6, -1, 2))                  # 1 örneklik dışarı sıçrama
    samples += [s(4, i, 2, 2) for i in range(7, 10)]
    events = feed(eng, samples)
    assert len(by_type(events, "zone_enter")) == 1
    assert by_type(events, "zone_exit") == []


def test_entry_edge_west():
    eng = ZoneEngine([SHELF])
    events = feed(eng, [s(5, 0, -1, 2), s(5, 1, 0.3, 2), s(5, 2, 1.0, 2)])
    enters = by_type(events, "zone_enter")
    assert len(enters) == 1
    assert enters[0].entry_edge == "west"


def test_staff_gets_zone_events_but_no_interaction():
    eng = ZoneEngine([SHELF])
    samples = [s(6, i, 2, 2, is_staff=True) for i in range(0, 15)] + [
        s(6, 15, 6, 2, is_staff=True), s(6, 16, 6, 2, is_staff=True),
    ]
    events = feed(eng, samples)
    assert len(by_type(events, "zone_enter")) == 1
    assert len(by_type(events, "zone_exit")) == 1
    assert by_type(events, "interaction_detected") == []


def test_end_track_closes_open_visit():
    eng = ZoneEngine([SHELF])
    feed(eng, [s(7, i, 2, 2) for i in range(0, 7)])
    events = eng.end_track(7, T0 + timedelta(seconds=7))
    exits = by_type(events, "zone_exit")
    assert len(exits) == 1
    assert exits[0].classification == "dwell"
    assert abs(exits[0].dwell_sec - 7.0) < 1e-6
    assert eng.end_track(7, T0 + timedelta(seconds=8)) == []  # tekrar çağrı boş
