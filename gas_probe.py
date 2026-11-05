# gas_probe.py — read-only gas supply/margin DIAGNOSTIC probe for GB Energy Monitor
#
# Build 261028.1  (version = YYMMDD.N in UT; bump on every change to this file)
# Change history: CHANGELOG.md
#
# Mirrors rain_probe.py: each cycle it evaluates the gas signals we have and LOGS the
# exact phrase the alert WOULD speak — with no tone, no Web Speech, no state forced onto
# the live alarm layer. It is deliberately side-effect-free apart from the GasState it is
# handed, so it can be watched against real conditions before any of it earns a tone.
# ("Honesty over plausibility": every derived signal is labelled derived; only National
# Gas's own published notices are spoken as fact.)
#
# Design reference: GAS_ALARM_DESIGN.md — the control-room ladder (Margins Notice → Gas
# Balancing Notification → NGSE), the verified National Gas REST publication IDs, the
# diurnal-safe signal choice (PCLP vs Forecast Minimum Linepack) and the slow, dwelled
# cadence. Publication IDs and the None-when-clear notice semantics were verified against
# the live API on 2026-09-19.
#
# Python 3, stdlib only. No import of the live server; call run_probe() from the gas
# collect path (or standalone) and hand it a GasState to carry across cycles.

from __future__ import annotations
import json, math, os, time, datetime, urllib.request, urllib.error
from dataclasses import dataclass, field, asdict

# ───────────────────────── National Gas REST API ────────────────────────────
GAS_API_BASE = os.environ.get("NG_API_BASE",
                              "https://api.nationalgas.com/operationaldata/v1").rstrip("/")
GAS_GASDAY_URL = GAS_API_BASE + "/publications/gasday"
UA = {"User-Agent": "uk-grid-monitor/1.0 (personal dashboard)",
      "Content-Type": "application/json"}
FETCH_TIMEOUT = 25

# Verified publication IDs (GAS_ALARM_DESIGN.md §3.2). Friendly key -> publicationId.
PUB = {
    "pclp1":        "PUBOB30",       # Predicted Closing Linepack (PCLP1) — primary metric
    "pclp2":        "PUBOB31",       # Predicted Closing Linepack (PCLP2)
    "forecast_min": "PUBOBJ111271",  # Forecast Minimum Linepack — the published floor
    "opening":      "PUBOB693",      # Opening linepack, actual
    "actual_lp":    "PUBOBJ486",     # Linepack, Hourly Actual, Aggregate (near-real-time)
    "change_lp":    "PUBOB3985",     # Change in linepack (day-on-day)
    "gbn_today":    "PUBOBJ111154",  # Gas Balancing Notification — Today   (official)
    "gbn_tomorrow": "PUBOBJ111155",  # Gas Balancing Notification — Tomorrow (official)
    "gdw_today":    "PUBOBJ2054",    # Gas Deficit Warning — Today   (official)
    "gdw_tomorrow": "PUBOBJ2055",    # Gas Deficit Warning — Tomorrow (official)
    "entry_total":  "PUBOBJ1162",    # Total Aggregate System Entry NOMINATIONS (kWh) — logged only
    "exit_total":   "PUBOBJ1163",    # Total Aggregate System Exit NOMINATIONS (kWh) — logged only
    "demand_fc":    "PUBOB623",      # Demand Forecast, NTS (mcm, gas day; D+1 from ~13:10)
    "storage_stock": "PUBOBJ330",    # Storage, Daily Aggregated Stock level, D+1 (kWh)
    "sap":          "PUBOB47",       # System Average Price (p/kWh), daily
}
# Daily deliverability (kWh/d) of each LNG terminal and storage site — the LNG and storage
# parts of Expected Available Supply (EAC), the quantity a real Margins Notice compares
# forecast demand against. Verified against the live API 2026-10-25 (storage total matched
# PUBOBJ335 "Storage, Daily Available Deliverability"). Rough is left out (261028.1):
# Centrica paused storage there in 2025 and National Gas excludes it from the storage
# stock; its live flow is production and is counted in non-storage supply instead, so
# this total is PUBOBJ335 less Rough's declared deliverability (~88 GWh/d).
DELIV_LNG = {"PUBOBJ2451": "Isle of Grain", "PUBOBJ2459": "South Hook", "PUBOBJ2460": "Dragon"}
DELIV_STORAGE = {"PUBOBJ2449": "Humbly Grove", "PUBOBJ2450": "Hornsea",
                 "PUBOBJ2453": "Hatfield Moor", "PUBOBJ2454": "Hole House Farm",
                 "PUBOBJ2455": "Aldbrough", "PUBOBJ2456": "Holford", "PUBOBJ2457": "Hill Top",
                 "PUBOBJ2458": "Stublach"}
