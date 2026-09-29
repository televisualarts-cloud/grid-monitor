# metar.py — keyless METAR wind layer for GB Energy Monitor's wind vs power-cuts tracker.
#
# One call to NOAA's Aviation Weather Center returns every METAR across the UK & Ireland
# (~60 stations), each with sustained wind and gust in KNOTS plus coordinates. No API key,
# no daily cap (unlike Open-Meteo). Stations are tagged with the licence area and country
# they fall in (point-in-polygon against region_polys.json, nearest-country fallback for
# coastal/offshore sites) so wind can be rolled up per region for the correlation model.
#
# Build 260929.1  (version = YYMMDD.N in UT; bump on every change to this file)
# Change history: CHANGELOG.md
#
# Python 3, stdlib only.

from __future__ import annotations
import json, os, time, math, threading
import urllib.request, urllib.error

_DIR = os.path.dirname(os.path.abspath(__file__))
POLY_FILE = os.path.join(_DIR, "region_polys.json")

# One bbox covering the UK & Ireland (S,W,N,E). JSON, keyless.
METAR_URL = ("https://aviationweather.gov/api/data/metar"
             "?format=json&bbox=49,-11,61,2")
UA = {"User-Agent": "uk-grid-monitor/1.0 (personal dashboard)"}
CACHE_TTL = 900          # 15 min — METARs refresh ~hourly, poll politely
TIMEOUT = 25
KT_TO_MPH = 1.15078


# ───────────────────────── polygons / classification ────────────────────────
def _load_polys():
    try:
        d = json.loads(open(POLY_FILE, encoding="utf-8").read())
        return d.get("areas") or [], d.get("countries") or []
    except Exception:
        return [], []


_AREAS, _COUNTRIES = _load_polys()


def _in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-9) + xi):
            inside = not inside
        j = i
    return inside


def _in_polys(lon, lat, feats):
    for f in feats:
        for r in f["rings"]:
            if _in_ring(lon, lat, r):
                return f["name"]
    return None


def _nearest(lon, lat, feats):
    kx = math.cos(lat * math.pi / 180)
    best, name = None, None
    for f in feats:
        for r in f["rings"]:
            for px, py in r:
                d = ((px - lon) * kx) ** 2 + (py - lat) ** 2
                if best is None or d < best:
                    best, name = d, f["name"]
    return name


def classify(lat, lon):
    """Return (licence_area, country) for a coordinate. Area may be None (offshore);
    country always resolves via a nearest-country fallback."""
    area = _in_polys(lon, lat, _AREAS)
    country = _in_polys(lon, lat, _COUNTRIES) or _nearest(lon, lat, _COUNTRIES)
    return area, country


# ───────────────────────── fetch + parse (cached) ───────────────────────────
_cache = {"ts": 0, "stations": [], "err": None}
_lock = threading.Lock()


def _get(url):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse(raw):
    """One normalised station per METAR: id, lat, lon, wind + gust in knots, obs time,
    and the region tags. Gust falls back to sustained wind when none is reported (gusts are
    only encoded when actually gusting)."""
    out = []
    for s in json.loads(raw):
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            continue
        wspd = _num(s.get("wspd"))
        wgst = _num(s.get("wgst"))
        area, country = classify(lat, lon)
        out.append({
            "id": s.get("icaoId"), "lat": lat, "lon": lon,
            "wspd_kt": wspd, "wgst_kt": wgst,
            "gust_kt": (wgst if wgst is not None else wspd),   # best available wind magnitude
            "obs": s.get("obsTime"),
            "area": area, "country": country,
        })
    return out


def stations(force=False):
    """Cached list of classified stations. Never raises; on fetch failure keeps the last
    good list and records the error."""
    now = time.time()
    with _lock:
        if not force and _cache["stations"] and now - _cache["ts"] < CACHE_TTL:
            return _cache["stations"]
    try:
        st = _parse(_get(METAR_URL))
        with _lock:
            _cache.update(ts=now, stations=st, err=None)
        return st
    except Exception as e:
        with _lock:
            _cache["err"] = f"{type(e).__name__}: {str(e)[:120]}"
            return _cache["stations"]     # last good (possibly empty)


# ───────────────────────── per-region rollup ────────────────────────────────
def _region_keys(st):
    """Region keys a station contributes to: ALL, its country, its licence area."""
    keys = ["ALL"]
    if st.get("country"):
        keys.append("country:" + st["country"])
    if st.get("area"):
        keys.append("area:" + st["area"])
    return keys


def live(st=None):
    """Current wind per region key: {key: {gust_kt, wind_kt, n_stations}}. gust_kt is the
    MAX gust (or sustained, where no gust) across the region's stations — the value the
    correlation model bins on."""
    st = stations() if st is None else st
    agg = {}
    for s in st:
        g = s.get("gust_kt")
        w = s.get("wspd_kt")
        for k in _region_keys(s):
            a = agg.get(k)
            if a is None:
                a = agg[k] = {"gust_kt": None, "wind_kt": None, "n_stations": 0}
            a["n_stations"] += 1
            if g is not None and (a["gust_kt"] is None or g > a["gust_kt"]):
                a["gust_kt"] = g
            if w is not None and (a["wind_kt"] is None or w > a["wind_kt"]):
                a["wind_kt"] = w
    return agg


def region_gust(key, st=None):
    """Max gust (kt) for one region key, or None."""
    return (live(st).get(key) or {}).get("gust_kt")


def status():
    with _lock:
        return {"stations": len(_cache["stations"]), "age_s": int(time.time() - _cache["ts"]) if _cache["ts"] else None,
                "error": _cache["err"]}


if __name__ == "__main__":
    st = stations(force=True)
    print("stations:", len(st), "| polys:", len(_AREAS), "areas", len(_COUNTRIES), "countries")
    lv = live(st)
    for k in ["ALL"] + sorted(x for x in lv if x.startswith("country:")):
        v = lv[k]
        print(f"  {k:34} gust {v['gust_kt']} kt  wind {v['wind_kt']} kt  n={v['n_stations']}")
