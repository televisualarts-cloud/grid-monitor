"""Gas storage: UK storage and LNG stock, per-site flows and estimated fill, and
European storage from GIE AGSI+.

Build 261028.3  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Standard library only.

Sources:
  * National Gas Data Portal API (no key), POST /publications/gasday with ids
    from National Gas's "API Data Item List" (v2.3.1):
      - per storage site, daily (D+1, ~12:30 UK): "NTS Energy Offtaken, <site>,
        Storage" (gas injected) and "System Entry Energy, <site>, D+1" (withdrawn);
      - "Storage, Daily Aggregated Stock level, D+1" (all operating sites, kWh,
        at the start of the gas day; Rough is not in it while its storage is paused);
      - LNG: "Daily Aggregated Stock level / Inflows / Outflows, D+1" (all
        terminals together; inflow = gas unloaded from tankers);
      - "Deliverability, <site>" (declared maximum withdrawal, kWh/d);
      - "Demand Actual, NTS, D+1 (Energy)" (kWh/d): its 12-month average is GB's
        "average daily use" for days of storage on the same basis as GIE's countries
        (NTS demand includes exports to Ireland and the continent).
  * GIE AGSI+ (agsi.gie.eu): EU total and countries, daily, published about
    19:30 and 23:00 Brussels time. Needs a free key, saved as "GIE" in
    powercut_keys.json alongside the electricity operator keys.

Per-site fill is ESTIMATED: National Gas publishes the GB total stock but not each
site's. Each site's stock moves by its own daily in/out flows, it can never go
below empty or above its working capacity, and the sites must add up to the
published total. Over a year of flows those limits pin each site to a range.
Capacities: GIE's working gas volume where the site reports to GIE, otherwise
Ofgem's GB gas storage table (correct as at 20 January 2025). mcm -> GWh at
10.97 kWh/m3, the same calorific value gas_probe.py uses.

Rough: Centrica paused storage there in 2025. While National Gas shows no
storage injection or withdrawal at Rough for a year it is treated as paused:
left out of capacity, the estimate and "storage" (its live flow is production).

Public entry points:
  get_gas_storage(demand_mcm_d=None) -> dict   (cheap: last built payload)
  gie_key_status() -> dict
  save_gie_key(key) -> dict                     (tests the key with GIE first)
"""

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE_FILE = HERE / "gas_storage_store.json"
KEYS_FILE = HERE / "powercut_keys.json"       # shared with powercuts.py
UA = {"User-Agent": "uk-grid-monitor/1.0 (gas storage)"}
NG_URL = "https://api.nationalgas.com/operationaldata/v1/publications/gasday"
GIE_URL = "https://agsi.gie.eu/api"

GWH_PER_MCM = 10.97           # kWh per m3, as gas_probe.KWH_PER_M3
NG_BACKFILL_DAYS = 730        # National Gas history fetched on first run
NG_KEEP_DAYS = 800
EST_WINDOW_DAYS = 365         # flows used for the per-site fill estimate
NG_IDS_PER_CALL = 4           # larger requests are refused by the API
NG_SPACING_S = 6.0            # between National Gas requests (shared IP with the server's own gas feed)
NG_BACKFILL_SPACING_S = 15.0  # gentler still for the one-off first-run history fetch
NG_BLOCK_S = 600              # back off this long after a 429
NG_SLOTS_UK = ((12, 45), (16, 15))   # after the D+1 publications
GIE_SPACING_S = 1.2
GIE_SLOTS_BRU = ((19, 40), (23, 10))  # after GIE's 19:30 and 23:00 updates
GIE_EU_FROM = "2020-10-01"    # enough for a 5-year band over the last 12 months
GIE_COUNTRY_DAYS = 400
LOOP_S = 300
STALE_GIE_DAYS = 5            # GIE site figure unchanged this long while National Gas shows flow -> stale

