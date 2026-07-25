"""Hourly weather forecast snapshots from MET Norway (Locationforecast 2.0).

Forecasts (not observations) are stored on purpose: at prediction time the model
may only use what would have been known then. Using observed weather for a
future timestamp would be data leakage.
"""

import json
from datetime import datetime, timedelta, timezone

from . import net
from .config import MET_USER_AGENT, WEATHER_LAT, WEATHER_LON

URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat}&lon={lon}"


def fetch_weather_rows():
    raw = net.get(
        URL.format(lat=WEATHER_LAT, lon=WEATHER_LON),
        headers={"User-Agent": MET_USER_AGENT},
    )
    doc = json.loads(raw)
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(hours=24)
    polled_at = now.isoformat()

    rows = []
    for entry in doc["properties"]["timeseries"]:
        t = datetime.fromisoformat(entry["time"].replace("Z", "+00:00"))
        if t > horizon:
            break
        instant = entry["data"]["instant"]["details"]
        next_hour = entry["data"].get("next_1_hours", {})
        rows.append(
            {
                "polled_at": polled_at,
                "lat": WEATHER_LAT,
                "lon": WEATHER_LON,
                "forecast_time": entry["time"],
                "air_temp": instant.get("air_temperature"),
                "precip_mm": next_hour.get("details", {}).get("precipitation_amount"),
                "wind_mps": instant.get("wind_speed"),
                "wind_dir": instant.get("wind_from_direction"),
                "symbol": next_hour.get("summary", {}).get("symbol_code"),
            }
        )
    return rows
