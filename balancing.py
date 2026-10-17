"""Balancing costs: what NESO spends each day to keep the system balanced.

Build 261017.1  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Standard library only. No API keys.

Sources:
  * NESO Data Portal (CKAN datastore, api.neso.energy):
      - "Daily Balancing Costs" (BSUoS cost data): cost per settlement period
        by category (constraints, energy imbalance, positive/negative reserve,
        frequency control, other). The confirmed figure, about 5 days behind.
      - "Constraint Breakdown": daily thermal / voltage / inertia /
        largest-loss constraint costs.
      - "Thermal Constraint Costs": thermal constraint cost per boundary per day.
      - "Day Ahead Constraint Flows and Limits": planned flow and limit per
        boundary per half-hour, published the day before.
  * Elexon Insights (BMRS) indicative settlement, about a day behind:
      - cashflows for every accepted bid and offer, per BM unit and period;
      - accepted bid volumes (for wind turned down);
      - the BM unit reference list (fuel type per unit).
  * The server's own local logs: daily mean wind output and frequency events.

Provisional cost = Elexon Balancing Mechanism net cost / calibration ratio. The
ratio (BM net / NESO total) is taken from recent days that have both, so the
estimate absorbs NESO's trades and contracts that the BM does not show. The
likely range uses the 10th-90th percentile of that ratio.

Days are UK settlement days (local clock), matching NESO and Elexon.

Public entry point: get_balancing(detail=False) -> dict. Cheap: it returns the
last built payload and wakes the background refresher. The first call after a
fresh install returns {"status": "building"} while the history is backfilled.
"""

import json
import math
import statistics
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE_FILE = HERE / "balancing_store.json"
BACKUP_FILE = HERE / "balancing_store.bak.json"
LOG_DIR = HERE / "logs"
ARCHIVE_DIR = LOG_DIR / "archive"
UA = {"User-Agent": "uk-grid-monitor/1.0 (balancing costs)"}
NESO = "https://api.neso.energy/api/3/action/"
ELEXON = "https://data.elexon.co.uk/bmrs/api/v1"

BACKFILL_DAYS = 50            # history fetched on first run
DISPLAY_DAYS = 47             # days shown on the panel (ending today)
KEEP_DAYS = 400               # days kept in the store
NESO_REFRESH_S = 6 * 3600
ELEXON_TODAY_S = 1800         # today: new settled periods fetched this often
ELEXON_PUBLISH_LAG_S = 900    # a period's indicative settlement appears ~10 min after it ends
ELEXON_RECENT_S = 3 * 3600    # retry spacing for the final whole-day fetch
ELEXON_FINAL_AFTER_D = 2      # a day's Elexon data is treated as final this many days after
BMU_REFRESH_S = 7 * 86400
LOOP_S = 300                  # background check cadence

# Calibration: confirmed days with NESO total above this are used for the ratio.
RATIO_MIN_TOTAL = 15e6
RATIO_WINDOW_DAYS = 60
RATIO_DEFAULT = (0.933, 0.873, 0.946)    # median, p10, p90 (measured Sep-Oct 2026)

# Day-ahead boundary groups summed for "planned flow over the Scottish limits".
SCOT_GROUPS = ("SCOTEX", "SSE_SP2", "SSHARN3", "SSEN_S", "HARSPNBLY", "SSE_GRMO")

# Frequency events (local logs, 15-second samples).
FREQ_LOW_HZ = 49.76           # a day whose lowest reading is below this gets a "frequency low" marker
INFEED_DROP_HZ = 0.2          # "major infeed loss": a fall this big ...
INFEED_WINDOW_S = 30          # ... within this many seconds ...
INFEED_FLOOR_HZ = 49.85       # ... ending at or below this
INFEED_ABS_HZ = 49.6          # or any reading below this
GRADUAL_DROP_HZ = 0.1         # a low whose sharpest 30-s fall is under this is "gradual"

NESO_CATS = {"Constraints": "constraints", "Energy Imbalance": "imbalance",
             "Positive Reserve": "reserve", "Negative Reserve": "reserve",
             "Frequency Control": "frequency", "Other": "other"}

_lock = threading.Lock()
_store = None
_payload = {"status": "building"}
_payload_detail = {"status": "building"}
_thread = None
_wake = threading.Event()
_last_error = None


# ---------------------------------------------------------------- time helpers
def _uk_offset_h(dt):
    """UK clock offset (h) at a UTC instant. Stdlib-only (no tz database on Windows)."""
    def last_sun(y, mo):
        d = datetime(y, mo, 31, 1, tzinfo=timezone.utc)
        return d - timedelta(days=(d.weekday() + 1) % 7)
    return 1 if last_sun(dt.year, 3) <= dt < last_sun(dt.year, 10) else 0


