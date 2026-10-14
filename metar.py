# metar.py — keyless METAR wind layer for GB Energy Monitor's wind vs power-cuts tracker.
#
# NOAA's Aviation Weather Center: one bbox call over the UK & Ireland plus one by-id call for
# stations the bbox omits (~60 UK/IE stations in all), each with sustained wind and gust in
# KNOTS plus coordinates. No API key, no daily cap (unlike Open-Meteo). Stations are tagged with the licence area and country
# they fall in (point-in-polygon against region_polys.json, nearest-country fallback for
# coastal/offshore sites) so wind can be rolled up per region for the correlation model.
#
# Build 261006.2  (version = YYMMDD.N in UT; bump on every change to this file)
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
# The bbox query returns a thinned set (~56 stations) that leaves out London City, Gatwick,
# Biggin Hill, Northolt, Luton and Southend — so the London licence area had no station at
# all. These are fetched by id in a second call and merged (bbox entry wins on a duplicate).
# EGLC is the only METAR inside the London area; it issues none while the airport is shut
# (Sat midday – Sun midday), when London has no reading rather than a borrowed one.
EXTRA_IDS = ("EGLC", "EGKK", "EGKB", "EGWU", "EGGW", "EGMC")
METAR_IDS_URL = ("https://aviationweather.gov/api/data/metar"
                 "?format=json&ids=" + ",".join(EXTRA_IDS))
# Only UK & Ireland stations feed the rollups. The bbox also returns French, Norwegian and
# Channel Islands sites, which the nearest-country fallback was filing under England /
# Scotland — so a Brittany gust could become "England's" gust.
HOME_PREFIXES = ("EG", "EI")       # UK + Ireland ICAO prefixes
EXCLUDE_PREFIXES = ("EGJ",)        # Channel Islands (not GB grid)
MAX_OBS_AGE_S = 3 * 3600           # drop METARs older than this (closed / silent stations)
# Stand-in station for a licence area with no current report of its own. London rests on
# EGLC alone, which is silent while the airport is shut; Heathrow (24 h, just west of the
# area) then supplies the area's figure, marked stand_in so it is never passed off as
# London's own. It counts once only: Heathrow already feeds Southern England, England and
# ALL, so the stand-in is added to the London area key alone.
STAND_INS = {"London": "EGLL"}
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


def _home(icao):
    """True for a UK / Ireland station that belongs in the regional rollups."""
    icao = (icao or "").upper()
    return icao.startswith(HOME_PREFIXES) and not icao.startswith(EXCLUDE_PREFIXES)


def _parse(raw, now=None):
    """One normalised station per METAR: id, lat, lon, wind + gust in knots, obs time,
    and the region tags. Gust falls back to sustained wind when none is reported (gusts are
    only encoded when actually gusting). Stations outside the UK & Ireland are kept in the
    list but untagged (area/country None, foreign True), so they feed no rollup. A METAR
    older than MAX_OBS_AGE_S is dropped."""
    now = time.time() if now is None else now
    out = []
    for s in json.loads(raw):
        lat, lon = s.get("lat"), s.get("lon")
        if lat is None or lon is None:
            continue
        obs = _num(s.get("obsTime"))
        if obs is not None and now - obs > MAX_OBS_AGE_S:
            continue
        wspd = _num(s.get("wspd"))
        wgst = _num(s.get("wgst"))
        icao = s.get("icaoId")
        home = _home(icao)
        area, country = classify(lat, lon) if home else (None, None)
        out.append({
            "id": s.get("icaoId"), "lat": lat, "lon": lon,
            "wspd_kt": wspd, "wgst_kt": wgst,
            "gust_kt": (wgst if wgst is not None else wspd),   # best available wind magnitude
            "obs": s.get("obsTime"),
            "area": area, "country": country,
            "foreign": not home,
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
        st = _parse(_get(METAR_URL), now)
        extra_err = None
        try:                                  # second call: stations the bbox leaves out
            have = {x["id"] for x in st}
            st += [x for x in _parse(_get(METAR_IDS_URL), now) if x["id"] not in have]
        except Exception as e:                # keep the bbox set; note the partial fetch
            extra_err = f"extra ids: {type(e).__name__}: {str(e)[:100]}"
        with _lock:
            _cache.update(ts=now, stations=st, err=extra_err)
        return st
    except Exception as e:
        with _lock:
            _cache["err"] = f"{type(e).__name__}: {str(e)[:120]}"
            return _cache["stations"]     # last good (possibly empty)


# ───────────────────────── per-region rollup ────────────────────────────────
def _region_keys(st):
    """Region keys a station contributes to: ALL, its country, its licence area. A station
    outside the UK & Ireland contributes to none."""
    if st.get("foreign"):
        return []
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
    for area, sid in STAND_INS.items():
        k = "area:" + area
        if (agg.get(k) or {}).get("gust_kt") is not None:
            continue                      # the area has its own reading
        sub = next((s for s in st if s.get("id") == sid and s.get("gust_kt") is not None), None)
        if sub is not None:
            agg[k] = {"gust_kt": sub["gust_kt"], "wind_kt": sub.get("wspd_kt"),
                      "n_stations": 0, "stand_in": sid}
    return agg


def region_gust(key, st=None):
    """Max gust (kt) for one region key, or None."""
    return (live(st).get(key) or {}).get("gust_kt")


def status():
    with _lock:
        return {"stations": sum(1 for s in _cache["stations"] if not s.get("foreign")),
                "foreign": sum(1 for s in _cache["stations"] if s.get("foreign")),
                "age_s": int(time.time() - _cache["ts"]) if _cache["ts"] else None,
                "error": _cache["err"]}


if __name__ == "__main__":
    st = stations(force=True)
    print("stations:", len(st), "| polys:", len(_AREAS), "areas", len(_COUNTRIES), "countries")
    lv = live(st)
    for k in ["ALL"] + sorted(x for x in lv if x.startswith("country:")):
        v = lv[k]
        print(f"  {k:34} gust {v['gust_kt']} kt  wind {v['wind_kt']} kt  n={v['n_stations']}")
