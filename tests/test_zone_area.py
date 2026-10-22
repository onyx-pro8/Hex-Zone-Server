"""Universal access requests open other networks' primary zones in that place."""
from math import pi

from app.services.zone_area import area_block_reason, measure_area_m2, ring_area_m2


def test_square_ring_has_area():
    ring = [[0.0, 0.0], [0.01, 0.0], [0.01, 0.01], [0.0, 0.01], [0.0, 0.0]]
    assert ring_area_m2(ring) > 1_000_000


def test_circle_radius_counts_when_there_is_no_polygon():
    area = measure_area_m2({}, {"radius_meters": 100})
    assert abs(area - pi * 100 * 100) < 1


def test_polygon_beats_a_radius():
    polygon = {
        "type": "Polygon",
        "coordinates": [[[0.0, 0.0], [0.01, 0.0], [0.01, 0.01], [0.0, 0.01], [0.0, 0.0]]],
    }
    area = measure_area_m2(
        {"geo_fence_polygon": polygon},
        {"radius_meters": 1},
    )
    assert area > pi


def test_each_zone_limit_blocks_before_the_total():
    assert area_block_reason(50, 0, each_zone_m2=10, total_m2=1000) == (
        "Exceeds the each-zone size limit."
    )


def test_total_limit_blocks_when_the_account_is_full():
    assert area_block_reason(30, 80, each_zone_m2=100, total_m2=100) == (
        "Exceeds the total size limit."
    )


def test_empty_limits_allow_any_size():
    assert area_block_reason(1_000_000, 1_000_000, each_zone_m2=None, total_m2=None) is None