def _uk_dt(ep):
    dt = datetime.fromtimestamp(ep, tz=timezone.utc)
    return dt + timedelta(hours=_uk_offset_h(dt))


def _uk_date(ep):
    return _uk_dt(ep).strftime("%Y-%m-%d")


def _today():
    return _uk_date(time.time())


def _add_days(ds, n):
    return (date.fromisoformat(ds) + timedelta(days=n)).isoformat()


def _iso_ep(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else 0.0
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- HTTP
def _get(url, timeout=120):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _neso_resources(pkg):
    """Datastore resource ids for a NESO dataset, newest last (the yearly files)."""
    res = _get(NESO + "package_show?id=" + pkg, 60)["result"]["resources"]
    ids = [r["id"] for r in res if r.get("datastore_active")]
    return ids[-2:]          # this financial year and the one before cover any window we fetch


def _neso_sql(sql, page=32000):
    out, off = [], 0
    while True:
        q = sql + f" LIMIT {page} OFFSET {off}"
        recs = _get(NESO + "datastore_search_sql?" + urllib.parse.urlencode({"sql": q}))["result"]["records"]
        out += recs
        if len(recs) < page:
            return out
        off += page


def _neso_rows(pkg, col, start, extra="", last_only=False):
    """All rows from `start` across a dataset's yearly resources. A resource whose
    query fails (yearly files sometimes name columns differently) is skipped; the
    call fails only if every resource fails."""
    rows, errs = [], []
    rids = _neso_resources(pkg)
    for rid in (rids[-1:] if last_only else rids):
        try:
            rows += _neso_sql(f'SELECT * FROM "{rid}" WHERE "{col}" >= \'{start}\'{extra}')
        except Exception as e:
            errs.append(e)
    if errs and not rows and len(errs) == len(rids[-1:] if last_only else rids):
        raise errs[0]
    return rows


def _cost_col(r):
    for k in ("Daily Cost GBP", "Daily Cost", "Cost GBP", "Cost"):
        if k in r:
            return r[k]
    return next((v for k, v in r.items() if "cost" in k.lower()), 0)


# ---------------------------------------------------------------- store
def _load():
    global _store
    if _store is not None:
        return _store
    for p in (STORE_FILE, BACKUP_FILE):
        try:
            s = json.loads(p.read_text())
            if isinstance(s, dict) and isinstance(s.get("days"), dict):
                _store = s
                return _store
        except Exception:
            pass
    _store = {"days": {}, "bmu_fuel": {}, "bmu_t": 0, "neso_t": 0}
    return _store


def _save():
    s = _store
    cutoff = _add_days(_today(), -KEEP_DAYS)
    for d in [d for d in s["days"] if d < cutoff]:
        del s["days"][d]
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, separators=(",", ":")))
    tmp.replace(STORE_FILE)
    try:   # one backup per day
        if not BACKUP_FILE.exists() or time.time() - BACKUP_FILE.stat().st_mtime > 86400:
            BACKUP_FILE.write_text(json.dumps(s, separators=(",", ":")))
    except Exception:
        pass


def _day(d):
    return _store["days"].setdefault(d, {})


