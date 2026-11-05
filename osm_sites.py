"""Generator sites and BM unit positions from OpenStreetMap and the REPD, for the transmission wall (/wall).

Build 261029.4  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Standard library only. Optional companion to wall.py; the wall runs unchanged without it.

Source
  UK Grid Atlas (github.com/jacobwright32/uk-grid-atlas, code MIT) publishes two files
  built from OpenStreetMap (data (c) OpenStreetMap contributors, ODbL):
    stations.json   about 3,600 GB generator sites: position, fuel, capacity, operator
    bmu-map.json    BM unit id -> the OpenStreetMap site it belongs to (about 380 units)
  They are fetched weekly into osm_sites.json (git-ignored); a failed fetch keeps the
  last good copy. Fetching runs in its own thread and never blocks a request.

  The Renewable Energy Planning Database (REPD, DESNZ, Open Government Licence v3.0) is a
  quarterly CSV of every renewable and storage project of 150 kW or more, with its status,
  capacity and an OSGB grid reference. Its file name changes every quarter, so the newest is
  found from the gov.uk publication page's content API, checked weekly, and downloaded (about
  5 MB) only when it changes. GB projects that are operational or under construction are kept
  in repd_sites.json (git-ignored), positions converted to WGS84
  (OSGB36 Transverse Mercator inverse, then the OS Helmert transform: within about 5 m).

Generator sites (/api/wall-sites)
  sites_bytes() gives the page a compact list of every OpenStreetMap site with a position, its
  fuel, capacity, name, operator and start date, biggest first, for the wall's faint
  "generators" map layer, each with the REPD record(s) matched to it, followed by the
  operational REPD projects that no OSM site matches (the "REPD" layer).
  Matching (see _repd_match): an REPD project matches the nearest OSM site of a compatible fuel
  within a radius that grows with its capacity (wind farms cover a large area; offshore REPD
  points can be far from OSM's), when the names share a word and no number or compass word in
  them disagrees ("Hornsea 1" is not "Hornsea 2", "Moray East" is not "Moray West"), or when
  the two capacities agree within 10 %, or (any name) within 0.5 km. OSM's capacity is kept
  where it has one; REPD's fills it where OSM has none, and is shown beside it where the two
  differ. An OSM site matched only to projects not yet operational shows that REPD record.

Unit positions
  The wall places each BM unit at the network node nearest to it, using Terravolt's unit
  list (bmu_locations.json). Many of those positions are approximate and some are wrong
  (Viking Wind Farm, in Shetland, was placed by the Humber). unit_locations() lays the
  OpenStreetMap positions over Terravolt's: a unit the atlas maps takes the OSM site's
  position where Terravolt gives none, or where the two differ by more than MOVE_KM
  (Terravolt gives most positions to 2 decimal places, about 1 km, so smaller
  differences are rounding). The atlas matches units to sites by name, and a near-miss
  name can pick the wrong site (Markinch CHP in Glenrothes matched "Rothes" in Moray), so a
  move of more than CHECK_KM is made only when the fuel agrees and the two names share a
  word; otherwise Terravolt's position is kept and the unit is listed in status()["kept"].
  Terravolt's names and types are kept. Each unit records
  where its position came from ("pos": "osm" or "tv") and how far it moved. The
  Terravolt cache file itself is not changed: it is a download and is replaced weekly.
"""
import json
import math
import re
import threading
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
SITES_FILE = HERE / "osm_sites.json"
REPD_FILE = HERE / "repd_sites.json"
REPD_API = ("https://www.gov.uk/api/content/government/publications/"
            "renewable-energy-planning-database-quarterly-extract")
