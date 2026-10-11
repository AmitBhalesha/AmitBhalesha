"""Loaders shared by the CLI and the Houston connectors."""

from __future__ import annotations

import sqlite3

from . import db

BEAT_ID_FIELDS = ("beat", "beats", "beat_id", "beatid", "beat_num", "beatnum", "hpd_beat", "id")


def beat_id_field(props: dict) -> str:
    """Pick the property that holds the beat code, e.g. BEAT or Beats."""
    lower = {k.lower(): k for k in props}
    for name in BEAT_ID_FIELDS:
        if name in lower:
            return lower[name]
    for key in props:
        if "beat" in key.lower():
            return key
    raise ValueError(f"No beat id property among {sorted(props)}; pass id_field explicitly")


def outer_rings(geometry: dict) -> list[list[list[float]]]:
    """Outer ring of every part of a Polygon/MultiPolygon (holes are ignored)."""
    if geometry["type"] == "Polygon":
        polys = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        polys = geometry["coordinates"]
    else:
        return []
    return [[[p[0], p[1]] for p in poly[0]] for poly in polys if poly and poly[0]]


def import_beats_geojson(
    conn: sqlite3.Connection, fc: dict, id_field: str | None = None, name_field: str | None = None
) -> int:
    """GeoJSON FeatureCollection -> beats table. Multi-part beats keep every part."""
    count = 0
    for feat in fc.get("features", []):
        props = feat.get("properties") or {}
        geom = feat.get("geometry")
        if not geom:
            continue
        field = id_field or (beat_id_field(props) if props else None)
        raw_id = props.get(field) if field else feat.get("id")
        if raw_id in (None, ""):
            continue
        beat_id = str(raw_id).strip().upper()
        rings = outer_rings(geom)
        if not rings:
            continue
        name = props.get(name_field) if name_field else props.get("name")
        name = str(name) if name else f"Beat {beat_id}"
        db.upsert_beat(conn, beat_id, name, rings[0] if len(rings) == 1 else rings)
        count += 1
    conn.commit()
    return count