# ---------------------------------------------------------------- NESO
def _refresh_neso(start):
    """Confirmed costs, constraint breakdown, thermal costs by boundary and the
    day-ahead Scottish flows, for every day from `start`."""
    # BSUoS cost data, per settlement period
    per = {}
    if True:
        for r in _neso_rows("daily-balancing-costs-balancing-services-use-of-system", "SETT_DATE", start):
            d = str(r.get("SETT_DATE", ""))[:10]
            sp = int(_f(r.get("SETT_PERIOD")))
            if not d or sp < 1 or sp > 50:
                continue
            cats = per.setdefault(d, {})
            cats[sp] = {k: _f(r.get(k)) for k in NESO_CATS}
    for d, sps in per.items():
        tot = {"constraints": 0.0, "imbalance": 0.0, "reserve": 0.0, "frequency": 0.0, "other": 0.0}
        n = max(sps)
        hh = [0.0] * max(n, 46)
        for sp, row in sps.items():
            for k, v in row.items():
                tot[NESO_CATS[k]] += v
                hh[sp - 1] += v
        rec = _day(d)
        rec["neso"] = {k: round(v) for k, v in tot.items()}
        rec["neso_hh"] = [round(x) for x in hh]
        rec["neso_n"] = len(sps)
    # Constraint breakdown
    if True:
        for r in _neso_rows("constraint-breakdown", "Date", start):
            d = str(r.get("Date", ""))[:10]
            if d:
                _day(d)["cons"] = {"thermal": _f(r.get("Thermal constraints cost")),
                                   "voltage": _f(r.get("Voltage constraints cost")),
                                   "inertia": _f(r.get("Increasing system inertia cost")),
                                   "largest_loss": _f(r.get("Reducing largest loss cost"))}
    # Thermal constraint cost per boundary
    tcc = {}
    if True:
        for r in _neso_rows("thermal-constraint-costs", "Settlement Date", start):
            d = str(r.get("Settlement Date", ""))[:10]
            g = r.get("Constraint Group")
            if d and g:
                tcc.setdefault(d, {})[g] = _f(_cost_col(r))
    for d, v in tcc.items():
        _day(d)["tcc"] = v
    # Day-ahead flows and limits, Scottish boundaries only (local clock times)
    over = {}
    groups = ",".join("'" + g + "'" for g in SCOT_GROUPS)
    if True:
        for r in _neso_rows("day-ahead-constraint-flows-and-limits", "Date_ Time GMT_BST", start,
                            f' AND "Constraint Group" IN ({groups})', last_only=True):
            d = str(r.get("Date_ Time GMT_BST", ""))[:10]
            lim, flow = _f(r.get("Limit_MW")), _f(r.get("Flow_MW"))
            if not d or lim <= 0:
                continue
            over[d] = over.get(d, 0.0) + max(0.0, flow - lim) * 0.5     # MWh per half-hour
    for d, v in over.items():
        _day(d)["over_sc_gwh"] = round(v / 1000, 1)
    _store["neso_t"] = time.time()


# ---------------------------------------------------------------- Elexon
def _fuel_of(r):
    f = r.get("fuelType")
    nm = ((r.get("bmUnitName") or "") + " " + (r.get("elexonBmUnit") or "")).lower()
    if not f:
        if r.get("interconnectorId"):
            f = "INTERCONNECTOR"
        elif any(k in nm for k in ("batt", "bess", "storage", "energy store")):
            f = "BATTERY"
        else:
            f = "OTHER"
    if f.startswith("INT"):
        f = "INTERCONNECTOR"
    if f in ("OCGT", "CCGT"):
        f = "GAS"
    return f


def _refresh_bmu():
    ref = _get(ELEXON + "/reference/bmunits/all?format=json", 120)
    fuel = {r["elexonBmUnit"]: _fuel_of(r) for r in ref if r.get("elexonBmUnit")}
    if len(fuel) > 500:
        _store["bmu_fuel"] = fuel
        _store["bmu_t"] = time.time()


def _elexon_side(side, d, sp=None):
    url = f"{ELEXON}/balancing/settlement/indicative/cashflows/all/{side}/{d}" + (f"/{sp}" if sp else "")
    return _get(url + "?format=json")["data"]


def _elexon_cashflows(d, sp=None):
    """BM offers and bids for one settlement day (or one period of it): totals,
    by fuel, per period."""
    fuel = _store.get("bmu_fuel") or {}
    out = {"offer": 0.0, "bid": 0.0, "by_fuel": {}, "hh": [], "rows": 0}
    hh = [0.0] * 50
    nmax = 0
    for side in ("offer", "bid"):
        for x in _elexon_side(side, d, sp):
            p = int(x.get("settlementPeriod") or 0)
            if p < 1 or p > 50:
                continue
            v = _f(x.get("totalCashflow"))
            out[side] += v
            k = fuel.get(x.get("bmUnit"), "OTHER") + "|" + side
            out["by_fuel"][k] = out["by_fuel"].get(k, 0.0) + v
            hh[p - 1] += v
            nmax = max(nmax, p)
            out["rows"] += 1
    out["hh"] = [round(v) for v in hh[:max(nmax, 1)]]
    out["by_fuel"] = {k: round(v) for k, v in out["by_fuel"].items()}
    out["offer"], out["bid"] = round(out["offer"]), round(out["bid"])
    return out


def _elexon_wind_down(d, sp=None):
    """MWh of accepted bids on wind units (wind turned down), for a day or one period."""
    fuel = _store.get("bmu_fuel") or {}
    url = f"{ELEXON}/balancing/settlement/indicative/volumes/all/bid/{d}" + (f"/{sp}" if sp else "")
    mwh = 0.0
    for x in _get(url + "?format=json")["data"]:
        if fuel.get(x.get("bmUnit")) == "WIND":
            v = _f(x.get("totalVolumeAccepted"))
            if v < 0:
                mwh -= v
    return mwh


