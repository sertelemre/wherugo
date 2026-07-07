"""Ajan-tabanlı sentetik müşteri üreteci (--source simulate, CONTRACTS §6).

Poisson varış (saat eğrisi), ilgi profilli zone rotası, lognormal dwell,
kuyruk sabırsızlık terki, personel track'leri. Determinizm: seed.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

from ..config import EdgeConfig, ZoneDef
from ..events import Event, Quality, TrackUpdate
from ..zones import (
    Point,
    TrackSample,
    ZoneEngine,
    point_in_polygon,
    polygon_bbox,
    polygon_centroid,
)


def _poisson(rng: random.Random, lam: float) -> int:
    """Knuth Poisson örnekleyici (küçük lambda için yeterli)."""
    if lam <= 0:
        return 0
    threshold = math.exp(-lam)
    k = 0
    p = 1.0
    while True:
        p *= rng.random()
        if p <= threshold:
            return k
        k += 1


def random_point_in(rng: random.Random, poly: list[Point]) -> Point:
    x0, y0, x1, y1 = polygon_bbox(poly)
    for _ in range(64):
        x = rng.uniform(x0, x1)
        y = rng.uniform(y0, y1)
        if point_in_polygon(x, y, poly):
            return (x, y)
    return polygon_centroid(poly)


@dataclass
class _Step:
    point: Point
    zone: Optional[ZoneDef] = None
    dwell_sec: float = 0.0
    kind: str = "visit"  # visit | queue | exit


@dataclass
class _Agent:
    track_id: int
    is_staff: bool
    pos: Point
    speed: float
    steps: list[_Step] = field(default_factory=list)
    state: str = "walk"  # walk | dwell | queue | service
    target: Optional[Point] = None
    current: Optional[_Step] = None
    dwell_until: Optional[datetime] = None
    joined_queue_at: Optional[datetime] = None
    patience_sec: float = 180.0
    service_until: Optional[datetime] = None
    service_point: Optional[Point] = None
    checkout_idx: Optional[int] = None
    pending_service: Optional[float] = None  # servis süresi (sn)
    done: bool = False


class Simulator:
    """20x12 m plan üzerinde 1 Hz adımlarla ajan hareketi üretir."""

    def __init__(
        self,
        config: EdgeConfig,
        *,
        seed: Optional[int] = None,
        start_time: Optional[datetime] = None,
    ) -> None:
        self.cfg = config
        self.sim = config.simulation
        self.rng = random.Random(seed)
        now = start_time or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self.now = now.replace(microsecond=0)
        self.agents: list[_Agent] = []
        self._next_track = 1001
        self._camera_id = config.cameras[0].id

        zones = config.zones
        entrances = [z for z in zones if z.zone_type == "entrance"]
        if not entrances:
            raise ValueError("simülasyon için en az bir 'entrance' zone gerekli")
        self.entrance = entrances[0]
        self.shelves = [z for z in zones if z.zone_type == "shelf"]
        fittings = [z for z in zones if z.zone_type == "fitting_room"]
        self.fitting = fittings[0] if fittings else None
        queues = [z for z in zones if z.zone_type == "queue"]
        self.queue_zone = queues[0] if queues else None
        self.checkout_zones = [z for z in zones if z.zone_type == "checkout"]

        try:
            from zoneinfo import ZoneInfo

            self._tz = ZoneInfo(config.store.timezone)
        except Exception:
            self._tz = timezone.utc

        self.queue: list[_Agent] = []
        n_co = max(1, self.sim.checkout_count)
        self.checkout_busy: list[Optional[_Agent]] = [None] * n_co
        self._init_queue_geometry(n_co)

    def _init_queue_geometry(self, n_checkout: int) -> None:
        self._q_front: Point = (0.0, 0.0)
        self._q_dir: Point = (1.0, 0.0)
        self._q_len_max = 0.0
        self._checkout_points: list[Point] = []
        if self.queue_zone is not None:
            x0, y0, x1, y1 = polygon_bbox(self.queue_zone.polygon)
            cx, cy = polygon_centroid(self.queue_zone.polygon)
            if (x1 - x0) >= (y1 - y0):
                a: Point = (x0 + 0.4, cy)
                b: Point = (x1 - 0.4, cy)
            else:
                a = (cx, y0 + 0.4)
                b = (cx, y1 - 0.4)
            ref = (
                polygon_centroid(self.checkout_zones[0].polygon)
                if self.checkout_zones
                else (cx, cy)
            )
            da = math.dist(a, ref)
            db = math.dist(b, ref)
            front, back = (a, b) if da <= db else (b, a)
            self._q_front = front
            length = math.dist(front, back)
            self._q_len_max = length
            if length > 1e-6:
                self._q_dir = ((back[0] - front[0]) / length, (back[1] - front[1]) / length)
        if self.checkout_zones:
            zone = self.checkout_zones[0]
            x0, y0, x1, y1 = polygon_bbox(zone.polygon)
            cx, cy = polygon_centroid(zone.polygon)
            for i in range(n_checkout):
                frac = (i + 1) / (n_checkout + 1)
                if (x1 - x0) >= (y1 - y0):
                    self._checkout_points.append((x0 + frac * (x1 - x0), cy))
                else:
                    self._checkout_points.append((cx, y0 + frac * (y1 - y0)))
        else:
            self._checkout_points = [self._q_front] * n_checkout

    def _queue_slot(self, idx: int) -> Point:
        offset = min(0.55 * idx, max(0.0, self._q_len_max))
        return (
            self._q_front[0] + self._q_dir[0] * offset,
            self._q_front[1] + self._q_dir[1] * offset,
        )

    # -- ajan yaşam döngüsü ------------------------------------------------

    def _spawn_arrivals(self) -> None:
        hour = self.now.astimezone(self._tz).hour
        lam = self.sim.arrivals_per_hour * self.sim.hourly_curve[hour] / 3600.0
        for _ in range(_poisson(self.rng, lam)):
            self._spawn_agent()

    def _spawn_agent(self) -> None:
        rng = self.rng
        tid = self._next_track
        self._next_track += 1
        agent = _Agent(
            track_id=tid,
            is_staff=rng.random() < self.sim.staff_ratio,
            pos=random_point_in(rng, self.entrance.polygon),
            speed=rng.uniform(*self.sim.walk_speed_mps),
        )
        if not agent.is_staff:
            agent.steps = self._customer_plan(rng)
        self.agents.append(agent)
        self._begin_next_step(agent)

    def _customer_plan(self, rng: random.Random) -> list[_Step]:
        steps: list[_Step] = []
        any_dwell = False
        if self.shelves:
            weights = {z.id: rng.random() + 0.15 for z in self.shelves}
            ordered = sorted(self.shelves, key=lambda z: -weights[z.id])
            k = rng.randint(1, min(4, len(ordered)))
            for zone in ordered[:k]:
                if rng.random() < self.sim.pass_by_probability:
                    dwell = rng.uniform(0.5, 3.5)
                else:
                    dwell = min(180.0, rng.lognormvariate(math.log(22.0), 0.6))
                    any_dwell = True
                steps.append(_Step(point=random_point_in(rng, zone.polygon), zone=zone, dwell_sec=dwell))
        if self.fitting is not None and any_dwell and rng.random() < self.sim.fitting_room_probability:
            steps.append(
                _Step(
                    point=random_point_in(rng, self.fitting.polygon),
                    zone=self.fitting,
                    dwell_sec=min(360.0, rng.lognormvariate(math.log(90.0), 0.5)),
                )
            )
        if self.queue_zone is not None and rng.random() < self.sim.buy_probability:
            steps.append(_Step(point=self._q_front, zone=self.queue_zone, kind="queue"))
        steps.append(
            _Step(point=random_point_in(rng, self.entrance.polygon), zone=self.entrance, kind="exit")
        )
        return steps

    def _staff_step(self) -> _Step:
        rng = self.rng
        candidates: list[ZoneDef] = list(self.shelves) + self.checkout_zones * 2
        if self.fitting is not None:
            candidates.append(self.fitting)
        if not candidates:
            candidates = [self.entrance]
        zone = candidates[rng.randrange(len(candidates))]
        return _Step(
            point=random_point_in(rng, zone.polygon),
            zone=zone,
            dwell_sec=rng.uniform(45.0, 240.0),
        )

    def _begin_next_step(self, agent: _Agent) -> None:
        if not agent.steps:
            if agent.is_staff:
                agent.steps.append(self._staff_step())
            else:
                agent.done = True
                return
        agent.current = agent.steps.pop(0)
        agent.target = agent.current.point
        agent.state = "walk"

    def _abandon_queue(self, agent: _Agent) -> None:
        agent.steps = [s for s in agent.steps if s.kind == "exit"]
        if not agent.steps:
            agent.steps = [
                _Step(point=random_point_in(self.rng, self.entrance.polygon), kind="exit")
            ]
        self._begin_next_step(agent)

    def _assign_checkouts(self) -> None:
        for i, occupant in enumerate(self.checkout_busy):
            if occupant is None and self.queue:
                agent = self.queue.pop(0)
                agent.checkout_idx = i
                agent.pending_service = min(240.0, self.rng.lognormvariate(math.log(40.0), 0.4))
                agent.service_point = self._checkout_points[i]
                agent.target = agent.service_point
                agent.state = "walk"
                self.checkout_busy[i] = agent

    def _move_towards(self, agent: _Agent) -> bool:
        tx, ty = agent.target
        dx = tx - agent.pos[0]
        dy = ty - agent.pos[1]
        dist = math.hypot(dx, dy)
        step = agent.speed * self.rng.uniform(0.85, 1.15)
        if dist <= step or dist < 1e-9:
            agent.pos = (tx, ty)
            return True
        agent.pos = (agent.pos[0] + dx / dist * step, agent.pos[1] + dy / dist * step)
        return False

    def _jitter(self, agent: _Agent, scale: float = 0.12) -> None:
        agent.pos = (
            agent.pos[0] + self.rng.gauss(0.0, scale),
            agent.pos[1] + self.rng.gauss(0.0, scale),
        )

    def _advance(self, agent: _Agent) -> None:
        rng = self.rng
        if agent.state == "walk":
            if self._move_towards(agent):
                if agent.pending_service is not None:
                    agent.service_until = self.now + timedelta(seconds=agent.pending_service)
                    agent.pending_service = None
                    agent.state = "service"
                elif agent.current is not None and agent.current.kind == "queue":
                    agent.state = "queue"
                    agent.joined_queue_at = self.now
                    agent.patience_sec = min(600.0, rng.lognormvariate(math.log(150.0), 0.5))
                    self.queue.append(agent)
                elif agent.current is not None and agent.current.kind == "exit":
                    agent.done = True
                elif agent.current is not None and agent.current.dwell_sec > 0:
                    agent.state = "dwell"
                    agent.dwell_until = self.now + timedelta(seconds=agent.current.dwell_sec)
                else:
                    self._begin_next_step(agent)
        elif agent.state == "dwell":
            self._jitter(agent, 0.10)
            if self.now >= agent.dwell_until:
                self._begin_next_step(agent)
        elif agent.state == "queue":
            try:
                idx = self.queue.index(agent)
            except ValueError:
                return
            slot = self._queue_slot(idx)
            agent.pos = (slot[0] + rng.gauss(0.0, 0.05), slot[1] + rng.gauss(0.0, 0.05))
            waited = (self.now - agent.joined_queue_at).total_seconds()
            if idx > 0 and waited > agent.patience_sec:
                self.queue.remove(agent)
                self._abandon_queue(agent)
        elif agent.state == "service":
            sp = agent.service_point or agent.pos
            agent.pos = (sp[0] + rng.gauss(0.0, 0.05), sp[1] + rng.gauss(0.0, 0.05))
            if self.now >= agent.service_until:
                if agent.checkout_idx is not None:
                    self.checkout_busy[agent.checkout_idx] = None
                    agent.checkout_idx = None
                self._begin_next_step(agent)

    # -- ana adım ----------------------------------------------------------

    def step(self) -> tuple[datetime, list[TrackSample], list[int]]:
        """1 simülasyon saniyesi ilerletir; örnekleri ve biten track'leri döndürür."""
        self.now += timedelta(seconds=1)
        self._spawn_arrivals()
        self._assign_checkouts()
        samples: list[TrackSample] = []
        ended: list[int] = []
        rng = self.rng
        for agent in self.agents:
            self._advance(agent)
            samples.append(
                TrackSample(
                    track_id=agent.track_id,
                    ts=self.now,
                    x_m=agent.pos[0] + rng.gauss(0.0, 0.05),
                    y_m=agent.pos[1] + rng.gauss(0.0, 0.05),
                    camera_id=self._camera_id,
                    is_staff=agent.is_staff,
                    conf=rng.uniform(0.82, 0.97),
                    sigma_cm=rng.uniform(12.0, 35.0),
                )
            )
            if agent.done:
                ended.append(agent.track_id)
        self.agents = [a for a in self.agents if not a.done]
        return self.now, samples, ended


