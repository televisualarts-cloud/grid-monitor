"""GB transmission wall (/wall): live node injections for the network mimic.

Build 261024.1  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Standard library only. Optional companion to grid_server.py; the server runs
unchanged without it. grid_server calls attach(globals()) once at start-up so this
module can reuse its fetch helper, BMU location cache and snapshot.

What it serves
  /api/wall-network   the static network (grid_network.json, built by build_network.py)
  /api/wall           every BM unit's output now, 15 min ago and scheduled +30/+60 min,
                      grouped by network node; interconnector flows (+ verified
                      schedules); NESO day-ahead boundary rows; Elexon derated margin/LoLP
  /api/wall-history   one node's units over the last 2 h plus the scheduled path ahead
  ...and in /api/wall: "outages", the REMIT unavailability reports (Elexon) for units and
                      interconnectors that are reduced now or will be within 24 h. These are
                      operators' own reports, not estimates.

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
IC_VERIFY_MW = 100.0          # a schedule is trusted only if its "now" matches FUELINST within
IC_VERIFY_PCT = 15.0          # max(100 MW, 15 %)

# Interconnector trader BM units are named I_I<x>G-/I_I<x>D-…; <x> -> FUELINST code.
# Ambiguous letters list several candidates and are settled by matching FUELINST.
IC_TOKEN = {"F": ["INTFR"], "2": ["INTIFA2"], "L": ["INTELEC"], "V": ["INTVKL"], "S": ["INTNSL"],
            "W": ["INTNSL"], "E": ["INTEW"], "G": ["INTGRNL"], "M": ["INTIRL"],
            "B": ["INTNEM", "INTNED"], "N": ["INTNED", "INTNEM"]}

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
    for k in ("fetch_json", "BMRS", "_load_bmu_locations", "_classify_bmu", "get_generation", "_cache", "_rows"):
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
        _state["net"] = json.loads(NET_FILE.read_text(encoding="utf-8"))
        _state["net_mtime"] = mt
        _state["unit_node"] = None
    return _state["net"]


def network_bytes():
    try:
        return NET_FILE.read_bytes()
    except OSError:
        return None


def _unit_nodes(locs, net):
    """BM unit -> node index (nearest main-GB node). Shetland units feed the mainland
    through the Shetland HVDC landing at Blackhillock, so they are placed there."""
    key = (id(locs), len(locs), _state["net_mtime"])
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
    _state["unit_node"], _state["unit_node_key"] = out, key
    return out


# ---------------------------------------------------------------- PN / BOALF
def _fetch_stream(ds, t0, t1):
    url = f"{_S['BMRS']}/datasets/{ds}/stream?format=json&from={_iso(t0)}&to={_iso(t1)}"
    r = _S["fetch_json"](url, timeout=45)
    return r if isinstance(r, list) else _S["_rows"](r)


def _seg(rows):
    """Pre-parse rows into per-unit segment lists [(t0, t1, l0, l1, acc_no, acc_time)]."""
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
                                      _pt(at).timestamp() if at else 0.0))
    return out


def _level(segs, t, now_ts=None):
    """Level at time t. PN: the last-listed covering segment wins. BOALF (now_ts given):
    the highest acceptance number already issued by now_ts whose profile covers t."""
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
        return None
    span = (best[1] - best[0]) or 1.0
    f = max(0.0, min(1.0, (t - best[0]) / span))
    return best[2] + (best[3] - best[2]) * f


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


def _fuelinst_ic():
    """Interconnector flows (MW, + = import) from the server's FUELINST snapshot."""
    snap = (_S.get("_cache") or {}).get("snap")
    gen = snap.get("generation") if snap and time.time() - (_S["_cache"].get("ts") or 0) < 180 else None
    if not gen:
        gen = _S["get_generation"]()
    if not gen:
        return {}, None
    return {f["code"]: f["mw"] for f in gen["fuels"] if f["code"].startswith("INT")}, gen.get("publishTime")


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
                                            1 if instr else 0, (loc.get("name") or "")[:40]])
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
    # constraints for this half-hour (+ the next two)
    rows = _constraints(now) or []
    keys = [_uk_local_key(now + timedelta(minutes=30 * k)) for k in range(3)]
    bnd = {k: [r for r in rows if r["t"] == k] for k in keys}
    payload = {
        "t": _iso(now), "net_built": net.get("built"), "fuelinst_t": fi_t,
        "units": {str(k): v for k, v in units.items()}, "ic": ic,
        "boundaries": {"keys": keys, "rows": bnd},
        "lolp": _lolp(now),
        "outages": _remit(now, un, locs, errors),
        "coverage": {"located_mw": round(loc_mw), "unlocated_mw": round(unloc_mw),
                     "units": sum(len(v) for v in units.values())},
        "errors": errors,
    }
    if not _ic_hist or _ic_hist[-1]["t"][:16] != payload["t"][:16]:
        _ic_hist.append({"t": payload["t"], "ic": {k: e["mw"] for k, e in ic.items()}})
    return payload


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