# (key, name, injected id, withdrawn id, deliverability id, Ofgem WGV mcm or None,
#  GIE (company, facility) or None, live instantaneous-flow site names)
SITES = [
    ("stublach", "Stublach", "PUBOBJ11554", "PUBOBJ11541", "PUBOBJ2458", 400,
     ("48XSTORENGYUK01P", "21W000000000101H"), ("STUBLACH",)),
    ("hornsea", "Hornsea", "PUBOBJ1007", "PUBOBJ620", "PUBOBJ2450", 308, None, ("HORNSEA",)),
    ("aldbrough", "Aldbrough", "PUBOBJ1004", "PUBOBJ614", "PUBOBJ2455", 282, None, ("ALDBROUGH",)),
    ("humbly", "Humbly Grove", "PUBOBJ1008", "PUBOBJ621", "PUBOBJ2449", 254,
     ("55XHUMBLYGROVE1H", "55WHUMBLY1GROVER"), ("HUMBLY GROVE",)),
    ("holford", "Holford", "PUBOBJ2001", "PUBOBJ2011", "PUBOBJ2456", 242,
     ("21X0000000013716", "21W000000000112C"), ("HOLFORD",)),
    ("hatfield", "Hatfield Moor", "PUBOBJ1005", "PUBOBJ616", "PUBOBJ2453", 70, None, ("HATFIELD MOOR",)),
    ("hilltop", "Hill Top", "PUBOBJ2040", "PUBOBJ2049", "PUBOBJ2457", 59, None, ("HILLTOP",)),
    ("holehouse", "Hole House Farm", "PUBOBJ1006", "PUBOBJ619", "PUBOBJ2454", None, None, ("HOLE HOUSE FARM",)),
    ("rough", "Rough", "PUBOBJ1009", "PUBOBJ624", "PUBOBJ2452", 1500, None, ("EASINGTON ROUGH ST",)),
]
STOCK_ID = "PUBOBJ330"
DEMAND_ID = "PUBOBJ1030"      # Demand Actual, NTS, D+1 (Energy), kWh per gas day
LNG_STOCK, LNG_IN, LNG_OUT = "PUBOBJ336", "PUBOBJ337", "PUBOBJ338"
FLOW_IDS = [s[2] for s in SITES] + [s[3] for s in SITES] + [STOCK_ID, LNG_STOCK, LNG_IN, LNG_OUT, DEMAND_ID]
_demand_tried = 0.0
DELIV_IDS = [s[4] for s in SITES]

COUNTRIES = [("DE", "Germany"), ("IT", "Italy"), ("NL", "Netherlands"), ("FR", "France"),
             ("AT", "Austria"), ("ES", "Spain"), ("PL", "Poland"), ("HU", "Hungary"),
             ("CZ", "Czechia"), ("SK", "Slovakia"), ("DK", "Denmark"), ("BE", "Belgium")]
GIE_FIELDS = ("gasInStorage", "full", "injection", "withdrawal", "workingGasVolume",
              "consumption", "status")

_lock = threading.Lock()
_store = None
_payload = {"status": "building"}
_thread = None
_wake = threading.Event()
_last_error = None
_ng_last = 0.0
_ng_block_until = 0.0
_gie_last = 0.0


class GieKeyError(Exception):
    pass


# ---------------------------------------------------------------- time helpers
def _last_sun(y, mo):
    d = datetime(y, mo, 31, 1, tzinfo=timezone.utc)
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _uk_offset_h(dt):
    """UK clock offset (h) at a UTC instant. Stdlib-only (no tz database on Windows)."""
    return 1 if _last_sun(dt.year, 3) <= dt < _last_sun(dt.year, 10) else 0


def _uk_now():
    n = datetime.now(timezone.utc)
    return n + timedelta(hours=_uk_offset_h(n))


def _gas_day_today():
    """Gas day runs 05:00-05:00 UK clock."""
    return (_uk_now() - timedelta(hours=5)).date().isoformat()


def _add_days(ds, n):
    return (date.fromisoformat(ds) + timedelta(days=n)).isoformat()


def _last_slot_ep(slots, offset_h):
    """UTC epoch of the most recent local-clock slot (hh, mm) that has passed."""
    now = datetime.now(timezone.utc)
    local = now + timedelta(hours=offset_h)
    best = None
    for back in (0, 1):
        day = (local - timedelta(days=back)).date()
        for hh, mm in slots:
            t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=timezone.utc) - timedelta(hours=offset_h)
            if t <= now and (best is None or t > best):
                best = t
    return best.timestamp() if best else 0


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _iso(ep):
    return datetime.fromtimestamp(ep, tz=timezone.utc).isoformat(timespec="seconds") if ep else None