REPD_PAGE = "https://www.gov.uk/government/publications/renewable-energy-planning-database-quarterly-extract"
REPD_CHECK = 7 * 86400        # look for a new quarterly file weekly
REPD_KEEP = {"Operational", "Under Construction"}   # projects not yet started are left out
REPD_MIN = 1000               # fewer usable rows = the file's layout changed: keep the old copy
ATLAS_RAW = "https://raw.githubusercontent.com/jacobwright32/uk-grid-atlas/main/src/data/gb/"
ATLAS_URL = "https://github.com/jacobwright32/uk-grid-atlas"
ATLAS_TTL = 7 * 86400         # refresh weekly; the atlas regenerates its OSM extract occasionally
RETRY_S = [600, 1800, 3600, 6 * 3600]
MIN_SITES = 1000              # fewer sites than this = the file's layout changed: keep the old copy
MOVE_KM = 1.0                 # a Terravolt position further than this from the OSM site is corrected
CHECK_KM = 20.0              # a bigger move must agree on fuel and share a name word
# Terravolt unit type -> the atlas's fuel names it may match
FUEL_OK = {"Wind: Onshore": {"wind_onshore", "wind_offshore"}, "Wind: Offshore": {"wind_offshore", "wind_onshore"},
           "Gas": {"gas", "oil", "other"}, "OCGT": {"gas", "oil", "other"}, "Peaker": {"gas", "oil", "storage", "other"},
           "Nuclear": {"nuclear"}, "Biomass": {"bioenergy", "waste", "gas", "coal"}, "Hydro": {"hydro", "pumped"},
           "Pumped": {"pumped", "hydro"}, "Battery": {"storage", "other"}, "Solar": {"solar", "storage"},
           "Coal": {"coal", "bioenergy", "gas"}, "Oil": {"oil", "gas"}}
_NAME_SKIP = {"wind", "farm", "windfarm", "power", "station", "energy", "battery", "storage", "bess", "solar", "park",
              "offshore", "extension", "ext", "plant", "limited", "ltd", "the", "and", "site", "project", "phase", "unit",
              "hydro", "scheme", "chp", "ccgt", "ocgt", "stor", "flex", "gen", "bio"}
UA = {"User-Agent": "uk-grid-monitor/1.0 (personal dashboard; generator sites)"}

_lock = threading.Lock()
_st = {"data": None, "loaded": False, "busy": False, "fails": 0, "next_try": 0.0, "error": None,
       "ov_key": None, "ov": None, "kept": [],
       "repd": None, "r_fails": 0, "r_next": 0.0, "r_error": None}


def _words(s):
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) > 2 and w not in _NAME_SKIP}


def _plausible(o, s, km):
    """Is the atlas's site believable for this Terravolt unit, given how far it moves it?"""
    if km <= CHECK_KM:
        return True
    ok = FUEL_OK.get(o.get("type"))
    if ok is not None and s[3] not in ok:
        return False
    return bool(_words(o.get("name")) & _words(s[2]))


def _km(lat1, lon1, lat2, lon2):
    return math.hypot((lon1 - lon2) * math.cos(math.radians((lat1 + lat2) / 2)) * 111.32,
                      (lat1 - lat2) * 110.57)


def _get(url, timeout=90):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _fetch():
    """Download and trim the atlas files. Raises on any failure or a changed layout."""
    st = json.loads(_get(ATLAS_RAW + "stations.json"))
    bm = json.loads(_get(ATLAS_RAW + "bmu-map.json")).get("byUnit") or {}
    try:
        meta = json.loads(_get(ATLAS_RAW + "meta.json", 30))
    except Exception:
        meta = {}
    sites = {}
    for f in st.get("features") or []:
        g, p = f.get("geometry") or {}, f.get("properties") or {}
        if g.get("type") != "Point" or not p.get("id"):
            continue
        lon, lat = g["coordinates"][:2]
        mw = p.get("capacityMW")
        sites[p["id"]] = [round(lat, 5), round(lon, 5), (p.get("name") or "")[:60], p.get("fuel") or "other",
                          round(mw, 1) if isinstance(mw, (int, float)) else None,
                          (p.get("operator") or "")[:50], (p.get("start") or "")[:10]]
    if len(sites) < MIN_SITES:
        raise ValueError(f"atlas layout changed: only {len(sites)} sites")
    return {"ts": time.time(), "generated": meta.get("generated"), "source": ATLAS_URL,
            "sites": sites, "bmu": {k: v for k, v in bm.items() if v in sites}}