KWH_PER_M3 = 10.97      # ~39.5 MJ/m3 typical NTS calorific value (kWh -> m3)
# Values that belong to ONE gas day: take the row for the current gas day only (never the
# next day's early prints — PCLP for tomorrow is published as 0 the afternoon before — and
# never yesterday's). Before 261024.1 the newest row of any day was used, so just after
# midnight PCLP came from the next gas day while the forecast minimum came from today.
DAY_STRICT = {"pclp1", "pclp2", "forecast_min", "opening", "entry_total", "exit_total"}
INSTFLOW_URL = GAS_API_BASE + "/instantaneousflow/sites"
# Entry points that are NOT non-storage supply (storage and LNG are counted by deliverability)
# Rough (EASINGTON ROUGH ST) is not here: storage paused, its flow is production (non-storage supply)
_STORAGE_SITES = ("ALDBROUGH", "HILLTOP", "HOLE HOUSE", "HOLFORD", "HORNSEA", "STUBLACH",
                  "HATFIELD", "HUMBLY")
_LNG_SITES = ("GRAIN", "DRAGON", "SOUTH HOOK")

# ───────────────────────── tunables (GAS_ALARM_DESIGN.md §6, first-pass) ─────
# All first-pass — to be calibrated from a logged winter before any tone is trusted.
# Alarm ladder (261024.2). First-pass levels anchored on National Gas's peak-day scale
# (a peak day can exceed 400 mcm; 417 mcm in 2017); to be calibrated over the winter.
MN_NARROW_FRAC       = 0.90     # demand / expected available supply -> notice
MN_NARROW_HEADROOM   = 50.0     #   ... or spare supply below this (mcm/d) -> notice
MN_EARLY_FRAC        = 0.95     # -> warn (National Gas's own early-warning band)
MN_FIRM_FRAC         = 1.00     # -> crit (the real Margins Notice trigger)
DEMAND_HIGH_MCM      = 300.0    # forecast demand -> notice
DEMAND_VHIGH_MCM     = 350.0    # -> warn
DEMAND_EXCEPT_MCM    = 400.0    # -> crit, only if the supply margin agrees (ratio >= MN_NARROW_FRAC)
PCLP_REVISION_MCM    = 10.0     # PCLP1 revised down by more than this over ...
PCLP_REVISION_S      = 3 * 3600 # ... this window, same gas day -> notice
LP_BELOW_MIN_MCM     = 5.0      # live linepack this far below today's forecast minimum -> warn
STORAGE_COVER_DAYS   = 5.0      # storage stock / full deliverability below this -> notice
PRICE_STRESS_X       = 2.0      # SAP above this multiple of its 30-day median -> notice
PRICE_HIST_DAYS      = 30
PCLP_MARGIN_WARN_MCM = 5.0      # PCLP1 above forecast-min by less than this -> warn
#                                 (PCLP1 - forecast-min <= 0 -> crit). Live comfortable
#                                 margin on 2026-09-19 was ~+10 mcm.
LP_FALL_K            = 2.5      # per-hour baseline: fall beyond mean - k*sd -> abnormal
LP_FALL_MIN_N        = 8       # samples in an hour-bin before its baseline is trusted
LP_SAMPLE_MIN_H      = 0.5     # only fold linepack change into the baseline when the gap
LP_SAMPLE_MAX_H      = 2.0     # between samples is roughly one hour (else it's a restart/gap)

GAS_DWELL_S     = 2 * 3600     # a derived state must persist this long before speaking
GAS_HEARTBEAT_S = 3 * 3600     # re-announce cadence while a derived state stays elevated
LP_HIST_WINDOW_S = 7 * 24 * 3600   # keep a week of linepack samples in memory (trend)

_TIER_RANK = {"notice": 1, "warn": 2, "crit": 3}

# ───────────────────────── spoken phrases ───────────────────────────────────
# Official National Gas notices — spoken as fact (no "derived" caveat).
GDW_PHRASE = ("Gas Deficit Warning in force. National Gas expects gas supply and demand "
              "to be tight.")
GBN_PHRASE = ("Gas Balancing Notification in force. National Gas has flagged a material "
              "risk to the end-of-day gas balance.")
# Derived proxies — always prefixed "Gas" and, on screen, labelled a derived early-warning.
PCLP_WARN_PHRASE = ("Gas: predicted closing linepack is within {margin} million cubic "
                    "metres of the forecast minimum.")
PCLP_CRIT_PHRASE = ("Gas: predicted closing linepack is at or below the forecast minimum.")
MN_NARROW_PHRASE = ("Gas: the supply margin for {day} is narrowing, about {spare} million "
                    "cubic metres spare.")