# ---------------------------------------------------------------- store
def _load():
    global _store
    if _store is not None:
        return _store
    try:
        s = json.loads(STORE_FILE.read_text())
        if isinstance(s, dict) and isinstance(s.get("ng"), dict):
            _store = s
            return _store
    except Exception:
        pass
    _store = {"ng": {}, "ng_t": 0, "ng_from": None, "deliv": {}, "deliv_t": 0,
              "gie": {"eu": {}, "cty": {}, "site": {}, "t": 0, "status": None, "err": None},
              "key": {"status": None, "checked": None}}
    return _store


def _save():
    s = _store
    cut = _add_days(_gas_day_today(), -NG_KEEP_DAYS)
    for ser in s["ng"].values():
        for d in [d for d in ser if d < cut]:
            del ser[d]
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, separators=(",", ":")))
    tmp.replace(STORE_FILE)


# ---------------------------------------------------------------- keys
def _load_keys():
    try:
        import powercuts
        return dict(powercuts.load_keys())
    except Exception:
        try:
            return dict(json.loads(KEYS_FILE.read_text(encoding="utf-8")))
        except Exception:
            return {}


def _save_keys(keys):
    try:
        import powercuts
        powercuts.save_keys(keys)
        return
    except Exception:
        pass
    tmp = KEYS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys, indent=1), encoding="utf-8")
    tmp.replace(KEYS_FILE)


def _gie_key():
    return (_load_keys().get("GIE") or "").strip() or None