def run_simulation(
    config: EdgeConfig,
    *,
    seed: Optional[int] = None,
    duration_sec: float = 3600.0,
    start_time: Optional[datetime] = None,
) -> Iterator[tuple[datetime, list[Event]]]:
    """Simülatör + zone motorunu bağlar; her sim-adımda olay listesi üretir."""
    sim = Simulator(config, seed=seed, start_time=start_time)
    ep = config.engine
    engine = ZoneEngine(
        config.zones,
        hysteresis_samples=ep.hysteresis_samples,
        dwell_threshold_sec=ep.dwell_threshold_sec,
        interaction_dwell_sec=ep.interaction_dwell_sec,
        queue_interval_sec=ep.queue_interval_sec,
        service_time_sec=ep.service_time_sec,
    )
    for _ in range(int(duration_sec)):
        now, samples, ended = sim.step()
        events: list[Event] = []
        for s in samples:
            events.append(
                TrackUpdate(
                    event_time=s.ts,
                    camera_id=s.camera_id,
                    track_id=s.track_id,
                    x_m=s.x_m,
                    y_m=s.y_m,
                    sigma_cm=s.sigma_cm,
                    is_staff=s.is_staff,
                    quality=Quality(
                        conf=s.conf,
                        occlusion_ratio=round(max(0.0, (0.97 - s.conf) * 0.8), 3),
                        coverage_ok=True,
                    ),
                )
            )
            events.extend(engine.process(s))
        for tid in ended:
            events.extend(engine.end_track(tid, now))
        events.extend(engine.tick(now))
        yield now, events
