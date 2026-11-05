"""GB transmission wall (/wall): live node injections for the network mimic.

Build 261030.4  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Standard library only. Optional companion to grid_server.py; the server runs
unchanged without it. grid_server calls attach(globals()) once at start-up so this
module can reuse its fetch helper, BMU location cache and snapshot.

What it serves
  /api/wall-network   the static network (grid_network.json, built by build_network.py), with
                      names for nodes OpenStreetMap left unnamed, found by a background task
                      (see "node names" below) and cached in node_names.json
  /api/wall           every BM unit's output now, 15 min ago and scheduled +30/+60 min,
                      grouped by network node; interconnector flows (+ verified
                      schedules); NESO day-ahead boundary rows; Elexon derated margin/LoLP
  /api/wall-sites     generator sites from OpenStreetMap for the map's Generators layer
                      (osm_sites.py; 503 until its first download)
  /api/wall-local     the local (DNO) networks under each node: embedded generation from the
                      operators' Embedded Capacity Registers (dno_data.py, optional)
  /api/wall-dnolines?kv=132|66|33   the operators' own local-network line maps (dno_data.py); where they
                      cover an area, /api/wall-network leaves out OpenStreetMap's 132 kV lines there
  /api/wall-history   one node's units over the last 2 h plus the scheduled path ahead
  ...and in /api/wall: "outages", the REMIT unavailability reports (Elexon) for units and
                      interconnectors that are reduced now or will be within 24 h. These are
                      operators' own reports, not estimates.

Unit positions: Terravolt's unit list, corrected and filled from OpenStreetMap where UK Grid
Atlas maps the unit to an OSM site (osm_sites.py, optional; see its notes). Each unit row says
which position was used.

Offshore wind connections: most offshore farms' export cables aren't in the network file, so
OFFSHORE_LINKS names each farm's connection point (the onshore substation, or the offshore
platform where the network has the farm's mapped export cable), with its source. The page draws a
straight line from the farm to it, and the farm's BM units are counted at that node instead of the
nearest one (Dogger Bank A/B and Sofia are over NEAREST_MAX_KM from every node, so they were left
out of the flow model). Smaller farms whose connection isn't listed (most connect to the local
132/33 kV network) are drawn to the nearest substation. See _offshore_links().

Unit output = the latest Bid-Offer Acceptance level where NESO has instructed the unit
(BOALF), otherwise its Physical Notification (PN). Both are published within minutes.
Line flows are NOT computed here: the page runs a DC load flow from these injections,
so every flow on the wall is a model estimate and is labelled as such.
"""
import json
import math
import re
import threading
import time
import urllib.parse
from collections import deque
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import osm_sites as _osm          # OpenStreetMap unit positions (optional)
except Exception:
    _osm = None
try:
    import dno_data as _dno           # local network operators' registers (optional)
except Exception:
    _dno = None
try:
    import etys as _etys              # transmission circuit ratings from NESO's ETYS (optional)
except Exception:
    _etys = None

HERE = Path(__file__).resolve().parent
NET_FILE = HERE / "grid_network.json"

WALL_TTL = 55                 # s; one refresh per minute however many screens are open
SAMPLE_S = 60                 # background sampler cadence while the wall is in use
IDLE_STOP_S = 30 * 60         # stop sampling after this long with no /api/wall request
IC_HIST_MIN = 20              # minutes of interconnector flows kept (for their 15-min change)
UNIT_HIST_MIN = 120           # minutes of per-unit history kept for node charts
NEAREST_MAX_KM = 80.0         # a located unit further than this from any node is left out
CONSTRAINT_TTL = 1800         # NESO day-ahead constraint file refreshes ~daily; poll 30 min
LOLP_TTL = 600
REMIT_TTL = 300               # s; REMIT unavailability reports, refreshed every 5 min
REMIT_AHEAD_H = 24            # report outages active now or starting within this many hours
REMIT_BATCH = 50              # message details fetched this many at a time
REMIT_LONG_D = 180            # reports running longer than this from now (closures, mothballing,
                              # conversions) are left out: they are not day-to-day outages
GB_ZONE = "10YGB----------A"
# REMIT asset ids for interconnectors are I_I<x><G|D>-<link>; G = import capacity, D = export
IC_REMIT = {"FRAN1": "INTFR", "IFA2": "INTIFA2", "ELEC1": "INTELEC", "EWIC1": "INTEW", "GRN1": "INTGRNL",
            "MOYL1": "INTIRL", "NSL1": "INTNSL", "VKL1": "INTVKL", "BRTN1": "INTNED", "NEMO1": "INTNEM"}
NESO_CKAN = "https://api.neso.energy/api/3/action/datastore_search_sql"
NESO_CONSTRAINT_RES = "38a18ec1-9e40-465d-93fb-301e80fd1352"   # Day Ahead Constraint Flows and Limits
NESO_DEMAND_RES = "177f6fa4-ae49-4182-81ea-0c6b35f26ca6"       # Demand Data Update: actuals + 7-day embedded forecast
DEMAND_TTL = 3 * 3600         # its actuals are added through the day; re-read every 3 h
SPLIT_MIN_DAYS = 7            # actual days needed before the Scotland share is fitted
SPLIT_RANGE = (0.01, 0.20)    # a fitted Scotland share outside this is not used
B6_LOG_FILE = HERE / "b6_log.json"   # the wall page's modelled Scotland -> England transfer per half-hour (git-ignored)
B6_LOG_DAYS = 4
GSP_LOG_FILE = HERE / "gsp_log.json"  # the page's modelled vs NGED-measured GSP imports per half-hour (git-ignored)
GSP_LOG_DAYS = 2
IC_VERIFY_MW = 100.0          # a schedule is trusted only if its "now" matches FUELINST within
IC_VERIFY_PCT = 15.0          # max(100 MW, 15 %)

# Interconnector trader BM units are named I_I<x>G-/I_I<x>D-…; <x> -> FUELINST code.
# Ambiguous letters list several candidates and are settled by matching FUELINST.
IC_TOKEN = {"F": ["INTFR"], "2": ["INTIFA2"], "L": ["INTELEC"], "V": ["INTVKL"], "S": ["INTNSL"],
            "W": ["INTNSL"], "E": ["INTEW"], "G": ["INTGRNL"], "M": ["INTIRL"],
            "B": ["INTNEM", "INTNED"], "N": ["INTNED", "INTNEM"]}

# Offshore wind farm (OpenStreetMap site name, as UK Grid Atlas gives it) -> its connection point(s)
# as (lat, lon) of a network node, and where that comes from. A point is matched to the nearest
# network node within 3 km; (lat, lon, "near") is a landing substation that isn't in the network (on
# the local 132/33 kV network): the straight line then starts there and runs to the nearest onshore
# node. Audited 2026-10-29 against the TEC register, Ofgem OFTO memoranda, the offshore transmission
# owners and the projects' own information; see the CHANGELOG entry of that date. Farms within LINK_MAPPED_KM of a mapped line are already connected.
OFFSHORE_LINKS = {
    "Hornsea 1 Offshore Wind Farm": ([(53.778, 1.667)], "its export cable to Horseshoe Point and North Killingholme (mapped)"),
    "Hornsea 2 Offshore Wind Farm": ([(53.767, 1.417)], "its export cable to North Killingholme (mapped)"),
    "Hornsea 3 Offshore Wind Farm": ([(52.574, 1.272)], "NESO TEC register: Norwich Main 400 kV"),
    "Dogger Bank A Wind Farm": ([(53.801, -0.415)], "Dogger Bank Wind Farm: onshore cable to Creyke Beck"),
    "Dogger Bank B Wind Farm": ([(53.801, -0.415)], "Dogger Bank Wind Farm: onshore cable to Creyke Beck"),
    "Sofia Wind Farm": ([(54.567, -1.132)], "NESO TEC register: Lackenby 400 kV"),
    "Moray East Wind Farm": ([(58.077, -2.678)], "its export cable to New Deer 400 kV (mapped; NESO TEC register)"),
    "Triton Knoll Wind Farm": ([(53.385, 0.758)], "its export cable to Bicker Fen (mapped; Siemens)"),
    "Race Bank Wind Farm": ([(53.22, 0.725)], "its export cable to Walpole 400 kV (mapped; Ofgem)"),
    "Lincs Wind Farm": ([(52.73, 0.20)], "Walpole 400 kV (project information)"),
    "Dudgeon Offshore Wind Farm": ([(52.661, 0.794)], "NESO TEC register: Necton 400 kV"),
    "Sheringham Shoal Offshore Wind Farm": ([(52.574, 1.272)], "Salle, then Norwich Main (Sheringham Shoal)"),
    "London Array Wind Farm": ([(51.338, 0.941)], "Cleve Hill (project information)"),
    "Thanet Wind Farm": ([(51.31, 1.347)], "Richborough (project information)"),
    "Greater Gabbard Wind Farm": ([(52.208, 1.608)], "Leiston 132 kV, beside Sizewell (Ofgem OFTO memorandum)"),
    "Galloper Wind Farm": ([(52.208, 1.608)], "Sizewell, beside Greater Gabbard's substation (project information)"),
    "Rampion Wind Farm": ([(50.975, -0.235)], "Bolney 400 kV (project information)"),
    "Humber Gateway Wind Farm": ([(53.747, -0.232)], "Salt End (project information); Hedon 275 kV is the nearest network node"),
    "Westermost Rough Wind Farm": ([(53.747, -0.232)], "Salt End (project information); Hedon 275 kV is the nearest network node"),
    "Walney 1 & 2": ([(54.032, -2.89), (53.879, -2.989)], "Walney 1 at Heysham, Walney 2 at Stanah (project information)"),
    "Walney 3": ([(54.032, -2.89)], "Walney Extension: Heysham, beside Middleton (project information)"),
    "West of Duddon Sands Wind Farm": ([(54.032, -2.89)], "Heysham (ScottishPower Renewables)"),
    "Ormonde Wind Farm": ([(54.032, -2.89)], "Heysham 132 kV, Electricity North West (Ofgem OFTO memorandum)"),
    "Gwynt y Môr Offshore Wind Farm": ([(53.25, -3.474)], "St Asaph, beside Bodelwyddan (project information); 132 kV export cables via Pensarn"),
    "Kincardine Offshore Windfarm": ([(57.108, -2.092)], "NESO TEC register: Redmoss"),
    "Seagreen Offshore Wind Farm": ([(56.585, -1.76)], "its export cable to Tealing (mapped; NESO TEC register)"),
    "Barrow Wind Farm": ([(54.032, -2.89)], "Heysham, 132 kV (Transmission Investment, its offshore transmission owner)"),
    # landing on the local network away from any network node: drawn to the nearest node to it (lat, lon, "near")
    "Robin Rigg Offshore Wind Farm": ([(54.66, -3.52, "near")], "Seaton, Cumbria, 132 kV local network (Transmission Investment); line on to the nearest network node"),
    "Gunfleet Sands Wind Farm": ([(51.80, 1.14, "near")], "Clacton 132 kV, UK Power Networks (Transmission Investment); line on to the nearest network node"),
    "Lynn and Inner Dowsing Wind Farms": ([(53.15, 0.27, "near")], "Middlemarsh 33/132 kV near Skegness (Siemens); line on to the nearest network node"),
    "Kentish Flats Offshore Wind Farm": ([(51.345, 1.095, "near")], "Red House Farm substation, Herne Bay (project information); line on to the nearest network node"),
    "North Hoyle Offshore Wind Farm": ([(53.31, -3.48, "near")], "Rhyl substation, local network (project information); line on to the nearest network node"),
    "Rhyl Flats Wind Farm": ([(53.26, -3.58, "near")], "local network near Moelfre, via Towyn (environmental statement); line on to the nearest network node"),
    "Scroby Sands Wind Farm": ([(52.61, 1.72, "near")], "33 kV local network, Great Yarmouth (project information); line on to the nearest network node"),
    "Teesside Offshore Wind Farm": ([(54.615, -1.10, "near")], "Warrenby, Redcar, 33 kV (EDF); line on to the nearest network node"),
    "European Offshore Wind Deployment Centre": ([(57.205, -2.185, "near")], "Dyce 132 kV via Blackdog (SSEN Transmission); line on to the nearest network node"),
    "Blyth Offshore\xa0Wind Test Site": ([(55.155, -1.53, "near")], "old Blyth power station site, 66 kV export (Boskalis); line on to the nearest network node"),
    "Gunfleet Sands 3 Offshore Wind Farm": ([(51.806, 1.129, "near")], "its own export cable, ashore near Jaywick, to a metering substation by Waterglade Retail Park, Clacton (DONG Energy GFS3 newsletter, 2012)"),
    "Hywind Scotland Wind Farm": ([(57.48, -1.79, "near")], "Peterhead (Balfour Beatty / Equinor); line on to the nearest network node"),
}
# BM units the atlas doesn't link to their farm's OpenStreetMap site, or that sit at a different point
# from the rest of their farm: unit id prefix -> (farm, connection (lat, lon)). Used for where the unit is
# counted in the flow model and for its farm's line output.
OFFSHORE_UNITS = {
    "T_WLNYW-1": ("Walney 1 & 2", (54.032, -2.89)),        # Walney 1: Heysham
    "T_WLNYO-2": ("Walney 1 & 2", (53.879, -2.989)),       # Walney 2: Stanah
    "T_WLNYO-3": ("Walney 3", (54.032, -2.89)),            # Walney Extension: Heysham
    "T_WLNYO-4": ("Walney 3", (54.032, -2.89)),
    "T_HOWCO-": ("Hornsea 3 Offshore Wind Farm", (52.574, 1.272)),   # Hornsea 3: Norwich Main (TEC register)
    "E_BURBO": ("Burbo Bank Offshore Wind Farm", (53.366, -3.062)),  # Burbo Bank (first phase): Birkenhead 275 kV (TEC)
}
OFFSHORE_SKIP = {"Walney Wind Farm"}   # OpenStreetMap's whole-Walney outline, over Walney 1 & 2 and 3
LINK_MIN_MW = 12              # smaller "offshore" sites are mostly harbour and island turbines
LINK_MAPPED_KM = 3.0
LINK_CABLE_KM = 3.0           # a mapped 132 kV cable this close to a farm is taken as its export cable ...
LINK_JOIN_KM = 0.3            # ... followed through sections whose ends meet within this ...
LINK_AT_KM = 2.0              # ... and if it ends this close to the connection node, no straight line is drawn
LINK_SEA_KM = 15.0            # a mapped cable from the connection node that ends at sea this close to a farm (and
                              # at most half as far from it as the node is) is its export cable, mapped part way:
                              # the straight line then runs only from the farm to where that cable ends