def _save(path, d):
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def _worker(do_atlas, do_repd):
    try:
        if do_atlas:
            try:
                d = _fetch()
                _save(SITES_FILE, d)
                with _lock:
                    _st.update(data=d, fails=0, error=None, next_try=0.0)
            except Exception as e:
                with _lock:
                    _st["error"] = f"{type(e).__name__}: {str(e)[:120]}"
                    _st["next_try"] = time.time() + RETRY_S[min(_st["fails"], len(RETRY_S) - 1)]
                    _st["fails"] += 1
        if do_repd:
            try:
                r = _repd_refresh(_st["repd"])
                _save(REPD_FILE, r)
                with _lock:
                    _st.update(repd=r, r_fails=0, r_error=None, r_next=0.0)
            except Exception as e:
                with _lock:
                    _st["r_error"] = f"{type(e).__name__}: {str(e)[:120]}"
                    _st["r_next"] = time.time() + RETRY_S[min(_st["r_fails"], len(RETRY_S) - 1)]
                    _st["r_fails"] += 1
    finally:
        with _lock:
            _st["busy"] = False


def atlas():
    """The atlas data ({"sites", "bmu", "ts", ...}) or None. Starts a background refresh when
    it, or the REPD check, is due; never waits for it."""
    with _lock:
        if not _st["loaded"]:
            _st["loaded"] = True
            for k, f in (("data", SITES_FILE), ("repd", REPD_FILE)):
                try:
                    _st[k] = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    _st[k] = None
        d, r, now = _st["data"], _st["repd"], time.time()
        do_a = (d is None or now - d.get("ts", 0) > ATLAS_TTL) and now >= _st["next_try"]
        do_r = (r is None or now - r.get("checked", 0) > REPD_CHECK) and now >= _st["r_next"]
        if (do_a or do_r) and not _st["busy"]:
            _st["busy"] = True
            threading.Thread(target=_worker, args=(do_a, do_r), name="osm-sites", daemon=True).start()
        return d


def status():
    d, r = _st["data"], _st["repd"]
    return {"ts": d.get("ts") if d else None, "generated": d.get("generated") if d else None,
            "sites": len(d["sites"]) if d else 0, "units": len(d["bmu"]) if d else 0, "error": _st["error"],
            "kept": _st["kept"], "repd": {"title": r.get("title"), "url": r.get("url"), "ts": r.get("ts"),
            "checked": r.get("checked"), "rows": len(r.get("recs") or [])} if r else None, "repd_error": _st["r_error"]}


# ---------------------------------------------------------------- REPD
def osgb_to_wgs84(E, N):
    """OSGB36 National Grid easting/northing (m) -> WGS84 (lat, lon) degrees. Transverse Mercator
    inverse on the Airy 1830 ellipsoid, then the OS 7-parameter Helmert transform (about 5 m)."""
    a, b, F0 = 6377563.396, 6356256.909, 0.9996012717
    lat0, lon0, N0, E0 = math.radians(49), math.radians(-2), -100000.0, 400000.0
    e2, n = 1 - b * b / (a * a), (a - b) / (a + b)
    lat, M = lat0, 0.0
    for _ in range(20):
        lat = (N - N0 - M) / (a * F0) + lat
        dl, sl = lat - lat0, lat + lat0
        M = b * F0 * ((1 + n + 1.25 * n ** 2 + 1.25 * n ** 3) * dl - (3 * n + 3 * n ** 2 + 2.625 * n ** 3) * math.sin(dl) * math.cos(sl)
                      + (1.875 * n ** 2 + 1.875 * n ** 3) * math.sin(2 * dl) * math.cos(2 * sl)
                      - 35 / 24 * n ** 3 * math.sin(3 * dl) * math.cos(3 * sl))
        if abs(N - N0 - M) < 1e-5:
            break
    s, c, t = math.sin(lat), math.cos(lat), math.tan(lat)
    nu = a * F0 / math.sqrt(1 - e2 * s * s)
    rho = a * F0 * (1 - e2) / (1 - e2 * s * s) ** 1.5
    eta2 = nu / rho - 1
    dE = E - E0
    phi = (lat - t / (2 * rho * nu) * dE ** 2 + t / (24 * rho * nu ** 3) * (5 + 3 * t * t + eta2 - 9 * t * t * eta2) * dE ** 4
           - t / (720 * rho * nu ** 5) * (61 + 90 * t * t + 45 * t ** 4) * dE ** 6)
    lam = (lon0 + dE / (c * nu) - (nu / rho + 2 * t * t) / (6 * c * nu ** 3) * dE ** 3
           + (5 + 28 * t * t + 24 * t ** 4) / (120 * c * nu ** 5) * dE ** 5
           - (61 + 662 * t * t + 1320 * t ** 4 + 720 * t ** 6) / (5040 * c * nu ** 7) * dE ** 7)
    s, c = math.sin(phi), math.cos(phi)
    nu = a / math.sqrt(1 - e2 * s * s)
    x, y, z = nu * c * math.cos(lam), nu * c * math.sin(lam), (1 - e2) * nu * s
    tx, ty, tz, sc = 446.448, -125.157, 542.060, -20.4894e-6
    rx, ry, rz = (math.radians(v / 3600) for v in (0.1502, 0.2470, 0.8421))
    x, y, z = (tx + (1 + sc) * x - rz * y + ry * z, ty + rz * x + (1 + sc) * y - rx * z, tz - ry * x + rx * y + (1 + sc) * z)
    a2, b2 = 6378137.0, 6356752.3142
    e22, p = 1 - b2 * b2 / (a2 * a2), math.hypot(x, y)
    la = math.atan2(z, p * (1 - e22))
    for _ in range(10):
        la = math.atan2(z + e22 * a2 / math.sqrt(1 - e22 * math.sin(la) ** 2) * math.sin(la), p)
    return math.degrees(la), math.degrees(math.atan2(y, x))


