"""Config yükleme + doğrulama; deploy/edge-demo.yaml sözleşme kontrolü."""
from pathlib import Path

import pytest

from wherugo_edge.config import ConfigError, config_from_dict, load_config

DEMO_YAML = Path(__file__).resolve().parents[2] / "deploy" / "edge-demo.yaml"


def minimal_dict():
    return {
        "tenant_id": "t_demo",
        "store_id": 1,
        "device_id": "edge-1a",
        "backend_url": "http://localhost:8000",
        "cameras": [{"id": 1}],
        "zones": [
            {"id": 1, "zone_type": "entrance", "polygon": [[0, 0], [1, 0], [1, 1]]}
        ],
    }


def test_demo_yaml_matches_contract():
    cfg = load_config(DEMO_YAML)
    assert cfg.tenant_id == "t_demo"
    assert cfg.store_id == 1
    assert cfg.device_id == "edge-1a"
    assert cfg.backend_url == "http://localhost:8000"
    assert cfg.store.plan_width_m == 20.0
    assert cfg.store.plan_height_m == 12.0
    assert len(cfg.zones) == 8
    types = [z.zone_type for z in cfg.zones]
    assert types.count("shelf") == 4
    for t in ("entrance", "fitting_room", "queue", "checkout"):
        assert t in types
    categories = {z.category for z in cfg.zones if z.zone_type == "shelf"}
    assert categories == {"kadin-ust", "erkek-ust", "aksesuar", "ayakkabi"}
    assert cfg.simulation.staff_ratio == 0.1
    assert cfg.engine.dwell_threshold_sec == 5.0
    assert cfg.engine.queue_interval_sec == 30.0


def test_missing_required_field():
    d = minimal_dict()
    del d["tenant_id"]
    with pytest.raises(ConfigError, match="tenant_id"):
        config_from_dict(d)


def test_invalid_zone_type():
    d = minimal_dict()
    d["zones"][0]["zone_type"] = "warehouse"
    with pytest.raises(ConfigError, match="zone_type"):
        config_from_dict(d)


def test_polygon_needs_three_points():
    d = minimal_dict()
    d["zones"][0]["polygon"] = [[0, 0], [1, 1]]
    with pytest.raises(ConfigError, match="polygon"):
        config_from_dict(d)


def test_duplicate_zone_ids():
    d = minimal_dict()
    d["zones"].append({"id": 1, "zone_type": "shelf", "polygon": [[2, 2], [3, 2], [3, 3]]})
    with pytest.raises(ConfigError, match="benzersiz"):
        config_from_dict(d)


def test_staff_ratio_bounds():
    d = minimal_dict()
    d["simulation"] = {"staff_ratio": 1.5}
    with pytest.raises(ConfigError, match="staff_ratio"):
        config_from_dict(d)


def test_hourly_curve_length():
    d = minimal_dict()
    d["simulation"] = {"hourly_curve": [1.0] * 23}
    with pytest.raises(ConfigError, match="hourly_curve"):
        config_from_dict(d)