def _day_bounds(d):
    """UTC epoch of the UK local midnight starting day d, and its number of periods (46/48/50)."""
    def start(ds):
        y, mo, dd = (int(x) for x in ds.split("-"))
        t = datetime(y, mo, dd, tzinfo=timezone.utc)
        return (t - timedelta(hours=_uk_offset_h(t - timedelta(hours=1)))).timestamp()
    t0, t1 = start(d), start(_add_days(d, 1))
    return t0, int(round((t1 - t0) / 1800))


def _today_period_jobs(d, rec, now):
    """Periods of today that have ended (plus a margin for Elexon to publish) and
    are not yet held."""
    t0, n = _day_bounds(d)
    have = rec.get("bm_sp") or {}
    if now - rec.get("bm_sp_t", 0) < ELEXON_TODAY_S:
        return []
    return [sp for sp in range(1, n + 1)
            if str(sp) not in have and now >= t0 + sp * 1800 + ELEXON_PUBLISH_LAG_S]


def _fold_today(rec, now):
    """Rebuild today's day totals from the per-period records."""
    sps = rec.get("bm_sp") or {}
    if not sps:
        return
    n = max(int(k) for k in sps)
    hh = [0] * n
    bf, off, bid, w = {}, 0, 0, 0.0
    for k, v in sps.items():
        hh[int(k) - 1] = v["o"] + v["b"]
        off += v["o"]
        bid += v["b"]
        w += v["w"]
        for fk, fv in v["f"].items():
            bf[fk] = bf.get(fk, 0) + fv
    rec["bm"] = {"offer": off, "bid": bid, "by_fuel": bf, "hh": hh, "t": now, "partial": True, "final": False}
    rec["curt_gwh"] = round(w / 1000, 1)


def _refresh_elexon(days):
    """Today: period by period as each settles (small requests). Earlier days: one
    whole-day fetch once the day is over, and a last one two days after, which is
    then kept as final."""
    now, today = time.time(), _today()
    jobs = []
    for d in days:
        rec = _store["days"].get(d, {})
        e = rec.get("bm")
        if d == today:
            jobs += [(d, sp) for sp in _today_period_jobs(d, rec, now)]
        elif d < today and not (e and e.get("final")):
            final_due = d <= _add_days(today, -ELEXON_FINAL_AFTER_D)
            first = not e or e.get("partial")
            if (first or final_due) and now - (e or {}).get("t", 0) >= (0 if first else ELEXON_RECENT_S):
                jobs.append((d, None))
    if not jobs:
        return 0

    def run(job):
        d, sp = job
        try:
            cf = _elexon_cashflows(d, sp)
            w = _elexon_wind_down(d, sp)
            return d, sp, cf, w, None
        except Exception as ex:
            return d, sp, None, None, f"{type(ex).__name__}: {ex}"

    errs = []
    with ThreadPoolExecutor(3) as ex:
        for d, sp, cf, w, err in ex.map(run, jobs):
            if err:
                errs.append(f"{d}{'/' + str(sp) if sp else ''}: {err}")
                continue
            with _lock:
                rec = _day(d)
                if sp:
                    t0, _ = _day_bounds(d)
                    # an empty period is kept only once it is well past (genuinely no actions)
                    if cf["rows"] == 0 and now < t0 + sp * 1800 + 3 * 3600:
                        continue
                    rec.setdefault("bm_sp", {})[str(sp)] = {"o": cf["offer"], "b": cf["bid"], "f": cf["by_fuel"], "w": round(w, 1)}
                else:
                    cf.pop("rows", None)
                    cf["t"] = now
                    cf["final"] = d <= _add_days(today, -ELEXON_FINAL_AFTER_D)
                    rec["bm"] = cf
                    rec["curt_gwh"] = round(w / 1000, 1)
                    rec.pop("bm_sp", None)
                    rec.pop("bm_sp_t", None)
    with _lock:
        rec = _store["days"].get(today)
        if rec is not None and any(j[0] == today for j in jobs):
            rec["bm_sp_t"] = now
            _fold_today(rec, now)
    if errs:
        raise RuntimeError(f"{len(errs)} of {len(jobs)} requests failed, e.g. " + "; ".join(errs[:2]))
    return len(jobs)


# ---------------------------------------------------------------- local logs
def _log_rows(paths, kind):
    rows = []
    tag = '"k":"%s"' % kind
    for p in paths:
        try:
            for line in Path(p).read_text().splitlines():
                if tag not in line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                ep = _iso_ep(r.get("t"))
                if ep is not None:
                    rows.append((ep, r))
        except Exception:
            pass
    rows.sort(key=lambda x: x[0])
    return rows


