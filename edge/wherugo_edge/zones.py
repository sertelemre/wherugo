"""Kaynak-bağımsız zone motoru (CONTRACTS §6).

Nokta-poligon testi, enter/exit histerezisi (2 ardışık örnek), dwell eşiği
(5 sn -> dwell|pass_by), shelf'te interaction_candidate, kuyruk zone'unda
30 sn periyotlu queue_measurement.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from .config import ZoneDef
from .events import (
    Event,
    InteractionDetected,
    Quality,
    QueueMeasurement,
    ZoneEnter,
    ZoneExit,
)

Point = tuple[float, float]


def point_in_polygon(x: float, y: float, poly: list[Point]) -> bool:
    """Işın-atma (ray casting) nokta-poligon testi."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_cross = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_cross:
                inside = not inside
        j = i
    return inside


def polygon_centroid(poly: list[Point]) -> Point:
    n = len(poly)
    return (sum(p[0] for p in poly) / n, sum(p[1] for p in poly) / n)


def polygon_bbox(poly: list[Point]) -> tuple[float, float, float, float]:
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class TrackSample:
    """Kaynaktan (simulate/video) gelen 1 Hz plan-koordinatlı konum örneği."""

    track_id: int
    ts: datetime
    x_m: float
    y_m: float
    camera_id: int = 1
    is_staff: bool = False
    conf: float = 0.9
    sigma_cm: float = 20.0


@dataclass
class _ZoneState:
    inside: bool = False
    streak_in: int = 0
    streak_out: int = 0
    enter_ts: Optional[datetime] = None
    pending_enter_ts: Optional[datetime] = None
    pending_enter_pos: Optional[Point] = None
    pending_exit_ts: Optional[datetime] = None
    interaction_done: bool = False


@dataclass
class _TrackState:
    last: Optional[TrackSample] = None
    zones: dict[int, _ZoneState] = field(default_factory=dict)


@dataclass
class _QueueState:
    members: set[int] = field(default_factory=set)      # teyitli kuyruk üyeleri
    provisional: set[int] = field(default_factory=set)  # dwell eşiğini beklyenler
    joins: int = 0
    abandons: int = 0
    next_due: Optional[datetime] = None


def _id_switch_risk(conf: float) -> float:
    return round(min(0.25, max(0.02, (1.0 - conf) * 0.6)), 3)


