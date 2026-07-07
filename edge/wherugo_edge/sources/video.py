"""Video kaynağı: RTSP/dosya + ultralytics takip ([cv] extra gerektirir).

Gizlilik değişmezi: frame'ler yalnız bu modülde, yerelde işlenir; dışarıya
yalnız plan-koordinatlı TrackSample çıkar (CONTRACTS §6).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator, Optional

from ..config import CameraDef, EdgeConfig
from ..zones import TrackSample

_CV_HINT = (
    "Video kaynağı için opsiyonel [cv] bağımlılıkları kurulu değil. "
    "Kurulum: pip install 'wherugo-edge[cv]'  (ultralytics + opencv-python). "
    "Donanımsız demo için --source simulate kullanın."
)


def _require_cv():
    try:
        import cv2  # noqa: F401
        from ultralytics import YOLO
    except ImportError as exc:
        raise ImportError(_CV_HINT) from exc
    return cv2, YOLO


class VideoSource:
    """Tek kamera akışını (RTSP URL veya dosya yolu) TrackSample'a çevirir."""

    def __init__(
        self,
        config: EdgeConfig,
        camera: CameraDef,
        source: str,
        *,
        model_name: str = "yolo11n.pt",
        sample_hz: float = 1.0,
    ) -> None:
        cv2, YOLO = _require_cv()
        self._cv2 = cv2
        self.config = config
        self.camera = camera
        self.source = source
        self.sample_period = 1.0 / max(0.1, sample_hz)
        self.model = YOLO(model_name)

    def samples(self) -> Iterator[TrackSample]:
        """Frame -> kişi takibi -> ayak noktası -> homografi -> plan örneği."""
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
            wall = now.timestamp()
            boxes = getattr(res, "boxes", None)
            if boxes is None or boxes.id is None:
                continue
            for box, tid_t, conf_t in zip(boxes.xyxy, boxes.id, boxes.conf):
                tid = int(tid_t)
                if wall - last_emit.get(tid, 0.0) < self.sample_period:
                    continue
                last_emit[tid] = wall
                x1, y1, x2, y2 = (float(v) for v in box[:4])
                # ayak noktası: kutunun alt-orta pikseli
                u = (x1 + x2) / 2.0
                v = y2
                try:
                    x_m, y_m = self.camera.homography.project(u, v)
                except ValueError:
                    continue
                yield TrackSample(
                    track_id=tid,
                    ts=now,
                    x_m=x_m,
                    y_m=y_m,
                    camera_id=self.camera.id,
                    is_staff=False,  # sahada BLE beacon eşlemesi doldurur
                    conf=float(conf_t),
                    sigma_cm=25.0,
                )


def open_source(config: EdgeConfig, source: str, camera_id: Optional[int] = None) -> VideoSource:
    """Config'ten kamerayı seçip VideoSource kurar; [cv] yoksa anlaşılır hata."""
    _require_cv()
    camera = config.cameras[0]
    if camera_id is not None:
        matches = [c for c in config.cameras if c.id == camera_id]
        if not matches:
            raise ValueError(f"config'te kamera bulunamadı: id={camera_id}")
        camera = matches[0]
    return VideoSource(config, camera, source)