# ---------------------------------------------------------------- National Gas
def _ng(ids, fr, to, spacing=NG_SPACING_S):
    """{id: {gas_day: value}} for ids over [fr, to]. Raises on HTTP error."""
    global _ng_last, _ng_block_until
    if time.time() < _ng_block_until:
        raise RuntimeError("National Gas rate limit back-off")
    wait = spacing - (time.time() - _ng_last)
    if wait > 0:
        time.sleep(wait)
    body = json.dumps({"fromDate": fr, "toDate": to, "publicationIds": list(ids),
                       "latestValue": "Y"}).encode()
    req = urllib.request.Request(NG_URL, data=body,
                                 headers={**UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 429:
            _ng_block_until = time.time() + NG_BLOCK_S
        raise
    finally:
        _ng_last = time.time()
    out = {}
    for blk in data or []:
        ser = out.setdefault(blk.get("publicationId"), {})
        for p in blk.get("publications") or []:
            d = str(p.get("applicableFor") or "")[:10]
            v = _f(p.get("value"))
            if d and v is not None:
                ser[d] = v
    return out


def _ng_fetch(ids, fr, to, into, spacing=NG_SPACING_S):
    for i in range(0, len(ids), NG_IDS_PER_CALL):
        got = _ng(ids[i:i + NG_IDS_PER_CALL], fr, to, spacing)
        for pid, ser in got.items():
            into.setdefault(pid, {}).update(ser)


def _refresh_ng(force=False):
    s = _store
    today = _gas_day_today()
    due = force or not s["ng_t"] or s["ng_t"] < _last_slot_ep(NG_SLOTS_UK, _uk_offset_h(datetime.now(timezone.utc))) \
        or time.time() - s["ng_t"] > 6 * 3600
    if not due:
        return False
    target = _add_days(today, -NG_BACKFILL_DAYS)
    if not s.get("ng_from") or s["ng_from"] > target:
        # first run: backfill newest-first in year-long chunks, saving progress after each
        # chunk so a rate-limit stop resumes where it left off instead of starting again
        b = _add_days(s["ng_from"], -1) if s.get("ng_from") else _add_days(today, 1)
        while b > target:
            if (date.fromisoformat(b) - date.fromisoformat(target)).days < 30:
                s["ng_from"] = target           # a few days short of the target: not worth a fetch
                break
            a = max(_add_days(b, -364), target)
            _ng_fetch(FLOW_IDS, a, b, s["ng"], NG_BACKFILL_SPACING_S)
            s["ng_from"] = a
            _save()
            b = _add_days(a, -1)
    else:
        _ng_fetch(FLOW_IDS, _add_days(today, -12), _add_days(today, 1), s["ng"])
    if time.time() - s.get("deliv_t", 0) > 6 * 3600:
        _ng_fetch(DELIV_IDS, _add_days(today, -6), _add_days(today, 1), s["deliv"])
        s["deliv_t"] = time.time()
    s["ng_t"] = time.time()
    return True


# ---------------------------------------------------------------- GIE
def _gie(params, key, one_page=False, timeout=60):
    """All pages of an AGSI query (or just the first). Raises GieKeyError if the key is refused."""
    global _gie_last
    rows, page = [], 1
    while True:
        wait = GIE_SPACING_S - (time.time() - _gie_last)
        if wait > 0:
            time.sleep(wait)
        q = dict({"size": 300}, **params, page=page)
        req = urllib.request.Request(GIE_URL + "?" + urllib.parse.urlencode(q),
                                     headers={**UA, "x-key": key})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise GieKeyError("GIE refused the key")
            raise
        finally:
            _gie_last = time.time()
        if isinstance(d, dict) and str(d.get("error") or "").lower().startswith("access denied"):
            raise GieKeyError(d.get("message") or "GIE refused the key")
        rows += d.get("data") or []
        if one_page or page >= int(d.get("last_page") or 1):
            return rows
        page += 1


def _keep(rows):
    out = {}
    for r in rows:
        d = str(r.get("gasDayStart") or "")[:10]
        if d:
            out[d] = {k: r.get(k) for k in GIE_FIELDS}
    return out


def _refresh_gie(force=False):
    g = _store["gie"]
    key = _gie_key()
    if not key:
        g["status"], g["err"] = "no_key", None
        return False
    slot = _last_slot_ep(GIE_SLOTS_BRU, _uk_offset_h(datetime.now(timezone.utc)) + 1)
    retry = g["status"] in ("error", "bad_key", "no_key", None) and time.time() - g["t"] > 1800
    due = force or not g["t"] or g["t"] < slot or retry or time.time() - g["t"] > 12 * 3600
    if not due:
        return False
    today = date.today().isoformat()
    try:
        last = max(g["eu"]) if g["eu"] else None
        fr = _add_days(last, -10) if last else GIE_EU_FROM
        g["eu"].update(_keep(_gie({"type": "eu", "from": fr, "to": today}, key)))
        for code, _name in COUNTRIES:
            ser = g["cty"].setdefault(code, {})
            fr = _add_days(max(ser), -10) if ser else _add_days(today, -GIE_COUNTRY_DAYS)
            ser.update(_keep(_gie({"country": code, "from": fr, "to": today}, key)))
        for s in SITES:
            if s[6]:
                ser = g["site"].setdefault(s[0], {})
                ser.update(_keep(_gie({"country": "GB*", "company": s[6][0], "facility": s[6][1],
                                       "from": _add_days(today, -30), "to": today}, key)))
        g["status"], g["err"] = "ok", None
        _store["key"] = {"status": "ok", "checked": _iso(time.time())}
    except GieKeyError as e:
        g["status"], g["err"] = "bad_key", str(e)
        _store["key"] = {"status": "bad_key", "checked": _iso(time.time())}
    except Exception as e:
        g["status"], g["err"] = "error", f"{type(e).__name__}: {e}"
    g["t"] = time.time()
    return True


# ---------------------------------------------------------------- UK estimate
def _rough_paused(ng, today):
    s = next(x for x in SITES if x[0] == "rough")
    cut = _add_days(today, -365)
    tot = sum(abs(v) for pid in (s[2], s[3]) for d, v in (ng.get(pid) or {}).items() if d >= cut)
    return tot < 1e6            # under 1 GWh moved in a year


def _cap_gwh(site, gie_sites):
    """(capacity GWh, source) — GIE working gas volume if it reports, else Ofgem."""
    key, mcm = site[0], site[5]
    ser = gie_sites.get(key) or {}
    if ser:
        w = _f(ser[max(ser)].get("workingGasVolume"))
        if w:
            return w * 1000, "GIE"
    if mcm:
        return mcm * GWH_PER_MCM, "Ofgem Jan 2025"
    return None, None


def _estimate(ng, gie_sites, today):
    """Per-site stock ranges at the start of the latest stock day. None if data short."""
    stock = ng.get(STOCK_ID) or {}
    if not stock:
        return None
    end = max(stock)
    T = stock[end] / 1e6                       # GWh, start of gas day `end`
    start = _add_days(end, -EST_WINDOW_DAYS)
    days = []
    d = start
    while d < end:
        days.append(d)
        d = _add_days(d, 1)
    paused = _rough_paused(ng, today)
    sites = [s for s in SITES if not (s[0] == "rough" and paused)]
    rows = []
    for s in sites:
        inj, wd = ng.get(s[2]) or {}, ng.get(s[3]) or {}
        F, c, mn, mx = [0.0], 0.0, 0.0, 0.0
        for d in days:
            c += ((inj.get(d) or 0) - (wd.get(d) or 0)) / 1e6
            F.append(c)
            mn, mx = min(mn, c), max(mx, c)
        cap, src = _cap_gwh(s, gie_sites)
        lo = -mn
        if cap is None:                          # unknown capacity: as small as the flows allow
            cap, src = mx - mn, "observed swing"
        hi = cap - mx
        tight = lo > hi
        if tight:                                # flows swing wider than the capacity figure
            lo = hi = (lo + hi) / 2
        rows.append({"s": s, "cap": cap, "src": src, "L": lo + c, "H": hi + c, "tight": tight})
    EL, EH = sum(r["L"] for r in rows), sum(r["H"] for r in rows)
    a = 0.5 if EH - EL < 1e-9 else max(0.0, min(1.0, (T - EL) / (EH - EL)))
    out = []
    for r in rows:
        others_L, others_H = EL - r["L"], EH - r["H"]
        lo = max(r["L"], T - others_H)
        hi = min(r["H"], T - others_L)
        if lo > hi:
            lo = hi = r["L"] + a * (r["H"] - r["L"])
        est = min(max(r["L"] + a * (r["H"] - r["L"]), lo), hi)
        out.append({"key": r["s"][0], "name": r["s"][1], "cap_gwh": r["cap"], "cap_src": r["src"],
                    "lo_gwh": lo, "hi_gwh": hi, "est_gwh": est, "tight": r["tight"]})
    # reconciliation: daily stock change vs the sum of site flows
    n_ok = n = 0
    for d in days:
        nd = _add_days(d, 1)
        if d in stock and nd in stock:
            net = sum(((ng.get(s[2]) or {}).get(d) or 0) - ((ng.get(s[3]) or {}).get(d) or 0) for s in sites) / 1e6
            n += 1
            n_ok += abs((stock[nd] - stock[d]) / 1e6 - net) <= 5
    cap_tot = sum(r["cap_gwh"] for r in out if r["cap_src"] != "observed swing")
    return {"stock_day": end, "stock_gwh": T, "cap_gwh": cap_tot, "sites": out,
            "fit": "inside" if EL <= T <= EH else "outside", "rough_paused": paused,
            "reconcile": {"days": n, "within_5gwh": n_ok}}


def _gie_stale(key, gie_sites, ng):
    ser = gie_sites.get(key) or {}
    if len(ser) < STALE_GIE_DAYS:
        return None
    ds = sorted(ser)[-STALE_GIE_DAYS:]
    vals = {ser[d].get("full") for d in ds}
    if len(vals) > 1:
        return False
    s = next(x for x in SITES if x[0] == key)
    moved = sum(abs(((ng.get(s[2]) or {}).get(d) or 0)) + abs(((ng.get(s[3]) or {}).get(d) or 0)) for d in ds) / 1e6
    return moved > 50


def _build_uk():
    s = _store
    ng, gsites = s["ng"], s["gie"]["site"]
    today = _gas_day_today()
    est = _estimate(ng, gsites, today)
    if not est:
        return None
    flow_day = max((d for d in (ng.get(SITES[0][2]) or {})), default=None)
    by = {r["key"]: r for r in est["sites"]}
    sites = []
    for site in SITES:
        key = site[0]
        inj = (ng.get(site[2]) or {}).get(flow_day)
        wd = (ng.get(site[3]) or {}).get(flow_day)
        dser = s["deliv"].get(site[4]) or {}
        deliv = dser[max(dser)] / 1e6 if dser else None
        row = {"key": key, "name": site[1], "live_names": list(site[7]),
               "inj_gwh": None if inj is None else round(inj / 1e6, 1),
               "wd_gwh": None if wd is None else round(wd / 1e6, 1),
               "flow_day": flow_day, "deliv_gwh_d": None if deliv is None else round(deliv, 1)}
        if key == "rough" and est["rough_paused"]:
            row.update({"state": "paused", "note": "storage paused (Centrica, 2025); live flow is production"})
        elif key in by:
            e = by[key]
            cap = e["cap_gwh"] if e["cap_src"] != "observed swing" else None
            row.update({
                "state": "est", "cap_gwh": round(cap) if cap else None, "cap_src": e["cap_src"],
                "est_gwh": round(e["est_gwh"]), "lo_gwh": round(e["lo_gwh"]), "hi_gwh": round(e["hi_gwh"]),
                "lo_pct": round(100 * e["lo_gwh"] / cap) if cap else None,
                "hi_pct": round(100 * e["hi_gwh"] / cap) if cap else None,
                "est_pct": round(100 * e["est_gwh"] / cap) if cap else None,
                "tight": e["tight"],
                "days_at_max": round(e["est_gwh"] / deliv, 1) if deliv else None})
            if e["cap_src"] == "observed swing":
                row["note"] = "capacity not published; range from its own flows"
        ser = gsites.get(key) or {}
        if ser:
            gd = max(ser)
            row["gie"] = {"pct": _f(ser[gd].get("full")), "gas_day": gd,
                          "stale": _gie_stale(key, gsites, ng), "status": ser[gd].get("status")}
        sites.append(row)
    lst = ng.get(LNG_STOCK) or {}
    lin = ng.get(LNG_IN) or {}
    lout = ng.get(LNG_OUT) or {}
    lday = max(lst) if lst else None
    in_day = max(lin) if lin else None
    cargo = [(d, v) for d, v in lin.items() if v > 1e8]
    last_cargo = max(cargo) if cargo else None
    out_day = max(lout) if lout else None
    # LNG inflow/outflow rows are dated the day AFTER the gas day they cover (the stock change
    # D -> D+1 matches the flow dated D+1 on 729 of 730 days, checked 28 Oct 2026), so the
    # gas day reported is the row's date minus one.
    lng = {"stock_gwh": round(lst[lday] / 1e6) if lday else None, "stock_day": lday,
           "inflow_gwh": round(lin[in_day] / 1e6, 1) if in_day else None,
           "inflow_day": _add_days(in_day, -1) if in_day else None,
           "last_cargo": {"day": _add_days(last_cargo[0], -1), "gwh": round(last_cargo[1] / 1e6)} if last_cargo else None,
           "outflow_gwh": round(lout[out_day] / 1e6, 1) if out_day else None,
           "outflow_day": _add_days(out_day, -1) if out_day else None}
    if lng["stock_gwh"] and lng["outflow_gwh"]:
        lng["days_at_sendout"] = round(lng["stock_gwh"] / lng["outflow_gwh"], 1)
    deliv_tot = sum(r["deliv_gwh_d"] or 0 for r in sites if r.get("state") == "est")
    dem = ng.get(DEMAND_ID) or {}
    dd = sorted(dem)[-365:]
    avg = sum(dem[d] for d in dd) / len(dd) / 1e6 if len(dd) >= 300 else None      # GWh/d
    return {"avg_use_gwh_d": round(avg) if avg else None,
            "avg_use_days": round(est["stock_gwh"] / avg, 1) if avg else None,
            "avg_use_basis": f"NTS demand, average of the last {len(dd)} days (incl. exports)" if avg else None,"stock_day": est["stock_day"], "stock_gwh": round(est["stock_gwh"]),
            "cap_gwh": round(est["cap_gwh"]),
            "pct": round(100 * est["stock_gwh"] / est["cap_gwh"]) if est["cap_gwh"] else None,
            "pct_basis": "National Gas stock / site capacities (GIE or Ofgem)",
            "rough_paused": est["rough_paused"], "fit": est["fit"], "reconcile": est["reconcile"],
            "deliv_gwh_d": round(deliv_tot), "deliv_mcm_d": round(deliv_tot / GWH_PER_MCM, 1),
            "sites": sites, "lng": lng, "est_window_days": EST_WINDOW_DAYS}


# ---------------------------------------------------------------- EU
def _gie_row(r):
    st, w = _f(r.get("gasInStorage")), _f(r.get("workingGasVolume"))
    inj, wd, cons = _f(r.get("injection")), _f(r.get("withdrawal")), _f(r.get("consumption"))
    return {"full": _f(r.get("full")), "stock_twh": st, "wgv_twh": w,
            "net_gwh_d": None if inj is None or wd is None else round(inj - wd, 1),
            "cons_twh_y": cons,
            "days": round(st / (cons / 365), 1) if st and cons else None,
            "status": r.get("status")}


def _same_day(ser, d, years):
    try:
        k = date.fromisoformat(d).replace(year=date.fromisoformat(d).year - years).isoformat()
    except ValueError:                      # 29 Feb
        k = _add_days(d, -365 * years)
    r = ser.get(k)
    return _f(r.get("full")) if r else None


def _build_eu():
    g = _store["gie"]
    base = {"status": g["status"], "error": g["err"], "fetched": _iso(g["t"])}
    eu = g["eu"]
    if not eu:
        return base
    last = max(eu)
    cur = _gie_row(eu[last])
    past = [v for v in (_same_day(eu, last, y) for y in range(1, 6)) if v is not None]
    cur.update({"gas_day": last, "ly": _same_day(eu, last, 1),
                "band_lo": min(past) if past else None, "band_hi": max(past) if past else None,
                "band_mean": round(sum(past) / len(past), 2) if past else None})
    trend = []
    d = _add_days(last, -364)
    while d <= last:
        r = eu.get(d)
        pv = [v for v in (_same_day(eu, d, y) for y in range(1, 6)) if v is not None]
        # [date, this year, 5-yr min, 5-yr max, 5-yr mean, 1 yr ago, 2 yrs ago, ... 5 yrs ago]
        trend.append([d, _f(r.get("full")) if r else None,
                      min(pv) if pv else None, max(pv) if pv else None,
                      round(sum(pv) / len(pv), 2) if pv else None]
                     + [_same_day(eu, d, y) for y in range(1, 6)])
        d = _add_days(d, 1)
    countries = []
    for code, name in COUNTRIES:
        ser = g["cty"].get(code) or {}
        if not ser:
            continue
        cd = max(ser)
        row = _gie_row(ser[cd])
        row.update({"code": code, "name": name, "gas_day": cd, "ly": _same_day(ser, cd, 1)})
        countries.append(row)
    countries.sort(key=lambda r: -(r["full"] or 0))
    age = (date.fromisoformat(_gas_day_today()) - date.fromisoformat(last)).days
    base.update({"eu": cur, "trend": trend, "countries": countries, "stale": age > 3})
    return base


# ---------------------------------------------------------------- build / loop
def _build():
    global _payload
    uk = _build_uk()
    eu = _build_eu()
    _payload = {"status": "ok" if uk else "building", "generated": _iso(time.time()),
                "uk": uk, "eu": eu, "ng_fetched": _iso(_store["ng_t"]),
                "key": gie_key_status()}


def _ensure_demand():
    """One year of NTS demand, fetched once (one request) if the store lacks it, e.g. after an
    upgrade from 261028.1; afterwards it updates with the other daily items."""
    global _demand_tried
    if len(_store["ng"].get(DEMAND_ID) or {}) >= 300 or time.time() - _demand_tried < 3600:
        return False
    _demand_tried = time.time()
    today = _gas_day_today()
    _ng_fetch([DEMAND_ID], _add_days(today, -366), _add_days(today, 1), _store["ng"])
    return True


def refresh_once(force=False):
    global _last_error
    with _lock:
        _load()
        if _payload.get("status") == "building" and _store["ng"]:
            _build()          # serve the saved figures at once after a restart, then refresh
        changed = False
        try:
            changed |= _refresh_ng(force)
        except Exception as e:
            _last_error = f"National Gas: {type(e).__name__}: {e}"
        try:
            changed |= _ensure_demand()
        except Exception as e:
            _last_error = f"National Gas demand: {type(e).__name__}: {e}"
        try:
            changed |= _refresh_gie(force)
        except Exception as e:
            _last_error = f"GIE: {type(e).__name__}: {e}"
        if changed:
            try:
                _save()
            except Exception as e:
                _last_error = f"save: {e}"
        _build()


def _loop():
    while True:
        try:
            refresh_once()
        except Exception as e:     # never let the thread die
            global _last_error
            _last_error = f"{type(e).__name__}: {e}"
        _wake.wait(LOOP_S)
        _wake.clear()


def _kick():
    global _thread
    if _thread is None or not _thread.is_alive():
        _thread = threading.Thread(target=_loop, name="gas-storage", daemon=True)
        _thread.start()


def get_gas_storage(demand_mcm_d=None):
    """Last built payload, plus days of gas at the given demand (mcm/d)."""
    _kick()
    p = dict(_payload)
    if p.get("status") == "building" and _last_error:
        p["error"] = _last_error
    uk = p.get("uk")
    if uk and demand_mcm_d:
        dem = demand_mcm_d * GWH_PER_MCM            # GWh/d
        st = uk["stock_gwh"]
        ln = (uk.get("lng") or {}).get("stock_gwh") or 0
        p = dict(p, days_of_gas={"demand_mcm_d": round(demand_mcm_d, 1),
                                 "storage_days": round(st / dem, 1), "lng_days": round(ln / dem, 1),
                                 "total_days": round((st + ln) / dem, 1),
                                 "storage_share_of_demand": round(uk["deliv_mcm_d"] / demand_mcm_d, 2)})
    return p


def gie_key_status():
    k = _load().get("key") or {}
    has = bool(_gie_key())
    return {"has_key": has, "status": (k.get("status") if has else None) or ("untested" if has else None),
            "checked": k.get("checked") if has else None}


def save_gie_key(key):
    """Save (or clear) the GIE key. Tests it with one small request first; a refused key is
    still saved so it can be corrected, and reported as rejected."""
    # Not under _lock: a background refresh can hold it for tens of seconds and the key
    # window should answer within a few. The refresher saves the store on its next pass.
    key = (key or "").strip()
    keys = _load_keys()
    _load()
    if not key:
        keys.pop("GIE", None)
        _save_keys(keys)
        _store["key"] = {"status": None, "checked": None}
        _store["gie"]["status"] = "no_key"
        _build()
        return {"ok": True, "has_key": False, "status": None}
    status = "untested"
    try:
        _gie({"type": "eu", "size": 1}, key, one_page=True, timeout=15)
        status = "ok"
    except GieKeyError:
        status = "bad_key"
    except Exception:
        status = "untested"            # GIE slow or unreachable: keep the key, checked on the next fetch
    keys["GIE"] = key
    _save_keys(keys)
    _store["key"] = {"status": status, "checked": _iso(time.time())}
    if status == "ok":
        _store["gie"]["t"] = 0         # fetch now
    _build()
    _wake.set()
    return {"ok": True, "has_key": True, "status": status}


if __name__ == "__main__":     # manual run: one refresh, print a summary
    import sys
    t0 = time.time()
    refresh_once(force="-f" in sys.argv)
    print(f"refresh {time.time() - t0:.0f} s, error: {_last_error}")
    p = get_gas_storage(demand_mcm_d=201.3)
    print(json.dumps({k: v for k, v in p.items() if k != "eu"}, indent=1, default=str)[:6000])
    eu = p.get("eu") or {}
    print("EU", eu.get("status"), eu.get("eu"))
    for c in eu.get("countries") or []:
        print(" ", c["code"], c["full"], c["ly"], c["days"], c["net_gwh_d"])