def _local_day(d):
    """Mean wind (MW) and frequency summary for one UK day from the local logs."""
    lo, n = _day_bounds(d)
    hi = lo + n * 1800
    utc_days = sorted({datetime.fromtimestamp(lo, tz=timezone.utc).strftime("%Y-%m-%d"),
                       datetime.fromtimestamp(hi - 1, tz=timezone.utc).strftime("%Y-%m-%d")})
    daily = [LOG_DIR / f"grid_log-{u}.jsonl" for u in utc_days]
    out = {}
    mix = [(ep, r) for ep, r in _log_rows([p for p in daily if p.exists()], "m") if lo <= ep < hi]
    if not mix and ARCHIVE_DIR.exists():      # older days: weekly archive files (Sun-Sat)
        arch = []
        for u in utc_days:
            ud = date.fromisoformat(u)
            sun = ud - timedelta(days=(ud.weekday() + 1) % 7)
            arch.append(ARCHIVE_DIR / f"{sun.isoformat()}-gen.jsonl")
        mix = [(ep, r) for ep, r in _log_rows([p for p in dict.fromkeys(arch) if p.exists()], "m")
               if lo <= ep < hi]
    w = [_f((r.get("gen") or {}).get("WIND")) for _, r in mix if (r.get("gen") or {}).get("WIND") is not None]
    if len(w) >= 12:
        out["wind_mw"] = round(sum(w) / len(w))
    fr = [(ep, _f(r.get("hz"))) for ep, r in _log_rows([p for p in daily if p.exists()], "f")
          if lo <= ep < hi and r.get("hz")]
    if len(fr) >= 240:
        i_min = min(range(len(fr)), key=lambda i: fr[i][1])
        # sharpest 30-s fall in the 10 minutes before the minimum
        sharp = 0.0
        for i in range(len(fr)):
            if fr[i_min][0] - 600 <= fr[i][0] <= fr[i_min][0]:
                for j in range(i + 1, len(fr)):
                    if fr[j][0] - fr[i][0] > INFEED_WINDOW_S:
                        break
                    sharp = max(sharp, fr[i][1] - fr[j][1])
        out["fmin"] = round(fr[i_min][1], 3)
        out["fmin_t"] = fr[i_min][0]
        out["fmin_sharp"] = round(sharp, 3)
        loss = []
        for i in range(len(fr)):
            hit = None
            if fr[i][1] < INFEED_ABS_HZ:
                hit = (fr[i][0], 0.0, fr[i][1])
            else:
                for j in range(i + 1, len(fr)):
                    if fr[j][0] - fr[i][0] > INFEED_WINDOW_S:
                        break
                    drop = fr[i][1] - fr[j][1]
                    if drop >= INFEED_DROP_HZ and fr[j][1] <= INFEED_FLOOR_HZ:
                        hit = (fr[j][0], drop, fr[j][1])
                        break
            if hit and (not loss or hit[0] - loss[-1]["t"] > 600):
                loss.append({"t": hit[0], "drop": round(hit[1], 3), "hz": round(hit[2], 3)})
            elif hit and loss and hit[2] < loss[-1]["hz"]:
                loss[-1]["hz"] = round(hit[2], 3)
                loss[-1]["drop"] = max(loss[-1]["drop"], round(hit[1], 3))
        out["infeed"] = loss
        out["fn"] = len(fr)
    return out


def _refresh_local(days):
    today = _today()
    for d in days:
        rec = _store["days"].get(d, {})
        loc = rec.get("local")
        if loc and loc.get("complete"):
            continue
        try:
            v = _local_day(d)
        except Exception:
            continue
        v["complete"] = d < today and ("fmin" in v or d < _add_days(today, -3))
        with _lock:
            _day(d)["local"] = v


# ---------------------------------------------------------------- payload
def _ratio():
    today = _today()
    lo = _add_days(today, -RATIO_WINDOW_DAYS)
    r = []
    for d, rec in _store["days"].items():
        n, e = rec.get("neso"), rec.get("bm")
        if d < lo or not n or not e or (rec.get("neso_n") or 0) < 46:
            continue
        tot = sum(n.values())
        if tot > RATIO_MIN_TOTAL:
            r.append((e["offer"] + e["bid"]) / tot)
    if len(r) < 5:
        med, p10, p90 = RATIO_DEFAULT
        return {"median": med, "p10": p10, "p90": p90, "n": len(r), "default": True}
    r.sort()
    return {"median": statistics.median(r), "p10": r[int(0.1 * len(r))],
            "p90": r[max(0, int(0.9 * len(r)) - 1)], "n": len(r), "default": False}


