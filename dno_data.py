"""Local electricity network (DNO) data for the transmission wall (/wall): the operators' Embedded
Capacity Registers, matched to the transmission network.

Build 261029.4  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Standard library only. Optional companion to wall.py; the wall runs unchanged without it.

Embedded Capacity Registers
  Every distribution network operator publishes a register of the generation and storage connected
  to its network (1 MW and over here): site, technology, capacity, connection voltage, position, and
  the grid supply point (GSP), bulk supply point and primary substation it sits under. Six
  operators, fetched weekly into dno_ecr.json (git-ignored); a source that fails keeps its last
  rows and is retried with backoff. Fetching runs in its own thread and never blocks a request.
    UK Power Networks, SP Energy Networks, Northern Powergrid, Electricity North West: their
      Opendatasoft portals, which need the account API key already kept for the power-cuts page
      (powercut_keys.json: "UKPN", "SPEN", "NPg", "ENWL"). Without a key the source is skipped.
    National Grid Electricity Distribution, SSEN Distribution: CKAN portals, no key; the newest
      CSV resource of the register is used.
  Column names differ by operator, so columns are found by their key words (see _COLS).
  Rows that are connected and of at least MIN_MW are kept, with OSGB grid references converted to
  WGS84 (osm_sites.osgb_to_wgs84).

What uses it (through wall.py)
  gsp_map(nodes)        each register's GSP name -> the transmission network node it is, by name
                        (tidied: "Walpole 132Kv S Stn" -> "walpole") checked against the position
                        of the sites under it
  node_summary(nodes)   per network node: embedded generation under its GSP(s) by technology, the
                        bulk supply points, and the largest sites
  match_sites(sites)    OpenStreetMap / REPD sites -> their register entry (where they connect)
  lines_for(kv)         the operators' own 132 / 66 / 33 kV line maps (see "network lines" below)
  live_by_node(nodes)   Northern Powergrid's live 132 kV circuit readings, by network node
"""
import csv
import io
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ECR_FILE = HERE / "dno_ecr.json"
KEYS_FILE = HERE / "powercut_keys.json"
ECR_TTL = 7 * 86400
RETRY_S = [900, 3600, 4 * 3600, 12 * 3600]
MIN_MW = 1.0
UA = {"User-Agent": "uk-grid-monitor/1.0 (personal dashboard; DNO registers)"}

# key, operator, portal kind, host / base, dataset(s) or package, resource name hint, licence
SOURCES = [
    ("UKPN", "UK Power Networks", "ods", "ukpowernetworks", ["ukpn-embedded-capacity-register"], "", "CC BY 4.0"),
    ("SPEN", "SP Energy Networks", "ods", "spenergynetworks", ["embedded-capacity-register"], "", "CC BY 4.0"),
    ("NPg", "Northern Powergrid", "ods", "northernpowergrid", ["embedded-capacity-register", "embedded-capacity-register-part-2"], "",
     "Northern Powergrid Open Data Licence v1.0"),
    ("ENWL", "Electricity North West", "ods", "electricitynorthwest", ["enwl-embedded-capacity-register-2-1mw-and-above"], "", "CC BY 4.0"),
    ("NGED", "National Grid Electricity Distribution", "ckan", "https://connecteddata.nationalgrid.co.uk",
     "embedded-capacity-register", "", "NGED open data terms"),
    ("SSEN", "SSEN Distribution", "ckan", "https://data-api.ssen.co.uk", "embedded_capacity_register", "1MW", "CC BY 4.0"),
]
CREDIT = ("Embedded Capacity Registers: UK Power Networks, SP Energy Networks, Electricity North West, SSEN (CC BY 4.0); "
          "Northern Powergrid (Open Data Licence); National Grid Electricity Distribution")

_lock = threading.Lock()
_st = {"data": None, "loaded": False, "busy": False, "next": {}, "fails": {}}


def _get(url, headers=None, timeout=180):
    h = dict(UA)
    h.update(headers or {})
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return r.read()


