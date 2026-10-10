"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    db_path: str = "beatline.db"
    agency_name: str = "Demo City Police Department"
    timezone: str = "America/Denver"
    retention_days: int = 90
    tile_url: str = "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
    tile_url_dark: str = "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"
    tile_attribution: str = (
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors '
        '&copy; <a href="https://carto.com/attributions">CARTO</a>'
    )
    secure_cookies: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        env = os.environ
        d = cls()
        return cls(
            db_path=env.get("BEATLINE_DB", d.db_path),
            agency_name=env.get("BEATLINE_AGENCY", d.agency_name),
            timezone=env.get("BEATLINE_TZ", d.timezone),
            retention_days=int(env.get("BEATLINE_RETENTION_DAYS", d.retention_days)),
            tile_url=env.get("BEATLINE_TILE_URL", d.tile_url),
            tile_url_dark=env.get("BEATLINE_TILE_URL_DARK", env.get("BEATLINE_TILE_URL", d.tile_url_dark)),
            tile_attribution=env.get("BEATLINE_TILE_ATTRIBUTION", d.tile_attribution),
            secure_cookies=env.get("BEATLINE_SECURE_COOKIES", "0") == "1",
        )