MN_EARLY_PHRASE  = ("Gas: forecast demand for {day} is within five percent of expected "
                    "available supply.")
MN_WARN_PHRASE   = ("Gas: forecast demand for {day} has reached expected available supply.")
PCLP_REV_PHRASE  = ("Gas: predicted closing linepack has fallen by {drop} million cubic metres "
                    "in the last three hours.")
LP_BELOW_PHRASE  = ("Gas: linepack is {below} million cubic metres below National Gas's "
                    "forecast minimum for today.")
STORAGE_PHRASE   = ("Gas: storage stocks would last about {days} days at full withdrawal.")
PRICE_PHRASE     = ("Gas: the system average price is {x} times its 30-day median, a sign of "
                    "a tight market.")
DEMAND_PHRASES   = {
    "notice": "Gas: high demand forecast for {day}, {mcm} million cubic metres.",
    "warn":   "Gas: very high demand forecast for {day}, {mcm} million cubic metres.",
    "except": "Gas: exceptional demand forecast for {day}, {mcm} million cubic metres.",
    "crit":   "Gas: exceptional demand forecast for {day}, {mcm} million cubic metres, "
              "close to the supply available.",
}
RATE_PHRASE      = ("Gas: linepack is falling faster than normal for this time of day.")
ALL_CLEAR_PHRASE = ("Gas: supply and demand are back to normal.")


# ───────────────────────── persistent state ─────────────────────────────────
@dataclass
class GasState:
    """Held by the caller across cycles (in memory, or JSON round-tripped)."""
    lp_hist: list = field(default_factory=list)       # [[ts, actual_lp_mcm]]
    hour_bins: dict = field(default_factory=dict)     # "H" -> [n, sum, sumsq] of LP change/hr
    last_lp: "float | None" = None
    last_lp_ts: "float | None" = None
    # cadence / edge-trigger memory
    announced: dict = field(default_factory=dict)     # "derived" -> {sig, pk, next}
    group: str = "clear"                              # current derived group (for dwell)
    group_since: float = 0.0
    episode_spoke: bool = False                       # did this derived episode announce?
    notices: dict = field(default_factory=dict)       # base -> episode id last announced
    pclp_hist: list = field(default_factory=list)     # [[ts, pclp1, gas_day]] for revisions
    demand_said: dict = field(default_factory=dict)   # forecast day -> highest tier announced
    events: list = field(default_factory=list)        # last spoken events, for the dashboard

    def to_json(self): return json.dumps(asdict(self))
    @classmethod
    def from_json(cls, s): return cls(**json.loads(s)) if s else cls()


# ───────────────────────── small helpers ────────────────────────────────────
def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _notice_active(raw):
    """A published notice item reads value=None when nothing is in force (verified). Treat
    any non-empty, non-falsey value as an active notice; carry the raw value for wording."""
    if raw is None:
        return False
    s = str(raw).strip().lower()
    return s not in ("", "none", "null", "false", "0", "no", "n", "off", "inactive")


def _accum(cell, x):
    cell[0] += 1
    cell[1] += x
    cell[2] += x * x


def _stats(cell):
    n, s, ss = cell
    if not n:
        return 0, 0.0, 0.0
    mean = s / n
    var = max(0.0, ss / n - mean * mean)
    return n, mean, math.sqrt(var)


# ───────────────────────── fetch ────────────────────────────────────────────
def _uk_offset_h(t):
    """UK clock offset (h) at a UTC datetime: BST from 01:00 UTC on the last Sunday of March
    to 01:00 UTC on the last Sunday of October. Stdlib only (no tz database on Windows)."""
    def last_sun(y, mo):
        d = datetime.datetime(y, mo, 31, 1, tzinfo=datetime.timezone.utc)
        return d - datetime.timedelta(days=(d.weekday() + 1) % 7)
    return 1 if last_sun(t.year, 3) <= t < last_sun(t.year, 10) else 0


def gas_day(now=None):
    """Gas day (datetime.date) at epoch `now`: 06:00 to 06:00 UK clock time."""
    t = datetime.datetime.fromtimestamp(now or time.time(), datetime.timezone.utc)
    return (t + datetime.timedelta(hours=_uk_offset_h(t) - 6)).date()


def _row_for(rows, day):
    """Most recently issued row whose applicableFor is `day` (ISO), or None."""
    best = None
    for r in rows:
        if str(r.get("applicableFor") or "")[:10] != day:
            continue
        k = str(r.get("generatedTimeStamp") or r.get("applicableAt") or "")
        if best is None or k >= best[0]:
            best = (k, r)
    return best[1] if best else None