class ZoneEngine:
    """TrackSample akışını zone olaylarına çevirir; kaynağı ayırt edemez."""

    def __init__(
        self,
        zones: list[ZoneDef],
        *,
        hysteresis_samples: int = 2,
        dwell_threshold_sec: float = 5.0,
        interaction_dwell_sec: float = 8.0,
        queue_interval_sec: float = 30.0,
        service_time_sec: float = 45.0,
    ) -> None:
        self.zones = list(zones)
        self.hysteresis = max(1, int(hysteresis_samples))
        self.dwell_threshold_sec = float(dwell_threshold_sec)
        self.interaction_dwell_sec = float(interaction_dwell_sec)
        self.queue_interval_sec = float(queue_interval_sec)
        self.service_time_sec = float(service_time_sec)
        self.checkout_zones = [z for z in self.zones if z.zone_type == "checkout"]
        self._tracks: dict[int, _TrackState] = {}
        self._queues: dict[int, _QueueState] = {
            z.id: _QueueState() for z in self.zones if z.zone_type == "queue"
        }
        self._centroids = {z.id: polygon_centroid(z.polygon) for z in self.zones}

    # -- yardımcılar ------------------------------------------------------

    def _entry_edge(self, zone: ZoneDef, pos: Point) -> str:
        cx, cy = self._centroids[zone.id]
        dx, dy = pos[0] - cx, pos[1] - cy
        if abs(dx) > abs(dy):
            return "east" if dx > 0 else "west"
        return "north" if dy > 0 else "south"

    def _in_checkout(self, x: float, y: float) -> bool:
        return any(point_in_polygon(x, y, z.polygon) for z in self.checkout_zones)

    def _active_checkouts(self) -> int:
        """Serviste görünen (personel olmayan) track sayısı ~ meşgul kasa sayısı."""
        active = 0
        for ts in self._tracks.values():
            if ts.last is not None and ts.last.is_staff:
                continue
            for zone in self.checkout_zones:
                zs = ts.zones.get(zone.id)
                if zs is not None and zs.inside:
                    active += 1
                    break
        return active

    # -- ana akış ---------------------------------------------------------

    def process(self, sample: TrackSample) -> list[Event]:
        """Bir konum örneğini işler, üretilen zone olaylarını döndürür."""
        st = self._tracks.setdefault(sample.track_id, _TrackState())
        st.last = sample
        events: list[Event] = []
        for zone in self.zones:
            zs = st.zones.setdefault(zone.id, _ZoneState())
            inside_now = point_in_polygon(sample.x_m, sample.y_m, zone.polygon)
            if not zs.inside:
                if inside_now:
                    zs.streak_in += 1
                    if zs.streak_in == 1:
                        zs.pending_enter_ts = sample.ts
                        zs.pending_enter_pos = (sample.x_m, sample.y_m)
                    # Giris bolgesi dar ve gecis hizli: tek ornek yeterli sayilir,
                    # yoksa 1 Hz'de hizli yuruyen musteri footfall'dan duser.
                    needed = 1 if zone.zone_type == "entrance" else self.hysteresis
                    if zs.streak_in >= needed:
                        events.append(self._confirm_enter(zone, zs, sample))
                else:
                    zs.streak_in = 0
            else:
                if inside_now:
                    zs.streak_out = 0
                    ev = self._maybe_interaction(zone, zs, sample)
                    if ev is not None:
                        events.append(ev)
                    self._maybe_confirm_queue_join(zone, zs, sample)
                else:
                    zs.streak_out += 1
                    if zs.streak_out == 1:
                        zs.pending_exit_ts = sample.ts
                    if zs.streak_out >= self.hysteresis:
                        events.append(
                            self._confirm_exit(zone, zs, sample.track_id, zs.pending_exit_ts, sample)
                        )
        return events

    def _confirm_enter(self, zone: ZoneDef, zs: _ZoneState, sample: TrackSample) -> ZoneEnter:
        zs.inside = True
        zs.streak_in = 0
        zs.streak_out = 0
        zs.enter_ts = zs.pending_enter_ts or sample.ts
        zs.interaction_done = False
        if zone.zone_type == "queue" and not sample.is_staff:
            # üyelik dwell eşiğinde teyit edilir; geçip gidenler join sayılmaz
            self._queues[zone.id].provisional.add(sample.track_id)
        return ZoneEnter(
            event_time=zs.enter_ts,
            zone_id=zone.id,
            zone_type=zone.zone_type,
            track_id=sample.track_id,
            entry_edge=self._entry_edge(zone, zs.pending_enter_pos or (sample.x_m, sample.y_m)),
            quality=Quality(
                conf=sample.conf,
                id_switch_risk=_id_switch_risk(sample.conf),
                coverage_ok=True,
            ),
        )

    def _maybe_confirm_queue_join(self, zone: ZoneDef, zs: _ZoneState, sample: TrackSample) -> None:
        if zone.zone_type != "queue" or sample.is_staff:
            return
        q = self._queues[zone.id]
        if sample.track_id in q.provisional:
            if (sample.ts - zs.enter_ts).total_seconds() >= self.dwell_threshold_sec:
                q.provisional.discard(sample.track_id)
                q.members.add(sample.track_id)
                q.joins += 1

    def _maybe_interaction(
        self, zone: ZoneDef, zs: _ZoneState, sample: TrackSample
    ) -> Optional[InteractionDetected]:
        if zone.zone_type != "shelf" or sample.is_staff or zs.interaction_done:
            return None
        dwell = (sample.ts - zs.enter_ts).total_seconds()
        if dwell < self.interaction_dwell_sec:
            return None
        zs.interaction_done = True
        conf = round(min(0.7, 0.5 + dwell / 80.0), 2)  # 8 sn -> ~0.6
        return InteractionDetected(
            event_time=sample.ts,
            zone_id=zone.id,
            track_id=sample.track_id,
            duration_sec=dwell,
            quality=Quality(conf=conf, id_switch_risk=_id_switch_risk(sample.conf), coverage_ok=True),
        )

    def _confirm_exit(
        self,
        zone: ZoneDef,
        zs: _ZoneState,
        track_id: int,
        exit_ts: Optional[datetime],
        last_sample: Optional[TrackSample],
    ) -> ZoneExit:
        exit_ts = exit_ts or (last_sample.ts if last_sample else zs.enter_ts)
        dwell = max(0.0, (exit_ts - zs.enter_ts).total_seconds())
        classification = "dwell" if dwell >= self.dwell_threshold_sec else "pass_by"
        conf = last_sample.conf if last_sample else 0.8
        if zone.zone_type == "queue":
            q = self._queues[zone.id]
            if track_id in q.members:
                q.members.discard(track_id)
                served = last_sample is not None and self._in_checkout(
                    last_sample.x_m, last_sample.y_m
                )
                if not served:
                    q.abandons += 1
            else:
                q.provisional.discard(track_id)  # geçip gitti: join/abandon yok
        zs.inside = False
        zs.streak_in = 0
        zs.streak_out = 0
        zs.enter_ts = None
        zs.interaction_done = False
        return ZoneExit(
            event_time=exit_ts,
            zone_id=zone.id,
            track_id=track_id,
            dwell_sec=dwell,
            classification=classification,
            quality=Quality(conf=conf, id_switch_risk=_id_switch_risk(conf), coverage_ok=True),
        )

    def end_track(self, track_id: int, ts: datetime) -> list[Event]:
        """Kaybolan/çıkan track'in açık ziyaretlerini kapatır."""
        st = self._tracks.pop(track_id, None)
        if st is None:
            return []
        events: list[Event] = []
        for zone in self.zones:
            zs = st.zones.get(zone.id)
            if zs is not None and zs.inside:
                events.append(self._confirm_exit(zone, zs, track_id, ts, st.last))
        return events

    def tick(self, now: datetime) -> list[Event]:
        """30 sn periyodik queue_measurement üretimi."""
        events: list[Event] = []
        interval = timedelta(seconds=self.queue_interval_sec)
        for zone in self.zones:
            if zone.zone_type != "queue":
                continue
            q = self._queues[zone.id]
            if q.next_due is None:
                q.next_due = now + interval
                continue
            while now >= q.next_due:
                events.append(self._measure_queue(zone, q, q.next_due))
                q.next_due += interval
        return events

    def _measure_queue(self, zone: ZoneDef, q: _QueueState, ts: datetime) -> QueueMeasurement:
        queue_len = len(q.members)
        active = self._active_checkouts()
        est_wait = int(round(queue_len * self.service_time_sec / max(1, active)))
        confs = [
            self._tracks[tid].last.conf
            for tid in q.members
            if tid in self._tracks and self._tracks[tid].last is not None
        ]
        conf = round(sum(confs) / len(confs), 3) if confs else 0.85
        ev = QueueMeasurement(
            event_time=ts,
            zone_id=zone.id,
            queue_len=queue_len,
            est_wait_sec=est_wait,
            joins_since_last=q.joins,
            abandons_since_last=q.abandons,
            active_checkouts=active,
            quality=Quality(conf=conf, coverage_ok=True),
        )
        q.joins = 0
        q.abandons = 0
        return ev