# REPD technology -> the atlas's fuel names
REPD_TECH = {"Solar Photovoltaics": "solar", "Wind Onshore": "wind_onshore", "Wind Offshore": "wind_offshore",
             "Battery": "storage", "Flywheels": "storage", "Liquid Air Energy Storage": "storage",
             "Compressed Air Energy Storage": "storage", "Landfill Gas": "bioenergy", "Anaerobic Digestion": "bioenergy",
             "Sewage Sludge Digestion": "bioenergy", "Biomass (dedicated)": "bioenergy", "Biomass (co-firing)": "bioenergy",
             "Advanced Conversion Technologies": "waste", "EfW Incineration": "waste", "Small Hydro": "hydro",
             "Large Hydro": "hydro", "Pumped Storage Hydroelectricity": "pumped", "Tidal Stream": "marine",
             "Tidal Lagoon": "marine", "Tidal Barrage and Tidal Stream": "marine", "Shoreline Wave": "marine"}
# an REPD fuel may match OSM sites of these fuels
REPD_OK = {"bioenergy": {"bioenergy", "waste", "gas", "other"}, "waste": {"waste", "bioenergy", "other"},
           "hydro": {"hydro", "pumped"}, "pumped": {"pumped", "hydro"}, "wind_onshore": {"wind_onshore"},
           "wind_offshore": {"wind_offshore"}, "solar": {"solar"}, "storage": {"storage", "other"},
           "marine": {"marine", "other"}, "other": {"other"}}


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _repd_refresh(old):
    """Find the newest quarterly REPD CSV; download and parse it if it is new. Returns the data
    to keep (the old data with a new check time if nothing changed). Raises on failure."""
    import csv
    import io
    page = json.loads(_get(REPD_API, 30))
    att = [x for x in (page.get("details") or {}).get("attachments") or []
           if (x.get("content_type") or "").startswith("text/csv") or str(x.get("url", "")).lower().endswith(".csv")]
    if not att:
        raise ValueError("REPD page: no CSV attachment")
    url, title = att[0]["url"], att[0].get("title") or ""
    if old and old.get("url") == url and old.get("recs"):
        return dict(old, checked=time.time())
    raw = _get(url, 180)
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows, recs = csv.DictReader(io.StringIO(text)), []
    for r in rows:
        r = {(k or "").strip(): (v or "").strip() for k, v in r.items()}
        st = r.get("Development Status (short)", "")
        if st not in REPD_KEEP or r.get("Country") == "Northern Ireland":
            continue
        E, N = _num(r.get("X-coordinate")), _num(r.get("Y-coordinate"))
        if E is None or N is None or not (0 < E < 700000 and 0 < N < 1300000):
            continue
        lat, lon = osgb_to_wgs84(E, N)
        tech = r.get("Technology Type", "")
        recs.append([r.get("Ref ID", ""), r.get("Site Name", "")[:60], (r.get("Operator (or Applicant)") or "")[:50], tech[:40],
                     REPD_TECH.get(tech, "other"), _num(r.get("Installed Capacity (MWelec)")), st,
                     round(lat, 5), round(lon, 5), r.get("Operational", "")[:10]])
    if len(recs) < REPD_MIN:
        raise ValueError(f"REPD layout changed: only {len(recs)} usable rows")
    return {"ts": time.time(), "checked": time.time(), "url": url, "title": title, "page": REPD_PAGE, "recs": recs}