def _latest_row(rows):
    """Most recently applicable / issued row, whatever its day."""
    return max(rows, key=lambda r: (str(r.get("applicableAt") or ""),
                                    str(r.get("generatedTimeStamp") or ""))) if rows else {}


def fetch_latest(ids=None, timeout=FETCH_TIMEOUT, now=None):
    """POST /publications/gasday for the latest value of each publicationId. Returns
    {publicationId: {value, applicableFor, ...}}, aligned to the current gas day (see
    DAY_STRICT), plus "_nss" (non-storage supply now, mcm/d) and "_gas_day". Raises on
    network/HTTP error so the caller can flag the feed; never invents data."""
    ids = list(ids) if ids is not None else (list(PUB.values()) + list(DELIV_LNG) + list(DELIV_STORAGE))
    gd = gas_day(now)
    body = json.dumps({
        "fromDate": (gd - datetime.timedelta(days=2)).isoformat(),
        "toDate": (gd + datetime.timedelta(days=1)).isoformat(),
        "publicationIds": ids,
        "latestValue": "Y",
    }).encode()
    req = urllib.request.Request(GAS_GASDAY_URL, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    strict = {PUB[k] for k in DAY_STRICT}
    today, tomorrow = gd.isoformat(), (gd + datetime.timedelta(days=1)).isoformat()
    out = {}
    for block in (data or []):
        pid = block.get("publicationId")
        pubs = block.get("publications") or []
        if pid in strict:
            row = _row_for(pubs, today) or {}
        elif pid == PUB["demand_fc"]:
            # day-ahead forecast once published (~13:10), as a real Margins Notice is
            row = _row_for(pubs, tomorrow) or _row_for(pubs, today) or {}
        elif pid in (PUB["gbn_today"], PUB["gdw_today"]):
            row = _row_for(pubs, today) or _latest_row(pubs)
        elif pid in (PUB["gbn_tomorrow"], PUB["gdw_tomorrow"]):
            row = _row_for(pubs, tomorrow) or _latest_row(pubs)
        elif pid == PUB["sap"]:
            row = _latest_row(pubs)
        else:
            row = _latest_row(pubs)
        out[pid] = row
    out["_gas_day"] = {"value": today}
    try:
        out["_sap_hist"] = {"value": fetch_sap_history(gd, timeout)}
    except Exception:
        out["_sap_hist"] = {}
    try:
        nss, lp_live = fetch_nss(timeout)
        out["_nss"] = {"value": nss}
        # PUBOBJ486 is a D+1 publication (yesterday's hours), so the rate guard was fed
        # one unchanging value all day; use the live NTS linepack instead.
        if lp_live is not None:
            out[PUB["actual_lp"]] = {"value": lp_live, "source": "instantaneous"}
    except Exception:
        out["_nss"] = {}
    return out


def fetch_sap_history(gd, timeout=FETCH_TIMEOUT):
    """[(gas_day, SAP p/kWh)] for the last PRICE_HIST_DAYS + 1 gas days, oldest first."""
    body = json.dumps({
        "fromDate": (gd - datetime.timedelta(days=PRICE_HIST_DAYS + 1)).isoformat(),
        "toDate": gd.isoformat(), "publicationIds": [PUB["sap"]], "latestValue": "Y",
    }).encode()
    req = urllib.request.Request(GAS_GASDAY_URL, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    rows = []
    for block in (data or []):
        for r in block.get("publications") or []:
            v = _to_float(r.get("value"))
            day = str(r.get("applicableFor") or "")[:10]
            if v is not None and v > 0 and day:
                rows.append((day, v))
    return sorted(dict(rows).items())


def fetch_nss(timeout=FETCH_TIMEOUT):
    """(non-storage supply now in mcm/d, live NTS linepack in mcm) from the 2-minute
    instantaneous flows. Non-storage supply = entry-point flows less storage and LNG sites:
    UK Continental Shelf, Norway and interconnector imports."""
    req = urllib.request.Request(INSTFLOW_URL, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    tot, n, lp = 0.0, 0, None
    for grp in d.get("instantaneousFlow") or []:
        desc = str(grp.get("description", "")).lower()
        if "linepack" in desc:
            for site in grp.get("sites") or []:
                det = site.get("siteGasDetail") or []
                if det and det[-1].get("flowRate") is not None:
                    lp = round(float(det[-1]["flowRate"]), 2)
            continue
        if "entry points" not in desc:
            continue
        for site in grp.get("sites") or []:
            name = str(site.get("siteName", "")).upper()
            if any(k in name for k in _STORAGE_SITES + _LNG_SITES):
                continue
            det = site.get("siteGasDetail") or []
            if det and det[-1].get("flowRate") is not None:
                tot += float(det[-1]["flowRate"]); n += 1
    return (round(tot, 1) if n else None), lp


def _eac(blocks):
    """Expected Available Supply estimate (mcm/d) = non-storage supply now + LNG and storage
    deliverability. None if any part is missing. Returns (eac, parts)."""
    nss = _to_float((blocks.get("_nss") or {}).get("value"))
    def deliv(ids):
        vals = [_to_float((blocks.get(i) or {}).get("value")) for i in ids]
        if not any(v is not None for v in vals):
            return None
        return sum(v for v in vals if v is not None) / KWH_PER_M3 / 1e6
    lng, sto = deliv(DELIV_LNG), deliv(DELIV_STORAGE)
    if nss is None or lng is None or sto is None:
        return None, None
    parts = {"nss": round(nss, 1), "lng": round(lng, 1), "storage": round(sto, 1)}
    return round(nss + lng + sto, 1), parts


def _val(blocks, key):
    b = blocks.get(PUB[key]) or {}
    v = b.get("value")
    if key in ("pclp1", "pclp2") and _to_float(v) is not None and _to_float(v) <= 0:
        return None          # placeholder 0 printed before the gas day's first real PCLP
    return v


def _fval(blocks, key):
    return _to_float(_val(blocks, key))


# ───────────────────────── per-hour linepack baseline ───────────────────────
def _update_baseline(state, actual_lp, now):
    """Fold the linepack change since the last sample into a per-(local)-hour running
    baseline, and return this interval's change-per-hour (or None). Diurnal-safe: the
    afternoon drawdown lives in the same hour-bin every day, so 'abnormal' means abnormal
    FOR THIS HOUR, not merely 'falling'."""
    change_per_h = None
    if actual_lp is not None:
        if state.last_lp is not None and state.last_lp_ts:
            dt_h = (now - state.last_lp_ts) / 3600.0
            if LP_SAMPLE_MIN_H <= dt_h <= LP_SAMPLE_MAX_H:
                change_per_h = (actual_lp - state.last_lp) / dt_h
                hr = datetime.datetime.fromtimestamp(state.last_lp_ts).hour   # local hour
                cell = state.hour_bins.setdefault(str(hr), [0, 0.0, 0.0])
                _accum(cell, change_per_h)
        state.last_lp = actual_lp
        state.last_lp_ts = now
        state.lp_hist.append([now, actual_lp])
        state.lp_hist = [e for e in state.lp_hist if now - e[0] <= LP_HIST_WINDOW_S]
    return change_per_h


def _rate_abnormal(state, change_per_h, now):
    """True only when this interval's fall is beyond the mature per-hour baseline (a fall
    far more negative than normal for this time of day). Corroborator, not a primary."""
    if change_per_h is None or change_per_h >= 0:
        return False
    hr = datetime.datetime.fromtimestamp(now).hour
    n, mean, sd = _stats(state.hour_bins.get(str(hr), [0, 0.0, 0.0]))
    if n < LP_FALL_MIN_N or sd <= 0:
        return False
    return change_per_h < (mean - LP_FALL_K * sd)


# ───────────────────────── official notice edges ────────────────────────────
def _notice_events(state, blocks):
    """Edge-triggered official GDW / GBN. Announce on first appearance or a changed value;
    a one-shot 'no longer in force' on clear. Today/Tomorrow are de-duplicated by episode
    identity (the raw value), so a notice rolling from tomorrow->today doesn't re-announce."""
    events = []
    specs = [
        ("gdw", "gdw_today", "gdw_tomorrow", "warn", "Gas Deficit Warning", GDW_PHRASE),
        ("gbn", "gbn_today", "gbn_tomorrow", "crit", "Gas Balancing Notification", GBN_PHRASE),
    ]
    for base, k_today, k_tom, tier, name, phrase in specs:
        raw_today, raw_tom = _val(blocks, k_today), _val(blocks, k_tom)
        active_today = _notice_active(raw_today)
        active = active_today or _notice_active(raw_tom)
        raw = raw_today if active_today else raw_tom
        prev = state.notices.get(base)
        if active:
            epid = (str(raw).strip() or "active")
            if prev != epid:
                events.append({"tier": tier, "key": "gas_" + base, "phrase": phrase,
                               "raw": raw, "official": True})
            state.notices[base] = epid
        elif prev is not None:
            events.append({"tier": "notice", "key": "gas_" + base + "_clear",
                           "phrase": "The " + name + " is no longer in force.",
                           "official": True})
            state.notices.pop(base, None)
    return events


# ───────────────────────── derived signals + scheduler ──────────────────────
def _demand_day(blocks):
    """'tomorrow' when the demand forecast is the day-ahead one, else 'today'."""
    b = blocks.get(PUB["demand_fc"]) or {}
    gd = (blocks.get("_gas_day") or {}).get("value")
    return "tomorrow" if gd and str(b.get("applicableFor") or "")[:10] > gd else "today"


def _mn_ratio(blocks):
    demand = _fval(blocks, "demand_fc")
    eac, _parts = _eac(blocks)
    return (demand / eac, eac - demand) if (demand and eac) else (None, None)


def _storage_cover_days(blocks):
    stock = _fval(blocks, "storage_stock")
    deliv = [_to_float((blocks.get(i) or {}).get("value")) for i in DELIV_STORAGE]
    deliv = sum(v for v in deliv if v is not None)
    return (stock / deliv) if (stock is not None and stock > 0 and deliv > 0) else None


def _price_stress(blocks):
    """(today's SAP / 30-day median of the days before) or None."""
    hist = (blocks.get("_sap_hist") or {}).get("value") or []
    if len(hist) < 10:
        return None
    *prev, (_, last) = hist
    vals = sorted(v for _, v in prev[-PRICE_HIST_DAYS:])
    med = vals[len(vals) // 2]
    return (last / med) if med > 0 else None


def _pclp_revision(state, blocks, now):
    """Fall in PCLP1 from its highest value in the last PCLP_REVISION_S (same gas day)."""
    pclp1 = _fval(blocks, "pclp1")
    gd = (blocks.get("_gas_day") or {}).get("value")
    if pclp1 is None or not gd:
        return None
    state.pclp_hist.append([now, pclp1, gd])
    state.pclp_hist = [e for e in state.pclp_hist if now - e[0] <= PCLP_REVISION_S + 600]
    same = [e[1] for e in state.pclp_hist if e[2] == gd and now - e[0] <= PCLP_REVISION_S]
    return (max(same) - pclp1) if len(same) >= 2 else None


def _derived_candidates(blocks, state, change_per_h, now, pclp_drop=None):
    """Build the derived (proxy) signals. Each: (key, tier, phrase, pk)."""
    cands = []
    pclp1 = _fval(blocks, "pclp1")
    fmin = _fval(blocks, "forecast_min")
    if pclp1 is not None and fmin is not None:
        margin = pclp1 - fmin
        if margin <= 0:
            cands.append(("pclp", "crit", PCLP_CRIT_PHRASE, 3))
        elif margin < PCLP_MARGIN_WARN_MCM:
            cands.append(("pclp", "warn", PCLP_WARN_PHRASE.format(margin=round(margin, 1)), 2))
    # MN-equivalent: National Gas's NTS demand forecast (day-ahead once published) vs an
    # Expected Available Supply estimate (non-storage supply now + LNG and storage
    # deliverability).
    ratio, spare = _mn_ratio(blocks)
    if ratio is not None:
        day = _demand_day(blocks)
        if ratio >= MN_FIRM_FRAC:
            cands.append(("mn", "crit", MN_WARN_PHRASE.format(day=day), 3))
        elif ratio >= MN_EARLY_FRAC:
            cands.append(("mn", "warn", MN_EARLY_PHRASE.format(day=day), 2))
        elif ratio >= MN_NARROW_FRAC or spare < MN_NARROW_HEADROOM:
            cands.append(("mn", "notice", MN_NARROW_PHRASE.format(day=day, spare=int(round(spare))), 1))
    # live linepack below today's forecast minimum
    lp = _fval(blocks, "actual_lp")
    if lp is not None and fmin is not None and fmin - lp > LP_BELOW_MIN_MCM:
        cands.append(("lpmin", "warn", LP_BELOW_PHRASE.format(below=int(round(fmin - lp))), 2))
    # PCLP revised down
    if pclp_drop is not None and pclp_drop > PCLP_REVISION_MCM:
        cands.append(("pclprev", "notice", PCLP_REV_PHRASE.format(drop=int(round(pclp_drop))), 1))
    # storage cover
    cover = _storage_cover_days(blocks)
    if cover is not None and cover < STORAGE_COVER_DAYS:
        cands.append(("storage", "notice", STORAGE_PHRASE.format(days=round(cover, 1)), 1))
    # price stress
    px = _price_stress(blocks)
    if px is not None and px >= PRICE_STRESS_X:
        cands.append(("price", "notice", PRICE_PHRASE.format(x=round(px, 1)), 1))
    # rate-of-change: a corroborator only — it speaks only alongside another signal
    if cands and _rate_abnormal(state, change_per_h, now):
        cands.append(("rate", "warn", RATE_PHRASE, 2))
    return cands


def _demand_events(state, blocks):
    """Forecast-demand level: context, not danger, so it is announced ONCE per forecast day
    and tier (an upgrade announces again, a downgrade never), with no dwell. 'Exceptional'
    is critical only when the supply margin agrees; otherwise it is capped at a warning."""
    demand = _fval(blocks, "demand_fc")
    day_iso = str((blocks.get(PUB["demand_fc"]) or {}).get("applicableFor") or "")[:10]
    if not demand or not day_iso:
        return []
    ratio, _spare = _mn_ratio(blocks)
    tier = None
    if demand >= DEMAND_EXCEPT_MCM:
        tier = "crit" if (ratio is not None and ratio >= MN_NARROW_FRAC) else "warn"
    elif demand >= DEMAND_VHIGH_MCM:
        tier = "warn"
    elif demand >= DEMAND_HIGH_MCM:
        tier = "notice"
    # forget days that are over
    gd = (blocks.get("_gas_day") or {}).get("value") or ""
    state.demand_said = {k: v for k, v in state.demand_said.items() if k >= gd}
    if tier is None:
        return []
    prev = state.demand_said.get(day_iso)
    if prev and _TIER_RANK[prev] >= _TIER_RANK[tier]:
        return []
    state.demand_said[day_iso] = tier
    pkey = ("crit" if tier == "crit" else "except" if demand >= DEMAND_EXCEPT_MCM else tier)
    return [{"tier": tier, "key": "gas_demand",
             "phrase": DEMAND_PHRASES[pkey].format(day=_demand_day(blocks), mcm=int(round(demand)))}]


def _schedule_derived(state, top, now):
    """Dwell + heartbeat + signature gating for the single most-severe derived signal.
    Mirrors the rain engine: announce once certain (dwell), then on a material change or a
    slow heartbeat; an intensity DOWNGRADE never re-announces; all-clear is a one-shot."""
    grp = (top[0] + ":" + top[1]) if top else "clear"
    if grp != state.group:
        state.group = grp
        state.group_since = now
    stable = now - (state.group_since or now)

    out = []
    if top is None:
        if state.episode_spoke:
            out.append({"tier": "notice", "key": "gas_clear", "phrase": ALL_CLEAR_PHRASE})
        state.episode_spoke = False
        state.announced = {}
        return out

    key, tier, phrase, pk = top
    if stable < GAS_DWELL_S:
        return out                                   # not yet certain — stay silent

    prev = state.announced.get("derived")
    sig = [key, tier]
    sig_changed = (prev is None) or (prev.get("sig") != sig)
    not_downgrade = (prev is None) or (pk >= prev.get("pk", 0))
    due = (prev is None) or (now >= prev.get("next", 0)) or (sig_changed and not_downgrade)
    if due:
        out.append({"tier": tier, "key": "gas_" + key, "phrase": phrase})
        state.episode_spoke = True
        state.announced = {"derived": {"sig": sig, "pk": pk, "next": now + GAS_HEARTBEAT_S}}
    else:
        state.announced = {"derived": {"sig": prev.get("sig"), "pk": prev.get("pk", pk),
                                       "next": prev.get("next", now + GAS_HEARTBEAT_S)}}
    return out


# ───────────────────────── main entry ───────────────────────────────────────
def run_probe(state: GasState, *, now=None, blocks=None, sampler=fetch_latest):
    """Evaluate one cycle. Read-only: mutates only `state`. Returns a diagnostic dict with
    `would_speak` (what the voice WOULD say this cycle — no tone is played), the raw values,
    the computed signals, and a one-line `log`. Pass `blocks` to evaluate injected data
    (tests); otherwise it fetches live via `sampler`."""
    now = now or time.time()

    feed_ok, feed_err = True, None
    if blocks is None:
        try:
            blocks = sampler()
        except Exception as e:
            blocks, feed_ok = {}, False
            feed_err = f"{type(e).__name__}: {str(e)[:120]}"

    # linepack + diurnal baseline
    actual_lp = _fval(blocks, "actual_lp")
    change_per_h = _update_baseline(state, actual_lp, now)

    # signals
    notice_events = _notice_events(state, blocks)
    pclp_drop = _pclp_revision(state, blocks, now)
    derived = _derived_candidates(blocks, state, change_per_h, now, pclp_drop)
    top = max(derived, key=lambda c: _TIER_RANK[c[1]], default=None)
    derived_speak = _schedule_derived(state, top, now)
    demand_speak = _demand_events(state, blocks) if feed_ok else []

    # official notices are edge-triggered and always announce; demand level once per day and
    # tier; derived rides the dwell/heartbeat scheduler.
    would_speak = notice_events + demand_speak + derived_speak
    # event list for the dashboard voice: each spoken line once, by timestamp
    for k, w in enumerate(would_speak):
        state.events.append(dict(w, ts=round(now, 3), id=f"{now:.3f}-{k}"))
    state.events = state.events[-20:]

    pclp1 = _fval(blocks, "pclp1")
    fmin = _fval(blocks, "forecast_min")
    margin = (round(pclp1 - fmin, 2) if (pclp1 is not None and fmin is not None) else None)
    signals = {
        "feed_ok": feed_ok, "feed_err": feed_err,
        "pclp1": pclp1, "pclp2": _fval(blocks, "pclp2"), "forecast_min": fmin,
        "pclp_margin_mcm": margin,
        "opening": _fval(blocks, "opening"), "actual_lp": actual_lp,
        "change_per_h": (round(change_per_h, 2) if change_per_h is not None else None),
        "entry_total": _fval(blocks, "entry_total"),
        "exit_total": _fval(blocks, "exit_total"),
        "gas_day": (blocks.get("_gas_day") or {}).get("value"),
        "demand_fc": _fval(blocks, "demand_fc"),
        "demand_fc_for": (blocks.get(PUB["demand_fc"]) or {}).get("applicableFor"),
        "eac": _eac(blocks)[0], "eac_parts": _eac(blocks)[1],
        "mn_ratio": (round(_fval(blocks, "demand_fc") / _eac(blocks)[0], 3)
                     if (_fval(blocks, "demand_fc") and _eac(blocks)[0]) else None),
        "gdw_today": _val(blocks, "gdw_today"), "gdw_tomorrow": _val(blocks, "gdw_tomorrow"),
        "gbn_today": _val(blocks, "gbn_today"), "gbn_tomorrow": _val(blocks, "gbn_tomorrow"),
        "derived_state": (top[0] + "/" + top[1]) if top else "clear",
        "derived_all": [c[0] + "/" + c[1] for c in derived],
        "spare_mcm": (round(_mn_ratio(blocks)[1], 1) if _mn_ratio(blocks)[1] is not None else None),
        "pclp_drop_3h": (round(pclp_drop, 2) if pclp_drop is not None else None),
        "storage_stock_kwh": _fval(blocks, "storage_stock"),
        "storage_cover_days": (round(_storage_cover_days(blocks), 1)
                               if _storage_cover_days(blocks) is not None else None),
        "sap": _fval(blocks, "sap"),
        "price_x_median": (round(_price_stress(blocks), 2)
                           if _price_stress(blocks) is not None else None),
        "demand_said": dict(state.demand_said),
    }

    log = (f"GAS feed={'ok' if feed_ok else 'DOWN'}"
           + (f"({feed_err})" if feed_err else "")
           + f" day={signals['gas_day']} pclp1={pclp1} fmin={fmin} margin={margin}"
           + f" demand_fc={signals['demand_fc']} eac={signals['eac']} ratio={signals['mn_ratio']}"
           + f" actual_lp={actual_lp}"
           + (f" dLP/h={signals['change_per_h']}" if change_per_h is not None else "")
           + f" derived={signals['derived_state']}"
           + (" | WOULD SPEAK: " + " || ".join(w["phrase"] for w in would_speak)
              if would_speak else ""))

    return {"ts": now, "would_speak": would_speak, "signals": signals, "log": log,
            "events": list(state.events)}


# ───────────────────────── history log (calibration) ────────────────────────
_HIST_PREFIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gas_probe-")


def _hist_path(ts):
    m = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m")
    return _HIST_PREFIX + m + ".jsonl"


def log_sample(diag):
    """Append one JSONL row of the raw values + signals for calibration. Never raises."""
    try:
        row = {"ts": diag["ts"],
               "iso": datetime.datetime.utcfromtimestamp(diag["ts"]).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "signals": diag["signals"],
               "spoke": [w["phrase"] for w in diag["would_speak"]]}
        with open(_hist_path(diag["ts"]), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row
    except Exception:
        return None


# ───────────────────────── standalone probe ─────────────────────────────────
if __name__ == "__main__":
    st = GasState()
    try:
        diag = run_probe(st)
        print(diag["log"])
        print(json.dumps(diag["signals"], indent=2))
        if diag["would_speak"]:
            print("\nWOULD SPEAK:")
            for w in diag["would_speak"]:
                print(f"  [{w['tier']}] {w['phrase']}")
        else:
            print("\n(nothing to speak — quiet)")
    except Exception as e:
        print("gas_probe self-test could not reach the API:", e)