def _confirmed(rec):
    return bool(rec.get("neso")) and (rec.get("neso_n") or 0) >= 46


def _pearson(x, y):
    if len(x) < 8:
        return None
    mx, my = sum(x) / len(x), sum(y) / len(y)
    sxy = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sxx = sum((a - mx) ** 2 for a in x)
    syy = sum((b - my) ** 2 for b in y)
    return round(sxy / math.sqrt(sxx * syy), 2) if sxx > 0 and syy > 0 else None


def _short(d):
    t = date.fromisoformat(d)
    return f"{t.day} {t.strftime('%b')}"


def _hm(ep):
    return _uk_dt(ep).strftime("%H:%M")


def _m(v, dp=1):
    return ("−£" if v < 0 else "£") + f"{abs(v) / 1e6:.{dp}f}m"


def _build():
    s = _store
    today = _today()
    ratio = _ratio()
    all_dates = sorted(d for d in s["days"] if d <= today)
    win = [_add_days(today, -k) for k in range(DISPLAY_DAYS - 1, -1, -1)]

    def day_row(d):
        rec = s["days"].get(d, {})
        e = rec.get("bm")
        row = {"date": d}
        loc = rec.get("local") or {}
        row["wind"] = loc.get("wind_mw")
        if e:
            bf = e.get("by_fuel", {})
            row.update({"bm_net": e["offer"] + e["bid"], "bm_offer": e["offer"], "bm_bid": e["bid"],
                        "gas_up": bf.get("GAS|offer", 0), "bio_up": bf.get("BIOMASS|offer", 0),
                        "wind_down": bf.get("WIND|bid", 0),
                        "other_up": sum(v for k, v in bf.items() if k.endswith("|offer") and not k.startswith(("GAS|", "BIOMASS|"))),
                        "other_down": sum(v for k, v in bf.items() if k.endswith("|bid") and not k.startswith("WIND|"))})
        if _confirmed(rec):
            row["confirmed"] = dict(rec["neso"])
            row["hh"] = rec.get("neso_hh")
            row["total"] = sum(rec["neso"].values())
        elif e:
            net = e["offer"] + e["bid"]
            row["est"] = net / ratio["median"]
            row["est_lo"] = net / ratio["p90"]
            row["est_hi"] = net / ratio["p10"]
            row["hh"] = e.get("hh")
            row["total"] = row["est"]
            row["partial"] = d == today
        if "curt_gwh" in rec:
            row["curt_gwh"] = rec["curt_gwh"]
        if "over_sc_gwh" in rec:
            row["over_sc_gwh"] = rec["over_sc_gwh"]
        if rec.get("tcc"):
            row["tcc"] = rec["tcc"]
        if rec.get("cons"):
            row["cons"] = rec["cons"]
        return row

    days = [day_row(d) for d in win]
    have = [r for r in days if r.get("total") is not None]
    conf_all = [d for d in all_dates if _confirmed(s["days"][d])]
    tot_of = {d: sum(s["days"][d]["neso"].values()) for d in conf_all}
    last30 = conf_all[-30:]
    baseline = sum(tot_of[d] for d in last30) / len(last30) if last30 else None

    # ---- events
    ev = []
    def add(d, lane, up, good, text):
        ev.append({"d": d, "lane": lane, "dir": "up" if up else "down", "c": "good" if good else "bad", "t": text})
    hist_tot = {}
    for d in all_dates:
        r = day_row(d) if d in win else None
        if r is None:
            rec = s["days"][d]
            if _confirmed(rec):
                hist_tot[d] = tot_of[d]
            elif rec.get("bm"):
                hist_tot[d] = (rec["bm"]["offer"] + rec["bm"]["bid"]) / ratio["median"]
        elif r.get("total") is not None and not r.get("partial"):
            hist_tot[d] = r["total"]
    first = all_dates[0] if all_dates else today
    top3 = sorted((r for r in have if not r.get("partial")), key=lambda r: -r["total"])[:3]
    top3d = {r["date"] for r in top3}
    run = 0.0
    rec_days = []
    for d in sorted(hist_tot):
        if hist_tot[d] > run:
            run = hist_tot[d]
            if d in top3d:
                rec_days.append(d)
    for i, d in enumerate(rec_days):
        r = next(x for x in days if x["date"] == d)
        later = next((x for x in rec_days[i + 1:]), None)
        since = f" since {_short(first)}" if first < d else ""
        if r.get("confirmed"):
            add(d, "cost", True, False, f"Highest cost{since}: {_m(r['total'])} confirmed"
                + (f" (stood until {_short(later)})" if later else ""))
        else:
            add(d, "cost", True, False, f"Highest cost{since}, provisional: about {_m(r['total'])}, likely "
                f"£{r['est_lo'] / 1e6:.0f}–{r['est_hi'] / 1e6:.0f}m" + (f" (passed on {_short(later)})" if later else ""))
    conf_win = [r for r in days if r.get("confirmed")]
    if conf_win:
        lo_r = min(conf_win, key=lambda r: r["total"])
        add(lo_r["date"], "cost", False, True, f"Lowest cost day: {_m(lo_r['total'])} confirmed")
        res = max(conf_win, key=lambda r: r["confirmed"]["reserve"])
        if res["confirmed"]["reserve"] > 0:
            add(res["date"], "svc", True, False, f"Reserve cost {_m(res['confirmed']['reserve'], 2)}, highest day in the period")
        imb = min(conf_win, key=lambda r: r["confirmed"]["imbalance"])
        if imb["confirmed"]["imbalance"] < 0:
            add(imb["date"], "svc", False, True, f"Energy imbalance earned NESO {_m(-imb['confirmed']['imbalance'], 2)}, best day in the period")
    for key, lane, good, fmt in (("over_sc_gwh", "flow", False, "Planned flow furthest over the Scottish limits: {:.0f} GWh"),
                                 ("curt_gwh", "curt", False, "Most wind turned down: {:.0f} GWh in one day"),
                                 ("wind", "wind", True, None)):
        cand = [r for r in days if r.get(key) is not None and not r.get("partial")]
        if cand:
            top = max(cand, key=lambda r: r[key])
            if top[key] > 0:
                add(top["date"], lane, True, good,
                    fmt.format(top[key]) if fmt else f"Highest daily mean wind: {top[key] / 1000:.1f} GW")
    for r in days:
        loc = (s["days"].get(r["date"], {}).get("local") or {})
        for x in loc.get("infeed") or []:
            add(r["date"], "cost", False, False,
                f"Major infeed loss? Frequency fell {x['drop']:.2f} Hz within {INFEED_WINDOW_S} s to {x['hz']:.3f} Hz at {_hm(x['t'])} (local logs)"
                if x["drop"] else f"Frequency below {INFEED_ABS_HZ} Hz: {x['hz']:.3f} Hz at {_hm(x['t'])} (local logs)")
        if loc.get("fmin") is not None and loc["fmin"] < FREQ_LOW_HZ and not loc.get("infeed"):
            add(r["date"], "cost", False, False,
                f"Frequency fell to {loc['fmin']:.3f} Hz at {_hm(loc['fmin_t'])} (local logs)"
                + (". A gradual fall, not a sudden loss" if loc.get("fmin_sharp", 1) < GRADUAL_DROP_HZ else ""))

    # ---- tiles
    est_days = [r for r in days if r.get("est") is not None]
    hi_est = max(est_days, key=lambda r: r["est"]) if est_days else None
    yday = next((r for r in days if r["date"] == _add_days(today, -1)), None)
    last_conf = conf_all[-1] if conf_all else None
    t = date.fromisoformat(today)
    pm_end = t.replace(day=1) - timedelta(days=1)
    pm = pm_end.strftime("%Y-%m")
    pm_days = [d for d in conf_all if d.startswith(pm)]
    c30 = sum(s["days"][d]["neso"]["constraints"] for d in last30)
    hi_conf = max(conf_all, key=lambda d: tot_of[d]) if conf_all else None
    tiles = {
        "highest_estimate": ({"date": hi_est["date"], "value": hi_est["est"], "lo": hi_est["est_lo"],
                              "hi": hi_est["est_hi"], "partial": bool(hi_est.get("partial")),
                              "pct": (hi_est["est"] / baseline - 1) * 100 if baseline else None}
                             if hi_est else None),
        "baseline30": baseline, "baseline_n": len(last30),
        "yesterday": ({"date": yday["date"], "value": yday["total"], "confirmed": bool(yday.get("confirmed"))}
                      if yday and yday.get("total") is not None else None),
        "latest_confirmed": {"date": last_conf, "value": tot_of[last_conf]} if last_conf else None,
        "prev_month": {"month": pm, "label": pm_end.strftime("%B"), "value": sum(tot_of[d] for d in pm_days),
                       "n": len(pm_days), "of": pm_end.day} if pm_days else None,
        "constraints_share": (c30 / sum(tot_of[d] for d in last30) * 100) if last30 else None,
        "highest_confirmed": {"date": hi_conf, "value": tot_of[hi_conf]} if hi_conf else None,
    }

    # ---- correlations over confirmed days in the window
    def corr(key, scale=1):
        pts = [(r[key] * scale, r["total"]) for r in conf_win if r.get(key) is not None]
        return _pearson([p[0] for p in pts], [p[1] for p in pts]), len(pts)
    corr_rows = []
    for key, name, src, avail in (("curt_gwh", "Wind turned down (accepted bids on wind units)", "Elexon", "about a day after"),
                                  ("over_sc_gwh", "Planned flow over the Scottish boundary limits", "NESO day-ahead", "the day before"),
                                  ("wind", "Mean wind output", "local logs", "live")):
        r, n = corr(key)
        if r is not None:
            corr_rows.append([name, r, src, avail, n])

    totals = None
    if conf_win:
        cats = ("constraints", "imbalance", "reserve", "frequency", "other")
        totals = {"from": conf_win[0]["date"], "to": conf_win[-1]["date"], "n": len(conf_win),
                  "sum": {c: sum(r["confirmed"][c] for r in conf_win) for c in cats},
                  "max": {c: max((r["confirmed"][c] for r in conf_win), key=abs) for c in cats}}

    base = {"status": "ok", "generated": datetime.now(timezone.utc).isoformat(), "today": today,
            "history_from": first, "ratio": ratio, "tiles": tiles, "events": ev,
            "error": _last_error,
            "updated": {"neso": s.get("neso_t"), "elexon": max((r.get("bm", {}).get("t", 0) for r in s["days"].values()), default=0)}}
    lite = [{k: v for k, v in r.items() if k not in ("hh",)} for r in days]
    summary = dict(base, days=lite)
    detail = dict(base, days=days, corr=corr_rows, totals=totals)
    return summary, detail