_RSKIP = _NAME_SKIP | {"pv", "array", "renewable", "renewables", "solarfarm", "landfill", "gas", "digestion", "anaerobic",
                       "plc", "house", "lane", "road", "green", "hill", "north", "south", "east", "west", "new", "old",
                       "generation", "one", "two", "three", "four", "five", "community", "repowering", "previously",
                       "application", "facility", "centre", "center"}
_NUMW = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "ii": "2", "iii": "3", "iv": "4"}
_DIRW = {"north", "south", "east", "west"}


def _rwords(s):
    return {w for w in re.findall(r"[a-z]+", (s or "").lower()) if len(w) > 2 and w not in _RSKIP}


def _disc(s):
    return {_NUMW.get(w, w) for w in re.findall(r"[a-z]+|\d+", (s or "").lower()) if w.isdigit() or w in _NUMW or w in _DIRW}


def _named(a, b):
    if not (_rwords(a) & _rwords(b)):
        return False
    da, db = _disc(a), _disc(b)
    return not (da and db and not (da & db))


def _radius(fuel, mw):
    m = mw or 1
    return max(30.0, math.sqrt(m) * 1.5) if fuel == "wind_offshore" else max(2.0, min(20.0, math.sqrt(m) * 0.9))


def _repd_match(sites, recs):
    """{osm id: [REPD rows]} and the operational REPD rows no OSM site matches (see module notes)."""
    grid, ids = {}, list(sites)
    for i, sid in enumerate(ids):
        s = sites[sid]
        grid.setdefault((int(s[0] * 10), int(s[1] * 6)), []).append(i)
    per, only = {}, []
    for r in recs:
        lat, lon, fuel, mw, name = r[7], r[8], r[4], r[5], r[1]
        R, ok = _radius(fuel, mw), REPD_OK.get(fuel, {"other"})
        n = int(R / 11) + 1
        gy, gx, best = int(lat * 10), int(lon * 6), None
        for dy in range(-n, n + 1):
            for dx in range(-n, n + 1):
                for i in grid.get((gy + dy, gx + dx), ()):
                    s = sites[ids[i]]
                    if s[3] not in ok:
                        continue
                    d = _km(lat, lon, s[0], s[1])
                    if d > R:
                        continue
                    nm = _named(name, s[2])
                    cap = mw and s[4] and mw >= 5 and abs(mw - s[4]) <= 0.1 * max(mw, s[4])
                    if not (nm or cap or d <= 0.5):
                        continue
                    k = (0 if nm else 1 if cap else 2, d)
                    if best is None or k < best[0]:
                        best = (k, ids[i])
        if best:
            per.setdefault(best[1], []).append(r)
        elif r[6] == "Operational":
            only.append(r)
    return per, only


CREDIT = "© OpenStreetMap contributors (ODbL), via UK Grid Atlas"
REPD_CREDIT = ("Contains public sector information licensed under the Open Government Licence v3.0 "
               "(DESNZ Renewable Energy Planning Database)")
STATUS_SHORT = {"Operational": "operational", "Under Construction": "under construction",
                "Awaiting Construction": "awaiting construction"}
FUELS = ["wind_onshore", "wind_offshore", "solar", "storage", "gas", "nuclear", "bioenergy", "waste", "hydro",
         "pumped", "oil", "coal", "marine", "other"]


def _repd_cell(rs):
    """What the page shows of the REPD row(s) matched to an OSM site: [ref(s), name, status, MW, operational date,
    count]. Operational rows win (their MW summed); otherwise the nearest row not yet built."""
    op = [r for r in rs if r[6] == "Operational"]
    use = op or rs[:1]
    mw = sum(r[5] or 0 for r in use) or None
    return [",".join(r[0] for r in use)[:40], use[0][1], STATUS_SHORT.get(use[0][6], use[0][6]),
            round(mw, 1) if mw else None, use[0][9], len(use)]