def _keys():
    try:
        return json.loads(KEYS_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _k(c):
    return re.sub(r"[^a-z0-9]", "", (c or "").lower())


# column -> key words it is found by (first match wins), on the column name with only a-z0-9 kept
_COLS = {
    "site": ["customersite", "customername"], "east": ["eastings"], "north": ["northings"],
    "gsp": ["gridsupplypoint"], "bsp": ["bulksupplypoint"], "primary": ["primary"],
    "kv": ["pocvoltage", "pointofconnectionpocvoltage", "pointofconnection"],
    "src": ["energysource1"], "tech": ["energyconversiontechnology1"],
    "mw": ["alreadyconnectedregisteredcapacity"], "mw1": ["technology1registeredcapacity", "tech1regcapacity", "technology1regcapacity"],
    "status": ["connectionstatus"], "date": ["dateconnected"], "lat": ["latitude"], "lon": ["longitude"],
}


def _colmap(names):
    out, ks = {}, [(n, _k(n)) for n in names]
    for col, words in _COLS.items():
        for w in words:
            hit = next((n for n, k in ks if w in k and not (col == "primary" and k != "primary")), None)
            if hit:
                out[col] = hit
                break
    return out


def _num(v):
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _fuel(src, tech):
    s = f"{src} {tech}".lower()
    if "offshore wind" in s:
        return "wind_offshore"
    for k, f in (("photovoltaic", "solar"), ("solar", "solar"), ("wind", "wind_onshore"), ("stor", "storage"),
                 ("batter", "storage"), ("landfill", "bioenergy"), ("biofuel", "bioenergy"), ("biomass", "bioenergy"),
                 ("sewage", "bioenergy"), ("anaerobic", "bioenergy"), ("waste", "waste"), ("advanced fuel", "waste"),
                 ("hydro", "hydro"), ("water", "hydro"), ("tidal", "marine"), ("marine", "marine"), ("wave", "marine"),
                 ("fossil - gas", "gas"), ("natural gas", "gas"), ("ocgt", "gas"), ("ccgt", "gas"),
                 ("oil", "oil"), ("diesel", "oil"), ("geothermal", "other")):
        if k in s:
            return f
    return "other"


def _clean(v):
    v = (v or "").strip()
    return "" if v.lower().startswith(("data not", "not applicable", "n/a")) else v


def _rows(text, key, delim=","):
    from osm_sites import osgb_to_wgs84
    rd = csv.DictReader(io.StringIO(text), delimiter=delim)
    cm = _colmap(rd.fieldnames or [])
    need = {"site", "gsp", "status"}
    if not need <= set(cm):
        raise ValueError(f"columns not found: {sorted(need - set(cm))}")
    out = []
    for r in rd:
        g = lambda c: _clean(r.get(cm[c])) if c in cm else ""
        if not g("status").lower().startswith("connected"):
            continue
        mw = _num(g("mw"))
        if not mw:
            mw = _num(g("mw1"))
        if not mw or mw < MIN_MW:
            continue
        lat = lon = None
        E, N = _num(g("east")), _num(g("north"))
        if E and N and 0 < E < 700000 and 0 < N < 1300000:
            lat, lon = osgb_to_wgs84(E, N)
        elif _num(g("lat")) and _num(g("lon")):
            lat, lon = _num(g("lat")), _num(g("lon"))
        if lat is None:
            continue
        kv = _num(g("kv"))
        out.append([key, g("site")[:60], _fuel(g("src"), g("tech")), round(mw, 2), kv, g("gsp")[:40], g("bsp")[:40],
                    g("primary")[:40], round(lat, 5), round(lon, 5), g("date")[:10], g("tech")[:40] or g("src")[:40]])
    return out


def _fetch(src, keys):
    key, name, kind, host, ds, hint, lic = src
    if kind == "ods":
        k = keys.get(key)
        if not k or len(str(k)) < 8:
            raise PermissionError("no API key (power-cuts page)")
        rows = []
        for d in ds:
            url = f"https://{host}.opendatasoft.com/api/explore/v2.1/catalog/datasets/{d}/exports/csv?delimiter=%3B"
            t = _get(url, {"Authorization": "Apikey " + str(k)}).decode("utf-8-sig", "replace")
            if t.count("\n") <= 1:
                raise PermissionError(f"{d}: no rows returned (key not accepted for this dataset?)")
            rows += _rows(t, key, ";")
        return rows, None
    pkg = json.loads(_get(f"{host}/api/3/action/package_show?id={ds}", timeout=60))["result"]
    res = [r for r in pkg.get("resources") or [] if (r.get("format") or "").upper() == "CSV" and hint in (r.get("name") or "")]
    if not res:
        raise ValueError("no CSV resource")
    res.sort(key=lambda r: r.get("last_modified") or r.get("created") or "", reverse=True)
    raw = _get(res[0]["url"])
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return _rows(raw.decode(enc), key), res[0].get("name")
        except UnicodeDecodeError:
            continue
    raise ValueError("could not decode")


def _worker(due):
    keys = _keys()
    try:
        for src in SOURCES:
            k = src[0]
            if k not in due:
                continue
            try:
                rows, title = _fetch(src, keys)
                with _lock:
                    d = _st["data"] or {"rows": {}, "status": {}}
                    d["rows"][k] = rows
                    d["status"][k] = {"ok": True, "at": time.time(), "n": len(rows), "title": title,
                                      "operator": src[1], "licence": src[6]}
                    d["ts"] = time.time()
                    _st["data"] = d
                    _st["fails"][k] = 0
                    _st["next"][k] = time.time() + ECR_TTL
            except Exception as e:
                with _lock:
                    d = _st["data"] or {"rows": {}, "status": {}}
                    prev = d["status"].get(k) or {}
                    d["status"][k] = dict(prev, ok=False, at_err=time.time(), error=f"{type(e).__name__}: {str(e)[:140]}",
                                          operator=src[1], licence=src[6])
                    _st["data"] = d
                    f = _st["fails"].get(k, 0)
                    _st["fails"][k] = f + 1
                    _st["next"][k] = time.time() + RETRY_S[min(f, len(RETRY_S) - 1)]
        with _lock:
            d = _st["data"]
        if d:
            try:
                tmp = ECR_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(d, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
                tmp.replace(ECR_FILE)
            except Exception:
                pass
    finally:
        with _lock:
            _st["busy"] = False


def data():
    """{"rows": {operator key: [rows]}, "status": {...}, "ts"} or None; kicks a refresh when due."""
    with _lock:
        if not _st["loaded"]:
            _st["loaded"] = True
            try:
                _st["data"] = json.loads(ECR_FILE.read_text(encoding="utf-8"))
                for k, s in (_st["data"].get("status") or {}).items():
                    if s.get("ok"):
                        _st["next"][k] = s.get("at", 0) + ECR_TTL
            except Exception:
                _st["data"] = None
        now = time.time()
        due = [s[0] for s in SOURCES if now >= _st["next"].get(s[0], 0)]
        if due and not _st["busy"]:
            _st["busy"] = True
            for k in due:
                _st["next"][k] = now + 600            # not again until this attempt is done
            threading.Thread(target=_worker, args=(due,), name="dno-ecr", daemon=True).start()
        return _st["data"]


def all_rows():
    d = data()
    return [r for rs in (d or {}).get("rows", {}).values() for r in rs]


def status():
    d = data()
    return (d or {}).get("status", {})


# ---------------------------------------------------------------- matching to the network
_GSP_SKIP = {"kv", "s", "stn", "sgp", "gsp", "grid", "substation", "sub", "station", "main", "bsp", "supply", "point",
             "d", "c", "a", "b", "e", "f", "g", "h", "j", "k", "data", "not", "available", "the"}


def gsp_key(name):
    s = re.sub(r"\(.*?\)", " ", (name or "").lower())
    s = re.sub(r"\d+\s*kv|\d+", " ", s)
    w = [x for x in re.findall(r"[a-z]+", s) if x not in _GSP_SKIP]
    return " ".join(w)


def _km(lat1, lon1, lat2, lon2):
    return math.hypot((lon1 - lon2) * math.cos(math.radians((lat1 + lat2) / 2)) * 111.32, (lat1 - lat2) * 110.57)


_cache = {}


def _same(a, b):
    """Tidied names that are the same place: equal, one the other plus more words, or differing only by a
    letter or two at the end ("bustleholm" / "bustleholme")."""
    if a == b or a.startswith(b + " ") or b.startswith(a + " "):
        return True
    s, t = sorted((a, b), key=len)
    return len(s) >= 5 and t.startswith(s) and len(t) - len(s) <= 2


def gsp_map(nodes):
    """{(operator, gsp key): node index} for nodes = [(index, name, lon, lat)]. A GSP matches a node whose tidied
    name is the same place as the GSP's (see _same); the nearest such node to the GSP's sites wins, within 40 km
    (80 km for an exact name: island sites can be far from their GSP). A GSP with no such node takes an unnamed node within 6 km of its sites."""
    d = data()
    key = ("gsp", (d or {}).get("ts"), len(nodes))
    if _cache.get("gsp_key") == key:
        return _cache["gsp"]
    by = {}
    for r in all_rows():
        k = (r[0], gsp_key(r[5]))
        if k[1]:
            a = by.setdefault(k, [0.0, 0.0, 0])
            a[0] += r[8]
            a[1] += r[9]
            a[2] += 1
    nk = [(i, gsp_key(nm), lon, lat) for i, nm, lon, lat in nodes if nm]
    out = {}
    for k, (sla, slo, n) in by.items():
        lat, lon = sla / n, slo / n
        cands = [(_km(lat, lon, y, x), i) for i, g, x, y in nk if g and _same(g, k[1])]
        exact = [(_km(lat, lon, y, x), i) for i, g, x, y in nk if g == k[1]]
        cands = [c for c in cands if c[0] <= 40] or [c for c in exact if c[0] <= 80]   # island sites: far from their GSP
        if not cands:                       # no name match: an unnamed node close to the sites under it
            cands = [(_km(lat, lon, y, x), i) for i, nm, x, y in nodes if not nm]
            cands = [c for c in cands if c[0] <= 6]
        if cands:
            out[k] = min(cands)[1]
    _cache["gsp_key"], _cache["gsp"] = key, out
    return out


def node_summary(nodes):
    """{node index: {"mw", "n", "by": {fuel: [MW, count]}, "bsp": [[name, MW]...], "top": [[site, fuel, MW, kV, operator]...],
    "ops": [operator keys]}} for the embedded generation under each node's GSP(s)."""
    gm = gsp_map(nodes)
    out = {}
    for r in all_rows():
        i = gm.get((r[0], gsp_key(r[5])))
        if i is None:
            continue
        s = out.setdefault(i, {"mw": 0.0, "n": 0, "by": {}, "bspd": {}, "top": [], "ops": set(), "gsp": set()})
        s["mw"] += r[3]
        s["n"] += 1
        b = s["by"].setdefault(r[2], [0.0, 0])
        b[0] += r[3]
        b[1] += 1
        if r[6]:
            s["bspd"][r[6]] = s["bspd"].get(r[6], 0.0) + r[3]
        s["top"].append([r[1], r[2], r[3], r[4], r[0]])
        s["ops"].add(r[0])
        s["gsp"].add(r[5])
    for s in out.values():
        s["mw"] = round(s["mw"], 1)
        s["by"] = {k: [round(v[0], 1), v[1]] for k, v in s["by"].items()}
        s["bsp"] = sorted(([k, round(v, 1)] for k, v in s.pop("bspd").items()), key=lambda x: -x[1])[:12]
        s["top"] = sorted(s["top"], key=lambda x: -x[2])[:8]
        s["ops"] = sorted(s["ops"])
        s["gsp"] = sorted(s["gsp"])[:4]
    return out


def match_sites(sites):
    """{site id: [operator, site name, GSP, BSP, kV, MW]} for sites = {id: (lat, lon, fuel, mw, name)}: the register
    entry for each site, by fuel and distance (a radius growing with size), when the names share a word or the
    capacities agree within 15 %, or within 0.5 km."""
    rows = all_rows()
    if not rows:
        return {}
    try:
        from osm_sites import _named, REPD_OK
    except Exception:
        return {}
    grid = {}
    for j, r in enumerate(rows):
        grid.setdefault((int(r[8] * 10), int(r[9] * 6)), []).append(j)
    out = {}
    for sid, (lat, lon, fuel, mw, name) in sites.items():
        ok = REPD_OK.get(fuel, {fuel}) | {fuel}
        R = max(1.5, min(10.0, math.sqrt(mw or 1) * 0.6))
        n = int(R / 11) + 1
        gy, gx, best = int(lat * 10), int(lon * 6), None
        for dy in range(-n, n + 1):
            for dx in range(-n, n + 1):
                for j in grid.get((gy + dy, gx + dx), ()):
                    r = rows[j]
                    if r[2] not in ok:
                        continue
                    dk = _km(lat, lon, r[8], r[9])
                    if dk > R:
                        continue
                    nm = _named(name, r[1]) if name else False
                    cap = mw and mw >= 2 and abs(mw - r[3]) <= 0.15 * max(mw, r[3])
                    if not (nm or cap or dk <= 0.5):
                        continue
                    k = (0 if nm else 1 if cap else 2, dk)
                    if best is None or k < best[0]:
                        best = (k, j)
        if best:
            r = rows[best[1]]
            out[sid] = [r[0], r[1], r[5], r[6], r[4], r[3]]
    return out


# ================================================================ network lines (132 / 66 / 33 kV)
# The operators' own line maps, for the wall's local-network layers. Display only: not in the flow model.
#   UK Power Networks: 132 and 33 kV overhead lines and underground cables, 66 kV overhead (Opendatasoft, key)
#   Northern Powergrid: EHV (33-132 kV) overhead and underground feeders (Opendatasoft, key)
#   Electricity North West: 132 and 33 kV overhead conductors (Opendatasoft, key)
#   SSEN: EHV (33 kV) and EHV+ (132 kV) overhead routes, shapefiles (CKAN, no key)
#   Any shapefile .zip placed in the dno_lines folder (for operators whose maps are a manual download, such as
#   NGED's from dataportal2.nationalgrid.co.uk): voltage from a "volt" field or the file name, underground if
#   the name says "ug" / "underground" / "cable".
# National Grid Electricity Distribution's and SP Energy Networks' maps aren't available through an API.
# Underground cables from UK Power Networks and Northern Powergrid are under the operators' shared-data
# licences, not open ones: they are fetched with your key and kept only in dno_lines.json (git-ignored).
# Lines are simplified (Douglas-Peucker, LINE_TOL_M) and kept as [operator, kV, underground, name, [lon, lat, ...]].
LINES_FILE = HERE / "dno_lines.json"
LINES_DIR = HERE / "dno_lines"
LINES_TTL = 30 * 86400
LINE_TOL_M = 20.0
LINE_SOURCES = [
    ("UKPN", "ods", "ukpowernetworks", [("ukpn-132kv-overhead-lines", 132, 0), ("ukpn-licence-area-132kv-underground-cables", 132, 1),
                                         ("ukpn-66kv-overhead-lines-shapefile", 66, 0), ("ukpn-33kv-overhead-lines", 33, 0),
                                         ("ukpn-licence-area-33kv-underground-cables", 33, 1)]),
    ("NPg", "ods", "northernpowergrid", [("npg-ehv-overhead-feeders", None, 0), ("npg-ehv-underground-feeders", None, 1)]),
    ("ENWL", "ods", "electricitynorthwest", [("enwl-132kv-overhead-conductors", 132, 0), ("enwl-33kv-overhead-conductors", 33, 0)]),
    ("SSEN", "ckanzip", "https://data-api.ssen.co.uk", "ssen_overhead_lines"),
]
LINE_KV = (132, 66, 33)
_lst = {"data": None, "loaded": False, "busy": False, "next": {}, "fails": {}}


def _kv(v):
    """'132kV', '33.000 kV', 33, 33000 -> 132 / 66 / 33 (nearest of LINE_KV), or None."""
    m = re.search(r"\d+(\.\d+)?", str(v or ""))
    if not m:
        return None
    x = float(m.group(0))
    if x > 1000:
        x /= 1000
    if x < 20:
        return None                                       # 11 kV and below: not drawn
    return min(LINE_KV, key=lambda k: abs(k - x))


def _simplify(pts, tol_m=LINE_TOL_M):
    """Douglas-Peucker on [(lon, lat)] with a tolerance in metres (local flat approximation)."""
    if len(pts) <= 2:
        return pts
    lat0 = pts[0][1]
    kx, ky = 111320 * math.cos(math.radians(lat0)), 110570
    xy = [(p[0] * kx, p[1] * ky) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = xy[a], xy[b]
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy or 1e-9
        best, bi = -1.0, None
        for i in range(a + 1, b):
            px, py = xy[i]
            t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
            d = math.hypot(px - ax - t * dx, py - ay - t * dy)
            if d > best:
                best, bi = d, i
        if bi is not None and best > tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


def _pack(op, kv, ug, name, pts):
    pts = _simplify(pts)
    if len(pts) < 2:
        return None
    flat = []
    for lon, lat in pts:
        flat += [round(lon, 4), round(lat, 4)]
    return [op, kv, ug, (name or "")[:40], flat]


def _geo_lines(g):
    """GeoJSON geometry -> list of [(lon, lat)] lines."""
    if not g:
        return []
    t, c = g.get("type"), g.get("coordinates") or []
    if t == "LineString":
        return [[(p[0], p[1]) for p in c]]
    if t == "MultiLineString":
        return [[(p[0], p[1]) for p in ln] for ln in c]
    return []


def _ods_lines(op, host, datasets, key):
    out = []
    for ds, kv0, ug0 in datasets:
        url = f"https://{host}.opendatasoft.com/api/explore/v2.1/catalog/datasets/{ds}/exports/geojson"
        fc = json.loads(_get(url, {"Authorization": "Apikey " + str(key)}, timeout=600))
        feats = fc.get("features") or []
        for f in feats:
            p = f.get("properties") or {}
            kv = kv0 or _kv(p.get("voltage"))
            if not kv:
                continue
            st = str(p.get("status") or "").lower()
            if st and any(w in st for w in ("proposed", "abandon", "removed", "decommission")):
                continue
            sit = str(p.get("line_situation") or "").lower()
            ug = 1 if (ug0 or "under" in sit or "cable" in sit) else 0
            name = p.get("circuit_name") or p.get("source_site_name") or ""
            for ln in _geo_lines(f.get("geometry")):
                x = _pack(op, kv, ug, name, ln)
                if x:
                    out.append(x)
    return out


def _shp_lines(zbytes, op, default_name=""):
    """Polylines from a zipped shapefile (OSGB27700 or WGS84 by its .prj) -> packed lines."""
    import struct
    import zipfile
    from osm_sites import osgb_to_wgs84
    z = zipfile.ZipFile(io.BytesIO(zbytes))
    out = []
    for shp in [n for n in z.namelist() if n.lower().endswith(".shp")]:
        stem = shp[:-4]
        names = {n.lower(): n for n in z.namelist()}
        b = z.read(shp)
        dbf = z.read(names[(stem + ".dbf").lower()]) if (stem + ".dbf").lower() in names else None
        prj = z.read(names[(stem + ".prj").lower()]).decode("latin-1") if (stem + ".prj").lower() in names else ""
        osgb = "osgb" in prj.lower() or "british" in prj.lower() or "27700" in prj or not prj
        recs, fl, hl, rl = [], [], 0, 0
        if dbf:
            hl, rl = int.from_bytes(dbf[8:10], "little"), int.from_bytes(dbf[10:12], "little")
            o = 32
            while o < hl and dbf[o] != 0x0D:
                fl.append((dbf[o:o + 11].split(b"\0")[0].decode("latin-1").lower(), dbf[o + 16]))
                o += 32
        low = shp.lower()
        ug_name = 1 if re.search(r"(^|[^a-z])(ug|underground|cable)", low) else 0
        kv_name = _kv(re.search(r"(132|66|33)\s*kv", low).group(1)) if re.search(r"(132|66|33)\s*kv", low) else None
        i, pos, n = 0, 100, len(b)
        while pos + 8 <= n:
            clen = struct.unpack(">i", b[pos + 4:pos + 8])[0] * 2
            body = b[pos + 8:pos + 8 + clen]
            pos += 8 + clen
            attrs = {}
            if dbf:
                s = hl + i * rl + 1
                for nm, ln in fl:
                    attrs[nm] = dbf[s:s + ln].decode("latin-1").strip()
                    s += ln
            i += 1
            if len(body) < 44 or struct.unpack("<i", body[:4])[0] not in (3, 13, 23):
                continue
            nparts, npts = struct.unpack("<ii", body[36:44])
            parts = list(struct.unpack(f"<{nparts}i", body[44:44 + 4 * nparts])) + [npts]
            xy = struct.unpack(f"<{2 * npts}d", body[44 + 4 * nparts:44 + 4 * nparts + 16 * npts])
            vf = next((v for k, v in attrs.items() if "volt" in k or k.startswith("nominal_vo")), None)
            kv = _kv(vf) or kv_name
            if not kv:
                continue
            st = (attrs.get("status") or "").lower()
            if st and not any(w in st for w in ("exist", "service", "live", "commission")):
                continue
            mount = (attrs.get("mounting") or "").lower()
            ug = 1 if ("under" in mount or ug_name) else 0
            name = attrs.get("name") or attrs.get("circuit_id") or default_name
            for a, c in zip(parts[:-1], parts[1:]):
                pts = []
                for j in range(a, c):
                    x, y = xy[2 * j], xy[2 * j + 1]
                    if osgb:
                        la, lo = osgb_to_wgs84(x, y)
                        pts.append((lo, la))
                    else:
                        pts.append((x, y))
                p = _pack(op, kv, ug, name, pts)
                if p:
                    out.append(p)
    return out


def _ckan_zip_lines(op, base, pkg):
    p = json.loads(_get(f"{base}/api/3/action/package_show?id={pkg}", timeout=60))["result"]
    out = []
    for r in p.get("resources") or []:
        if (r.get("format") or "").upper() == "ZIP" and "EHV" in (r.get("name") or "").upper():
            out += _shp_lines(_get(r["url"], timeout=600), op)
    if not out:
        raise ValueError("no EHV shapefile found")
    return out


def _lines_worker(due):
    keys = _keys()
    try:
        for src in LINE_SOURCES + [("FILES", "dir", None, None)]:
            k = src[0]
            if k not in due:
                continue
            try:
                if src[1] == "ods":
                    key = keys.get(k)
                    if not key or len(str(key)) < 8:
                        raise PermissionError("no API key (power-cuts page)")
                    rows = _ods_lines(k, src[2], src[3], key)
                elif src[1] == "ckanzip":
                    rows = _ckan_zip_lines(k, src[2], src[3])
                else:
                    rows = []
                    for f in sorted(LINES_DIR.glob("*.zip")) if LINES_DIR.is_dir() else []:
                        op = f.stem.split("_")[0].upper()[:8] or "FILE"
                        rows += _shp_lines(f.read_bytes(), op)
                with _lock:
                    d = _lst["data"] or {"lines": {}, "status": {}}
                    d["lines"][k] = rows
                    d["status"][k] = {"ok": True, "at": time.time(), "n": len(rows)}
                    d["ts"] = time.time()
                    _lst["data"] = d
                    _lst["fails"][k] = 0
                    _lst["next"][k] = time.time() + (LINES_TTL if k != "FILES" else 3600)
            except Exception as e:
                with _lock:
                    d = _lst["data"] or {"lines": {}, "status": {}}
                    d["status"][k] = dict(d["status"].get(k) or {}, ok=False, at_err=time.time(),
                                          error=f"{type(e).__name__}: {str(e)[:140]}")
                    _lst["data"] = d
                    f = _lst["fails"].get(k, 0)
                    _lst["fails"][k] = f + 1
                    _lst["next"][k] = time.time() + RETRY_S[min(f, len(RETRY_S) - 1)]
        with _lock:
            d = _lst["data"]
        if d:
            try:
                tmp = LINES_FILE.with_suffix(".tmp")
                tmp.write_text(json.dumps(d, separators=(",", ":"), ensure_ascii=False), encoding="utf-8")
                tmp.replace(LINES_FILE)
            except Exception:
                pass
    finally:
        with _lock:
            _lst["busy"] = False


def lines_data():
    """{"lines": {source: [rows]}, "status", "ts"} or None; kicks a refresh when due (monthly; the dno_lines
    folder is looked at hourly)."""
    with _lock:
        if not _lst["loaded"]:
            _lst["loaded"] = True
            try:
                _lst["data"] = json.loads(LINES_FILE.read_text(encoding="utf-8"))
                for k, s in (_lst["data"].get("status") or {}).items():
                    if s.get("ok"):
                        _lst["next"][k] = s.get("at", 0) + (LINES_TTL if k != "FILES" else 3600)
            except Exception:
                _lst["data"] = None
        now = time.time()
        due = [s[0] for s in LINE_SOURCES + [("FILES",)] if now >= _lst["next"].get(s[0], 0)]
        if due and not _lst["busy"]:
            _lst["busy"] = True
            for k in due:
                _lst["next"][k] = now + 1800
            threading.Thread(target=_lines_worker, args=(due,), name="dno-lines", daemon=True).start()
        return _lst["data"]


def lines_for(kv):
    """All packed lines of one voltage, every source."""
    d = lines_data()
    return [r for rs in (d or {}).get("lines", {}).values() for r in rs if r[1] == kv]


# ================================================================ Northern Powergrid live circuit readings
# Northern Powergrid publishes live readings for its 132 kV circuits (npg-live-132kv-circuit: site, feeder,
# measurement name, time, value, unit). The latest reading of each is fetched at most every LIVE_TTL while the
# wall is in use, with the power-cuts page's NPg key. These are measurements, not model estimates.
# The feed runs about 2 hours behind, so a window counted back from now ("the last 90 minutes") found nothing.
# Instead the newest reading's time is asked for first, and the readings within LIVE_SPAN_MIN of it are exported,
# read with a hard time limit (LIVE_DEADLINE) so a slow export can't hold the worker.
LIVE_DS = [("npg-live-132kv-circuit", 132)]
LIVE_TTL = 300
LIVE_SPAN_MIN = 40              # readings within this of the newest (half-hourly, so the latest one of each)
LIVE_DEADLINE = 120             # seconds for the export
NPG_ODS = "https://northernpowergrid.opendatasoft.com/api/explore/v2.1/catalog/datasets/"
_live = {"data": None, "ts": 0.0, "busy": False, "error": None, "newest": None}


def _live_worker():
    try:
        key = _keys().get("NPg")
        if not key or len(str(key)) < 8:
            raise PermissionError("no API key (power-cuts page)")
        latest, auth = {}, {"Authorization": "Apikey " + str(key)}
        for ds, kv in LIVE_DS:
            q = urllib.parse.quote("max(timestamp) as t")
            res = json.loads(_get(f"{NPG_ODS}{ds}/records?select={q}", auth, timeout=60)).get("results") or []
            newest = datetime.fromisoformat(str((res[0] if res else {}).get("t") or "").replace("Z", "+00:00"))
            frm = (newest - timedelta(minutes=LIVE_SPAN_MIN)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            where = urllib.parse.quote(f"timestamp >= date'{frm}'")
            t0, buf = time.time(), []
            req = urllib.request.Request(f"{NPG_ODS}{ds}/exports/csv?delimiter=%3B&where={where}", headers=dict(UA, **auth))
            with urllib.request.urlopen(req, timeout=60) as r:
                while True:
                    if time.time() - t0 > LIVE_DEADLINE:
                        raise TimeoutError(f"export not finished in {LIVE_DEADLINE} s")
                    c = r.read(65536)
                    if not c:
                        break
                    buf.append(c)
            t = b"".join(buf).decode("utf-8-sig", "replace")
            _live["newest"] = newest.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UT")
            for r in csv.DictReader(io.StringIO(t), delimiter=";"):
                k = (r.get("site_name"), r.get("feeder_name"), r.get("name"), r.get("uom"))
                ts = r.get("timestamp") or ""
                if k not in latest or ts > latest[k][4]:
                    latest[k] = [r.get("site_name") or "", r.get("feeder_name") or "", r.get("name") or "",
                                 _num(r.get("value")), ts, r.get("uom") or "", (r.get("downstream_sites") or "")[:80], kv]
        with _lock:
            _live.update(data=list(latest.values()), ts=time.time(), error=None)
    except Exception as e:
        with _lock:
            _live.update(ts=time.time(), error=f"{type(e).__name__}: {str(e)[:140]}")
    finally:
        with _lock:
            _live["busy"] = False


def npg_live():
    """[[site, feeder, measurement, value, time, unit, downstream sites, kV]] (latest of each), or []; refreshes in
    the background when older than LIVE_TTL."""
    with _lock:
        if time.time() - _live["ts"] > LIVE_TTL and not _live["busy"]:
            _live["busy"] = True
            threading.Thread(target=_live_worker, name="npg-live", daemon=True).start()
        return _live["data"] or []


def npg_live_status():
    return {"ok": _live["error"] is None and bool(_live["data"]), "n": len(_live["data"] or []), "at": _live["ts"] or None,
            "newest": _live["newest"], "error": _live["error"]}


def live_by_node(nodes):
    """{node index: [rows]} matching each reading's site name to a network node (as for GSPs), within the NPg area."""
    rows = npg_live()
    if not rows:
        return {}
    nk = [(i, gsp_key(nm), lon, lat) for i, nm, lon, lat in nodes if nm and 53.3 <= lat <= 55.9 and -2.7 <= lon <= 0.2]
    out, memo = {}, {}
    for r in rows:
        g = gsp_key(r[0])
        if g not in memo:
            memo[g] = next((i for i, x, lon, lat in nk if x and _same(x, g)), None)
        i = memo[g]
        if i is not None:
            out.setdefault(i, []).append(r)
    return out


# ================================================================ NGED live grid supply point data
# National Grid Electricity Distribution publishes live 5-minute data for each of its grid supply points (no key):
# one CSV per GSP (Timestamp, Net Demand, Generation, Import, Solar, Wind, STOR, Other; MW, UK clock time), about
# 10 minutes behind. Import = net demand - generation: what the GSP takes from the transmission network (negative
# when it exports). Fetched every GSP_TTL in the background with a hard time limit; measurements, not estimates.
NGED_CKAN = "https://connecteddata.nationalgrid.co.uk"
NGED_GSP_PKGS = ("live-gsp-data-south-west", "live-gsp-data-south-wales", "live-gsp-data-east-midlands",
                 "live-gsp-data-west-midlands")
GSP_TTL = 300
GSP_DEADLINE = 150              # seconds for the whole fetch (about 55 small files)
GSP_HDR = ["Timestamp", "Net Demand", "Generation", "Import", "Solar", "Wind", "STOR", "Other"]
_gsp = {"data": None, "ts": 0.0, "busy": False, "error": None, "res": None, "res_ts": 0.0}


def _uk_to_utc(s):
    """'2026-10-29T21:45:00' in UK clock time -> UTC epoch seconds."""
    from datetime import datetime, timedelta, timezone
    t = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
    y = t.year
    def last_sun(m):
        d = datetime(y, m, 31)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    bst = last_sun(3).replace(hour=1) <= t < last_sun(10).replace(hour=2)
    return (t - timedelta(hours=1 if bst else 0)).replace(tzinfo=timezone.utc).timestamp()


def _gsp_worker():
    t0 = time.time()
    try:
        if not _gsp["res"] or time.time() - _gsp["res_ts"] > 86400:          # the list of files, daily
            res = []
            for pk in NGED_GSP_PKGS:
                p = json.loads(_get(f"{NGED_CKAN}/api/3/action/package_show?id={pk}", timeout=30))["result"]
                for r in p.get("resources", []):
                    if (r.get("format") or "").upper() == "CSV" and r.get("url"):
                        res.append((r["name"].rsplit("_", 1)[0].strip(), pk.replace("live-gsp-data-", ""), r["url"]))
            _gsp.update(res=res, res_ts=time.time())
        out, bad = {}, []
        for name, region, url in _gsp["res"]:
            if time.time() - t0 > GSP_DEADLINE:
                bad.append(name + " (time limit)")
                continue
            try:
                rows = list(csv.reader(io.StringIO(_get(url, timeout=20).decode("utf-8-sig", "replace"))))
                if rows[0][:8] != GSP_HDR or len(rows) < 2:
                    raise ValueError("unexpected columns")
                last = rows[-1]
                out[name] = {"region": region, "t": _uk_to_utc(last[0]),
                             **{k: _num(v) for k, v in zip(("nd", "gen", "imp", "solar", "wind", "stor", "other"), last[1:8])}}
            except Exception as e:
                bad.append(f"{name} ({type(e).__name__})")
        with _lock:
            _gsp.update(data=out, ts=time.time(), error="; ".join(bad)[:300] if bad else None)
    except Exception as e:
        with _lock:
            _gsp.update(ts=time.time(), error=f"{type(e).__name__}: {str(e)[:140]}")
    finally:
        with _lock:
            _gsp["busy"] = False


def nged_gsp():
    """{GSP name: {"region", "t" (UTC epoch), "nd", "gen", "imp", "solar", "wind", "stor", "other"}} (latest of each)
    or {}; refreshes in the background when older than GSP_TTL."""
    with _lock:
        if time.time() - _gsp["ts"] > GSP_TTL and not _gsp["busy"]:
            _gsp["busy"] = True
            threading.Thread(target=_gsp_worker, name="nged-gsp", daemon=True).start()
        return _gsp["data"] or {}


def nged_gsp_status():
    return {"ok": bool(_gsp["data"]), "n": len(_gsp["data"] or {}), "at": _gsp["ts"] or None, "error": _gsp["error"]}