# ---------------------------------------------------------------- loop
def refresh_once():
    """One refresh pass: NESO when due, Elexon for due days, local logs. Returns
    the number of Elexon jobs run."""
    global _payload, _payload_detail, _last_error
    with _lock:
        _load()
    today = _today()
    days = [_add_days(today, -k) for k in range(BACKFILL_DAYS, -1, -1)]
    errs = []
    if time.time() - _store.get("bmu_t", 0) > BMU_REFRESH_S or not _store.get("bmu_fuel"):
        try:
            _refresh_bmu()
        except Exception as e:
            errs.append(f"BMU list: {type(e).__name__}: {e}")
    if time.time() - _store.get("neso_t", 0) > NESO_REFRESH_S:
        try:
            # From 10 days before the newest confirmed day, or earlier if an older day
            # in the window is missing any NESO part (e.g. an earlier fetch failed).
            with _lock:
                have = [d for d in _store["days"] if _store["days"][d].get("neso")]
                old = [d for d in days if d < _add_days(today, -10)]
                gaps = [d for d in old if not all(k in _store["days"].get(d, {})
                                                  for k in ("neso", "tcc", "over_sc_gwh"))]
            start = _add_days(max(have), -10) if have else days[0]
            if gaps:
                start = min(start, gaps[0])
            _refresh_neso(start)
        except Exception as e:
            errs.append(f"NESO: {type(e).__name__}: {e}")
    n = 0
    if _store.get("bmu_fuel"):
        try:
            n = _refresh_elexon(days)
        except Exception as e:
            errs.append(f"Elexon: {e}")
    _refresh_local(days[-DISPLAY_DAYS:])
    _last_error = "; ".join(errs) or None
    with _lock:
        try:
            _save()
        except Exception as e:
            _last_error = (_last_error + "; " if _last_error else "") + f"store: {e}"
        if any(r.get("bm") or r.get("neso") for r in _store["days"].values()):
            _payload, _payload_detail = _build()
    return n


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
        _thread = threading.Thread(target=_loop, name="balancing", daemon=True)
        _thread.start()


def get_balancing(detail=False):
    _kick()
    p = _payload_detail if detail else _payload
    if p.get("status") == "building" and _last_error:
        return {"status": "building", "error": _last_error}
    return p


if __name__ == "__main__":     # manual run: one refresh, print the tiles
    import sys
    t0 = time.time()
    refresh_once()
    print(f"refresh {time.time() - t0:.0f} s, error: {_last_error}")
    p = _payload_detail
    print(json.dumps(p.get("tiles"), indent=1, default=str))
    for e in p.get("events", []):
        print(e["d"], e["lane"], e["dir"], e["c"], e["t"])
    print("corr", p.get("corr"))
    if "-v" in sys.argv:
        for r in p.get("days", [])[-8:]:
            print(r["date"], round((r.get("total") or 0) / 1e6, 1), "conf" if r.get("confirmed") else "est",
                  r.get("curt_gwh"), r.get("over_sc_gwh"), r.get("wind"))