def sites_bytes():
    """JSON for /api/wall-sites, or None while there is no OSM copy yet. Cached per download.
    Columns: lat, lon, fuel (index into fuels), mw, name, operator, start, src ("osm" | "repd"),
    mw_src ("osm" | "repd" | ""), repd ([ref, name, status, MW, operational date, count] or null),
    conn ([operator, register name, GSP, BSP, kV, MW] from the DNO registers, or null)."""
    d = atlas()
    if not d:
        return None
    r = _st["repd"]
    try:
        import dno_data as _dno               # where each site connects (DNO registers; optional)
        dd = _dno.data()
    except Exception:
        _dno, dd = None, None
    key = (d.get("ts"), r.get("ts") if r else None, (dd or {}).get("ts"))
    if _st.get("sb_key") == key and _st.get("sb"):
        return _st["sb"]
    per, only = _repd_match(d["sites"], r["recs"]) if r and r.get("recs") else ({}, [])
    look = {sid: (s[0], s[1], s[3], s[4], s[2]) for sid, s in d["sites"].items()}
    look.update({"repd:" + x[0]: (x[7], x[8], x[4], x[5], x[1]) for x in only})
    try:
        conn = _dno.match_sites(look) if dd else {}
    except Exception:
        conn = {}
    fi = {f: i for i, f in enumerate(FUELS)}
    rows = []
    for sid, s in d["sites"].items():
        lat, lon, name, fuel, mw, op, start = s
        rc = _repd_cell(per[sid]) if sid in per else None
        mws = "osm" if mw else ""
        if not mw and rc and rc[2] == "operational" and rc[3]:
            mw, mws = rc[3], "repd"
        rows.append([lat, lon, fi.get(fuel, len(FUELS) - 1), mw, name if name != "Unnamed site" else "", op, start,
                     "osm", mws, rc, conn.get(sid)])
    for x in only:
        rows.append([x[7], x[8], fi.get(x[4], len(FUELS) - 1), x[5], x[1], x[2], x[9], "repd", "repd" if x[5] else "",
                     [x[0], x[1], "operational", x[5], x[9], 1, x[3]], conn.get("repd:" + x[0])])
    rows.sort(key=lambda q: -(q[3] or 0))
    b = json.dumps({"ts": d.get("ts"), "generated": d.get("generated"), "credit": CREDIT, "source": ATLAS_URL,
                    "repd": {"credit": REPD_CREDIT, "title": r.get("title"), "page": REPD_PAGE,
                             "matched": sum(len(v) for v in per.values()), "only": len(only)} if r else None,
                    "fuels": FUELS, "cols": ["lat", "lon", "fuel", "mw", "name", "operator", "start", "src", "mw_src", "repd",
                                             "conn"],
                    "sites": rows}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    _st["sb_key"], _st["sb"] = key, b
    return b


def unit_locations(locs):
    """Terravolt's {BMU_ID: {name, type, lat, lng}} with OpenStreetMap positions laid over it
    (see the module notes). Returns the same dict object for the same inputs, so callers can
    cache on its id."""
    locs = locs or {}
    d = atlas()
    if not d:
        return locs
    key = (id(locs), len(locs), d.get("ts"))
    if _st["ov_key"] == key:
        return _st["ov"]
    out, kept = dict(locs), []
    for code, sid in d["bmu"].items():
        s = d["sites"].get(sid)
        if not s:
            continue
        lat, lon = s[0], s[1]
        o = locs.get(code) or {}
        try:
            tl, tg = float(o.get("lat")), float(o.get("lng"))
        except (TypeError, ValueError):
            tl = tg = None
        if tl is None:
            out[code] = dict(o, name=o.get("name") or s[2] or code, lat=lat, lng=lon, pos="osm", moved=None, osm=sid)
            continue
        km = _km(tl, tg, lat, lon)
        if km > MOVE_KM and not _plausible(o, s, km):
            kept.append({"unit": code, "name": o.get("name"), "osm": s[2], "km": round(km)})
        elif km > MOVE_KM:
            out[code] = dict(o, lat=lat, lng=lon, pos="osm", moved=round(km, 1), osm=sid)
    _st["ov_key"], _st["ov"], _st["kept"] = key, out, kept
    return out
