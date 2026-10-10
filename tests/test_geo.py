from beatline.geo import BeatIndex, Grid, haversine_m, point_in_polygon

SQUARE = [[0, 0], [10, 0], [10, 10], [0, 10]]
# A "C" shape: the notch on the east side is outside.
CONCAVE = [[0, 0], [10, 0], [10, 3], [4, 3], [4, 7], [10, 7], [10, 10], [0, 10]]


def test_point_in_square():
    assert point_in_polygon(5, 5, SQUARE)
    assert not point_in_polygon(11, 5, SQUARE)
    assert not point_in_polygon(5, -0.1, SQUARE)


def test_point_in_concave_polygon():
    assert point_in_polygon(2, 5, CONCAVE)
    assert not point_in_polygon(7, 5, CONCAVE)
    assert point_in_polygon(7, 1, CONCAVE)


def test_haversine_one_degree_latitude():
    assert abs(haversine_m(0, 0, 1, 0) - 111_195) < 50


def test_beat_index_locates_points():
    idx = BeatIndex({"A": SQUARE, "B": [[10, 0], [20, 0], [20, 10], [10, 10]]})
    assert idx.locate(5, 5) == "A"   # lat, lon
    assert idx.locate(5, 15) == "B"
    assert idx.locate(50, 50) is None


def test_grid_bounds_contain_point():
    g = Grid(200, 40.0)
    lat, lon = 40.0123, -111.987
    (s, w), (n, e) = g.bounds(g.cell(lat, lon))
    assert s <= lat < n and w <= lon < e
