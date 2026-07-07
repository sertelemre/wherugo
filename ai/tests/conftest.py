import pytest


@pytest.fixture
def sample_bundle():
    return {
        "footfall_total": 312,
        "footfall_by_hour": {
            "09": 18, "10": 25, "11": 31, "12": 44, "13": 38,
            "14": 29, "15": 33, "16": 41, "17": 53,
        },
        "zones": [
            {"name": "Giriş", "visits": 310, "dwell_p50": 8.2, "draw_rate": 0.99},
            {"name": "Kadın Giyim", "visits": 178, "dwell_p50": 94.5, "draw_rate": 0.57},
            {"name": "Aksesuar", "visits": 66, "dwell_p50": 21.0, "draw_rate": 0.21},
            {"name": "Kasa", "visits": 121, "dwell_p50": 45.0, "draw_rate": 0.39},
        ],
        "queue": {"max_len": 7, "avg_wait_sec": 186.0, "abandons": 4},
        "funnel": {"entered": 312, "engaged": 236, "interacted": 88, "transactions": 61},
        "coverage_gap_min": 12.5,
        "prev_day": {"footfall_total": 287},
    }
