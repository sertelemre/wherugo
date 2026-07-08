"""Video kaynağı: RTSP/dosya -> dedektör -> takip -> homografi -> TrackSample.

Dedektör soyutlaması (CONTRACTS §16):
- ``yolo``: ultralytics takibi ([yolo] extra, sahada; torch indirir).
- ``mock``: MOG2 arka plan çıkarımı + kontur + basit centroid takibi
  (yalnız opencv-python-headless, [cv] extra) — GPU'suz test/CI yolu.

Gizlilik değişmezi: frame'ler yalnız bu modülde ve klip ring buffer'ında
(yerel disk için) işlenir; dışarıya yalnız plan-koordinatlı TrackSample
çıkar (CONTRACTS §6). Frame'ler publisher'a ASLA girmez.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator, Optional

from ..config import CameraDef, EdgeConfig
from ..zones import TrackSample

_CV_HINT = (
    "Video kaynağı için opsiyonel [cv] bağımlılığı kurulu değil. "
    "Kurulum: pip install 'wherugo-edge[cv]'  (opencv-python-headless). "
    "Donanımsız demo için --source simulate kullanın."
)
_YOLO_HINT = (
    "detector: yolo için opsiyonel [yolo] bağımlılıkları kurulu değil. "
    "Kurulum: pip install 'wherugo-edge[yolo]'  (ultralytics + opencv-python-headless). "
    "GPU'suz test için --detector mock, donanımsız demo için --source simulate kullanın."
)


def _require_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise ImportError(_CV_HINT) from exc
    return cv2


def _require_yolo():
    cv2 = _require_cv2()
    try:
        from ultralytics import YOLO
    except ImportError as exc:
        raise ImportError(_YOLO_HINT) from exc
    return cv2, YOLO


# -- mock dedektör + takip (GPU'suz yol, CONTRACTS §16) --------------------


@dataclass
class Detection:
    """Piksel uzayında tek kutu (x1,y1 sol-üst; x2,y2 sağ-alt)."""

    x1: float
    y1: float
    x2: float
    y2: float
    conf: float = 0.6

    @property
    def centroid(self) -> tuple[float, float]:
        return ((self.x1 + self.x2) / 2.0, (self.y1 + self.y2) / 2.0)

    @property
    def foot(self) -> tuple[float, float]:
        """Ayak noktası: kutunun alt-orta pikseli (homografi girdisi)."""
        return ((self.x1 + self.x2) / 2.0, self.y2)


def _iou(a: Detection, b: Detection) -> float:
    ix1, iy1 = max(a.x1, b.x1), max(a.y1, b.y1)
    ix2, iy2 = min(a.x2, b.x2), min(a.y2, b.y2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0.0:
        return 0.0
    area_a = (a.x2 - a.x1) * (a.y2 - a.y1)
    area_b = (b.x2 - b.x1) * (b.y2 - b.y1)
    return inter / max(1e-9, area_a + area_b - inter)


class MockDetector:
    """MOG2 arka plan çıkarımı + kontur: kişi-vari hareketli blob tespiti.

    Model/ağırlık gerektirmez; sentetik ve test videolarındaki hareketli
    nesneleri Detection listesine çevirir (saf OpenCV).
    """

    def __init__(
        self,
        *,
        min_area_px: float = 120.0,
        history: int = 120,
        var_threshold: float = 16.0,
    ) -> None:
        cv2 = _require_cv2()
        self._cv2 = cv2
        self.min_area_px = float(min_area_px)
        self._subtractor = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=False
        )
        self._kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def detect(self, frame: Any) -> list[Detection]:
        cv2 = self._cv2
        mask = self._subtractor.apply(frame)
        _, mask = cv2.threshold(mask, 127, 255, cv2.THRESH_BINARY)
        # delikleri kapat + yakın parçaları birleştir (tek kişi = tek blob)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self._kernel)
        mask = cv2.dilate(mask, self._kernel, iterations=1)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        dets: list[Detection] = []
        for c in contours:
            if cv2.contourArea(c) < self.min_area_px:
                continue
            x, y, w, h = cv2.boundingRect(c)
            dets.append(Detection(float(x), float(y), float(x + w), float(y + h), conf=0.6))
        return dets


@dataclass
class _MockTrack:
    box: Detection
    lost: int = 0


class CentroidTracker:
    """Basit çok-nesne takibi: IoU öncelikli, sonra centroid mesafesi eşleme.

    Kayıp toleransı: eşleşmeyen track hemen silinmez, ``max_lost`` ardışık
    kare boyunca bekletilir (kısa oklüzyonda id korunur).
    """

    def __init__(
        self,
        *,
        max_lost: int = 5,
        max_dist_px: float = 80.0,
        min_iou: float = 0.05,
        next_id: int = 1,
    ) -> None:
        self.max_lost = int(max_lost)
        self.max_dist_px = float(max_dist_px)
        self.min_iou = float(min_iou)
        self._next_id = int(next_id)
        self._tracks: dict[int, _MockTrack] = {}

    def update(self, detections: list[Detection]) -> list[tuple[int, Detection]]:
        """Bu karedeki tespitlere id atar; yalnız bu karede görülenleri döndürür."""
        candidates: list[tuple[float, float, int, int]] = []
        for tid, tr in self._tracks.items():
            cx, cy = tr.box.centroid
            for i, det in enumerate(detections):
                iou = _iou(tr.box, det)
                dx, dy = det.centroid[0] - cx, det.centroid[1] - cy
                dist = (dx * dx + dy * dy) ** 0.5
                if iou >= self.min_iou or dist <= self.max_dist_px:
                    candidates.append((-iou, dist, tid, i))
        candidates.sort()  # önce en yüksek IoU, eşitse en yakın centroid

        matched: list[tuple[int, Detection]] = []
        used_tracks: set[int] = set()
        used_dets: set[int] = set()
        for _neg_iou, _dist, tid, i in candidates:
            if tid in used_tracks or i in used_dets:
                continue
            used_tracks.add(tid)
            used_dets.add(i)
            self._tracks[tid] = _MockTrack(box=detections[i], lost=0)
            matched.append((tid, detections[i]))

        for i, det in enumerate(detections):
            if i in used_dets:
                continue
            tid = self._next_id
            self._next_id += 1
            self._tracks[tid] = _MockTrack(box=det, lost=0)
            used_tracks.add(tid)
            matched.append((tid, det))

        for tid in list(self._tracks):
            if tid in used_tracks:
                continue
            tr = self._tracks[tid]
            tr.lost += 1
            if tr.lost > self.max_lost:
                del self._tracks[tid]

        matched.sort(key=lambda pair: pair[0])
        return matched


# -- kaynak ----------------------------------------------------------------


class VideoSource:
    """Tek kamera akışını (RTSP URL / dosya / enjekte frame iteratörü) TrackSample'a çevirir.

    ``frames`` verilirse VideoCapture yerine ``(ts, frame)`` iteratörü kullanılır
    (birim testleri sentetik kare dizisiyle GPU'suz uçtan uca doğrular).
    Son ~10 sn'nin kareleri klip çıkarımı (CONTRACTS §15) için yerel ring
    buffer'da tutulur; bu buffer ASLA tel'e çıkmaz.
    """

    def __init__(
        self,
        config: EdgeConfig,
        camera: CameraDef,
        source: str,
        *,
        detector: Optional[str] = None,
        model_name: Optional[str] = None,
        sample_hz: Optional[float] = None,
        frames: Optional[Iterable[tuple[datetime, Any]]] = None,
        mock_detector: Optional[MockDetector] = None,
        tracker: Optional[CentroidTracker] = None,
        clip_window_sec: float = 10.0,
        clip_buffer_frames: int = 64,
    ) -> None:
        self.config = config
        self.camera = camera
        self.source = source
        vp = config.video
        self.detector_kind = detector or vp.detector
        hz = sample_hz if sample_hz is not None else vp.sample_hz
        self.sample_period = 1.0 / max(0.001, hz)
        self.clip_window_sec = float(clip_window_sec)
        self._frames_override = frames
        self._clip_buffer: deque[tuple[datetime, Any]] = deque(maxlen=max(8, clip_buffer_frames))

        if self.detector_kind == "yolo":
            self._cv2, YOLO = _require_yolo()
            self.model = YOLO(model_name or vp.model)
        elif self.detector_kind == "mock":
            self._cv2 = _require_cv2()
            self.detector = mock_detector or MockDetector()
            self.tracker = tracker or CentroidTracker()
        else:
            raise ValueError(
                f"bilinmeyen detector: '{self.detector_kind}' (izinli: yolo, mock)"
            )

    # -- klip ring buffer (yalnız yerel; CONTRACTS §15) --------------------

    def _remember_frame(self, ts: datetime, frame: Any) -> None:
        if frame is not None:
            self._clip_buffer.append((ts, frame))

    def recent_frames(self, now: datetime) -> list[tuple[datetime, Any]]:
        """Son ``clip_window_sec`` penceresindeki kareler (klip çıkarımı için)."""
        cutoff = now - timedelta(seconds=self.clip_window_sec)
        return [(ts, f) for ts, f in self._clip_buffer if ts >= cutoff]

    # -- örnek akışı --------------------------------------------------------

    def samples(self) -> Iterator[TrackSample]:
        """Frame -> tespit/takip -> ayak noktası -> homografi -> plan örneği."""
        if self.detector_kind == "yolo":
            yield from self._samples_yolo()
        else:
            yield from self._samples_mock()

    def _project(self, uv: tuple[float, float]) -> Optional[tuple[float, float]]:
        try:
            return self.camera.homography.project(*uv)
        except ValueError:
            return None

    def _samples_yolo(self) -> Iterator[TrackSample]:
        last_emit: dict[int, float] = {}
        results = self.model.track(
            source=self.source,
            stream=True,
            persist=True,
            classes=[0],  # yalnız 'person'
            verbose=False,
        )
        for res in results:
            now = datetime.now(timezone.utc)
            self._remember_frame(now, getattr(res, "orig_img", None))
            wall = now.timestamp()
            boxes = getattr(res, "boxes", None)
            if boxes is None or boxes.id is None:
                continue
            for box, tid_t, conf_t in zip(boxes.xyxy, boxes.id, boxes.conf):
                tid = int(tid_t)
                if wall - last_emit.get(tid, float("-inf")) < self.sample_period:
                    continue
                last_emit[tid] = wall
                x1, y1, x2, y2 = (float(v) for v in box[:4])
                # ayak noktası: kutunun alt-orta pikseli
                pos = self._project(((x1 + x2) / 2.0, y2))
                if pos is None:
                    continue
                yield TrackSample(
                    track_id=tid,
                    ts=now,
                    x_m=pos[0],
                    y_m=pos[1],
                    camera_id=self.camera.id,
                    is_staff=False,  # sahada BLE beacon eşlemesi doldurur
                    conf=float(conf_t),
                    sigma_cm=25.0,
                )

    def _samples_mock(self) -> Iterator[TrackSample]:
        last_emit: dict[int, float] = {}
        for ts, frame in self._iter_frames():
            self._remember_frame(ts, frame)
            wall = ts.timestamp()
            detections = self.detector.detect(frame)
            for tid, det in self.tracker.update(detections):
                if wall - last_emit.get(tid, float("-inf")) < self.sample_period:
                    continue
                last_emit[tid] = wall
                pos = self._project(det.foot)
                if pos is None:
                    continue
                yield TrackSample(
                    track_id=tid,
                    ts=ts,
                    x_m=pos[0],
                    y_m=pos[1],
                    camera_id=self.camera.id,
                    is_staff=False,
                    conf=det.conf,
                    sigma_cm=35.0,  # arka plan çıkarımı YOLO'dan daha gürültülü
                )

    def _iter_frames(self) -> Iterator[tuple[datetime, Any]]:
        """(ts, frame) akışı: enjekte iteratör veya cv2.VideoCapture."""
        if self._frames_override is not None:
            yield from self._frames_override
            return
        cv2 = self._cv2
        cap = cv2.VideoCapture(self.source)
        if not cap.isOpened():
            raise RuntimeError(f"video kaynağı açılamadı: {self.source}")
        try:
            fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            is_stream = "://" in str(self.source)
            start = datetime.now(timezone.utc)
            i = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                if is_stream or fps <= 0:
                    ts = datetime.now(timezone.utc)  # canlı akış: duvar saati
                else:
                    ts = start + timedelta(seconds=i / fps)  # dosya: kare saati
                yield ts, frame
                i += 1
        finally:
            cap.release()


def open_source(
    config: EdgeConfig,
    source: str,
    camera_id: Optional[int] = None,
    *,
    detector: Optional[str] = None,
    frames: Optional[Iterable[tuple[datetime, Any]]] = None,
) -> VideoSource:
    """Config'ten kamerayı seçip VideoSource kurar; bağımlılık yoksa anlaşılır hata."""
    camera = config.cameras[0]
    if camera_id is not None:
        matches = [c for c in config.cameras if c.id == camera_id]
        if not matches:
            raise ValueError(f"config'te kamera bulunamadı: id={camera_id}")
        camera = matches[0]
    return VideoSource(config, camera, source, detector=detector, frames=frames)
