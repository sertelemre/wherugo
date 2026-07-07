"""Kuyruk ölçümü: 30 sn periyot, queue_len, joins/abandons, est_wait_sec."""
from datetime import datetime, timedelta, timezone

from wherugo_edge.config import ZoneDef
from wherugo_edge.zones import TrackSample, ZoneEngine

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)

QUEUE = ZoneDef(id=7, name="kuyruk", zone_type="queue", polygon=[(0, 0), (6, 0), (6, 2), (0, 2)])
CHECKOUT = ZoneDef(id=8, name="kasa", zone_type="checkout", polygon=[(0, 2), (6, 2), (6, 4), (0, 4)])


def mk_engine():
    return ZoneEngine([QUEUE, CHECKOUT], queue_interval_sec=30.0, service_time_sec=45.0)


def s(tid, sec, x, y, **kw):
    return TrackSample(track_id=tid, ts=T0 + timedelta(seconds=sec), x_m=x, y_m=y, **kw)


def put_in_queue(eng, tid, sec0, x=1.0):
    # histerezis (2 örnek) + üyelik teyidi (dwell eşiği 5 sn) için 7 örnek
    for i in range(7):
        eng.process(s(tid, sec0 + i, x + tid * 0.1, 1.0))


def measurements(events):
    return [e for e in events if e.TYPE == "queue_measurement"]


def test_periodic_measurement_every_30s():
    eng = mk_engine()
    eng.tick(T0)  # periyot başlangıcı
    put_in_queue(eng, 1, 1)
    put_in_queue(eng, 2, 2)
    put_in_queue(eng, 3, 3)
    assert measurements(eng.tick(T0 + timedelta(seconds=29))) == []
    evs = measurements(eng.tick(T0 + timedelta(seconds=30)))
    assert len(evs) == 1
    m = evs[0]
    assert m.zone_id == 7
    assert m.zone_type == "queue"
    assert m.queue_len == 3
    assert m.joins_since_last == 3
    assert m.abandons_since_last == 0
    assert m.active_checkouts == 0
    assert m.est_wait_sec == 3 * 45  # aktif kasa yokken payda 1

    # sayaçlar sıfırlandı; kuyruk uzunluğu kalıcı
    evs2 = measurements(eng.tick(T0 + timedelta(seconds=60)))
    assert evs2[0].joins_since_last == 0
    assert evs2[0].queue_len == 3


def test_served_vs_abandon():
    eng = mk_engine()
    eng.tick(T0)
    put_in_queue(eng, 1, 1)
    put_in_queue(eng, 2, 2)

    # track 1 kasaya yürür (servis): çıkış anındaki konum checkout içinde
    eng.process(s(1, 10, 1.1, 2.5))
    eng.process(s(1, 11, 1.1, 3.0))

    # track 2 kuyruğu terk eder (abandon): mağaza içine geri döner
    eng.process(s(2, 12, 1.2, -1.0))
    eng.process(s(2, 13, 1.2, -2.0))

    # track 3 kuyruktan yalnızca geçer (pass-through): join/abandon sayılmaz
    eng.process(s(3, 14, 3.0, 1.0))
    eng.process(s(3, 15, 3.5, 1.0))
    eng.process(s(3, 16, 3.5, -1.0))
    eng.process(s(3, 17, 3.5, -2.0))

    m = measurements(eng.tick(T0 + timedelta(seconds=30)))[0]
    assert m.queue_len == 0
    assert m.joins_since_last == 2
    assert m.abandons_since_last == 1  # yalnız track 2
    assert m.active_checkouts == 1    # track 1 kasada
    assert m.est_wait_sec == 0


def test_staff_not_counted_in_queue():
    eng = mk_engine()
    eng.tick(T0)
    for i in range(2):
        eng.process(s(9, 1 + i, 3.0, 1.0, is_staff=True))
    m = measurements(eng.tick(T0 + timedelta(seconds=30)))[0]
    assert m.queue_len == 0
    assert m.joins_since_last == 0


def test_vanished_track_in_queue_counts_abandon():
    eng = mk_engine()
    eng.tick(T0)
    put_in_queue(eng, 5, 1)
    eng.end_track(5, T0 + timedelta(seconds=10))
    m = measurements(eng.tick(T0 + timedelta(seconds=30)))[0]
    assert m.queue_len == 0
    assert m.abandons_since_last == 1