# Grid substations that OpenStreetMap's network record names after a wind farm's substation beside them
# (the farm's line, and other generation there such as Sizewell B, would otherwise carry the farm's name):
# OSM name -> the grid substation's name. Each was checked against the node's position and its circuits.
NODE_RENAME = {
    "Galloper Onshore Substation": "Sizewell Substation",         # 400 kV to Bramford; Sizewell B's node
    "Ormonde onshore substation": "Heysham Substation",           # 400 kV to Heysham power station
    "Moray East Onshore substation": "New Deer Substation",       # 400 kV, Peterhead - Rothienorman
    "Neart na Gaoithe Wind Farm Substation": "Crystal Rig Substation",   # 400 kV, Branxton / Fallago, 132 kV
}

# Terravolt type -> short fuel key used by the page
FUEL_KEY = {"Wind: Onshore": "wind", "Wind: Offshore": "wind", "Gas": "gas", "OCGT": "ocgt",
            "Peaker": "ocgt", "Nuclear": "nuclear", "Biomass": "biomass", "Hydro": "hydro",
            "Pumped": "pumped", "Battery": "battery", "VPP": "other", "Synchronous Compensator": "syncon",
            "Coal": "coal", "Oil": "oil", "Solar": "solar"}

_S = {}                       # bound grid_server globals
_lock = threading.Lock()
_state = {"payload": None, "ts": 0.0, "last_req": 0.0, "sampler": False,
          "net": None, "net_mtime": 0, "unit_node": None, "unit_node_key": None,
          "constraints": None, "constraints_ts": 0.0, "lolp": None, "lolp_ts": 0.0,
          "ic_tok": {}, "remit": None, "remit_ts": 0.0}
_ic_hist = deque(maxlen=IC_HIST_MIN + 2)     # one {"t", "ic": {code: mw}} per minute, in memory only
_unit_hist = {}                             # code -> deque[(t_iso, mw)]
_remit_msgs = {}                            # REMIT message id -> details (only ids still listed are kept)


def attach(server_globals):
    """Called once by grid_server with its globals()."""
    for k in ("fetch_json", "BMRS", "_load_bmu_locations", "_load_bmu_registry", "_classify_bmu", "get_generation", "_cache", "_rows"):
        if k in server_globals:
            _S[k] = server_globals[k]


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _pt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _km(ax, ay, bx, by):
    return math.hypot((ax - bx) * math.cos(math.radians((ay + by) / 2)) * 111.32, (ay - by) * 110.57)


# ---------------------------------------------------------------- network
def network():
    try:
        mt = NET_FILE.stat().st_mtime
    except OSError:
        return None
    if _state["net"] is None or mt != _state["net_mtime"]:
        net = json.loads(NET_FILE.read_text(encoding="utf-8"))
        ren = {}
        for i, n in enumerate(net["nodes"]):
            if n[3] in NODE_RENAME:
                ren[i] = n[3]
                n[3] = NODE_RENAME[n[3]]
        _state["net"], _state["renamed"] = net, ren
        _state["net_mtime"] = mt
        _state["unit_node"] = None
    return _state["net"]


def _offshore_unit(code):
    """(farm, (lat, lon)) from OFFSHORE_UNITS for this BM unit id, or None."""
    for p, v in OFFSHORE_UNITS.items():
        if code == p or (p.endswith("-") and code.startswith(p)):
            return v
    return None


