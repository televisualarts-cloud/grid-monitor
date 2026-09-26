# gas_probe.py — read-only gas supply/margin DIAGNOSTIC probe for GB Energy Monitor
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
    "entry_total":  "PUBOBJ1162",    # Total Aggregate System Entry — supply proxy
    "exit_total":   "PUBOBJ1163",    # Total Aggregate System Exit — demand
    "demand_fc":    "PUBOB623",      # Demand Forecast, NTS (daily) — demand fallback
}

# ───────────────────────── tunables (GAS_ALARM_DESIGN.md §6, first-pass) ─────
# All first-pass — to be calibrated from a logged winter before any tone is trusted.
MN_EARLY_FRAC        = 0.95     # demand / available supply -> notice
MN_FIRM_FRAC         = 1.00     # demand >= supply -> warn (matches the real MN trigger)
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
MN_EARLY_PHRASE  = ("Gas: forecast demand is within five percent of available supply.")
MN_WARN_PHRASE   = ("Gas: forecast demand has reached available supply.")
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
def fetch_latest(ids=None, timeout=FETCH_TIMEOUT):
    """POST /publications/gasday for the latest value of each publicationId. Returns
    {publicationId: {value, applicableFor, ...}}. Raises on network/HTTP error so the
    caller can flag the feed; never invents data."""
    ids = list(ids) if ids is not None else list(PUB.values())
    today = datetime.date.today()
    body = json.dumps({
        "fromDate": (today - datetime.timedelta(days=2)).isoformat(),
        "toDate": today.isoformat(),
        "publicationIds": ids,
        "latestValue": "Y",
    }).encode()
    req = urllib.request.Request(GAS_GASDAY_URL, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read())
    out = {}
    for block in (data or []):
        pid = block.get("publicationId")
        pubs = block.get("publications") or []
        out[pid] = pubs[-1] if pubs else {}
    return out


def _val(blocks, key):
    b = blocks.get(PUB[key]) or {}
    return b.get("value")


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
def _derived_candidates(blocks, state, change_per_h, now):
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
    # MN-equivalent: demand (exit total, else demand forecast) vs available supply (entry)
    supply = _fval(blocks, "entry_total")
    demand = _fval(blocks, "exit_total")
    if demand is None:
        demand = _fval(blocks, "demand_fc")
    if supply and demand and supply > 0:
        ratio = demand / supply
        if ratio >= MN_FIRM_FRAC:
            cands.append(("mn", "warn", MN_WARN_PHRASE, 2))
        elif ratio >= MN_EARLY_FRAC:
            cands.append(("mn", "notice", MN_EARLY_PHRASE, 1))
    # rate-of-change corroborator
    if _rate_abnormal(state, change_per_h, now):
        cands.append(("rate", "warn", RATE_PHRASE, 2))
    return cands


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
    derived = _derived_candidates(blocks, state, change_per_h, now)
    top = max(derived, key=lambda c: _TIER_RANK[c[1]], default=None)
    derived_speak = _schedule_derived(state, top, now)

    # official notices are edge-triggered and always announce; derived rides the scheduler.
    would_speak = notice_events + derived_speak

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
        "gdw_today": _val(blocks, "gdw_today"), "gdw_tomorrow": _val(blocks, "gdw_tomorrow"),
        "gbn_today": _val(blocks, "gbn_today"), "gbn_tomorrow": _val(blocks, "gbn_tomorrow"),
        "derived_state": (top[0] + "/" + top[1]) if top else "clear",
    }

    log = (f"GAS feed={'ok' if feed_ok else 'DOWN'}"
           + (f"({feed_err})" if feed_err else "")
           + f" pclp1={pclp1} fmin={fmin} margin={margin}"
           + f" actual_lp={actual_lp}"
           + (f" dLP/h={signals['change_per_h']}" if change_per_h is not None else "")
           + f" derived={signals['derived_state']}"
           + (" | WOULD SPEAK: " + " || ".join(w["phrase"] for w in would_speak)
              if would_speak else ""))

    return {"ts": now, "would_speak": would_speak, "signals": signals, "log": log}


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
