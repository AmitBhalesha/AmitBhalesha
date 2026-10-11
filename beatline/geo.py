"""Small geometry helpers. Everything is lon/lat in WGS84; distances in meters.

Beats and zones in a single city are small enough that a flat-earth
approximation around a reference latitude is accurate to well under a meter,
so we avoid a GIS dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

EARTH_RADIUS_M = 6_371_008.8
M_PER_DEG_LAT = 111_320.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def point_in_polygon(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray-casting test. `ring` is [[lon, lat], ...]; closing vertex optional."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_cross = (xj - xi) * (lat - yi) / (yj - yi) + xi
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


@dataclass(frozen=True)
class Area:
    id: str
    ring: list[list[float]]
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    @classmethod
    def from_ring(cls, area_id: str, ring: list[list[float]]) -> "Area":
        lons = [p[0] for p in ring]
        lats = [p[1] for p in ring]
        return cls(area_id, ring, min(lons), min(lats), max(lons), max(lats))

    def contains(self, lat: float, lon: float) -> bool:
        if not (self.min_lon <= lon <= self.max_lon and self.min_lat <= lat <= self.max_lat):
            return False
        return point_in_polygon(lon, lat, self.ring)


class BeatIndex:
    """Answers "which beat is this point in?". Values are one ring or a list of
    rings (multi-part beats); a few hundred beats is fine with a bbox pre-check."""

    def __init__(self, beats: dict[str, list]):
        self.areas = []
        for beat_id, polygon in beats.items():
            rings = [polygon] if polygon and isinstance(polygon[0][0], (int, float)) else polygon
            self.areas += [Area.from_ring(beat_id, ring) for ring in rings]

    def locate(self, lat: float, lon: float) -> str | None:
        for area in self.areas:
            if area.contains(lat, lon):
                return area.id
        return None


class Grid:
    """Square-ish cells of `cell_m` meters, anchored at a reference latitude."""

    def __init__(self, cell_m: float, ref_lat: float):
        self.dlat = cell_m / M_PER_DEG_LAT
        self.dlon = cell_m / (M_PER_DEG_LAT * math.cos(math.radians(ref_lat)))

    def cell(self, lat: float, lon: float) -> tuple[int, int]:
        return math.floor(lat / self.dlat), math.floor(lon / self.dlon)

    def bounds(self, cell: tuple[int, int]) -> list[list[float]]:
        """[[south, west], [north, east]] - the order Leaflet expects."""
        i, j = cell
        return [
            [round(i * self.dlat, 6), round(j * self.dlon, 6)],
            [round((i + 1) * self.dlat, 6), round((j + 1) * self.dlon, 6)],
        ]