def _offshore_links(net):
    """[{"site", "name", "mw", "lat", "lon", "nodes": [i...], "from": [[lat, lon]...], "src", "known", "cable"}] for
    offshore farms with no mapped transmission connection, and {osm site id: node} for counting their units (single
    connection only). Where a mapped 132 kV cable (OpenStreetMap or an operator's map) starts at the farm, the
    straight line starts where that cable ends (its landward end), and is left out if the cable already ends at the
    connection; a farm with no published connection is drawn to the nearest substation to that end. Where a mapped
    cable leaves the farm's connection and ends at sea short of the farm (mapped part way), the straight line runs
    from the farm only to that end ("to")."""
    d = _osm.atlas() if _osm is not None else None
    if not d or net is None:
        return [], {}
    dd = _dno.data() if _dno is not None else None
    ld = _dno.lines_data() if _dno is not None else None
    key = (d.get("ts"), _state["net_mtime"], (dd or {}).get("ts"), (ld or {}).get("ts"))
    if _state.get("links_key") == key:
        return _state["links"], _state["link_node"]
    ecr = {}                                                  # register entries for the offshore farms
    if dd:
        try:
            ecr = _dno.match_sites({sid: (s[0], s[1], s[3], s[4], s[2]) for sid, s in d["sites"].items()
                                    if s[3] == "wind_offshore" and s[2] not in OFFSHORE_LINKS})
            gm = _dno.gsp_map(named_nodes(net))
        except Exception:
            ecr, gm = {}, {}
    nodes = net["nodes"]
    main = [(i, n[0], n[1]) for i, n in enumerate(nodes) if not n[4]]
    pts = [(q[0], q[1]) for l in net["lines"] for q in l[7]]

    # offshore platforms (unnamed nodes of 145-260 kV only) aren't substations to draw a small farm to
    plat = {i for i, n in enumerate(nodes) if not n[3] and n[2] and all(145 <= v <= 260 for v in n[2])}
    # nor are another wind farm's own substations (Triton Knoll's, Burbo Bank 2's, Galloper's onshore one)
    plat |= {i for i, n in enumerate(nodes) if re.search(r"wind|offshore|onshore substation", n[3] or "", re.I)}

    def nearest(lat, lon, lim=None, onshore=False):
        b = min(((_km(lon, lat, x, y), i) for i, x, y in main if not (onshore and i in plat)), default=None)
        return b[1] if b and (lim is None or b[0] <= lim) else None
    # mapped 132 kV cables: OpenStreetMap's (as served) and the operators' own, as point lists [(lon, lat)]
    cables = [[tuple(p) for p in g] for v, ug, g in (_dist_not_covered(net.get("dist") or [], _dno.lines_for(132))[0]
                                                     if ld else net.get("dist") or [])]
    if ld:
        cables += [[(r[4][j], r[4][j + 1]) for j in range(0, len(r[4]), 2)] for r in _dno.lines_for(132)]

    def cable_end(lat, lon):
        """The landward end of the mapped cable starting at this farm, or None."""
        best = None
        for c in cables:
            dk = min(_km(lon, lat, x, y) for x, y in (c[0], c[-1], c[len(c) // 2]))
            if dk <= LINK_CABLE_KM and (best is None or dk < best[0]):
                best = (dk, c)
        if not best:
            return None
        c, used = best[1], {id(best[1])}
        e = follow(c[-1] if _km(lon, lat, *c[0]) <= _km(lon, lat, *c[-1]) else c[0], used)
        taken.update(used)                                # a farm's own export cable is no other farm's
        return e

    def follow(end, used):
        """Follow joined sections on from this end while the cable runs on alone; stop where it branches (a
        substation) or reaches a network node. Returns (lat, lon)."""
        for _ in range(30):
            if any(_km(end[0], end[1], x, y) <= LINK_AT_KM for i, x, y in main):
                break
            nxt = []
            for c2 in cables:
                if id(c2) in used:
                    continue
                for a, b in ((c2[0], c2[-1]), (c2[-1], c2[0])):
                    if _km(end[0], end[1], *a) <= LINK_JOIN_KM:
                        nxt.append((c2, b))
                        break
            if len(nxt) != 1:
                break
            used.add(id(nxt[0][0]))
            end = nxt[0][1]
        return end[1], end[0]

    taken = set()

    def sea_ends(lat, lon, node):
        """[(km from the farm, cable id, (lat, lon))]: mapped cables leaving this node that end at sea near the farm."""
        nx, ny = nodes[node][0], nodes[node][1]
        dn, out = _km(lon, lat, nx, ny), []
        for c in cables:
            for a, b in ((c[0], c[-1]), (c[-1], c[0])):
                if _km(nx, ny, *a) <= LINK_AT_KM:
                    e = follow(b, {id(c)})
                    dk = _km(lon, lat, e[1], e[0])
                    if dk <= LINK_SEA_KM and dk <= dn / 2:
                        out.append((dk, id(c), e))
                    break
        return out

    links, link_node = [], {}
    for sid, s in d["sites"].items():
        lat, lon, name, fuel, mw = s[0], s[1], s[2], s[3], s[4]
        if fuel != "wind_offshore" or name in OFFSHORE_SKIP or (mw or 0) < LINK_MIN_MW:
            continue
        known = OFFSHORE_LINKS.get(name)
        if not known and any(_km(lon, lat, x, y) <= LINK_MAPPED_KM for x, y in pts):
            continue                                          # already reaches a mapped line
        ce = cable_end(lat, lon)                              # its mapped 132 kV export cable's landward end
        via = None
        if known:
            tg = [nearest(t[0], t[1], onshore=True) if len(t) > 2 else nearest(t[0], t[1], 3.0) for t in known[0]]
            tg = [i for i in tg if i is not None]
            if not tg:
                continue
            src = known[1]
            land = [t for t in known[0] if len(t) > 2]
            if land:                                          # its landing substation, off the network:
                lp = (land[0][0], land[0][1])                 # start there if a mapped cable reaches it, else go via it
                ce, via = (lp, None) if ce else (None, lp)
        elif sid in ecr and gm.get((ecr[sid][0], _dno.gsp_key(ecr[sid][2]))) is not None:
            e = ecr[sid]                                      # where its operator's register says it connects
            tg = [gm[(e[0], _dno.gsp_key(e[2]))]]
            src = f"{e[0]} Embedded Capacity Register: {e[2]} grid supply point" + (f", {e[4]:g} kV" if e[4] else "")
            known = True
        else:
            tg, src = [nearest(*(ce or (lat, lon)), onshore=True)], ""
            if tg[0] is None:
                continue
        draw, frm = [], []
        for i in tg:
            x, y = nodes[i][0], nodes[i][1]
            if ce and _km(ce[1], ce[0], x, y) <= LINK_AT_KM:
                continue                                      # the mapped cable already ends there
            draw.append(i)
            frm.append(list(ce) if ce and len(tg) == 1 else [lat, lon])
        if via and draw and _km(via[1], via[0], nodes[draw[0]][0], nodes[draw[0]][1]) <= LINK_AT_KM:
            via = None
        links.append({"site": sid, "name": name, "mw": mw, "lat": lat, "lon": lon, "nodes": draw, "from": frm,
                      "all": tg, "src": src, "known": bool(known), "cable": bool(ce), "via": list(via) if via else None,
                      "to": [None] * len(draw),
                      "_sea": [(dk, cid, e, j) for j, i in enumerate(draw) if frm[j] == [lat, lon] and not via
                               for dk, cid, e in sea_ends(lat, lon, i)]})
        if len(tg) == 1:
            link_node[sid] = tg[0]
    # export cables mapped part way out from the connection: each cable to the nearest farm it could be (Greater
    # Gabbard and Galloper each have their own from Sizewell), and the farm's line then ends where its cable does
    used = set()
    for dk, k, j, cid, e in sorted((dk, k, j, cid, e) for k, x in enumerate(links) for dk, cid, e, j in x["_sea"]):
        x = links[k]
        if cid in used or cid in taken or x["to"][j]:
            continue
        used.add(cid)
        x["to"][j] = [round(e[0], 5), round(e[1], 5)]
    # a landing beside where another farm's line already starts (Gunfleet Sands 3 at Clacton, beside Gunfleet
    # Sands' cable): the line stops at the landing rather than run a second leg on top of that farm's line
    for x in links:
        if not x["via"] or len(x["nodes"]) != 1:
            continue
        for o in links:
            if o is x or o["nodes"] != x["nodes"]:
                continue
            if any(f != [o["lat"], o["lon"]] and _km(f[1], f[0], x["via"][1], x["via"][0]) <= LINK_AT_KM for f in o["from"]):
                x["to"][0] = [x["via"][0], x["via"][1], "land", o["name"]]
                x["via"] = None
                break
    for x in links:
        x.pop("_sea", None)
        if not any(x["to"]):
            x["to"] = None
    _state["links_key"], _state["links"], _state["link_node"] = key, links, link_node
    return links, link_node


def _dist_not_covered(dist, dno132):
    """OpenStreetMap's 132 kV distribution lines, less those an operator's own 132 kV map covers (70 % or more of
    the line within about 400 m of one of the operator's lines). Returns (kept lines, number left out)."""
    if not dno132:
        return dist, 0
    cy, cx = 0.0036, 0.006                                        # ~400 m cells
    cells = set()
    for r in dno132:
        f = r[4]
        for j in range(0, len(f) - 2, 2):
            x0, y0, x1, y1 = f[j], f[j + 1], f[j + 2], f[j + 3]
            n = max(1, int(max(abs(x1 - x0) / cx, abs(y1 - y0) / cy) * 2))
            for q in range(n + 1):
                cells.add((int((y0 + (y1 - y0) * q / n) / cy), int((x0 + (x1 - x0) * q / n) / cx)))
    near = lambda x, y: any((int(y / cy) + a, int(x / cx) + b) in cells for a in (-1, 0, 1) for b in (-1, 0, 1))
    kept, drop = [], 0
    for d in dist:
        g = d[2]
        pts = []
        for (x0, y0), (x1, y1) in zip(g[:-1], g[1:]):
            n = max(1, int(max(abs(x1 - x0) / cx, abs(y1 - y0) / cy)))
            pts += [(x0 + (x1 - x0) * q / n, y0 + (y1 - y0) * q / n) for q in range(n)]
        pts.append(tuple(g[-1]))
        if pts and sum(1 for x, y in pts if near(x, y)) >= 0.7 * len(pts):
            drop += 1
        else:
            kept.append(d)
    return kept, drop


def dnolines_bytes(kv):
    """/api/wall-dnolines?kv=: {"kv", "ts", "ops", "lines": [[operator index, underground, name, [lon, lat, ...]]]}."""
    if _dno is None or kv not in (132, 66, 33):
        return None
    d = _dno.lines_data()
    key = (kv, (d or {}).get("ts"))
    c = _state.setdefault("dnol", {})
    if c.get(kv, (None,))[0] == key:
        return c[kv][1]
    rows = _dno.lines_for(kv)
    ops = sorted({r[0] for r in rows})
    oi = {o: i for i, o in enumerate(ops)}
    b = json.dumps({"kv": kv, "ts": (d or {}).get("ts"), "ops": ops,
                    "lines": [[oi[r[0]], r[2], r[3], r[4]] for r in rows]}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    c[kv] = (key, b)
    return b


def named_nodes(net=None):
    """[(index, name, lon, lat)] for the main-GB nodes, with the borrowed OpenStreetMap names filled in."""
    net = net or network()
    if net is None:
        return []
    names = _names_for_net(net)
    out = []
    for i, n in enumerate(net["nodes"]):
        if n[4]:
            continue
        nm = n[3]
        if not nm:
            m = names.get(_nkey(n))
            if m and m.get("k") in ("osm", "plant"):
                nm = NODE_RENAME.get(m.get("n"), m.get("n"))
        out.append((i, nm or "", n[0], n[1]))
    return out


def get_local():
    """/api/wall-local: embedded generation under each node's grid supply point(s), from the DNO registers."""
    if _dno is None:
        return {"error": "dno_data.py not installed"}
    d = _dno.data()
    key = ((d or {}).get("ts"), _state["net_mtime"], _names["ver"], ((_dno.lines_data() or {}).get("ts")))
    if _state.get("local_key") == key and _state.get("local"):
        return _state["local"]
    nodes = named_nodes()
    summ = _dno.node_summary(nodes) if d else {}
    gm = _dno.gsp_map(nodes) if d else {}
    rows = _dno.all_rows()
    mapped = sum(1 for r in rows if (r[0], _dno.gsp_key(r[5])) in gm)
    ld = _dno.lines_data()
    out = {"nodes": {str(k): v for k, v in summ.items()}, "status": _dno.status(), "credit": _dno.CREDIT,
           "rows": len(rows), "mapped": mapped, "ts": (d or {}).get("ts"),
           "lines": {k: {kk: v.get(kk) for kk in ("ok", "n", "error")} for k, v in ((ld or {}).get("status") or {}).items()},
           "live": _dno.npg_live_status()}
    _state["local_key"], _state["local"] = key, out
    return out


def sites_bytes():
    """/api/wall-sites body (bytes), or None if osm_sites.py is missing or has no data yet."""
    return _osm.sites_bytes() if _osm is not None else None


def network_bytes():
    """grid_network.json with node names overlaid: each node gains a 7th element
    {"k": kind, "s": source text, "loc": location phrase} where a name was borrowed or a
    location found, and kind "osm"/"plant" names fill the node's own name slot."""
    net = network()
    if net is None:
        return None
    names = _names_for_net(net)
    links, _ = _offshore_links(net)
    ld = _dno.lines_data() if _dno is not None else None
    ed = _etys.data() if _etys is not None else None
    eb = _etys.boundaries() if _etys is not None and hasattr(_etys, "boundaries") else None
    key = (_state["net_mtime"], _names["ver"], _state.get("links_key"), (ld or {}).get("ts"), (ed or {}).get("at"),
           (eb or {}).get("at"))
    if _state.get("net_bytes_key") == key and _state.get("net_bytes"):
        return _state["net_bytes"]
    out = dict(net)
    if ld and net.get("dist"):
        out["dist"], out["dist_dropped"] = _dist_not_covered(net["dist"], _dno.lines_for(132))
    out["offlinks"] = [[x["lat"], x["lon"], x["nodes"], x["mw"], x["name"], x["src"], 1 if x["known"] else 0,
                        x["from"], 1 if x["cable"] else 0, x["via"], x["to"]] for x in links]
    nodes, ren = [], _state.get("renamed") or {}
    for i, n in enumerate(net["nodes"]):
        m = names.get(_nkey(n)) if not n[3] else None
        if i in ren:
            m = {"k": "fix", "s": f"the grid substation here; OpenStreetMap's network record names this node after "
                                 f"the wind farm substation beside it ({ren[i]})"}
        if m:
            n = list(n[:6]) + [m]
            if m["k"] in ("osm", "plant"):
                n[3] = m["n"]
                if m["n"] in NODE_RENAME:                 # a borrowed name that is the wind farm's substation beside it
                    n[3] = NODE_RENAME[m["n"]]
                    n[6] = {"k": "fix", "s": f"the grid substation here; the nearest named substation in OpenStreetMap "
                                             f"is the wind farm substation beside it ({m['n']})"}
        nodes.append(n)
    out["nodes"] = nodes
    out["names_ts"] = _names.get("ts")
    # each line's ETYS ratings [winter, spring, summer, autumn] MVA where matched (see etys.py)
    if _etys is not None:
        try:
            names = _names_for_net(net)
            alt = []
            for i, n in enumerate(net["nodes"]):                # unnamed nodes: a nearby substation or place
                m = names.get(_nkey(n)) if not n[3] and not n[4] else None
                if m and m.get("k") == "near":
                    alt.append((i, re.sub(r"^near\s+", "", m.get("n") or "")))
                elif m and m.get("k") == "place" and (m.get("loc") or "").startswith("near "):
                    alt.append((i, m["loc"][5:]))
            rt, rs = _etys.match(net, named_nodes(net), alt)
            out["ratings"], out["ratings_info"] = {str(k): v for k, v in rt.items()}, rs
            bd = _etys.boundaries() if hasattr(_etys, "boundaries") else None
            if bd:
                out["etysbnd"] = bd["lines"]
        except Exception as e:
            out["ratings_info"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    b = json.dumps(out, separators=(",", ":")).encode("utf-8")
    _state["net_bytes"], _state["net_bytes_key"] = b, key
    return b


# ---------------------------------------------------------------- node names
# About 160 of the ~590 network nodes have no name in OpenStreetMap's record for them
# (often the substation next to them is named, but on a different OSM element). A
# background task, started when this module loads, looks around each unnamed node in
# OpenStreetMap (Overpass API) for named substations, power stations and places, keeps
# the candidates in node_names.json (git-ignored), and refreshes them monthly or when the
# network changes. node_names_seed.json (committed) gives a new install names at once.
# Names are chosen from the candidates by the rules in _pick_name, so the rules can change
# without a new query. Nothing here blocks the wall; on failure it retries with backoff.
NAMES_FILE = HERE / "node_names.json"
NAMES_SEED = HERE / "node_names_seed.json"
NAMES_START_S = 90            # first look this long after start-up
NAMES_MAX_AGE_S = 30 * 86400  # refresh monthly
NAMES_RETRY_S = [600, 1200, 2400, 4800, 9600, 21600]
OVERPASS = ["https://overpass-api.de/api/interpreter",
            "https://overpass.private.coffee/api/interpreter"]
UA_NAMES = "uk-grid-monitor/1.0 (personal dashboard; node names)"
NM_POWER_KM = 2.5             # substations / power stations searched within this
NM_PLACE_KM = 15.0            # towns and villages
NM_PLACE_SEA_KM = 70.0        # towns and cities, for offshore nodes
NM_EXACT_KM = 0.5             # a 132 kV+ substation this close is the same site
NM_NEAR_KM = 2.0              # ... up to this far: "near <name>"
NM_PLANT_MIN_MW = 5.0
_GENERIC_RE = re.compile(r"^(landfill gas generator|solar (farm|park)|wind (farm|turbine)|substation|"
                         r"power station|generator|battery( storage)?)$", re.I)
_PLANT_RE = re.compile(r"power station|power plant|wind ?farm|offshore|hydro|energy (centre|recovery)|"
                       r"\bERF\b|\bCHP\b|converter|nuclear|biomass", re.I)
_names = {"ver": 0, "cands": None, "ts": None, "busy": False}


def _nkey(n):
    return f"{n[0]:.4f},{n[1]:.4f}"


def _load_names():
    if _names["cands"] is not None:
        return
    for f in (NAMES_FILE, NAMES_SEED):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            _names["cands"], _names["ts"] = d.get("nodes") or {}, d.get("ts")
            _names["ver"] += 1
            return
        except Exception:
            continue
    _names["cands"] = {}


def _hv(volt):
    try:
        return max(int(x) for x in re.findall(r"\d+", volt or "")) >= 132000
    except ValueError:
        return False


def _plant_mw(out):
    m = re.match(r"\s*([\d.]+)\s*(GW|MW|kW)?", out or "", re.I)
    if not m:
        return None
    v = float(m.group(1))
    u = (m.group(2) or "MW").upper()
    return v * 1000 if u == "GW" else v / 1000 if u == "KW" else v


def _bearing8(ax, ay, bx, by):
    a = math.degrees(math.atan2((bx - ax) * math.cos(math.radians((ay + by) / 2)), by - ay)) % 360
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((a + 22.5) // 45) % 8]


def _pick_name(n, cands):
    """Name record for one node from its candidates [kind, name, voltage, output, lon, lat]."""
    x, y, offshore = n[0], n[1], not n[5]
    subs, plants, places = [], [], []
    for c in cands:
        k, name, volt, out, cx, cy = c
        if _GENERIC_RE.match(name.strip()):
            continue                      # a type, not a name ("Landfill gas generator")
        d = _km(x, y, cx, cy)
        if k == "substation" and _hv(volt) and d <= NM_NEAR_KM:
            subs.append((d, name))
        elif k == "plant" and d <= NM_POWER_KM:
            mw = _plant_mw(out)
            if (mw is not None and mw >= NM_PLANT_MIN_MW) or (mw is None and _PLANT_RE.search(name)):
                plants.append((d, name))
        elif k.startswith("place"):
            places.append((d, name, cx, cy, k))
    subs.sort(); plants.sort(); places.sort()
    if subs and subs[0][0] <= NM_EXACT_KM:
        return {"k": "osm", "n": subs[0][1], "s": f"OpenStreetMap substation {subs[0][0]:.1f} km away"}
    if plants and plants[0][0] <= 1.0:
        return {"k": "plant", "n": plants[0][1], "s": f"power station {plants[0][0]:.1f} km away (OpenStreetMap)"}
    if subs:
        return {"k": "near", "n": "near " + subs[0][1],
                "s": f"nearest named substation {subs[0][0]:.1f} km away (OpenStreetMap); not confirmed as this node"}
    if plants:
        return {"k": "near", "n": "near " + plants[0][1],
                "s": f"nearest power station {plants[0][0]:.1f} km away (OpenStreetMap); not confirmed as this node"}
    if places:
        d, name, cx, cy, k = places[0]
        if offshore and d > 5:
            loc = f"offshore, {round(d)} km {_bearing8(cx, cy, x, y)} of {name}"
        else:
            loc = f"near {name}"
        return {"k": "place", "loc": loc, "s": "location from OpenStreetMap places"}
    return None


def _names_for_net(net):
    """{node key: name record} for the unnamed nodes of this network."""
    _load_names()
    ck = (_state["net_mtime"], _names["ver"])
    if _names.get("pick_key") == ck:
        return _names["pick"]
    out = {}
    for n in net["nodes"]:
        if n[3] or n[4]:
            continue
        c = _names["cands"].get(_nkey(n))
        if c:
            r = _pick_name(n, c)
            if r:
                out[_nkey(n)] = r
    _names["pick"], _names["pick_key"] = out, ck
    return out


def _overpass_query(nodes):
    parts = []
    for n in nodes:
        x, y = n[0], n[1]
        parts.append(f'nwr(around:{int(NM_POWER_KM * 1000)},{y},{x})["power"~"^(substation|plant)$"]["name"];')
        if n[5]:
            parts.append(f'node(around:{int(NM_PLACE_KM * 1000)},{y},{x})["place"~"^(city|town|village)$"]["name"];')
        else:
            parts.append(f'node(around:{int(NM_PLACE_SEA_KM * 1000)},{y},{x})["place"~"^(city|town)$"]["name"];')
    return "[out:json][timeout:240];(" + "".join(parts) + ");out tags center qt;"


def _fetch_candidates(nodes):
    """Query Overpass once for all `nodes`; return {node key: [candidates]} or raise."""
    import urllib.request
    q = urllib.parse.urlencode({"data": _overpass_query(nodes)}).encode()
    last = None
    for u in OVERPASS:
        try:
            req = urllib.request.Request(u, data=q, headers={"User-Agent": UA_NAMES})
            with urllib.request.urlopen(req, timeout=300) as r:
                body = r.read()
            if not body.lstrip().startswith(b"{"):
                raise ValueError("not JSON (server busy)")
            els = json.loads(body).get("elements") or []
            break
        except Exception as e:
            last = e
    else:
        raise RuntimeError(f"Overpass unavailable: {last}")
    pts = []
    for e in els:
        t = e.get("tags") or {}
        c = e.get("center") or {"lat": e.get("lat"), "lon": e.get("lon")}
        if c.get("lat") is None or not t.get("name"):
            continue
        kind = t.get("power") or ("place_" + t.get("place", ""))
        pts.append([kind, t["name"][:60], t.get("voltage", ""), t.get("plant:output:electricity", ""),
                    round(c["lon"], 5), round(c["lat"], 5)])
    return {_nkey(n): _trim_cands(n, pts) for n in nodes}


def _trim_cands(n, pts):
    """Keep what _pick_name can use: 132 kV+ substations and power stations within NM_POWER_KM and
    the 3 nearest places (the full place lists made the cache ~650 KB)."""
    lim = NM_PLACE_KM if n[5] else NM_PLACE_SEA_KM
    power = [p for p in pts if (p[0] == "plant" or (p[0] == "substation" and _hv(p[2])))
             and _km(n[0], n[1], p[4], p[5]) <= NM_POWER_KM]
    places = sorted((_km(n[0], n[1], p[4], p[5]), p) for p in pts if p[0].startswith("place"))
    return power + [p for d, p in places if d <= lim][:3]


def _names_task():
    time.sleep(NAMES_START_S)
    fail = 0
    while True:
        wait = NAMES_MAX_AGE_S / 10
        try:
            net = network()
            _load_names()
            if net is not None:
                unnamed = [n for n in net["nodes"] if not n[3] and not n[4]]
                stale = not _names["ts"] or time.time() - _names["ts"] > NAMES_MAX_AGE_S
                cached_file = NAMES_FILE.exists()
                missing = [n for n in unnamed if _nkey(n) not in _names["cands"]]
                todo = unnamed if (stale or not cached_file) else missing
                if todo:
                    got = _fetch_candidates(todo)
                    cands = dict(_names["cands"])
                    cands.update(got)
                    keep = {_nkey(n) for n in unnamed}
                    cands = {k: v for k, v in cands.items() if k in keep}
                    blob = {"ts": time.time(), "built": net.get("built"), "nodes": cands}
                    tmp = NAMES_FILE.with_suffix(".tmp")
                    tmp.write_text(json.dumps(blob, separators=(",", ":")), encoding="utf-8")
                    tmp.replace(NAMES_FILE)
                    _names["cands"], _names["ts"] = cands, blob["ts"]
                    _names["ver"] += 1
                fail = 0
        except Exception:
            wait = NAMES_RETRY_S[min(fail, len(NAMES_RETRY_S) - 1)]
            fail += 1
        time.sleep(wait)


def _start_names_task():
    if not _names["busy"]:
        _names["busy"] = True
        threading.Thread(target=_names_task, name="wall-node-names", daemon=True).start()


def _unit_nodes(locs, net):
    """BM unit -> node index (nearest main-GB node). Shetland units feed the mainland
    through the Shetland HVDC landing at Blackhillock, so they are placed there."""
    key = (id(locs), len(locs), _state["net_mtime"], _state.get("links_key"))
    if _state["unit_node"] is not None and _state["unit_node_key"] == key:
        return _state["unit_node"]
    nodes = net["nodes"]
    main = [(i, n[0], n[1]) for i, n in enumerate(nodes) if not n[4]]
    cm = next((h for h in net.get("hvdc", []) if "Caithness" in h["name"]), None)
    out = {}
    for code, loc in locs.items():
        try:
            lat, lng = float(loc.get("lat")), float(loc.get("lng"))
        except (TypeError, ValueError):
            continue
        if lat > 59.5 and cm is not None:                      # Shetland
            out[code] = (cm["b"], 0.0)
            continue
        best = None
        for i, x, y in main:
            d = _km(lng, lat, x, y)
            if best is None or d < best[1]:
                best = (i, d)
        if best and best[1] <= NEAREST_MAX_KM:
            out[code] = (best[0], round(best[1], 1))
    # offshore farms with a known connection: their units count at it (see OFFSHORE_LINKS, OFFSHORE_UNITS)
    d = _osm.atlas() if _osm is not None else None
    _, link_node = _offshore_links(net)
    if d and link_node:
        for code, sid in d["bmu"].items():
            if sid in link_node and code in locs:
                out[code] = (link_node[sid], 0.0)
    for code in locs:
        u = _offshore_unit(code)
        if u:
            b = min(((_km(u[1][1], u[1][0], x, y), i) for i, x, y in main), default=None)
            if b and b[0] <= 3.0:
                out[code] = (b[1], 0.0)
    _state["unit_node"], _state["unit_node_key"] = out, key
    return out


# ---------------------------------------------------------------- PN / BOALF
def _fetch_stream(ds, t0, t1):
    url = f"{_S['BMRS']}/datasets/{ds}/stream?format=json&from={_iso(t0)}&to={_iso(t1)}"
    r = _S["fetch_json"](url, timeout=45)
    return r if isinstance(r, list) else _S["_rows"](r)


def _seg(rows):
    """Pre-parse rows into per-unit segment lists [(t0, t1, l0, l1, acc_no, acc_time, so_flag)]. so_flag (BOALF
    only): Elexon's SO flag, set on an acceptance NESO made for system reasons (mostly constraints), not energy."""
    out = {}
    for r in rows:
        u = r.get("bmUnit")
        if not u:
            continue
        try:
            t0 = _pt(r["timeFrom"]).timestamp(); t1 = _pt(r["timeTo"]).timestamp()
        except Exception:
            continue
        at = r.get("acceptanceTime")
        out.setdefault(u, []).append((t0, t1, float(r.get("levelFrom") or 0), float(r.get("levelTo") or 0),
                                      int(r.get("acceptanceNumber") or 0),
                                      _pt(at).timestamp() if at else 0.0, bool(r.get("soFlag"))))
    return out


def _level(segs, t, now_ts=None, seg=False):
    """Level at time t. PN: the last-listed covering segment wins. BOALF (now_ts given):
    the highest acceptance number already issued by now_ts whose profile covers t. seg=True: (level, segment)."""
    best = None
    for s in segs:
        if s[0] <= t <= s[1]:
            if now_ts is not None:
                if s[5] > now_ts:
                    continue
                if best is not None and s[4] < best[4]:
                    continue
            best = s
    if best is None:
        return (None, None) if seg else None
    span = (best[1] - best[0]) or 1.0
    f = max(0.0, min(1.0, (t - best[0]) / span))
    v = best[2] + (best[3] - best[2]) * f
    return (v, best) if seg else v


# ---------------------------------------------------------------- turn-down for system reasons
# A unit NESO has instructed below its notified level (PN) by an SO-flagged acceptance (BOALF: made for system
# reasons, mostly transmission constraints) is turned down for the system by PN - accepted level. Summed by node,
# this is measured evidence of constraints: the page compares it with the ETYS boundaries (the turn-down behind each).
SO_MIN_MW = 5


def _so_turndown(un, pn, bo, nts, locs=None):
    """{"nodes": {node: MW}, "total", "unloc", "units": [[code, node or None, MW, name], ...] biggest first} or {}."""
    nodes, units, unloc = {}, [], 0.0
    for u, segs in bo.items():
        v, sg = _level(segs, nts, now_ts=nts, seg=True)
        if v is None or not sg or not sg[6]:
            continue
        p = _level(pn.get(u, []), nts)
        if p is None or p - v < SO_MIN_MW:
            continue
        d = p - v; nd = un.get(u)
        if nd is None:
            unloc += d
        else:
            nodes[nd[0]] = nodes.get(nd[0], 0.0) + d
        units.append([u, nd[0] if nd else None, round(d), ((locs or {}).get(u) or {}).get("name") or ""])
    units.sort(key=lambda x: -x[2])
    return {"nodes": {str(k): round(v) for k, v in nodes.items()}, "total": round(sum(nodes.values()) + unloc),
            "unloc": round(unloc), "units": units[:40]}


# The dashboard's constraint chip: the same turn-down, kept fresh whether or not a wall page is open (the wall's
# payload is used when it is fresh, otherwise PN and BOALF are fetched in the background at most every SO_TTL),
# summed by area (the node's region; Scottish regions as "Scotland"), with the boundaries a wall page last
# reported as constrained (POST /api/wall-b6, "bnd"; kept BND_REP_S).
SO_TTL = 120
BND_REP_S = 300
_so = {"data": None, "t": None, "ts": 0.0, "busy": False, "err": None}
_bnd_rep = {"ts": 0.0, "names": []}
_area_of = {}


def _so_worker():
    try:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        net = network()
        if net is None:
            raise RuntimeError("no network")
        pn = _seg(_fetch_stream("PN", now - timedelta(minutes=20), now + timedelta(minutes=5)))
        bo = _seg(_fetch_stream("BOALF", now - timedelta(minutes=90), now + timedelta(minutes=5)))
        locs = _S["_load_bmu_locations"]() or {}
        if _osm is not None:
            try:
                locs = _osm.unit_locations(locs)
            except Exception:
                pass
        d = _so_turndown(_unit_nodes(locs, net), pn, bo, now.timestamp(), locs)
        with _lock:
            _so.update(data=d, t=_iso(now), ts=time.time(), err=None)
    except Exception as e:
        with _lock:
            _so.update(ts=time.time(), err=f"{type(e).__name__}: {str(e)[:120]}")
    finally:
        with _lock:
            _so["busy"] = False


def _area(net, i):
    """A node's area for the chip: its region (nearest node with one, for offshore platforms); Scotland's two
    regions as "Scotland"."""
    if i not in _area_of:
        nd = net["nodes"]
        r = nd[i][5] if len(nd[i]) > 5 else ""
        if not r:
            x, y = nd[i][0], nd[i][1]
            near = min((n for n in nd if len(n) > 5 and n[5]), key=lambda n: (n[0] - x) ** 2 * .4 + (n[1] - y) ** 2, default=None)
            r = near[5] if near else ""
        _area_of[i] = "Scotland" if "Scotland" in r else (r or "other")
    return _area_of[i]


def _so_areas(net, so):
    """[[area, MW]] biggest first, for a turn-down dict from _so_turndown()."""
    areas = {}
    for k, mw in (so.get("nodes") or {}).items():
        a = _area(net, int(k)) if net else "other"
        areas[a] = areas.get(a, 0) + mw
    if so.get("unloc"):
        areas["not located"] = so["unloc"]
    return sorted(([a, round(v)] for a, v in areas.items()), key=lambda x: -x[1])


def so_summary():
    """{"t", "total", "areas": [[area, MW]], "units": [[code, node name, MW]], "bnd": [names]} or {"err"}."""
    p = _state["payload"]
    if p and time.time() - _state["ts"] < 150 and isinstance(p.get("so"), dict) and "err" not in p["so"]:
        so, t = p["so"], p["t"]
    else:
        with _lock:
            if time.time() - _so["ts"] > SO_TTL and not _so["busy"]:
                _so["busy"] = True
                threading.Thread(target=_so_worker, name="wall-so", daemon=True).start()
            so, t = _so["data"], _so["t"]
        if so is None:
            return {"err": _so["err"] or "not fetched yet"}
    net = network()
    names = lambda i: re.sub(r"\s+(Substation|Switching Station)$", "", (net["nodes"][i][3] if net and i is not None and len(net["nodes"][i]) > 3 else "") or "")
    return {"t": t, "total": so.get("total", 0), "areas": _so_areas(net, so),
            "units": [[u[0], (u[3] if len(u) > 3 else "") or names(u[1]), u[2]] for u in (so.get("units") or [])[:5]],
            "bnd": _bnd_rep["names"] if time.time() - _bnd_rep["ts"] < BND_REP_S else []}


# ---------------------------------------------------------------- NESO / Elexon extras
def _constraints(now):
    if _state["constraints"] is not None and time.time() - _state["constraints_ts"] < CONSTRAINT_TTL:
        return _state["constraints"]
    day = (now - timedelta(hours=2)).strftime("%Y-%m-%d")
    sql = (f'SELECT "Constraint Group","Date_ Time GMT_BST","Limit_MW","Flow_MW" FROM "{NESO_CONSTRAINT_RES}" '
           f'WHERE "Date_ Time GMT_BST" >= \'{day}T00:00:00\' ORDER BY "Date_ Time GMT_BST"')
    try:
        r = _S["fetch_json"](NESO_CKAN + "?sql=" + urllib.parse.quote(sql), timeout=40)
        recs = r["result"]["records"]
        rows = [{"g": x["Constraint Group"], "t": x["Date_ Time GMT_BST"],
                 "lim": float(x["Limit_MW"]), "f": float(x["Flow_MW"])} for x in recs]
        _state["constraints"], _state["constraints_ts"] = rows, time.time()
    except Exception:
        _state["constraints_ts"] = time.time() - CONSTRAINT_TTL + 300     # retry in 5 min
    return _state["constraints"]


def _uk_local_key(dt):
    """UK-local half-hour key 'YYYY-MM-DDTHH:MM:00' (the NESO file is in GMT/BST)."""
    y = dt.year
    # BST: last Sunday of March 01:00 UTC to last Sunday of October 01:00 UTC
    def last_sun(m):
        d = datetime(y, m, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    off = 1 if last_sun(3) <= dt < last_sun(10) else 0
    loc = dt + timedelta(hours=off)
    return loc.strftime("%Y-%m-%dT%H:") + ("00" if loc.minute < 30 else "30") + ":00"


def _lolp(now):
    if _state["lolp"] is not None and time.time() - _state["lolp_ts"] < LOLP_TTL:
        return _state["lolp"]
    try:
        url = f"{_S['BMRS']}/forecast/system/loss-of-load?format=json&from={_iso(now - timedelta(minutes=30))}&to={_iso(now + timedelta(hours=12))}"
        rows = _S["_rows"](_S["fetch_json"](url, timeout=30))
        best = {}
        for r in rows:
            k = r["startTime"]
            if k not in best or r["publishTime"] > best[k]["publishTime"]:
                best[k] = r
        _state["lolp"] = [{"t": k, "drm": round(best[k]["deratedMargin"]), "lolp": best[k]["lossOfLoadProbability"]}
                          for k in sorted(best)]
        _state["lolp_ts"] = time.time()
    except Exception:
        _state["lolp_ts"] = time.time() - LOLP_TTL + 120
    return _state["lolp"]


# ---------------------------------------------------------------- Scotland / England & Wales demand split
# The flow model spreads transmission demand over the nodes. Fixed regional shares put about 9 % of it in
# Scotland at every hour, but NESO's metered split (its Demand Data Update: national demand ND and England &
# Wales demand) shows Scotland's share is 4-5 % at night and 7-8 % by day, as Scottish demand is met by its
# embedded wind. NESO publishes that split only after the event (actuals up to about a day behind), so the share
# now is estimated from the last weeks of actuals: least squares on half-hour of day x weekday/weekend, plus the
# forecast embedded wind and solar for the half-hour (published live in the same file). The fit is redone each
# time the file is read; its error on the latest whole day of actuals (fitted without that day) is reported, and
# the page's modelled transfer is compared with NESO's metered Scottish transfer once that is published.
_split = {"rows": None, "ts": 0.0, "fit": None, "check": None, "err": None}
_b6 = {"log": None, "saved": 0.0}
_gl = {"log": None, "saved": 0.0}


def _sp_key(dt):
    """(UK-local settlement date, settlement period 1-48) of a UTC datetime (clock-change days are off by two)."""
    k = _uk_local_key(dt)
    return k[:10], int(k[11:13]) * 2 + (1 if int(k[14:16]) >= 30 else 0) + 1


def _split_x(date, sp, ew, es):
    """Feature vector: one-hot (weekday/weekend x half-hour), then embedded wind and solar in GW."""
    we = 1 if datetime.strptime(date, "%Y-%m-%d").weekday() >= 5 else 0
    x = [0.0] * 98
    x[we * 48 + (min(max(sp, 1), 48) - 1)] = 1.0
    x[96], x[97] = (ew or 0) / 1000.0, (es or 0) / 1000.0
    return x


def _lstsq(X, y, ridge=1e-6):
    """Least squares by the normal equations (stdlib only; 98 unknowns)."""
    n = len(X[0])
    A = [[0.0] * n for _ in range(n)]
    b = [0.0] * n
    for x, t in zip(X, y):
        nz = [i for i, v in enumerate(x) if v]
        for i in nz:
            b[i] += x[i] * t
            Ai = A[i]
            for j in nz:
                Ai[j] += x[i] * x[j]
    for i in range(n):
        A[i][i] += ridge
    for c in range(n):                                     # Gauss-Jordan with partial pivoting
        p = max(range(c, n), key=lambda r: abs(A[r][c]))
        A[c], A[p], b[c], b[p] = A[p], A[c], b[p], b[c]
        d = A[c][c] or 1e-12
        for r in range(n):
            if r != c and A[r][c]:
                f = A[r][c] / d
                Ar, Ac = A[r], A[c]
                for k in range(c, n):
                    Ar[k] -= f * Ac[k]
                b[r] -= f * b[c]
    return [b[i] / (A[i][i] or 1e-12) for i in range(n)]


def _split_fit(rows):
    """Fit on the actual rows; returns {"w", "n", "days", "rmse_mw"} or None, and a check on the last actual day."""
    act = [r for r in rows if r[2] == "A" and r[3] > 0 and 0 < r[4] < r[3]]
    days = sorted({r[0] for r in act})
    if len(days) < SPLIT_MIN_DAYS:
        return None, None
    def fit(sub):
        X = [_split_x(r[0], r[1], r[5], r[6]) for r in sub]
        y = [(r[3] - r[4]) / r[3] for r in sub]
        return _lstsq(X, y)
    pred = lambda w, r: sum(a * b for a, b in zip(w, _split_x(r[0], r[1], r[5], r[6])))
    # check: the last whole day of actuals, fitted without it
    last = next((d for d in reversed(days) if sum(1 for r in act if r[0] == d) >= 46), None)
    check = None
    if last:
        w0 = fit([r for r in act if r[0] < last])
        e = [(pred(w0, r) * r[3]) - (r[3] - r[4]) for r in act if r[0] == last]
        check = {"day": last, "n": len(e), "mae_mw": round(sum(abs(v) for v in e) / len(e)),
                 "bias_mw": round(sum(e) / len(e))}
    w = fit(act)
    res = [pred(w, r) * r[3] - (r[3] - r[4]) for r in act]
    return {"w": w, "n": len(act), "days": len(days), "from": days[0], "to": days[-1],
            "rmse_mw": round((sum(v * v for v in res) / len(res)) ** 0.5)}, check


def _demand_rows():
    if _split["rows"] is not None and time.time() - _split["ts"] < DEMAND_TTL:
        return _split["rows"]
    sql = (f'SELECT "SETTLEMENT_DATE","SETTLEMENT_PERIOD","FORECAST_ACTUAL_INDICATOR","ND","ENGLAND_WALES_DEMAND",'
           f'"EMBEDDED_WIND_GENERATION","EMBEDDED_SOLAR_GENERATION","SCOTTISH_TRANSFER","EMBEDDED_WIND_CAPACITY",'
           f'"EMBEDDED_SOLAR_CAPACITY" FROM "{NESO_DEMAND_RES}" '
           f'ORDER BY "SETTLEMENT_DATE","SETTLEMENT_PERIOD"')
    try:
        r = _S["fetch_json"](NESO_CKAN + "?sql=" + urllib.parse.quote(sql), timeout=40)
        num = lambda v: float(v) if v not in (None, "") else 0.0
        rows = [(str(x["SETTLEMENT_DATE"])[:10], int(x["SETTLEMENT_PERIOD"]), x["FORECAST_ACTUAL_INDICATOR"],
                 num(x["ND"]), num(x["ENGLAND_WALES_DEMAND"]), num(x["EMBEDDED_WIND_GENERATION"]),
                 num(x["EMBEDDED_SOLAR_GENERATION"]), num(x["SCOTTISH_TRANSFER"]), num(x["EMBEDDED_WIND_CAPACITY"]),
                 num(x["EMBEDDED_SOLAR_CAPACITY"])) for x in r["result"]["records"]]
        fit, check = _split_fit(rows)
        _split.update(rows=rows, ts=time.time(), fit=fit, check=check, err=None if fit else "too few days of actuals")
    except Exception as e:
        _split["ts"] = time.time() - DEMAND_TTL + 600                   # retry in 10 min
        _split["err"] = f"{type(e).__name__}: {e}"[:120]
    return _split["rows"]


def _b6_load():
    if _b6["log"] is None:
        try:
            _b6["log"] = json.loads(B6_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            _b6["log"] = {}
    return _b6["log"]


def post_b6(body):
    """POST /api/wall-b6 {"t": the /api/wall time, "b6": MW}: the page's modelled transfer, kept per half-hour."""
    try:
        t = _pt(body["t"]); v = float(body["b6"])
    except Exception:
        return {"ok": False, "error": "need t and b6"}
    if abs(v) > 20000:
        return {"ok": False, "error": "out of range"}
    d, sp = _sp_key(t)
    _gsp_log(f"{d}/{sp}", body.get("gsp"))
    if isinstance(body.get("bnd"), list):                               # boundaries the page shows as constrained
        _bnd_rep.update(ts=time.time(), names=[str(x)[:8] for x in body["bnd"][:12]])
    log = _b6_load()
    e = log.setdefault(f"{d}/{sp}", {})
    e[t.strftime("%H:%M")] = round(v)                                  # one value per model run (any page)
    cut = (datetime.now(timezone.utc) - timedelta(days=B6_LOG_DAYS)).strftime("%Y-%m-%d")
    for k in [k for k in log if k[:10] < cut]:
        del log[k]
    if time.time() - _b6["saved"] > 300:
        try:
            B6_LOG_FILE.write_text(json.dumps(log, separators=(",", ":")), encoding="utf-8")
            _b6["saved"] = time.time()
        except OSError:
            pass
    return {"ok": True}


def _gsp_log(key, rows):
    """Add one run's [[node, modelled import without the measurement, measured import]] to the half-hour's sums."""
    if not isinstance(rows, list) or not rows:
        return
    if _gl["log"] is None:
        try:
            _gl["log"] = json.loads(GSP_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            _gl["log"] = {}
    e = _gl["log"].setdefault(key, {})
    for r in rows[:200]:
        try:
            nd, est, mea = int(r[0]), float(r[1]), float(r[2])
        except Exception:
            continue
        a = e.setdefault(str(nd), [0.0, 0.0, 0])
        a[0] += est; a[1] += mea; a[2] += 1
    cut = (datetime.now(timezone.utc) - timedelta(days=GSP_LOG_DAYS)).strftime("%Y-%m-%d")
    for k in [k for k in _gl["log"] if k[:10] < cut]:
        del _gl["log"][k]
    if time.time() - _gl["saved"] > 300:
        try:
            GSP_LOG_FILE.write_text(json.dumps(_gl["log"], separators=(",", ":")), encoding="utf-8")
            _gl["saved"] = time.time()
        except OSError:
            pass


def _gsp_check():
    """The model's import at the measured nodes (worked out without the measurement) against the measurements, over
    the last 48 half-hours: mean error per node and half-hour, bias, totals ratio, and correlation of the totals."""
    log = _gl["log"]
    if log is None:
        try:
            log = _gl["log"] = json.loads(GSP_LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return None
    keys = sorted(log, key=lambda k: (k[:10], int(k.split("/")[1])))[-48:]
    err, tots = [], []
    for k in keys:
        te = tm = 0.0
        for a in log[k].values():
            if a[2]:
                e_, m_ = a[0] / a[2], a[1] / a[2]
                err.append(e_ - m_); te += e_; tm += m_
        tots.append((te, tm))
    if not err:
        return None
    out = {"n": len(keys), "mae_mw": round(sum(abs(x) for x in err) / len(err)), "bias_mw": round(sum(err) / len(err)),
           "ratio": round(sum(t for t, _ in tots) / max(1.0, sum(m for _, m in tots)), 3), "corr": None}
    if len(tots) >= 6:
        xs, ys = [t for t, _ in tots], [m for _, m in tots]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        sx = sum((x - mx) ** 2 for x in xs) ** 0.5; sy = sum((y - my) ** 2 for y in ys) ** 0.5
        if sx > 0 and sy > 0:
            out["corr"] = round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy), 3)
    return out


def _b6_check(rows):
    """Modelled transfer (mean of the runs in each half-hour) against NESO's metered Scottish transfer."""
    log = _b6_load()
    act = {(r[0], r[1]): r[7] for r in rows if r[2] == "A" and r[3] > 0}
    e = []
    for k, runs in log.items():
        d, sp = k.split("/")
        tr = act.get((d, int(sp)))
        if tr is not None and len(runs) >= 10:
            e.append((sum(runs.values()) / len(runs), tr))
    if not e:
        return None
    return {"n": len(e), "mae_mw": round(sum(abs(m - t) for m, t in e) / len(e)),
            "bias_mw": round(sum(m - t for m, t in e) / len(e)),
            "ratio": round(sum(m for m, _ in e) / max(1.0, sum(t for _, t in e)), 3)}


def _split_now(now):
    """{"scot": Scotland's share of transmission demand now, "src", ...} for the page's flow model."""
    rows = _demand_rows()
    if not rows:
        return {"scot": None, "src": "none", "err": _split["err"]}
    d, sp = _sp_key(now)
    cand = [r for r in rows if r[0] == d and r[1] == sp]
    row = next((r for r in cand if r[2] == "A"), cand[0] if cand else None)
    out = {"sp": f"{d} SP{sp}", "fit": {k: v for k, v in (_split["fit"] or {}).items() if k != "w"} or None,
           "check": _split["check"], "b6check": _b6_check(rows), "err": _split["err"]}
    if row:
        out.update(ew=round(row[5]), es=round(row[6]), ewc=round(row[8]), esc=round(row[9]))
    if row and row[2] == "A" and row[3] > 0 and 0 < row[4] < row[3]:
        out.update(scot=round((row[3] - row[4]) / row[3], 4), src="actual")
    elif _split["fit"] and row:
        v = sum(a * b for a, b in zip(_split["fit"]["w"], _split_x(d, sp, row[5], row[6])))
        out.update(scot=round(v, 4) if SPLIT_RANGE[0] <= v <= SPLIT_RANGE[1] else None, src="fit")
    else:
        out.update(scot=None, src="none")
    return out


# ---------------------------------------------------------------- embedded wind and solar by node
# NESO estimates the output of embedded (distribution-connected) wind and solar for GB as a whole. Leaving it
# out of the flow model treats it as an even cut in demand everywhere, though it is concentrated (solar in the
# South West and East, wind in the north and Wales). Here NESO's estimate is shared over the nodes by the
# wind and solar capacity under each node's grid supply points in the operators' Embedded Capacity Registers
# (1 MW and above). The registers hold only part of NESO's embedded capacity (most rooftop solar is smaller), so
# only that part is placed (register capacity / NESO capacity of NESO's output); the rest stays spread with demand.
# For the same reason an operator whose register isn't loaded skews nothing: its area's share just stays spread
# with demand, as before (the missing registers are reported).
EMB_OPS = ("UKPN", "SPEN", "NPg", "ENWL", "NGED", "SSEN")
EMB_FUEL = {"wind": "wind_onshore", "solar": "solar"}


def _emb_now(split):
    """{"nodes": {node: MW}, "wind", "solar" (MW placed), "share": {...}} or {"err"}."""
    if _dno is None:
        return {"err": "dno_data.py not installed"}
    st = _dno.status() or {}
    miss = [k for k in EMB_OPS if not (st.get(k) or {}).get("ok")]
    if len(miss) == len(EMB_OPS):
        return {"err": "no Embedded Capacity Register loaded"}
    if not split or split.get("ew") is None:
        return {"err": "no NESO embedded estimate for this half-hour"}
    summ = (get_local() or {}).get("nodes") or {}
    out, info = {}, {}
    for f, key, cap in (("wind", "ew", "ewc"), ("solar", "es", "esc")):
        caps = {int(k): v["by"].get(EMB_FUEL[f], [0])[0] for k, v in summ.items()}
        tot = sum(caps.values())
        if tot <= 0 or not split.get(cap):
            continue
        frac = min(1.0, tot / split[cap])                       # the part of NESO's embedded capacity registered
        mw = split[key] * frac
        for i, c in caps.items():
            if c > 0:
                out[i] = out.get(i, 0.0) + mw * c / tot
        info[f] = {"mw": round(mw), "reg_mw": round(tot), "neso_cap": split[cap], "frac": round(frac, 3)}
    return {"nodes": {str(k): round(v, 1) for k, v in out.items() if v >= 0.5}, "by": info,
            "partial": miss or None}


# ---------------------------------------------------------------- measured GSP imports (NGED live)
# NGED publishes each of its grid supply points' import from the transmission network every 5 minutes (see
# dno_data.nged_gsp). At those nodes the flow model uses the measured import instead of a share of demand; the rest
# of the demand is spread over the other nodes as before. A node whose GSPs also serve another operator's area
# (from the Embedded Capacity Registers) is left out, as NGED's figure would be only part of its demand, as is a
# reading over MEAS_MAX_AGE old. The page also works out what it would have put there without the measurement
# and reports both (POST /api/wall-b6), so the server can score the model against the measurements.
GSP_ALIAS = {"stayleybridge": "Stalybridge", "nechells east sec1": "Nechells East", "nechells east sec2": "Nechells East",
             "lea marston": "Hams Hall"}
MEAS_MAX_AGE = 1200


def _measured(net, now_ts):
    if _dno is None or not hasattr(_dno, "nged_gsp"):
        return {"err": "dno_data.py without NGED live data", "nodes": {}}
    g = _dno.nged_gsp()
    st = _dno.nged_gsp_status()
    if not g:
        return {"err": st.get("error") or "not loaded yet", "nodes": {}}
    nodes = named_nodes(net)
    nk = [(i, _dno.gsp_key(nm)) for i, nm, lon, lat in nodes if nm]
    ops = {k: set(v.get("ops") or []) for k, v in ((get_local() or {}).get("nodes") or {}).items()}
    out, unm, stale, shared = {}, [], [], []
    for name, r in sorted(g.items()):
        k = _dno.gsp_key(GSP_ALIAS.get(name.lower(), name))
        i = next((i for i, x in nk if x and _dno._same(x, k)), None)
        if i is None:
            unm.append(name)
            continue
        if ops.get(str(i), {"NGED"}) - {"NGED"}:
            shared.append(name)
            continue
        if now_ts - r["t"] > MEAS_MAX_AGE or (not r.get("nd") and not r.get("gen")):
            stale.append(name)                            # old, or all zero (a gap in the feed, not a reading)
            continue
        e = out.setdefault(i, {"imp": 0.0, "nd": 0.0, "gen": 0.0, "gsps": [], "t": r["t"]})
        for f in ("imp", "nd", "gen"):
            e[f] += r[f] or 0.0
        e["gsps"].append(name)
        e["t"] = min(e["t"], r["t"])
    return {"src": "NGED live GSP data", "n": len(g), "err": st.get("error"), "unmatched": unm, "stale": stale,
            "shared": shared, "check": _gsp_check(),
            "nodes": {str(i): {"imp": round(e["imp"], 1), "nd": round(e["nd"], 1), "gen": round(e["gen"], 1),
                               "gsps": e["gsps"], "t": _iso(datetime.fromtimestamp(e["t"], timezone.utc))}
                      for i, e in out.items()}}


# ---------------------------------------------------------------- FUELINST reconciliation
# The units' notified / accepted levels (PN / BOALF) don't add up to Elexon's metered output by fuel (FUELINST).
# The gap is mostly grid batteries (Elexon fuel type OTHER): in frequency services they don't follow their PN,
# and at the evening peak their metered output was 2 GW above their PNs on 28 Oct. For each FUELINST fuel the
# gap at FUELINST's time (the units' levels then against FUELINST) is spread over that fuel's located units, by
# registered capacity for batteries and pumped storage (whose PN says little about which are running) and by
# output now for the rest (so a stopped station isn't given any), each kept within its capacity. It goes into
# the flow model as extra injection at those nodes; the units' published levels shown in the panels are unchanged.
RECON_FUELS = ("CCGT", "OCGT", "OIL", "COAL", "NUCLEAR", "WIND", "PS", "NPSHYD", "OTHER", "BIOMASS")
RECON_BY_CAP = ("OTHER", "PS")
RECON_MIN_MW = 30


def _fuelinst_all():
    """({FUELINST fuel: MW}, publish time) for the generation fuels (not interconnectors)."""
    snap = (_S.get("_cache") or {}).get("snap")
    gen = snap.get("generation") if snap and time.time() - (_S["_cache"].get("ts") or 0) < 180 else None
    if not gen:
        gen = _S["get_generation"]()
    if not gen:
        return {}, None
    return {f["code"]: f["mw"] for f in gen["fuels"] if f["code"] in RECON_FUELS}, gen.get("publishTime")


def _recon(reg, un, pn, bo, lv, nts, skip_nodes=()):
    """{"t", "by": {fuel: [FUELINST, units' levels, applied]}, "nodes": {node: MW}} or {"err"}."""
    fi, pub = _fuelinst_all()
    if not fi or not pub:
        return {"err": "no FUELINST"}
    t_fi = _pt(pub).timestamp() - 150                     # middle of FUELINST's 5-minute period
    if nts - t_fi > 900:
        return {"err": "FUELINST more than 15 minutes old"}
    by_ft, model = {}, {}
    for u, r in reg.items():
        ft = (r or {}).get("ft")
        if ft not in fi:
            continue
        v_then, _ = lv(u, t_fi) if (u in pn or u in bo) else (None, False)
        v_now, _ = lv(u, nts) if (u in pn or u in bo) else (None, False)
        model[ft] = model.get(ft, 0.0) + (v_then or 0.0)
        nd = un.get(u)
        try:
            cap = float(r.get("cap") or 0)
        except (TypeError, ValueError):
            cap = 0.0
        if nd is not None and cap > 0 and not (u.startswith("E_") and nd[0] in skip_nodes):
            by_ft.setdefault(ft, []).append((nd[0], v_now or 0.0, cap))     # (an embedded unit behind a measured GSP is in its import)
    out, by = {}, {}
    for ft, mw in fi.items():
        res = mw - model.get(ft, 0.0)
        pool = by_ft.get(ft) or []
        applied = 0.0
        if abs(res) >= RECON_MIN_MW and pool:
            w = [c if ft in RECON_BY_CAP else max(v, 0.0) for _, v, c in pool]
            if sum(w) <= 0:
                w = [c for _, _, c in pool]
            tw = sum(w)
            for (nd, v, c), wi in zip(pool, w):
                add = res * wi / tw
                add = max(-c - v, min(c - v, add))             # stay within the unit's capacity
                if add:
                    out[nd] = out.get(nd, 0.0) + add
                    applied += add
        by[ft] = [round(mw), round(model.get(ft, 0.0)), round(applied)]
    return {"t": pub, "by": by, "nodes": {str(k): round(v, 1) for k, v in out.items() if abs(v) >= 0.5}}


_fi_last = {"ic": None, "t": None, "ts": 0.0}
FI_HOLD_S = 600                 # keep the last good interconnector flows this long through a FUELINST gap


def _fuelinst_ic():
    """Interconnector flows (MW, + = import) from the server's FUELINST snapshot. Through a gap in FUELINST (a
    failed fetch) the last good flows are kept for up to FI_HOLD_S, so the map doesn't drop several GW of
    interconnector flow for one run and jump back."""
    snap = (_S.get("_cache") or {}).get("snap")
    gen = snap.get("generation") if snap and time.time() - (_S["_cache"].get("ts") or 0) < 180 else None
    if not gen:
        gen = _S["get_generation"]()
    ic = {f["code"]: f["mw"] for f in gen["fuels"] if f["code"].startswith("INT")} if gen else {}
    if ic:
        _fi_last.update(ic=ic, t=gen.get("publishTime"), ts=time.time())
        return ic, gen.get("publishTime")
    if _fi_last["ic"] and time.time() - _fi_last["ts"] < FI_HOLD_S:
        return _fi_last["ic"], _fi_last["t"]
    return {}, None


def _cap_at(d, t):
    """Available capacity at time t: from the outage profile when the report has one."""
    best = None
    for p in d.get("outageProfile") or []:
        try:
            if _pt(p["startTime"]).timestamp() <= t < _pt(p["endTime"]).timestamp():
                best = p.get("capacity")
        except Exception:
            continue
    return d.get("availableCapacity") if best is None else best


def _remit(now, un, locs, errors):
    """REMIT unavailability reports for GB units and interconnectors that are reduced now or
    start within REMIT_AHEAD_H hours. Only each report's latest revision is listed by Elexon;
    cancelled ("Dismissed") reports and long-term ones (> REMIT_LONG_D days) are left out."""
    if _state["remit"] is not None and time.time() - _state["remit_ts"] < REMIT_TTL:
        return _state["remit"]
    nts = now.timestamp()
    try:
        url = (f"{_S['BMRS']}/remit/list/by-event?format=json&from={_iso(now)}"
               f"&to={_iso(now + timedelta(hours=REMIT_AHEAD_H))}")
        listed = [r["id"] for r in _S["_rows"](_S["fetch_json"](url, timeout=40))]
        need = [i for i in listed if i not in _remit_msgs]
        for k in range(0, len(need), REMIT_BATCH):
            q = "&".join(f"messageId={i}" for i in need[k:k + REMIT_BATCH])
            for d in _S["_rows"](_S["fetch_json"](f"{_S['BMRS']}/remit?format=json&{q}", timeout=40)):
                _remit_msgs[d["id"]] = d
        keep = set(listed)
        for i in [i for i in _remit_msgs if i not in keep]:
            del _remit_msgs[i]
    except Exception as e:
        errors.append(f"REMIT: {type(e).__name__}")
        _state["remit_ts"] = time.time() - REMIT_TTL + 60                  # retry in a minute
        return _state["remit"]
    out = []
    for d in _remit_msgs.values():
        if d.get("eventStatus") != "Active" or (d.get("biddingZone") or GB_ZONE) != GB_ZONE:
            continue
        try:
            t0 = _pt(d["eventStartTime"]).timestamp()
            t1 = _pt(d["eventEndTime"]).timestamp() if d.get("eventEndTime") else None
        except Exception:
            continue
        if t1 is None or t1 <= nts or t1 - nts > REMIT_LONG_D * 86400:
            continue
        soon = t0 > nts
        normal, avail = d.get("normalCapacity"), _cap_at(d, t0 + 1 if soon else nts)
        if normal is None or avail is None or normal <= 0 or normal - avail < 1:
            continue
        asset = d.get("assetId") or ""
        e = {"id": d["id"], "asset": asset, "name": asset, "node": None, "ic": None, "dir": None,
             "fuel": d.get("fuelType"), "kind": (d.get("eventType") or "").replace(" unavailability", "").lower(),
             "normal": round(normal), "avail": round(max(avail, 0)), "lost": round(normal - max(avail, 0)),
             "planned": d.get("unavailabilityType") == "Planned", "cause": (d.get("cause") or "")[:60],
             "start": d.get("eventStartTime"), "end": d.get("eventEndTime"), "soon": soon,
             "pub": d.get("publishTime")}
        m = re.match(r"I_I\w([GD])-(\w+)", asset)
        if m:
            e["ic"] = IC_REMIT.get(m.group(2))
            e["dir"] = "imp" if m.group(1) == "G" else "exp"
        else:
            au = d.get("affectedUnit") or ""
            for code in (asset, "T_" + au, "E_" + au):
                if code in un:
                    e["asset"], e["node"] = code, un[code][0]
                    break
            e["name"] = ((locs.get(e["asset"]) or {}).get("name") or au or asset)[:40]
        out.append(e)
    out.sort(key=lambda e: (e["soon"], -e["lost"]))
    _state["remit"], _state["remit_ts"] = out, time.time()
    return out


# ---------------------------------------------------------------- main refresh
def _refresh():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    nts = now.timestamp()
    net = network()
    if net is None:
        return {"error": "grid_network.json not found next to grid_server.py (run build_network.py)"}
    errors = []
    try:
        pn = _seg(_fetch_stream("PN", now - timedelta(minutes=20), now + timedelta(minutes=65)))
    except Exception as e:
        return {"error": f"PN fetch failed: {type(e).__name__}: {e}"}
    try:
        bo = _seg(_fetch_stream("BOALF", now - timedelta(minutes=90), now + timedelta(minutes=65)))
    except Exception as e:
        bo = {}
        errors.append(f"BOALF: {type(e).__name__}")
    locs = _S["_load_bmu_locations"]() or {}
    if _osm is not None:
        try:
            locs = _osm.unit_locations(locs)
        except Exception as e:
            errors.append(f"OSM positions: {type(e).__name__}")
    un = _unit_nodes(locs, net)
    T = {"now": nts, "m15": nts - 900, "p30": nts + 1800, "p60": nts + 3600}

    def lv(u, t):
        if u in bo:
            v = _level(bo[u], t, now_ts=nts)
            if v is not None:
                return v, True
        v = _level(pn.get(u, []), t)
        return v, False

    units = {}            # node -> list
    unloc_mw, loc_mw = 0.0, 0.0
    try:
        reg = (_S["_load_bmu_registry"]() or {}) if _S.get("_load_bmu_registry") else {}
    except Exception:
        reg = {}
    # offshore links: their farms' units, for each link's output now (index into network "offlinks")
    links, _ = _offshore_links(net)
    _d = _osm.atlas() if _osm is not None else None
    sid_link = {x["site"]: i for i, x in enumerate(links)}
    link_unit = {c: sid_link[s] for c, s in (_d["bmu"].items() if _d else ()) if s in sid_link}
    name_link = {x["name"]: i for i, x in enumerate(links)}
    for u in set(pn) | set(bo):
        ou = _offshore_unit(u)
        if ou and ou[0] in name_link:
            link_unit[u] = name_link[ou[0]]
    link_mw = {}
    ic_pn = {}            # token -> [now, p30, p60]
    for u in set(pn) | set(bo):
        if u.startswith("I_"):
            m = re.match(r"I_I(\w)[GD]-", u)
            if m:
                a = ic_pn.setdefault(m.group(1), [0.0, 0.0, 0.0, 0, 0, 0])   # sums + counts
                for i, k in enumerate(("now", "p30", "p60")):
                    v, _ = lv(u, T[k])
                    if v is not None:
                        a[i] += v
                        a[3 + i] += 1
            continue
        if u.startswith("2__") or u.startswith("C__") or u.startswith("V__"):
            continue                                   # supplier / embedded aggregates: not at a node
        vnow, instr = lv(u, T["now"])
        if vnow is None:
            continue
        vm15, _ = lv(u, T["m15"]); vp30, _ = lv(u, T["p30"]); vp60, _ = lv(u, T["p60"])
        if max(abs(vnow), abs(vm15 or 0), abs(vp30 or 0), abs(vp60 or 0)) < 1:
            continue
        nd = un.get(u)
        if nd is None:
            if vnow > 0:
                unloc_mw += vnow
            continue
        loc = locs.get(u) or {}
        fuel = FUEL_KEY.get(loc.get("type"))
        if not fuel and _S.get("_classify_bmu"):
            try:
                fuel = (_S["_classify_bmu"](u)[2] or "other").lower()
            except Exception:
                fuel = "other"
        if vnow > 0:
            loc_mw += vnow
        units.setdefault(nd[0], []).append([u, round(vnow), None if vm15 is None else round(vm15),
                                            None if vp30 is None else round(vp30),
                                            None if vp60 is None else round(vp60), fuel or "other",
                                            1 if instr else 0, (loc.get("name") or "")[:40],
                                            loc.get("pos") or "tv", loc.get("moved"),   # position source, km moved
                                            _cap(reg, u)])
        if u in link_unit:
            link_mw[link_unit[u]] = link_mw.get(link_unit[u], 0.0) + vnow
        h = _unit_hist.setdefault(u, deque(maxlen=UNIT_HIST_MIN + 5))
        if not h or h[-1][0] != _iso(now)[:16]:
            h.append((_iso(now)[:16], round(vnow)))
    # interconnectors
    fi, fi_t = {}, None
    try:
        fi, fi_t = _fuelinst_ic()
    except Exception as e:
        errors.append(f"FUELINST: {type(e).__name__}")
    tok = _state["ic_tok"]
    used = set(v for v in tok.values())
    for t_, sums in ic_pn.items():                       # settle each token against FUELINST
        cands = [c for c in IC_TOKEN.get(t_, []) if c in fi]
        if t_ in tok or not cands:
            continue
        c = min(cands, key=lambda c: abs(fi[c] - sums[0]) + (1e6 if c in used else 0))
        if abs(fi[c] - sums[0]) <= max(IC_VERIFY_MW, abs(fi[c]) * IC_VERIFY_PCT / 100):
            tok[t_] = c; used.add(c)
    ic = {}
    prev = _ic_hist[-16]["ic"] if len(_ic_hist) >= 16 else (_ic_hist[0]["ic"] if _ic_hist else {})
    for code, mw in fi.items():
        ent = {"mw": mw, "m15": prev.get(code), "node": net["ic"].get(code)}
        tks = [t_ for t_, c in tok.items() if c == code]
        if tks:
            s = [sum(ic_pn[t_][i] for t_ in tks if t_ in ic_pn) for i in range(6)]
            ok = abs(fi[code] - s[0]) <= max(IC_VERIFY_MW, abs(fi[code]) * IC_VERIFY_PCT / 100)
            if ok:
                # a horizon counts only if (nearly) every trader with a level now has one then
                for i, k in ((1, "p30"), (2, "p60")):
                    if s[3 + i] >= 0.8 * s[3] and s[3] > 0:
                        ent[k] = round(s[i])
            ent["verified"] = ok
        ic[code] = ent
    try:
        meas = _measured(net, nts)
    except Exception as e:
        meas = {"err": f"{type(e).__name__}: {e}"[:120], "nodes": {}}
    try:
        recon = _recon(reg, un, pn, bo, lv, nts, {int(k) for k in meas.get("nodes") or {}})
    except Exception as e:
        recon = {"err": f"{type(e).__name__}: {e}"[:120]}
    try:
        so = _so_turndown(un, pn, bo, nts, locs)
        so["areas"] = _so_areas(net, so)
    except Exception as e:
        so = {"err": f"{type(e).__name__}: {e}"[:120]}
    # constraints for this half-hour (+ the next two)
    rows = _constraints(now) or []
    keys = [_uk_local_key(now + timedelta(minutes=30 * k)) for k in range(3)]
    bnd = {k: [r for r in rows if r["t"] == k] for k in keys}
    spl = _split_now(now)
    payload = {
        "t": _iso(now), "net_built": net.get("built"), "fuelinst_t": fi_t,
        "units": {str(k): v for k, v in units.items()}, "ic": ic,
        "offlinks": {str(k): round(v) for k, v in link_mw.items()},
        "local_live": _local_live(net),
        "boundaries": {"keys": keys, "rows": bnd},
        "lolp": _lolp(now),
        "split": spl,
        "emb": _emb_now(spl),
        "recon": recon,
        "meas": meas,
        "so": so,
        "outages": _remit(now, un, locs, errors),
        "coverage": {"located_mw": round(loc_mw), "unlocated_mw": round(unloc_mw),
                     "units": sum(len(v) for v in units.values())},
        "errors": errors,
    }
    if not _ic_hist or _ic_hist[-1]["t"][:16] != payload["t"][:16]:
        _ic_hist.append({"t": payload["t"], "ic": {k: e["mw"] for k, e in ic.items()}})
    return payload


def _local_live(net):
    """Northern Powergrid's live 132 kV circuit readings by node ({node: [[site, feeder, name, value, time, unit,
    downstream, kV]]}), or {} when dno_data.py or its key isn't there."""
    if _dno is None:
        return {}
    try:
        return {str(k): v for k, v in _dno.live_by_node(named_nodes(net)).items()}
    except Exception:
        return {}


def _cap(reg, code):
    """A unit's registered generation capacity (MW) from Elexon's BM unit list, or None."""
    try:
        c = float((reg.get(code) or {}).get("cap"))
        return round(c) if c > 0 else None
    except (TypeError, ValueError):
        return None


def get_wall():
    """Payload for /api/wall (cached ~1 min; starts the sampler on first use)."""
    _state["last_req"] = time.time()
    _start_sampler()
    with _lock:
        if _state["payload"] is None or time.time() - _state["ts"] > WALL_TTL:
            p = _refresh()
            if "error" not in p or _state["payload"] is None:
                _state["payload"], _state["ts"] = p, time.time()
        return _state["payload"]


def get_history(node):
    """Units at one node: last 2 h actual (sampled) + scheduled path for the next hour."""
    p = _state["payload"] or {}
    us = (p.get("units") or {}).get(str(node), [])
    out = []
    for u in us:
        h = list(_unit_hist.get(u[0], []))
        out.append({"code": u[0], "fuel": u[5], "name": u[7], "instructed": u[6],
                    "pts": h, "ahead": [[p.get("t"), u[1]], ["+30", u[3]], ["+60", u[4]]]})
    return {"t": p.get("t"), "node": node, "units": out}


# ---------------------------------------------------------------- sampler
def _sampler():
    while True:
        time.sleep(SAMPLE_S)
        if time.time() - _state["last_req"] > IDLE_STOP_S:
            _state["sampler"] = False
            return
        try:
            with _lock:
                if time.time() - _state["ts"] > WALL_TTL:
                    p = _refresh()
                    if "error" not in p:
                        _state["payload"], _state["ts"] = p, time.time()
        except Exception:
            pass


def _start_sampler():
    if not _state["sampler"]:
        _state["sampler"] = True
        threading.Thread(target=_sampler, name="wall-sampler", daemon=True).start()


_start_names_task()          # background node naming runs whenever the server has this module
