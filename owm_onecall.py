# owm_onecall.py — OpenWeather One Call API 4.0 client for GB Energy Monitor.
#
# OC4 is modular: separate endpoints, and every record is nested in a `data[]`
# array (unlike the flat 2.5 body). This wraps the two endpoints we use:
#   * current           /data/4.0/onecall/current
#   * one-minute nowcast /data/4.0/onecall/timeline/1min   (60 x per-minute mm/h)
# and flattens them into the same key shape the server already builds from 2.5,
# so the caller can swap sources with a minimal branch.
#
# Auto-detect contract: an unsubscribed key returns HTTP 401/403 on these
# endpoints. Callers try OC4 and, on is_unauthorized(err), fall back to the free
# 2.5 Current Weather call — the base weather panel keeps working, only the
# nowcast features go dark. Stdlib only, no import of the server.

from __future__ import annotations
import json, urllib.request, urllib.error

OC4_BASE = "https://api.openweathermap.org/data/4.0/onecall"


def _default_fetch(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "uk-grid-monitor/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def is_unauthorized(exc):
    """True if the error means 'key not subscribed to this endpoint' — the signal
    to fall back to 2.5. 401 = bad/free key on a paid endpoint, 403 = forbidden."""
    return isinstance(exc, urllib.error.HTTPError) and exc.code in (401, 403)


def fetch_current(lat, lon, api_key, units="metric", lang="en",
                  timeout=12, fetch=_default_fetch):
    """OC4 current conditions, flattened to the server's `cond`/`home` key shape.
    Raises on HTTP/network error (caller classifies via is_unauthorized)."""
    url = (f"{OC4_BASE}/current?lat={lat}&lon={lon}"
           f"&units={units}&lang={lang}&appid={api_key}")
    root = fetch(url, timeout)
    data = root.get("data") or [{}]
    rec = data[0] if data else {}
    wx = (rec.get("weather") or [{}])
    wx0 = wx[0] if wx else {}
    return {
        "temp": rec.get("temp"),
        "feels_like": rec.get("feels_like"),
        "pressure": rec.get("pressure"),          # hPa
        "humidity": rec.get("humidity"),
        "dew_point": rec.get("dew_point"),        # OC4 supplies it directly (metric = C)
        "clouds_pct": rec.get("clouds"),
        "uvi": rec.get("uvi"),
        "visibility_m": rec.get("visibility"),
        "wind_speed_ms": rec.get("wind_speed"),   # metric = m/s
        "wind_deg": rec.get("wind_deg"),
        "wind_gust_ms": rec.get("wind_gust"),
        "rain_1h": (rec.get("rain") or {}).get("1h"),    # mm/h (rate)
        "snow_1h": (rec.get("snow") or {}).get("1h"),
        "cond_main": wx0.get("main"),
        "cond_desc": wx0.get("description"),
        "sunrise": rec.get("sunrise"),
        "sunset": rec.get("sunset"),
        "tz_offset": root.get("timezone_offset"),        # moved to ROOT in OC4
        "alert_ids": rec.get("alerts") or [],            # IDs only; detail = separate call
        "api": "OC4",
    }


def fetch_minute(lat, lon, api_key, timeout=12, fetch=_default_fetch):
    """OC4 one-minute nowcast: up to 60 forward per-minute precipitation records.
    Returns [{'dt': unix, 'mm_h': rate}, ...] sorted by time. Raises on error."""
    url = f"{OC4_BASE}/timeline/1min?lat={lat}&lon={lon}&appid={api_key}"
    root = fetch(url, timeout)
    out = [{"dt": r.get("dt"), "mm_h": r.get("precipitation")}
           for r in (root.get("data") or []) if r.get("dt") is not None]
    out.sort(key=lambda r: r["dt"])
    return out


def try_conditions(lat, lon, api_key, want_minute=True, timeout=12, fetch=_default_fetch):
    """Convenience for the auto-detect caller. Returns a dict:
       {'tier': 'OC4', 'cond': {...}, 'minute': [...] , 'error': None}
    on success; {'tier': None, 'error': 'unauthorized'|str} when the key isn't
    subscribed (caller then falls back to 2.5) or the call failed. Never raises."""
    try:
        cond = fetch_current(lat, lon, api_key, timeout=timeout, fetch=fetch)
    except Exception as e:
        return {"tier": None, "cond": None, "minute": None,
                "error": "unauthorized" if is_unauthorized(e) else f"{type(e).__name__}: {e}"}
    minute = None
    if want_minute:
        try:
            minute = fetch_minute(lat, lon, api_key, timeout=timeout, fetch=fetch)
        except Exception as e:
            minute = None            # nowcast optional; current already succeeded
    return {"tier": "OC4", "cond": cond, "minute": minute, "error": None}
