#!/usr/bin/env python3
#
# GB Energy Monitor - data backend
# Build 260827.4  (version = YYMMDD.N in UT; bump on every change to this file)
# Copyright (c) 2026 Andy Smith, G7IZU
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
"""
GB Energy Monitor - data backend
================================
Pulls live GB electricity system data from free, key-less public APIs and
serves a single consolidated JSON snapshot the dashboard consumes.

Sources (all free, no API key):
  * Elexon Insights (BMRS)      https://data.elexon.co.uk/bmrs/api/v1
      - FUELINST   instantaneous generation by fuel type (~5 min)
      - FREQ       system frequency (~15 s)
      - MELNGC     indicated generation margin (headroom forecast)
      - SYSWARN    official NESO system warnings
      - demand/outturn  initial national demand outturn
  * Carbon Intensity API        https://api.carbonintensity.org.uk
      - national carbon intensity + generation mix percentages

Run:
    python3 grid_server.py            # serves dashboard + /api/grid on :8000
    python3 grid_server.py --once     # write snapshot.json once and exit
    python3 grid_server.py --port 9000

The dashboard (grid_dashboard.html) fetches /api/grid every 60 s.
"""

import argparse
import json
import math
import os
import random
import re
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
import base64
import concurrent.futures
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Rainfall-alert diagnostic probe (read-only; logs the phrase it WOULD speak).
# Optional: if rain_probe.py isn't beside this file the server runs unchanged.
try:
    import rain_probe as _rain_probe
    _RAIN_PROBE_STATE = _rain_probe.ProbeState()
except Exception:            # never let a missing/broken probe stop the server
    _rain_probe = None
    _RAIN_PROBE_STATE = None

try:
    import owm_onecall as _owm_onecall
except Exception:            # missing helper -> server just uses the free 2.5 call
    _owm_onecall = None

BMRS = "https://data.elexon.co.uk/bmrs/api/v1"
CARBON = "https://api.carbonintensity.org.uk"
UA = {"User-Agent": "uk-grid-monitor/1.0 (personal dashboard)"}

# Single source of truth for this server's build tag. Keep it in step with the
# "# Build YYMMDD.N" header comment above AND the dashboard's HTML build tag —
# bump all three together on every change. It is emitted in the snapshot so the
# dashboard footer can show the REAL running server build instead of a hard-coded
# string that silently goes stale.
SERVER_BUILD = "260827.4"

# ---- Debug logging ----------------------------------------------------------
# Off by default. Enable by running with --debug or setting GRIDMON_DEBUG=1.
# When on, every HTTP fetch logs its URL, outcome, HTTP status, timing, and (on
# error) a snippet of the response body — enough to see which upstream call is
# failing and why, without a heavyweight logging setup.
DEBUG = bool(os.environ.get("GRIDMON_DEBUG"))


def dbg(*args):
    """Print a timestamped debug line to stderr when DEBUG is on."""
    if not DEBUG:
        return
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[dbg {ts}]", *args, file=sys.stderr, flush=True)

# ---- Fuel type presentation -------------------------------------------------
# Elexon FUELINST fuel codes -> (display name, category, colour)
FUEL_META = {
    "CCGT":     ("Gas (CCGT)",    "fossil",         "#f2683c"),
    "OCGT":     ("Gas (OCGT)",    "fossil",         "#f28f5c"),
    "OIL":      ("Oil",           "fossil",         "#8a5a3c"),
    "COAL":     ("Coal",          "fossil",         "#4a4a4a"),
    "NUCLEAR":  ("Nuclear",       "firm",           "#c96fd6"),
    "WIND":     ("Wind",          "renewable",      "#3fb6c9"),
    "NPSHYD":   ("Hydro",         "renewable",      "#4f8cff"),
    "PS":       ("Pumped storage","storage",        "#7a86ff"),
    "BATTERY":  ("Battery",       "storage",        "#e0b0ff"),
    "BIOMASS":  ("Biomass",       "renewable",      "#7bb662"),
    "OTHER":    ("Other",         "other",          "#9aa0aa"),
    "SOLAR":    ("Solar",         "renewable",      "#f5c542"),
    "INTFR":    ("IC France (IFA)",   "interconnector", "#5b8def"),
    "INTIFA2":  ("IC France (IFA2)",  "interconnector", "#5b8def"),
    "INTELEC":  ("IC France (ElecLink)","interconnector","#5b8def"),
    "INTNED":   ("IC Netherlands",    "interconnector", "#5b8def"),
    "INTEW":    ("IC Ireland (E-W)",  "interconnector", "#5b8def"),
    "INTIRL":   ("IC Ireland (Moyle)","interconnector", "#5b8def"),
    "INTNEM":   ("IC Belgium (Nemo)", "interconnector", "#5b8def"),
    "INTNSL":   ("IC Norway (NSL)",   "interconnector", "#5b8def"),
    "INTVKL":   ("IC Denmark (Viking)","interconnector","#5b8def"),
    "INTGRNL":  ("IC Greenlink",      "interconnector", "#5b8def"),
}


def fetch_json(url, timeout=30):
    return _fetch_json_retry(url, timeout=timeout)


# ---- Retry + per-host throttle ---------------------------------------------
# Transient upstream failures (CDN 503 'Backend fetch failed', 502/504 gateway
# errors, 429 rate-limits, and connection timeouts) are common on the free
# public APIs this dashboard uses. Rather than let one blip blank a panel for a
# whole cache cycle, retry a bounded number of times with exponential backoff +
# jitter. Non-transient statuses (400/401/403/404) are a real answer — retrying
# them just wastes time and hammers the upstream — so they raise immediately.
#
# A per-host semaphore caps how many requests are in flight to any single host
# at once. This matters now that the snapshot is built in parallel: without it,
# a cold cache could fire six simultaneous Elexon calls and trip rate-limits or
# make the upstream itself the bottleneck.
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
FETCH_MAX_RETRIES = 2          # 3 attempts total (1 + 2 retries)
FETCH_BACKOFF_BASE = 0.6       # seconds; grows 0.6, 1.2, 2.4 … with jitter
FETCH_BACKOFF_CAP = 6.0        # never sleep longer than this between tries
HOST_MAX_INFLIGHT = 4          # concurrent requests allowed per host

_host_sems = {}
_host_sems_lock = threading.Lock()


def _host_sem(url):
    """Return the concurrency semaphore for this URL's host, creating it once."""
    host = urllib.parse.urlsplit(url).netloc or "?"
    with _host_sems_lock:
        s = _host_sems.get(host)
        if s is None:
            s = threading.BoundedSemaphore(HOST_MAX_INFLIGHT)
            _host_sems[host] = s
        return s


def _retry_after_seconds(e):
    """Parse a Retry-After header (seconds form) from a 429/503 if present."""
    try:
        ra = e.headers.get("Retry-After") if getattr(e, "headers", None) else None
        if ra and ra.strip().isdigit():
            return min(float(ra.strip()), FETCH_BACKOFF_CAP)
    except Exception:
        pass
    return None


def _fetch_json_retry(url, timeout=30, max_retries=FETCH_MAX_RETRIES):
    """GET and parse JSON with bounded retries on transient failures and a
    per-host in-flight cap. On permanent failure raises the last exception with
    the URL, HTTP status, and a short body snippet attached (surfaced when DEBUG
    is on and in callers' out['error'])."""
    sem = _host_sem(url)
    last_exc = None
    for attempt in range(max_retries + 1):
        # Bound the wait to acquire a host slot so a wedged host can't hang the
        # caller indefinitely; treat a slot-starvation as a transient failure.
        got = sem.acquire(timeout=timeout + 5)
        if not got:
            last_exc = TimeoutError(f"host busy: {urllib.parse.urlsplit(url).netloc}")
            dbg(f"GET BUSY (no host slot) {url}")
            break
        req = urllib.request.Request(url, headers=UA)
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
            dbg(f"GET ok   {(time.monotonic()-t0)*1000:.0f}ms  "
                f"{'(try %d) ' % (attempt+1) if attempt else ''}{url}")
            return data
        except urllib.error.HTTPError as e:
            dt = (time.monotonic() - t0) * 1000
            body = ""
            try:
                body = e.read(500).decode("utf-8", "replace")
            except Exception:
                pass
            e.grid_url = url
            e.grid_body = body[:300]
            last_exc = e
            transient = e.code in RETRYABLE_STATUS
            dbg(f"GET HTTP {e.code} {dt:.0f}ms {'transient' if transient else 'permanent'} "
                f"{url}\n       body: {body[:200]!r}")
            if not transient:
                raise         # 4xx (bar 429) is a real answer — don't retry
            wait_hint = _retry_after_seconds(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                json.JSONDecodeError) as e:
            dt = (time.monotonic() - t0) * 1000
            last_exc = e
            dbg(f"GET FAIL {type(e).__name__} {dt:.0f}ms  {url}\n       {e}")
            wait_hint = None
        finally:
            sem.release()

        # Out of attempts? Stop and raise below.
        if attempt >= max_retries:
            break
        # Backoff with jitter (or honour Retry-After if the server gave one).
        delay = wait_hint if wait_hint is not None else min(
            FETCH_BACKOFF_BASE * (2 ** attempt), FETCH_BACKOFF_CAP)
        delay += random.uniform(0, delay * 0.25)
        dbg(f"GET retry in {delay:.1f}s (attempt {attempt+1}/{max_retries}) {url}")
        time.sleep(delay)

    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"fetch failed with no exception: {url}")


def post_json(url, body, timeout=30):
    """POST a JSON body and parse the JSON response. Used by the National Gas
    Published Data API (publications/gasday)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={**UA, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _rows(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def get_generation():
    """Latest instantaneous generation by fuel type (MW), with 1-hour trend."""
    d = _rows(fetch_json(f"{BMRS}/datasets/FUELINST?format=json"))
    if not d:
        return None
    # Latest publish batch
    latest_pub = max(r["publishTime"] for r in d)
    batch = [r for r in d if r["publishTime"] == latest_pub]

    # Find the publish batch closest to 60 minutes earlier for trend arrows
    pubs = sorted(set(r["publishTime"] for r in d))
    t_now = datetime.fromisoformat(latest_pub.replace("Z", "+00:00"))
    target = t_now - timedelta(minutes=60)
    then_pub = min(pubs, key=lambda p: abs(
        datetime.fromisoformat(p.replace("Z", "+00:00")) - target))
    then = {r["fuelType"]: (r.get("generation") or 0)
            for r in d if r["publishTime"] == then_pub}

    fuels = []
    total = 0.0
    ic_net = 0.0
    for r in batch:
        code = r["fuelType"]
        mw = r.get("generation") or 0
        name, cat, colour = FUEL_META.get(code, (code.title(), "other", "#9aa0aa"))
        prev = then.get(code)
        delta = (mw - prev) if prev is not None else None
        fuels.append({"code": code, "name": name, "category": cat,
                      "colour": colour, "mw": mw, "delta_1h": delta})
        if cat == "interconnector":
            ic_net += mw
        else:
            total += max(mw, 0)
    fuels.sort(key=lambda f: f["mw"], reverse=True)
    return {"publishTime": latest_pub, "trend_from": then_pub, "fuels": fuels,
            "generation_total_mw": round(total),
            "interconnector_net_mw": round(ic_net)}


def get_frequency():
    """Latest grid frequency (Hz) plus a recent history trace.
    Uses the near-real-time system/frequency endpoint (15-second cadence,
    ~1-2 minute latency) with an explicit recent window. The older
    datasets/FREQ archive feed lags to the previous midnight, so it is only
    a fallback if the live endpoint returns nothing."""
    now = datetime.now(timezone.utc)
    frm = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = []
    for url in (f"{BMRS}/system/frequency?format=json&from={frm}&to={to}",
                f"{BMRS}/datasets/FREQ?format=json"):   # fallback only
        try:
            rows += _rows(fetch_json(url))
            if rows:
                break     # prefer the live endpoint; skip the laggy fallback
        except Exception:
            pass
    if not rows:
        return None
    # de-dup on measurementTime, sort ascending
    uniq = {r["measurementTime"]: r["frequency"] for r in rows if r.get("frequency") is not None}
    ordered = sorted(uniq.items())
    latest_t, latest_hz = ordered[-1]
    # down-sample the last window to <=120 points for the trace, keeping timestamps
    tail = ordered[-720:]
    stepn = max(1, len(tail) // 120)
    sampled = tail[::stepn]
    trace = [round(hz, 3) for _, hz in sampled]
    trace_points = [{"t": t, "hz": round(hz, 3)} for t, hz in sampled]
    return {"time": latest_t, "hz": latest_hz,
            "trace": trace,                 # kept for the live-append path
            "trace_points": trace_points,   # timestamped, for axis labels
            "window_start": sampled[0][0] if sampled else latest_t,
            "window_end": latest_t}


# ---- Fast frequency feed (phase-learned burst rhythm) ----------------------
# BMRS frequency is 15-second RESOLUTION but publishes in ~2-minute BURSTS: the
# newest timestamp sits still for ~2 min, then jumps forward ~2 min at once,
# bringing ~8 fresh 15s points together. So we phase-learn the BURST cadence
# (~120s) — not the 15s resolution — and fetch just after each burst is due.
# The client then animates the dial through the burst's 15s points for a live
# FEEL, while every displayed age stays the measured timestamp (honest: it's
# visibly replaying data 1-2 min old, never claiming real-time).
_freq_fast_cache = {"data": None, "ts": 0, "next_due": 0,
                    "last_sample_t": None,   # epoch of newest sample last seen
                    "period_s": None,        # learned burst period (~120s)
                    "newest_age_s": None}
FREQ_BURST_PERIOD = 120.0     # observed ~2-min publish burst cadence
FREQ_FAST_MIN = 10.0          # never refetch more often than this (rate guard)
FREQ_FAST_MAX = 115.0         # normal wait between bursts (just under one period)
FREQ_PHASE_LEAD = 6.0         # poll this many s after a burst is expected
FREQ_RETRY = 10.0             # if a burst is due but hasn't arrived, retry this often


def get_frequency_fast():
    """Return the freshest frequency payload, refetching only when the next
    ~2-min publish burst is due (phase-learned). Serves cache between bursts.
    Includes a full-resolution 15s recent tail so the client can animate the
    dial through the newly-arrived points."""
    c = _freq_fast_cache
    now_mono = time.monotonic()
    if c["data"] is not None and now_mono < c["next_due"]:
        return c["data"]        # not due yet — serve cache, no upstream call
    freq = get_frequency()
    now_wall = datetime.now(timezone.utc).timestamp()
    if not freq:
        c["next_due"] = now_mono + FREQ_FAST_MIN
        return c["data"]
    newest_epoch = _ea_parse_dt(freq.get("time"))
    # Attach a FULL-RESOLUTION recent tail (undecimated 15s points) so the client
    # can sweep the dial through the last couple of minutes of real movement.
    try:
        now2 = datetime.now(timezone.utc)
        frm2 = (now2 - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to2 = now2.strftime("%Y-%m-%dT%H:%M:%SZ")
        rrows = _rows(fetch_json(f"{BMRS}/system/frequency?format=json&from={frm2}&to={to2}"))
        ru = {r["measurementTime"]: r["frequency"] for r in rrows if r.get("frequency") is not None}
        recent = sorted(ru.items())[-40:]
        freq["recent_points"] = [{"t": t, "hz": round(hz, 3)} for t, hz in recent]
    except Exception:
        freq["recent_points"] = None
    # --- phase-learn the BURST rhythm and schedule just after the next burst ---
    if newest_epoch is not None:
        age = max(0.0, now_wall - newest_epoch)
        c["newest_age_s"] = age
        prev = c.get("last_sample_t")
        period = c.get("period_s") or FREQ_BURST_PERIOD
        got_new = bool(prev and newest_epoch > prev)
        if got_new:
            step = newest_epoch - prev
            if 0.5 * FREQ_BURST_PERIOD <= step <= 2.0 * FREQ_BURST_PERIOD:
                period = 0.7 * period + 0.3 * step
                c["period_s"] = period
        c["last_sample_t"] = newest_epoch
        # If the freshest sample is already older than one period, the next burst
        # is OVERDUE — retry quickly (every FREQ_RETRY) so we catch it within ~10s
        # of it landing, rather than waiting a full period. Otherwise wait until
        # just after the next burst is expected (phase-aligned).
        if age >= period:
            wait = FREQ_RETRY                       # burst overdue — poll again soon
        else:
            secs_to_next = (period - age) + FREQ_PHASE_LEAD
            wait = min(max(secs_to_next, FREQ_FAST_MIN), FREQ_FAST_MAX)
        c["next_due"] = now_mono + wait
    else:
        c["next_due"] = now_mono + FREQ_FAST_MIN
        c["newest_age_s"] = None
    freq["newest_age_s"] = c["newest_age_s"]
    c["data"] = freq
    c["ts"] = now_wall
    return freq


def get_demand():
    """Most recent settled national demand outturn (MW), with 1-hour trend."""
    d = _rows(fetch_json(f"{BMRS}/demand/outturn?format=json"))
    if not d:
        return None
    rows = sorted(d, key=lambda x: x["startTime"])
    latest = rows[-1]
    prev = rows[-3] if len(rows) >= 3 else None  # ~1h earlier (2 x 30-min periods)
    now_mw = latest.get("initialDemandOutturn")
    delta = (now_mw - prev.get("initialDemandOutturn")) if prev and now_mw is not None else None
    return {"time": latest["startTime"],
            "national_mw": now_mw,
            "transmission_mw": latest.get("initialTransmissionSystemDemandOutturn"),
            "delta_1h": delta}


def get_margin():
    """Indicated generation margin (MELNGC). Lowest upcoming margin = tightest point."""
    d = _rows(fetch_json(f"{BMRS}/datasets/MELNGC?format=json"))
    if not d:
        return None
    now = datetime.now(timezone.utc)
    future = []
    for r in d:
        # Boundary 'N' is the national margin. B1..B17 are regional transmission
        # constraint boundaries and are routinely negative by design — not a
        # supply-adequacy signal, so we exclude them from power-cut risk.
        if r.get("boundary") != "N":
            continue
        try:
            st = datetime.fromisoformat(r["startTime"].replace("Z", "+00:00"))
        except Exception:
            continue
        if st >= now - timedelta(hours=1) and r.get("margin") is not None:
            future.append((st, r["margin"], r.get("boundary")))
    if not future:
        return None
    future.sort()
    tightest = min(future, key=lambda x: x[1])
    # Near-term trend: change in margin from now to ~1 hour ahead (2 periods).
    # MELNGC is a forecast, so this shows whether headroom is tightening or
    # easing over the next hour rather than a past change.
    ahead_delta = None
    if len(future) >= 3:
        ahead_delta = future[2][1] - future[0][1]
    return {
        "current_mw": future[0][1],
        "current_time": future[0][0].isoformat(),
        "min_mw": tightest[1],
        "min_time": tightest[0].isoformat(),
        "ahead_delta_1h": ahead_delta,
        "horizon": [{"time": t.isoformat(), "margin_mw": m} for t, m, _ in future[:96]],
    }


# Operating reserve is derived from ~8k per-unit records, so it gets its own
# short cache rather than being recomputed on every dashboard refresh.
_reserve_cache = {"data": None, "ts": 0}
RESERVE_TTL = 120     # 2 minutes

# Shared PN/MEL fetch. get_operating_reserve and get_units both need the same
# per-BM-Unit PN stream (and MELS), so we fetch once and cache the raw rows
# briefly, letting both consumers work off one network round-trip rather than
# two. Keyed nothing fancy — just the raw row lists plus a fetch timestamp.
_pnmel_cache = {"pns": None, "mels": None, "ts": 0}
PNMEL_TTL = 60        # raw rows reused for 60s across reserve + units


def _get_pn_mel(now):
    """Fetch (and briefly cache) the raw PN stream and MELS rows. Returns
    (pns, mels) or raises. The PN window is widened to 15 min so get_units can
    measure the rate of change across the window (levelFrom vs levelTo)."""
    c = _pnmel_cache
    if c["pns"] is not None and time.time() - c["ts"] < PNMEL_TTL:
        return c["pns"], c["mels"]
    mel_from = (now - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pn_from = (now - timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    to = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    mels = _rows(fetch_json(f"{BMRS}/datasets/MELS?format=json&from={mel_from}&to={to}", timeout=40))
    pnr = fetch_json(f"{BMRS}/datasets/PN/stream?format=json&from={pn_from}&to={to}", timeout=40)
    pns = pnr if isinstance(pnr, list) else _rows(pnr)
    c["pns"], c["mels"], c["ts"] = pns, mels, time.time()
    return pns, mels


# ---- BM Unit -> station name & fuel reference (Elexon registry) ------------
# Rather than hand-maintain a partial table, we fetch Elexon's full BM Unit
# registration list (~3000 units) once and cache it to disk, refreshing daily.
# It carries fuelType, bmUnitName and generationCapacity per unit, so we can
# classify every generating unit by its real fuel and label it with its real
# station name. Units whose registry fuelType is null/unknown fall back to a
# prefix heuristic, then to 'other' — never a fabricated fuel.
BMU_REG_URL = f"{BMRS}/reference/bmunits/all"
BMU_REG_FILE = Path(__file__).with_name("bmu_registry.json")
BMU_REG_TTL = 24 * 3600          # refresh the registry once a day
_bmu_reg = {"map": None, "ts": 0}

# Elexon fuelType -> our display category.
_FUELTYPE_MAP = {
    "CCGT": "gas", "OCGT": "gas", "OIL": "gas", "COAL": "gas",
    "NUCLEAR": "nuclear", "WIND": "wind", "NPSHYD": "hydro", "PS": "hydro",
    "BIOMASS": "biomass", "SOLAR": "solar",
    "INTELEC": "interconnector", "INTGRNL": "interconnector", "INTVKL": "interconnector",
    "INTNED": "interconnector", "INTEW": "interconnector", "INTFR": "interconnector",
    "INTIFA2": "interconnector", "INTIRL": "interconnector", "INTNSL": "interconnector",
    "INTNEM": "interconnector",
}

# Curated nicer names for well-known stations, used in preference to the
# registry's bmUnitName where that name is ugly or just the raw code. Keyed by
# station stem (code minus leading X_ and trailing unit number).
_NAME_OVERRIDE = {
    "TORN": "Torness", "HEYM": "Heysham 2", "HYSM": "Heysham 1", "HRTL": "Hartlepool",
    "SIZB": "Sizewell B", "DRAXX": "Drax", "LNMTH": "Lynemouth",
    "MOWEO": "Moray East", "MOWWO": "Moray West", "SGRWO": "Seagreen",
    "BEATO": "Beatrice", "NNGAO": "Neart na Gaoithe", "HOWAO": "Hornsea 1",
    "HOWBO": "Hornsea 2", "DBAWO": "Dogger Bank A", "DDGNO": "Dudgeon",
    "DINO": "Dinorwig", "FFES": "Ffestiniog", "CRUA": "Ben Cruachan",
    "FOYE": "Foyers", "SLOY": "Sloy", "CLVHS": "Cleve Hill",
    "STAY": "Staythorpe", "CARR": "Carrington", "PEMB": "Pembroke",
}

# Interconnector stem -> friendly name (fuelType is null for these; identified
# by the I_ prefix). Keyed by the token after 'I_XXG-' style codes.
_IC_NAMES = {
    "IF": "IFA (FR)", "I2": "IFA2 (FR)", "IL": "ElecLink (FR)", "IN": "BritNed (NL)",
    "IB": "Nemo (BE)", "IV": "Viking (DK)", "IE": "EWIC (IE)", "IG": "Greenlink (IE)",
    "IM": "Moyle (IE)", "IW": "NSL (NO)", "IES": "EWIC (IE)",
}


def _station_stem(code):
    """Station grouping key: code minus leading X_ prefix and trailing unit
    number (T_HEYM28 -> HEYM, E_MOWEO-3 -> MOWEO)."""
    s = re.sub(r"^[A-Z0-9]_", "", code)
    s = re.sub(r"-?\d+$", "", s)
    return s


def _load_bmu_registry():
    """Return {elexonBmUnit: {'fuel','name','stem','cap'}}, fetched from Elexon
    and cached to disk for a day. Never raises — returns {} on total failure so
    classification degrades to the prefix heuristic."""
    now = time.time()
    if _bmu_reg["map"] is not None and now - _bmu_reg["ts"] < BMU_REG_TTL:
        return _bmu_reg["map"]
    rows = None
    # try disk cache first
    try:
        blob = json.loads(BMU_REG_FILE.read_text())
        if now - blob.get("ts", 0) < BMU_REG_TTL:
            _bmu_reg["map"], _bmu_reg["ts"] = blob["map"], blob["ts"]
            return _bmu_reg["map"]
    except Exception:
        pass
    # fetch fresh
    try:
        raw = fetch_json(BMU_REG_URL, timeout=60)
        rows = raw if isinstance(raw, list) else _rows(raw)
    except Exception:
        # fall back to any stale disk copy, else empty
        try:
            blob = json.loads(BMU_REG_FILE.read_text())
            _bmu_reg["map"], _bmu_reg["ts"] = blob["map"], blob["ts"]
            return _bmu_reg["map"]
        except Exception:
            return {}
    mp = {}
    for r in (rows or []):
        code = r.get("elexonBmUnit")
        if not code:
            continue
        ft = r.get("fuelType")
        fuel = _FUELTYPE_MAP.get(ft) if ft else None
        name = (r.get("bmUnitName") or "").strip()
        mp[code] = {"fuel": fuel, "name": name, "stem": _station_stem(code),
                    "cap": r.get("generationCapacity")}
    if mp:
        _bmu_reg["map"], _bmu_reg["ts"] = mp, now
        try:
            BMU_REG_FILE.write_text(json.dumps({"map": mp, "ts": now}))
        except Exception:
            pass
    return _bmu_reg["map"] or {}


# ---- Battery / storage site classification (Terravolt) --------------------
# Elexon's FUELINST has no battery fuel type and the BMU registry doesn't flag
# grid-scale batteries, so battery output is a genuine blind spot. Terravolt
# publishes a public map of BM units -> {site name, type, lat/lng}, classifying
# batteries and pumped storage. We fetch that map ONCE and cache it to disk
# (it's a slow-changing classification list, not live data); all live output
# then comes from Elexon PN, our own primary source. This is the same pattern
# as adding Sheffield PVLive for embedded solar: an external source only to
# fill a gap the primary feed doesn't cover.
BMU_LOCATIONS_URL = "https://app.terravolt.co.uk/js/map-data/bmu-locations.js"
BMU_LOC_FILE = Path(__file__).with_name("bmu_locations.json")
BMU_LOC_TTL = 7 * 24 * 3600      # refresh weekly; classification changes slowly
_bmu_loc = {"map": None, "ts": 0}


def _parse_bmu_locations_js(text):
    """Extract {BMU_ID: {'name','type','lat','lng'}} from Terravolt's
    bmu-locations.js (a JS object literal). Tolerant regex parse — we only need
    id, name and type. Never raises; returns {} on failure."""
    out = {}
    try:
        # each entry: "ID": { name: "..", type: "..", lat: .., lng: .., ... }
        entry = re.compile(
            r'"([A-Z0-9_.\-]+)"\s*:\s*\{([^}]*)\}', re.IGNORECASE)
        field = lambda body, key: (
            re.search(r'\b' + key + r'\s*:\s*"([^"]*)"', body) or [None, None])[1]
        latf = lambda body, key: (
            re.search(r'\b' + key + r'\s*:\s*(-?\d+(?:\.\d+)?)', body) or [None, None])[1]
        for m in entry.finditer(text):
            code, body = m.group(1), m.group(2)
            typ = field(body, "type")
            if not typ:
                continue
            out[code] = {"name": field(body, "name") or code, "type": typ,
                         "lat": latf(body, "lat"), "lng": latf(body, "lng")}
    except Exception:
        return {}
    return out


def _load_bmu_locations():
    """Return {BMU_ID: {'name','type','lat','lng'}} from Terravolt, cached to
    disk weekly. Never raises — returns {} so battery classification simply
    yields no sites if the source is unreachable (honest: no fabricated data)."""
    now = time.time()
    if _bmu_loc["map"] is not None and now - _bmu_loc["ts"] < BMU_LOC_TTL:
        return _bmu_loc["map"]
    # disk cache
    try:
        blob = json.loads(BMU_LOC_FILE.read_text())
        if now - blob.get("ts", 0) < BMU_LOC_TTL:
            _bmu_loc["map"], _bmu_loc["ts"] = blob["map"], blob["ts"]
            return _bmu_loc["map"]
    except Exception:
        pass
    # fetch fresh (plain text JS file)
    try:
        req = urllib.request.Request(BMU_LOCATIONS_URL, headers=UA)
        with urllib.request.urlopen(req, timeout=40) as r:
            text = r.read().decode("utf-8", "replace")
        mp = _parse_bmu_locations_js(text)
    except Exception:
        # fall back to stale disk copy if any, else empty
        try:
            blob = json.loads(BMU_LOC_FILE.read_text())
            _bmu_loc["map"], _bmu_loc["ts"] = blob["map"], blob["ts"]
            return _bmu_loc["map"]
        except Exception:
            return {}
    if mp:
        _bmu_loc["map"], _bmu_loc["ts"] = mp, now
        try:
            BMU_LOC_FILE.write_text(json.dumps({"map": mp, "ts": now}))
        except Exception:
            pass
    return _bmu_loc["map"] or {}


def _battery_site(code):
    """(site_name, type) if this BM unit is a Battery or Pumped storage unit per
    the Terravolt classification, else (None, None). type is 'Battery'|'Pumped'."""
    loc = _load_bmu_locations().get(code)
    if not loc:
        return None, None
    t = loc.get("type")
    if t in ("Battery", "Pumped"):
        return (loc.get("name") or code), t
    return None, None


def _classify_bmu(code):
    """Map a BM Unit code to (station_stem, friendly_name, fuel) using the
    Elexon registry, with prefix-based fallbacks. Never fabricates a name."""
    if not code:
        return None, None, None
    reg = _load_bmu_registry()
    info = reg.get(code)
    # Interconnectors: fuelType is null in the registry, identify by prefix.
    if code.startswith("I_"):
        m = re.match(r"I_([A-Z0-9]+?)G?-", code)
        tok = m.group(1) if m else ""
        stem = "IC_" + (tok or code)
        name = _IC_NAMES.get(tok) or (info["name"] if info and info["name"] else stem)
        return stem, name, "interconnector"
    stem = info["stem"] if info else _station_stem(code)
    # fuel: registry first, then name/prefix heuristics, then 'other'
    fuel = info["fuel"] if info else None
    rname = (info["name"] if info else "") or ""
    if fuel is None:
        low = rname.lower()
        if "solar" in low or " pv" in low or low.endswith(" pv") or "photovolt" in low:
            fuel = "solar"
        elif "battery" in low or "storage" in low or "bess" in low:
            fuel = "other"          # storage isn't a generation fuel — keep 'other'
        elif re.search(r"[WO]$", stem):
            fuel = "wind"           # trailing W/O strongly implies wind
        else:
            fuel = "other"
    # name: curated override > registry name (if not just the code) > stem
    name = _NAME_OVERRIDE.get(stem)
    if not name and info and info["name"] and info["name"].upper() != code.upper():
        name = info["name"]
    if not name:
        name = stem
    return stem, name, fuel


# ---- Margin history -------------------------------------------------------
# MELNGC is forecast-only (no past), so to show a -24h..+24h view we log the
# indicated national margin to a small JSON file on each snapshot build and
# prune to the last 24h. Keyed by the reading's own timestamp so repeated
# refreshes within the same half-hour dedupe and restarts don't lose history.
MARGIN_LOG = Path(__file__).with_name("margin_history.json")
MARGIN_LOG_HOURS = 24

# Gas balance/linepack history: the National Gas feed is instantaneous-only
# (no past), so to show a 48h trend we log linepack + supply + demand on each
# snapshot build, keyed by the reading's timestamp (dedupes repeated refreshes),
# pruned to 48h. Lets the gas page draw a balance trend line.
GAS_LOG = Path(__file__).with_name("gas_history.json")
GAS_LOG_HOURS = 48

# Alert event log. Rather than re-writing every active alert on every 60s cycle
# (which would bloat the file with duplicates), we record state TRANSITIONS: an
# event when an alert is first raised and another when it clears, each with a
# timestamp, so the log reads like a control-room event journal and lets you
# calibrate how often each trigger fires and for how long.
ALERT_LOG = Path(__file__).with_name("alert_history.json")
ALERT_LOG_DAYS = 30          # prune cleared events older than this
# 'System nominal'/'ok' is a UI reassurance, not an event worth journalling.
ALERT_LOG_SKIP_LEVELS = {"ok"}


def _log_margin_history(margin):
    """Append current margin to the on-disk log, prune to 24h, return the
    history as a sorted list of {time, margin_mw}. Never raises."""
    if not margin or margin.get("current_mw") is None:
        # still return whatever history we have on file
        try:
            return sorted(json.loads(MARGIN_LOG.read_text()).items())
        except Exception:
            return []
    try:
        store = {}
        if MARGIN_LOG.exists():
            try:
                store = json.loads(MARGIN_LOG.read_text())
            except Exception:
                store = {}
        # key by current reading's timestamp
        t = margin.get("current_time") or datetime.now(timezone.utc).isoformat()
        store[t] = margin["current_mw"]
        # prune to last MARGIN_LOG_HOURS
        cutoff = datetime.now(timezone.utc) - timedelta(hours=MARGIN_LOG_HOURS)
        pruned = {}
        for k, v in store.items():
            try:
                if datetime.fromisoformat(k.replace("Z", "+00:00")) >= cutoff:
                    pruned[k] = v
            except Exception:
                continue
        MARGIN_LOG.write_text(json.dumps(pruned))
        return sorted(pruned.items())
    except Exception:
        return []


def _log_gas_history(linepack, supply, demand, at):
    """Append current gas balance to the on-disk log, prune to 48h, return a
    sorted list of {t, linepack, supply, demand}. Never raises."""
    try:
        store = {}
        if GAS_LOG.exists():
            try:
                store = json.loads(GAS_LOG.read_text())
            except Exception:
                store = {}
        if linepack is not None:
            t = at or datetime.now(timezone.utc).isoformat()
            store[t] = {"lp": round(linepack, 1),
                        "s": round(supply, 1) if supply is not None else None,
                        "d": round(demand, 1) if demand is not None else None}
        cutoff = datetime.now(timezone.utc) - timedelta(hours=GAS_LOG_HOURS)
        pruned = {}
        for k, v in store.items():
            try:
                if datetime.fromisoformat(k.replace("Z", "+00:00")) >= cutoff:
                    pruned[k] = v
            except Exception:
                continue
        GAS_LOG.write_text(json.dumps(pruned))
        return [{"t": k, **v} for k, v in sorted(pruned.items())]
    except Exception:
        return []


def _level_at(rows, t):
    """For each BM Unit, find the record whose window covers time t and
    linearly interpolate levelFrom->levelTo. Later records overwrite earlier
    ones, so the most recent notification for a unit wins."""
    out = {}
    for r in rows:
        try:
            t0 = datetime.fromisoformat(r["timeFrom"].replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(r["timeTo"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t0 <= t <= t1:
            span = (t1 - t0).total_seconds() or 1
            frac = max(0.0, min(1.0, (t - t0).total_seconds() / span))
            out[r["bmUnit"]] = r["levelFrom"] + (r["levelTo"] - r["levelFrom"]) * frac
    return out


def get_operating_reserve():
    """Immediate response headroom, i.e. what could cover a sudden demand rise
    or a generator trip *right now* — not theoretical MELNGC capacity.

    Spinning reserve = sum over synchronised units (physical notification > 0)
    of (Maximum Export Limit - current output), i.e. how much more the plant
    already running could produce. Also reports the largest single running
    unit (the largest single infeed the system would lose if it tripped).

    MEL notifications are sparse — a unit only republishes its limit when it
    changes — so MELS needs a wider lookback (30 min) than PN (10 min) to have
    a standing limit on file for every running unit. If we still can't match
    most running units to a MEL we return an error rather than a misleading
    figure, since a partial match understates reserve badly."""
    now = datetime.now(timezone.utc)
    c = _reserve_cache
    if c["data"] and not c["data"].get("error") and time.time() - c["ts"] < RESERVE_TTL:
        return c["data"]
    try:
        pns, mels = _get_pn_mel(now)
    except Exception as e:
        return c["data"] or {"error": f"{type(e).__name__}: {e}"}

    mel_now = _level_at(mels, now)
    pn_now = _level_at(pns, now)
    spinning = 0.0
    running = 0
    matched = 0
    running_output = 0.0
    biggest = (0.0, None)
    for bmu, pn in pn_now.items():
        if pn > 1:                      # unit is synchronised and exporting
            running += 1
            running_output += pn
            if pn > biggest[0]:
                biggest = (pn, bmu)
            mel = mel_now.get(bmu)
            if mel is not None:
                matched += 1
                if mel > pn:
                    spinning += (mel - pn)

    # Coverage guard: if we matched a MEL to fewer than 80% of running units,
    # the spinning figure is unreliable (this is exactly the failure that showed
    # 0.04 GW). Keep the last good value if we have one, else report the issue.
    coverage = (matched / running) if running else 0
    if running == 0 or coverage < 0.8:
        if c["data"] and not c["data"].get("error"):
            stale = dict(c["data"]); stale["stale"] = True
            return stale
        return {"error": f"insufficient MEL coverage ({matched}/{running} units)",
                "spinning_reserve_mw": None}

    result = {
        "time": now.isoformat(),
        "spinning_reserve_mw": round(spinning),
        "units_running": running,
        "units_matched": matched,
        "running_output_mw": round(running_output),
        "largest_infeed_mw": round(biggest[0]),
        "largest_infeed_unit": biggest[1],
        # cover ratio: how many times over spinning reserve covers the worst
        # single loss. >1 means an instantaneous trip of the biggest unit could
        # be absorbed by plant already running.
        "cover_ratio": round(spinning / biggest[0], 1) if biggest[0] else None,
    }
    c["data"] = result
    c["ts"] = time.time()
    return result


# Rapid-change thresholds. A station (or fuel group) is flagged as ramping when
# its output moved by BOTH at least RAMP_PCT percent AND at least RAMP_MW MW
# across the PN window. The dual test stops tiny units oscillating around zero
# (a -6->+6 MW swing is "+200%") from triggering, while the absolute floor is
# low enough that a real fleet move is caught. Applied at two levels: per
# station, and on each fuel group's aggregate — so a distributed ramp spread
# across many sub-threshold units (e.g. ten small gas plants each dropping
# 25 MW) still flags the group even when no single tile qualifies.
RAMP_PCT = 10.0
RAMP_MW = 50.0
_units_cache = {"data": None, "ts": 0}
UNITS_TTL = 60


def _ramp_flag(first, last):
    """Return +1 (rising), -1 (falling) or 0 (steady) for a first->last change,
    applying the dual percent+absolute threshold."""
    dm = last - first
    if abs(dm) < RAMP_MW:
        return 0
    base = max(abs(first), abs(last), 1.0)
    if abs(100.0 * dm / base) < RAMP_PCT:
        return 0
    return 1 if dm > 0 else -1


def get_units():
    """Per-station live output for the generator drill-down, grouped by fuel,
    with rapid-change flags at station and fuel-group level.

    Reuses the shared PN stream (no extra API call). For each BM Unit we take
    its current output (latest levelTo) and its output at the start of the
    window (earliest levelFrom) to measure the rate of change. Units are grouped
    into parent stations, stations into fuel groups. Only units currently
    exporting (>1 MW) are shown; interconnector imports are positive, exports
    are excluded from the 'generating' view (they're a load, not a source)."""
    now = datetime.now(timezone.utc)
    c = _units_cache
    if c["data"] and time.time() - c["ts"] < UNITS_TTL:
        return c["data"]
    try:
        pns, _mels = _get_pn_mel(now)
    except Exception as e:
        return c["data"] or {"error": f"{type(e).__name__}: {e}", "stations": []}

    # Per BM Unit: earliest levelFrom and latest levelTo across the window.
    per = {}
    for r in pns:
        bmu = r.get("bmUnit")
        if not bmu:
            continue
        tf = r.get("timeFrom") or ""
        rec = per.setdefault(bmu, {"first_t": None, "first": None, "last_t": None, "last": None})
        if rec["first_t"] is None or tf < rec["first_t"]:
            rec["first_t"] = tf
            rec["first"] = r.get("levelFrom") or 0
        tt = r.get("timeTo") or ""
        if rec["last_t"] is None or tt > rec["last_t"]:
            rec["last_t"] = tt
            rec["last"] = r.get("levelTo") or 0

    # Aggregate units into stations. Battery units are pulled out FIRST and
    # grouped by Terravolt site name (one tile per site), since they aren't in
    # FUELINST and aren't flagged in the Elexon registry. Only discharging
    # (positive PN) battery units get a tile — matching the supply view used for
    # every other group. Charging (negative) battery draw is handled separately
    # in the two-way supply figure, not the treemap.
    stations = {}
    battery_sites = {}     # site name -> {'mw','units'} for discharging batteries
    for bmu, rec in per.items():
        cur = rec["last"] or 0
        site, stype = _battery_site(bmu)
        if stype == "Battery":
            if cur > 1:                    # discharging only
                bs = battery_sites.setdefault(site, {"mw": 0.0, "units": []})
                bs["mw"] += cur
                bs["units"].append({"code": bmu, "mw": round(cur)})
            continue                        # batteries never fall through to fuel groups
        if cur <= 1:                       # only currently-exporting units
            continue
        stem, name, fuel = _classify_bmu(bmu)
        if stem is None:
            continue
        s = stations.setdefault(stem, {"station": stem, "name": name, "fuel": fuel,
                                        "mw": 0.0, "first": 0.0, "units": []})
        s["mw"] += cur
        s["first"] += rec["first"] or 0
        s["units"].append({"code": bmu, "mw": round(cur)})

    # Per-station ramp flag + tidy. Ramp is computed on RAW PN (change of
    # notified level), which is the honest rate-of-change signal; scaling to
    # metered totals below is monotonic so ramp direction is preserved.
    out_stations = []
    for s in stations.values():
        s["mw_pn"] = round(s["mw"])          # raw notified sum (pre-scaling)
        s["ramp"] = _ramp_flag(s["first"], s["mw"])
        base = max(abs(s["first"]), abs(s["mw"]), 1.0)
        s["ramp_pct"] = round(100.0 * (s["mw"] - s["first"]) / base)
        s["units"].sort(key=lambda u: -u["mw"])
        out_stations.append(s)

    # ---- Reconcile group SIZES to metered reality --------------------------
    # PN (notified) proportions are wrong at the fuel-group level (the SO
    # dispatches units away from their notifications). So we size each fuel
    # block by the authoritative metered total the Generation panel uses
    # (FUELINST), and scale each station's PN share to sum to that total. This
    # makes group proportions match reality (and energydashboard) while keeping
    # PN only for the RELATIVE split of stations within a group.
    #   - solar: FUELINST has no solar (embedded, unmetered) -> single PVLive
    #     "Embedded solar (est)" tile, no per-station data exists.
    #   - interconnector / hydro: handled from FUELINST net, shown only when
    #     net-positive (importing / generating).
    metered = {}          # our category -> metered MW (>=0)
    # FUELINST fuel-type code -> our treemap category (must match _classify_bmu
    # categories, NOT FUEL_META's 'fossil'/'firm'/'renewable' grouping).
    _FI_TO_CAT = {
        "CCGT": "gas", "OCGT": "gas", "OIL": "gas", "COAL": "gas",
        "NUCLEAR": "nuclear", "WIND": "wind", "NPSHYD": "hydro", "PS": "hydro",
        "BIOMASS": "biomass", "SOLAR": "solar", "OTHER": "other",
        "INTFR": "interconnector", "INTIFA2": "interconnector",
        "INTELEC": "interconnector", "INTNED": "interconnector",
        "INTEW": "interconnector", "INTIRL": "interconnector",
        "INTNEM": "interconnector", "INTNSL": "interconnector",
        "INTVKL": "interconnector", "INTGRNL": "interconnector",
    }
    _INT_NAMES = {
        "INTFR": "IFA (FR)", "INTIFA2": "IFA2 (FR)", "INTELEC": "ElecLink (FR)",
        "INTNED": "BritNed (NL)", "INTNEM": "Nemo (BE)", "INTVKL": "Viking (DK)",
        "INTNSL": "NSL (NO)", "INTEW": "EWIC (IE)", "INTIRL": "Moyle (IE)",
        "INTGRNL": "Greenlink (IE)",
    }
    ic_imports = []       # [(name, mw)] for interconnectors currently importing
    try:
        gen = get_generation()
        for f in (gen or {}).get("fuels", []):
            code = f.get("code")
            mw = f.get("mw") or 0
            cat = _FI_TO_CAT.get(code, "other")
            if cat == "interconnector":
                # Only importing links supply GB; exporting links are loads and
                # are excluded from this generation view. Size the group by the
                # SUM OF IMPORTS, not the net (which cancels imports against
                # exports into a meaningless near-zero sliver).
                if mw > 0:
                    ic_imports.append((_INT_NAMES.get(code, code), mw))
                continue          # don't fold interconnectors into metered[] here
            metered[cat] = metered.get(cat, 0.0) + mw
        metered["interconnector"] = sum(mw for _, mw in ic_imports)
    except Exception:
        metered = {}

    # PVLive embedded solar for the solar block.
    solar_est = None
    try:
        sol = get_solar()
        if sol and sol.get("mw") is not None:
            solar_est = sol["mw"]
    except Exception:
        pass

    # Sum raw PN per group (for computing each station's within-group share).
    pn_group = {}
    for s in out_stations:
        pn_group[s["fuel"]] = pn_group.get(s["fuel"], 0.0) + s["mw_pn"]

    # Decide each group's displayed total (metered where we have it).
    # Category names from FUEL_META: 'gas','nuclear','wind','biomass','hydro',
    # 'solar','interconnector','other' — aligned with our classifier.
    battery_total = round(sum(bs["mw"] for bs in battery_sites.values()))
    def _group_total(fuel):
        if fuel == "battery":
            return battery_total       # summed discharging-battery PN (no FUELINST equiv)
        m = metered.get(fuel)
        if fuel == "solar":
            return solar_est if solar_est is not None else (m or 0)
        if m is not None:
            return max(m, 0)          # negative (pumping) -> 0, hidden
        return pn_group.get(fuel, 0)  # fallback: raw PN if no metered figure

    # Scale each station's shown MW so its group sums to the metered total.
    scaled = []
    for s in out_stations:
        if s["fuel"] in ("solar", "interconnector"):
            continue                  # replaced by dedicated tiles below
        gt = _group_total(s["fuel"])
        gp = pn_group.get(s["fuel"], 0) or 1
        share = s["mw_pn"] / gp
        s["mw"] = round(gt * share)
        s["scaled"] = True            # MW is metered-total * notified share
        if s["mw"] >= 1:
            scaled.append(s)
    # tidy fields
    for s in scaled:
        s.pop("first", None)
    # Single embedded-solar tile (estimated, no per-station breakdown).
    if solar_est and solar_est > 0:
        scaled.append({"station": "SOLAR_EMB", "name": "Embedded solar",
                       "fuel": "solar", "mw": round(solar_est), "estimated": True,
                       "ramp": 0, "ramp_pct": 0, "scaled": False,
                       "units": [{"code": "PVLive estimate", "mw": round(solar_est)}]})
    # Interconnector tiles direct from FUELINST — one per IMPORTING link (each
    # INT code is effectively one interconnector). Exporting links are loads and
    # are omitted. Metered, not notified, so not flagged 'scaled'.
    for name, mw in ic_imports:
        if mw >= 1:
            scaled.append({"station": "IC_" + name, "name": name,
                           "fuel": "interconnector", "mw": round(mw),
                           "metered": True, "ramp": 0, "ramp_pct": 0, "scaled": False,
                           "units": [{"code": name + " import", "mw": round(mw)}]})
    # Battery tiles — one per site, sized by summed discharging-unit PN. Batteries
    # are dispatched to their notifications closely, and there is no FUELINST
    # battery total to scale to, so PN sum is the honest figure (flagged so the
    # UI can note it's notified per-unit output, not a separate metered feed).
    for site, bs in battery_sites.items():
        mw = bs["mw"]
        if mw >= 1:
            bs["units"].sort(key=lambda u: -u["mw"])
            scaled.append({"station": "BATT_" + site, "name": site,
                           "fuel": "battery", "mw": round(mw),
                           "battery": True, "ramp": 0, "ramp_pct": 0, "scaled": False,
                           "units": bs["units"]})
    scaled.sort(key=lambda s: -s["mw"])
    out_stations = scaled

    # Per-fuel-group totals now reflect the metered sizes; ramp still from PN.
    fuel_first = {}
    fuel_last = {}
    for bmu, rec in per.items():
        cur = rec["last"] or 0
        if cur <= 1:
            continue
        _stem, _name, fuel = _classify_bmu(bmu)
        if fuel is None:
            continue
        fuel_first[fuel] = fuel_first.get(fuel, 0.0) + (rec["first"] or 0)
        fuel_last[fuel] = fuel_last.get(fuel, 0.0) + cur

    fuel_groups = {}
    all_fuels = set(list(fuel_last.keys()) + [s["fuel"] for s in out_stations])
    for fuel in all_fuels:
        f0, l0 = fuel_first.get(fuel, 0.0), fuel_last.get(fuel, 0.0)
        fuel_groups[fuel] = {
            "mw": round(_group_total(fuel)),        # metered size
            "mw_pn": round(l0),                     # notified sum (for reference)
            "ramp": _ramp_flag(f0, l0),
            "ramp_pct": round(100.0 * (l0 - f0) / max(abs(f0), abs(l0), 1.0)),
        }

    result = {
        "time": now.isoformat(),
        "stations": out_stations,
        "fuel_groups": fuel_groups,
        "total_mw": round(sum(s["mw"] for s in out_stations)),
        "n_stations": len(out_stations),
        "ramp_pct": RAMP_PCT,
        "ramp_mw": RAMP_MW,
        "basis": "group sizes metered (FUELINST/PVLive); station split notified (PN)",
    }
    c["data"] = result
    c["ts"] = time.time()
    return result


_battery_cache = {"data": None, "ts": 0}
BATTERY_TTL = 120


def get_battery():
    """Net grid-scale battery output (MW) from Elexon PN, classified via the
    Terravolt site map. Two-way like pumped storage: positive = discharging
    (supply), negative = charging (demand). Also returns discharge/charge totals,
    active unit counts and the number of classified sites. Reuses the shared PN
    stream (no extra Elexon call). Returns None if classification unavailable."""
    now = datetime.now(timezone.utc)
    c = _battery_cache
    if c["data"] and time.time() - c["ts"] < BATTERY_TTL:
        return c["data"]
    loc = _load_bmu_locations()
    if not loc:
        return None            # no classification -> honestly report nothing
    try:
        pns, _mels = _get_pn_mel(now)
    except Exception:
        return c["data"]
    # latest levelTo per battery unit
    per = {}
    for r in pns:
        bmu = r.get("bmUnit")
        if not bmu:
            continue
        site, stype = _battery_site(bmu)
        if stype != "Battery":
            continue
        tt = r.get("timeTo") or ""
        cur = per.get(bmu)
        if cur is None or tt > cur[0]:
            per[bmu] = (tt, r.get("levelTo") or 0, site)
    discharge = charge = 0.0
    disc_units = chg_units = 0
    for bmu, (_t, lvl, site) in per.items():
        if lvl > 0:
            discharge += lvl; disc_units += 1
        elif lvl < 0:
            charge += -lvl; chg_units += 1
    result = {
        "time": now.isoformat(),
        "net_mw": round(discharge - charge),
        "discharge_mw": round(discharge),
        "charge_mw": round(charge),
        "units_discharging": disc_units,
        "units_charging": chg_units,
        "sites_classified": sum(1 for v in loc.values() if v.get("type") == "Battery"),
        "basis": "Elexon PN per-unit; battery units classified via Terravolt site map",
    }
    c["data"] = result; c["ts"] = time.time()
    return result


def get_warnings():
    """Official NESO system warnings (SYSWARN)."""
    d = _rows(fetch_json(f"{BMRS}/datasets/SYSWARN?format=json"))
    out = []
    for r in (d or []):
        text = (r.get("warningText") or "").replace("\r", " ").replace("\\n", " ").strip()
        out.append({"type": r.get("warningType"),
                    "time": r.get("publishTime"),
                    "text": text[:600]})
    return out


# Solar is not centrally metered, so it never appears in Elexon's FUELINST feed
# (it shows up only as reduced transmission demand). Sheffield University's
# PVLive gives the national estimate — the same source Gridwatch uses. Cache it
# for 5 minutes (PVLive updates every 5 min).
SOLAR_URL = "https://api.solar.sheffield.ac.uk/pvlive/api/v4/gsp/0"
_solar_cache = {"data": None, "ts": 0}
SOLAR_TTL = 300


def get_solar():
    """National solar generation estimate (MW) from Sheffield PVLive.
    Returns {'mw', 'time'} or None. Marked as an estimate downstream because
    it is modelled, not metered."""
    if _solar_cache["data"] and time.time() - _solar_cache["ts"] < SOLAR_TTL:
        return _solar_cache["data"]
    try:
        d = fetch_json(SOLAR_URL, timeout=20)
        rows = d.get("data") or []
        if not rows:
            return _solar_cache["data"]
        # rows: [gsp_id, datetime_gmt, generation_mw]
        latest = rows[-1]
        result = {"mw": round(latest[2]), "time": latest[1], "estimated": True}
        _solar_cache["data"] = result
        _solar_cache["ts"] = time.time()
        return result
    except Exception:
        return _solar_cache["data"]


def get_price():
    """Current GB wholesale market price (MID — market index data, GBP/MWh),
    with a ~1h trend. This is the number that explains why GB imports even with
    a large capacity margin: when the market price is low, importing (or holding
    plant off) is cheaper than dispatching more domestic generation. Returns
    None on failure."""
    try:
        now = datetime.now(timezone.utc)
        frm = (now - timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%SZ")
        to = (now + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = _rows(fetch_json(f"{BMRS}/datasets/MID/stream?format=json&from={frm}&to={to}", timeout=30))
        # keep priced records only; MID carries both APX and N2EX providers —
        # average providers within each settlement period for a single series.
        pr = [r for r in rows if r.get("price")]
        if not pr:
            return None
        by_period = {}
        for r in pr:
            sp = (r.get("settlementDate"), r.get("settlementPeriod"))
            by_period.setdefault(sp, []).append((r["startTime"], r["price"]))
        series = []
        for sp, items in by_period.items():
            st = items[0][0]
            avg = sum(p for _, p in items) / len(items)
            series.append((st, avg))
        series.sort()
        latest = series[-1][1]
        delta = None
        if len(series) >= 3:
            delta = latest - series[-3][1]     # ~1h earlier (2 periods)
        return {"gbp_per_mwh": round(latest, 2),
                "delta_1h": round(delta, 2) if delta is not None else None,
                # up to ~12h of half-hourly points for the history sparkline
                "history": [{"t": st, "p": round(p, 1)} for st, p in series[-24:]]}
    except Exception:
        return None


def get_carbon():
    try:
        intensity = _rows(fetch_json(f"{CARBON}/intensity"))
        mix = fetch_json(f"{CARBON}/generation")["data"]["generationmix"]
        cur = intensity[0]["intensity"] if intensity else {}
        now_val = cur.get("actual") or cur.get("forecast")
        delta = None
        try:
            past = _rows(fetch_json(f"{CARBON}/intensity/date"))  # today, half-hourly
            vals = [(p["intensity"].get("actual") or p["intensity"].get("forecast"))
                    for p in past if p.get("intensity")]
            vals = [v for v in vals if v is not None]
            if len(vals) >= 3 and now_val is not None:
                delta = now_val - vals[-3]  # 2 periods = ~1h earlier
        except Exception:
            pass
        return {"gco2_per_kwh": now_val, "index": cur.get("index"),
                "delta_1h": delta, "mix": mix}
    except Exception:
        return None


# Representative GB sampling points for inferred grid drivers.
# Wind: centres of the largest OPERATIONAL offshore wind farms, with a rough
# capacity weight (GW) so bigger farms influence the resource average more.
# Coordinates verified against each farm's published location.
#
# RESOURCE_SITES drive the per-location panel. Each is tagged with the power
# type it represents so its conditions can be rated against the right physics:
#   wind  -> offshore/onshore wind farms (rate on wind speed at hub height)
#   solar -> big solar/PV regions (rate on cloud cover + daylight)
#   hydro -> upland catchments feeding GB hydro/pumped storage (rate on rain)
# 12 sites total, a deliberate mix so the panel shows all three resources
# with genuine geographic spread including the South West peninsula.
RESOURCE_SITES = [
    # name, lat, lon, type, weight(GW, rough), offshore?(wind shear)
    ("Dogger Bank",   54.75,   1.92, "wind",  3.6, True),   # North Sea, largest offshore
    ("Hornsea",       53.885,  1.79, "wind",  2.6, True),   # off Yorkshire/Lincs
    ("Moray Firth",   58.15,  -2.85, "wind",  1.5, True),   # NE Scotland offshore
    ("Seagreen",      56.588, -1.74, "wind",  1.1, True),   # off Angus
    ("Delabole",      50.633, -4.708,"wind",  0.01,False),  # N Cornwall, UK's first onshore farm (SW)
    ("Cleve Hill",    51.35,   0.95, "solar", 0.37, False), # Kent, largest GB solar
    ("Shotwick",      53.24,  -3.03, "solar", 0.07, False), # Flintshire solar park
    ("Lyneham Solar", 51.50,  -1.99, "solar", 0.07, False), # Wiltshire (indicative S-England PV)
    ("Mid-Cornwall",  50.39,  -4.93, "solar", 0.15, False), # Indian Queens/St Dennis solar belt (SW)
    ("Dinorwig",      53.12,  -4.11, "hydro", 1.8, False),  # Snowdonia pumped storage
    ("Ben Cruachan",  56.39,  -5.12, "hydro", 0.44, False), # Argyll pumped storage
    ("Foyers",        57.25,  -4.49, "hydro", 0.30, False), # Loch Ness area hydro
]

# Wind shear: OpenWeather reports wind at ~10 m, but turbines sit at ~100 m.
# Scale 10 m -> ~100 m with the power-law profile v(h)=v10*(h/10)**alpha.
# alpha ~0.11 offshore (smooth sea surface), ~0.14 onshore (rougher terrain).
# This is an estimate, flagged as such downstream so it isn't read as metered.
def _hub_wind(v10, offshore):
    if v10 is None:
        return None
    alpha = 0.11 if offshore else 0.14
    return v10 * (100.0 / 10.0) ** alpha

# Demand: population-weighted GB conurbations -> temperature drives heating/cooling.
DEMAND_SITES = [
    ("London", 51.51, -0.13, 0.30), ("Manchester", 53.48, -2.24, 0.18),
    ("Birmingham", 52.48, -1.90, 0.16), ("Glasgow", 55.86, -4.25, 0.12),
    ("Leeds", 53.80, -1.55, 0.12), ("Bristol", 51.45, -2.59, 0.12),
]

OWM_URL = "https://api.openweathermap.org/data/2.5/weather"


def _openweather_one(lat, lon, api_key, timeout=12):
    """Fetch current conditions for one point from OpenWeather (Current Weather
    Data 2.5). Returns the parsed dict, or raises on HTTP/network error so the
    caller can classify (401 bad key, 429 rate limit, etc.)."""
    u = f"{OWM_URL}?lat={lat}&lon={lon}&units=metric&appid={api_key}"
    return fetch_json(u, timeout=timeout)


def _fetch_owm(lat, lon, key):
    """Return (data, tier, minute). Tries One Call 4.0 first (if the key is
    subscribed); on 401/403 or if the helper is absent, falls back to the free
    2.5 Current Weather call so the base panel keeps working. The OC4 response is
    adapted into the 2.5 body shape so the existing parse code runs unchanged.
    `minute` is the OC4 one-minute nowcast series (or None on 2.5)."""
    if _owm_onecall is not None:
        oc = _owm_onecall.try_conditions(lat, lon, key, want_minute=True, timeout=12)
        if oc.get("tier") == "OC4":
            c = oc["cond"]
            data = {
                "wind": {"speed": c["wind_speed_ms"], "deg": c["wind_deg"],
                         "gust": c["wind_gust_ms"]},
                "main": {"temp": c["temp"], "feels_like": c["feels_like"],
                         "temp_min": None, "temp_max": None,
                         "pressure": c["pressure"], "humidity": c["humidity"]},
                "clouds": {"all": c["clouds_pct"]},
                "rain": ({"1h": c["rain_1h"]} if c["rain_1h"] is not None else {}),
                "snow": ({"1h": c["snow_1h"]} if c["snow_1h"] is not None else {}),
                "weather": [{"main": c["cond_main"], "description": c["cond_desc"]}],
                "sys": {"sunrise": c["sunrise"], "sunset": c["sunset"]},
                "visibility": c["visibility_m"],
                "timezone": c["tz_offset"],
            }
            return data, "OC4", oc.get("minute")
    return _openweather_one(lat, lon, key, timeout=12), "2.5", None


# ---- OpenWeather daily call budget -----------------------------------------
# Counts every OpenWeather HTTP call (success OR failure — a failed call still
# consumes quota upstream) against a per-UTC-day ceiling. Persisted to disk so
# a restart mid-day doesn't reset the count and blow the budget. The count
# auto-resets when the UTC date rolls over.
WEATHER_BUDGET_FILE = Path(__file__).with_name("openweather_budget.json")
_weather_budget = {"date": None, "count": 0}


def _utc_date_str(epoch=None):
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc) if epoch else datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%d")


def _load_budget():
    """Load today's call count from disk, resetting if the stored date isn't
    today (UTC). Never raises."""
    today = _utc_date_str()
    if _weather_budget["date"] == today:
        return _weather_budget
    # try disk
    stored = {}
    try:
        stored = json.loads(WEATHER_BUDGET_FILE.read_text())
    except Exception:
        stored = {}
    if stored.get("date") == today:
        _weather_budget["date"] = today
        _weather_budget["count"] = int(stored.get("count", 0))
    else:
        # new day (or no file) — start fresh
        _weather_budget["date"] = today
        _weather_budget["count"] = 0
        _save_budget()
    return _weather_budget


def _save_budget():
    try:
        WEATHER_BUDGET_FILE.write_text(json.dumps(_weather_budget))
    except Exception:
        pass


def _budget_remaining():
    """Calls still allowed today under WEATHER_DAILY_MAX."""
    b = _load_budget()
    return max(0, WEATHER_DAILY_MAX - b["count"])


def _budget_spend(n):
    """Record that n OpenWeather calls were made. Persists immediately so the
    count survives a crash between calls."""
    b = _load_budget()
    b["count"] += n
    _save_budget()


def _seconds_to_utc_midnight(epoch=None):
    now = epoch or time.time()
    now_dt = datetime.fromtimestamp(now, tz=timezone.utc)
    tomorrow = (now_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp() - now


# Weather changes slowly and the OpenWeather free plan is capped at 1000
# calls/day, so we (a) cache independently of the snapshot and (b) enforce a
# hard daily call budget well under the plan limit.
#
# Budget math: each refresh makes one call per site = len(RESOURCE_SITES) +
# len(DEMAND_SITES) calls. With 12 + 6 = 18 calls/refresh and a self-imposed
# ceiling of WEATHER_DAILY_MAX calls/day, the most refreshes we can do is
# floor(MAX / 18) = 11. WEATHER_TTL is set to spread those evenly across 24h;
# the budget counter is the hard stop that TTL alone can't guarantee (restarts,
# manual key re-entry and retries would otherwise let calls creep over).
WEATHER_DAILY_MAX = 200          # hard ceiling on OpenWeather calls per UTC day
WEATHER_TTL = 7920               # 132 min -> ~11 refreshes/day * 18 = 198 calls
# backoff_until: don't hit the API again before this wall-clock time.
# fails: consecutive failure count, drives exponential backoff.
# ts: epoch of the reading in `data` (used to age a disk-restored reading).
_weather_cache = {"data": None, "ts": 0, "err": None, "backoff_until": 0, "fails": 0}

# Persist the last good reading to disk so a restart mid-rate-limit still shows
# a (clearly-aged) value instead of a blank panel — weather changes slowly
# enough that an hour-old reading is far more useful than nothing.
WEATHER_LOG = Path(__file__).with_name("weather_last_good.json")
# How stale a disk-restored reading may be before we stop showing it at all.
WEATHER_DISK_MAX_AGE = 6 * 3600   # 6 hours


def _weather_next_utc_midnight(now_epoch):
    """Epoch of the next 00:00 UTC — when OpenWeather's daily quota resets."""
    now_dt = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    tomorrow = (now_dt + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def _load_weather_disk():
    """Restore the last-good weather reading from disk if present and not too
    old. Returns (data, ts) or (None, 0). Never raises."""
    try:
        blob = json.loads(WEATHER_LOG.read_text())
        data, ts = blob.get("data"), blob.get("ts", 0)
        if data and (time.time() - ts) < WEATHER_DISK_MAX_AGE:
            return data, ts
    except Exception:
        pass
    return None, 0


def _save_weather_disk(data, ts):
    """Persist a good weather reading. Never raises."""
    try:
        WEATHER_LOG.write_text(json.dumps({"data": data, "ts": ts}))
    except Exception:
        pass


# ---- OpenWeather API key store ---------------------------------------------
# The key is entered in the browser and POSTed to /api/weather-key, held in
# memory, and persisted to disk so it survives a restart. It is NEVER baked
# into the served HTML. Kept next to the server; treat this file as a secret.
WEATHER_KEY_FILE = Path(__file__).with_name("openweather_key.json")
_weather_key = {"key": None}


def _load_weather_key():
    if _weather_key["key"]:
        return _weather_key["key"]
    try:
        blob = json.loads(WEATHER_KEY_FILE.read_text())
        k = (blob.get("key") or "").strip()
        if k:
            _weather_key["key"] = k
    except Exception:
        pass
    return _weather_key["key"]


def _save_weather_key(k):
    _weather_key["key"] = (k or "").strip() or None
    try:
        if _weather_key["key"]:
            WEATHER_KEY_FILE.write_text(json.dumps({"key": _weather_key["key"]}))
        elif WEATHER_KEY_FILE.exists():
            WEATHER_KEY_FILE.unlink()
    except Exception:
        pass


# ---- Resource condition ratings --------------------------------------------
# Each returns (rating, headline) where rating is one of
# good / fair / poor / none / unknown. These are deliberately conservative and
# clearly labelled as indicative — they estimate *resource availability* from
# current weather, not actual metered output.

def _rate_wind(hub_ms):
    """Wind resource from hub-height (~100m) wind speed, m/s. Turbines start
    ~3-4 m/s, reach rated output ~12-15 m/s, and cut out ~25 m/s to protect
    the machine — so 'storm' is poor for output, not good."""
    if hub_ms is None:
        return "unknown", "no wind data"
    if hub_ms < 4:    return "poor", "calm — near cut-in, little output"
    if hub_ms < 8:    return "fair", "light — part-load"
    if hub_ms < 12:   return "good", "fresh — strong output"
    if hub_ms < 25:   return "good", "windy — near rated output"
    return "poor", "storm — turbines cut out"


SOLAR_MIN_DEG = 5.0    # sun below this is too low for meaningful output
SOLAR_LOW_DEG = 15.0   # below this, geometry caps output — clear sky is "fair" at best


def _solar_elevation(lat, lon, dt_unix):
    """Sun elevation (degrees above horizon) at a lat/lon and Unix-UTC time.
    Standard declination + hour-angle approximation, accurate to a fraction of a
    degree — ample for gating a resource verdict. Negative = below the horizon."""
    if lat is None or lon is None or dt_unix is None:
        return None
    d = datetime.fromtimestamp(dt_unix, tz=timezone.utc)
    n = d.timetuple().tm_yday
    frac_hr = d.hour + d.minute / 60.0 + d.second / 3600.0
    decl = -23.44 * math.cos(math.radians(360.0 / 365.0 * (n + 10)))
    b = math.radians(360.0 / 365.0 * (n - 81))
    eot = 9.87 * math.sin(2 * b) - 7.53 * math.cos(b) - 1.5 * math.sin(b)   # minutes
    tst = frac_hr * 60.0 + eot + 4.0 * lon        # true solar time, minutes (lon E +)
    ha = math.radians(tst / 4.0 - 180.0)          # hour angle, 0 at solar noon
    la, de = math.radians(lat), math.radians(decl)
    s_elev = math.sin(la) * math.sin(de) + math.cos(la) * math.cos(de) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, s_elev))))


def _rate_solar(clouds_pct, elev_deg):
    """Solar resource from sun elevation AND cloud. Elevation gates the verdict so
    a clear but low sun near dawn/dusk can't read "near full output" when geometry
    limits it (output scales ~sin(elevation)). Night (elev <= 0) is "none", not a
    misleading red "poor". Above SOLAR_LOW_DEG it is the usual cloud rating."""
    if elev_deg is None:
        return "unknown", "no sun-position data"
    if elev_deg <= 0:
        return "none", "night — no solar output"
    if elev_deg < SOLAR_MIN_DEG:
        return "poor", f"low sun ({elev_deg:.0f}\u00b0) — minimal output"
    if clouds_pct is None:
        return "unknown", "no cloud data"
    if elev_deg < SOLAR_LOW_DEG:            # geometry caps output near dawn/dusk
        if clouds_pct <= 50:  return "fair", f"low sun ({elev_deg:.0f}\u00b0) — reduced output"
        if clouds_pct <= 80:  return "poor", "low sun, cloudy — low output"
        return "poor", "low sun, overcast — minimal output"
    if clouds_pct <= 20:  return "good", "clear — near full output"
    if clouds_pct <= 50:  return "fair", "partly cloudy — reduced"
    if clouds_pct <= 80:  return "poor", "cloudy — low output"
    return "poor", "overcast — minimal output"


def _rate_hydro(rain_1h_mm, humidity_pct):
    """Hydro resource is a WEAK proxy. Real hydro/pumped-storage output depends
    on reservoir level and catchment state over days-to-weeks, not the last
    hour's rain. We only indicate whether conditions are adding inflow now, and
    label it plainly as indicative so it isn't mistaken for a generation figure.
    Pumped storage in particular is dispatched on price, not rainfall."""
    if rain_1h_mm is None and humidity_pct is None:
        return "unknown", "no data"
    r = rain_1h_mm or 0.0
    if r >= 2.0:   return "good", f"heavy rain ({r:.1f} mm/h) — catchment inflow"
    if r >= 0.2:   return "fair", f"light rain ({r:.1f} mm/h) — some inflow"
    if humidity_pct is not None and humidity_pct >= 90:
        return "fair", "damp — saturated catchment"
    return "poor", "dry now — inflow depends on stored water"


def _rate_site(site, obs):
    """Given a RESOURCE_SITES tuple and an OpenWeather observation dict, return
    a per-site card: type, rating, headline, and the raw driver value."""
    name, lat, lon, typ, weight, offshore = site
    wind = (obs.get("wind") or {})
    clouds = (obs.get("clouds") or {})
    main = (obs.get("main") or {})
    rain = (obs.get("rain") or {})
    v10 = wind.get("speed")
    dt = obs.get("dt")

    card = {"name": name, "type": typ, "lat": lat, "lon": lon}
    if typ == "wind":
        hub = _hub_wind(v10, offshore)
        rating, head = _rate_wind(hub)
        card.update({"rating": rating, "headline": head,
                     "wind_hub_ms": round(hub, 1) if hub is not None else None,
                     "wind_10m_ms": round(v10, 1) if v10 is not None else None,
                     "estimated": True})   # hub speed is extrapolated
    elif typ == "solar":
        cl = clouds.get("all")
        elev = _solar_elevation(lat, lon, dt)
        rating, head = _rate_solar(cl, elev)
        card.update({"rating": rating, "headline": head, "clouds_pct": cl,
                     "sun_elev_deg": round(elev, 1) if elev is not None else None,
                     "is_day": (elev is not None and elev > 0)})
    else:  # hydro
        r1 = rain.get("1h")
        hum = main.get("humidity")
        rating, head = _rate_hydro(r1, hum)
        card.update({"rating": rating, "headline": head,
                     "rain_1h_mm": r1, "humidity_pct": hum, "indicative": True})
    return card


def get_weather():
    """Per-location resource conditions from OpenWeather (Current Weather Data).
    Fetches each RESOURCE_SITE and rates it for its power type (wind/solar/hydro),
    plus the demand-temperature sites for the population-weighted temperature.

    Requires an API key entered via the dashboard (POST /api/weather-key). One
    HTTP call per site; the free tier allows 60/min so ~16 calls is comfortable.
    Cached 15 min, with a disk last-good fallback and limit-aware backoff, so a
    transient failure or restart shows a clearly-aged reading, not a blank panel.

    Retains aggregate fields (avg_wind_100m_ms, avg_temp_c) for the existing
    wind-vs-metered cross-check and supply-stack logic."""
    now = time.time()
    c = _weather_cache

    if c["data"] is None:
        disk_data, disk_ts = _load_weather_disk()
        if disk_data:
            c["data"], c["ts"] = disk_data, disk_ts

    if c["data"] and now - c["ts"] < WEATHER_TTL:
        return c["data"]

    def _fallback():
        if c["data"]:
            stale = dict(c["data"])
            stale["stale"] = True
            stale["stale_age_s"] = round(now - c["ts"])
            if c["err"]:
                stale["error"] = c["err"]
            return stale
        return {"error": c["err"] or "weather unavailable",
                "avg_wind_100m_ms": None, "avg_temp_c": None,
                "sites": [], "needs_key": not _load_weather_key()}

    api_key = _load_weather_key()
    if not api_key:
        # No key yet — tell the frontend to prompt for one. Not an error state.
        c["err"] = "OpenWeather API key not set"
        out = _fallback()
        out["needs_key"] = True
        return out

    if now < c["backoff_until"]:
        return _fallback()

    # Daily budget gate: a full refresh needs one call per site. If the whole
    # cycle won't fit in today's remaining budget, don't start it — serve the
    # last-good reading and wait. This is the hard ceiling that guarantees we
    # stay under WEATHER_DAILY_MAX regardless of TTL, restarts or retries.
    cycle_cost = len(RESOURCE_SITES) + len(DEMAND_SITES)
    remaining = _budget_remaining()
    if remaining < cycle_cost:
        mins = round(_seconds_to_utc_midnight(now) / 60)
        c["err"] = (f"Daily OpenWeather budget reached "
                    f"({_load_budget()['count']}/{WEATHER_DAILY_MAX} calls) — "
                    f"resets at 00:00 UTC (~{mins} min)")
        # hold off until the budget resets so we don't re-check every snapshot
        c["backoff_until"] = now + min(_seconds_to_utc_midnight(now), 3600)
        out = _fallback()
        out["budget_capped"] = True
        return out
    site_cards = []
    winds_hub = []            # (hub_ms, weight) for aggregate wind
    fetched, failed = 0, 0
    first_err = None
    for site in RESOURCE_SITES:
        name, lat, lon, typ, weight, offshore = site
        try:
            _budget_spend(1)          # count before the call — failures cost quota too
            obs = _openweather_one(lat, lon, api_key)
            fetched += 1
        except urllib.error.HTTPError as e:
            failed += 1
            if first_err is None:
                first_err = e
            # 401 = bad/inactive key: stop early, it'll fail for every site.
            if e.code == 401:
                break
            continue
        except Exception as e:
            failed += 1
            if first_err is None:
                first_err = e
            continue
        card = _rate_site(site, obs)
        site_cards.append(card)
        if typ == "wind" and card.get("wind_hub_ms") is not None:
            winds_hub.append((card["wind_hub_ms"], weight))

    # Handle a total failure (bad key, rate limit, network) with classification.
    if not site_cards:
        c["fails"] += 1
        if isinstance(first_err, urllib.error.HTTPError):
            if first_err.code == 401:
                c["err"] = ("OpenWeather rejected the API key (HTTP 401). New keys "
                            "can take up to ~2 hours to activate.")
                # A bad/inactive key won't recover in minutes, and each retry
                # still spends a call. Back off 30 min (a re-save via the key
                # modal clears this immediately for the "I just fixed it" case).
                c["backoff_until"] = now + 1800
                out = _fallback(); out["bad_key"] = True; out["needs_key"] = True
                return out
            if first_err.code == 429:
                import random
                base = min(180 * (2 ** (c["fails"] - 1)), 1800)
                c["backoff_until"] = now + base + random.uniform(0, 30)
                c["err"] = (f"OpenWeather rate-limit (HTTP 429) — backing off "
                            f"~{round(base / 60)} min")
                return _fallback()
            c["err"] = f"HTTP {first_err.code} {first_err.reason}"
        else:
            c["err"] = f"{type(first_err).__name__}: {first_err}" if first_err else "no data"
        # Network / non-HTTP failure. Do NOT retry every snapshot — that would
        # burn budget on doomed calls (a total-failure cycle still spends one
        # call per site attempted before it bails). Back off progressively,
        # anchored to the normal refresh cadence: half the TTL on the first
        # failure, growing to a full TTL, so an outage costs at most a couple of
        # wasted cycles rather than ~30/hour. Weather is slow; waiting is cheap.
        base = min((WEATHER_TTL // 2) * (2 ** (c["fails"] - 1)), WEATHER_TTL)
        c["backoff_until"] = now + base
        return _fallback()

    # Demand-weighted temperature from the conurbation sites.
    tsum, twt = 0.0, 0.0
    for name, lat, lon, wt in DEMAND_SITES:
        try:
            _budget_spend(1)
            obs = _openweather_one(lat, lon, api_key)
        except Exception:
            continue
        t = (obs.get("main") or {}).get("temp")
        if t is not None:
            tsum += t * wt; twt += wt
    avg_temp = round(tsum / twt, 1) if twt else None

    # Capacity-weighted aggregate hub wind (kept for the wind-vs-metered check).
    if winds_hub:
        wsum = sum(v * w for v, w in winds_hub)
        wwt = sum(w for _, w in winds_hub)
        avg_wind = round(wsum / wwt, 1) if wwt else None
    else:
        avg_wind = None

    band = None
    if avg_wind is not None:
        if avg_wind < 4:    band = "calm — low wind output"
        elif avg_wind < 8:  band = "light — part-load"
        elif avg_wind < 12: band = "fresh — strong output"
        elif avg_wind < 25: band = "windy — near rated output"
        else:               band = "storm — cut-out risk"

    # Roll up a headline count per resource type for the panel summary. We keep
    # the raw per-rating counts so the dashboard can say something clearer than
    # "0/N good" (e.g. all-fair reads as "mixed", not "bad"). 'good' is retained
    # for backward compatibility with older frontends.
    def _summary(typ):
        cards = [s for s in site_cards if s["type"] == typ]
        n = len(cards)
        good = sum(1 for s in cards if s["rating"] == "good")
        fair = sum(1 for s in cards if s["rating"] == "fair")
        poor = sum(1 for s in cards if s["rating"] == "poor")
        # night-time solar cards are rated "none" — surfaced separately so the
        # dashboard can say "in darkness" rather than implying a bad resource.
        none = sum(1 for s in cards if s["rating"] == "none")
        rated = good + fair + poor          # cards carrying a live day-time verdict
        # Overall verdict for the resource type, from the distribution of the
        # cards that actually have one. Deliberately conservative wording.
        if n == 0:
            verdict = None
        elif rated == 0:
            verdict = "none" if none else "unknown"
        elif good == rated:
            verdict = "strong"
        elif poor == rated:
            verdict = "weak"
        elif good >= rated - good:          # good sites are at least half
            verdict = "fair-good"           # -> "moderate-good"
        elif poor > good + fair:            # poor dominates
            verdict = "poor-weak"           # -> "mostly weak"
        else:
            verdict = "mixed"
        return {"n": n, "good": good, "fair": fair, "poor": poor,
                "none": none, "rated": rated, "verdict": verdict}

    result = {
        "provider": "OpenWeather",
        "sites": site_cards,
        "summary": {t: _summary(t) for t in ("wind", "solar", "hydro")},
        "avg_wind_100m_ms": avg_wind,     # aggregate, for cross-checks
        "wind_band": band,
        "avg_temp_c": avg_temp,
        "n_sites": len(site_cards),
        "partial": failed > 0,
        "calls_today": _load_budget()["count"],
        "calls_max": WEATHER_DAILY_MAX,
    }
    c["data"] = result; c["ts"] = now; c["err"] = None
    c["fails"] = 0; c["backoff_until"] = 0
    _save_weather_disk(result, now)
    return result


# ---- Octopus Energy: personal home consumption -----------------------------
# Pulls the user's own electricity + gas half-hourly consumption from the
# Octopus API (HTTP Basic auth, API key as username, blank password). The key
# and meter IDs are entered in the browser, POSTed to /api/octopus-config, held
# in memory and persisted to disk. NEVER baked into the served HTML; treat the
# config file as a secret. Tariff rates are stored too so we can cost usage.
OCTOPUS_BASE = "https://api.octopus.energy/v1"
OCTOPUS_CFG_FILE = Path(__file__).with_name("octopus_config.json")
_octopus_cfg = {"data": None, "mtime": None}
_octopus_cache = {"data": None, "ts": 0}
OCTOPUS_TTL = 1800        # consumption updates ~half-hourly; refresh every 30 min

# Gas m3 -> kWh conversion (SMETS2 meters report volume in m3). Standard Ofgem
# formula: kWh = m3 * volume_correction(1.02264) * calorific_value(~39.5) / 3.6.
GAS_VOL_CORRECTION = 1.02264
GAS_CALORIFIC = 39.5
GAS_M3_TO_KWH = GAS_VOL_CORRECTION * GAS_CALORIFIC / 3.6   # ~11.22 kWh per m3

# There is NO unit field in the Octopus consumption payload. SMETS1 gas meters
# return kWh already; SMETS2 return m3. The unit is a fixed property of the
# meter, so it is a user-declared config value (gas_units: "m3" | "kwh"), not
# something we infer at runtime. Magnitude auto-detection is retained ONLY as a
# soft plausibility WARNING — it never overrides the declared setting, because a
# genuinely low-usage summer month in kWh can masquerade as m3 and mispricing by
# ~11x is a far worse failure than asking the user to set one flag once.


def _resolve_gas_units(cfg, results=None):
    """Single source of truth for gas unit interpretation. Returns a dict:
        is_m3      : bool  -- apply the m3->kWh conversion?
        conv       : float -- multiplier to reach kWh (GAS_M3_TO_KWH or 1.0)
        declared   : str   -- the raw configured value ("m3"/"kwh"/"auto"/unset)
        confirmed  : bool  -- did the user explicitly declare m3 or kwh?
        warning    : str|None -- plausibility mismatch note, if any

    'results' (raw half-hourly records) is optional; when supplied it drives the
    plausibility guard. The guard NEVER changes is_m3 for an explicit setting."""
    raw = (cfg.get("gas_units") or "").lower().strip()
    declared = raw or "unset"

    # median half-hour consumption, if we have data to look at
    med = None
    if results:
        vals = sorted(r["consumption"] for r in results if r.get("consumption"))
        if vals:
            med = vals[len(vals) // 2]

    # Thresholds. Typical domestic half-hour reading:
    #   kWh mode : ~0.1 - 3.0  (a quiet summer half-hour can dip to ~0.03-0.05)
    #   m3  mode : ~0.01 - 0.3 (that same energy is ~11x smaller as volume)
    # LOOKS_M3: at/under this, the data is more consistent with m3 than kWh —
    #   used both to WARN and (for 'auto' only) to flip. Set at 0.15 so it spans
    #   the bulk of the m3 range while staying under normal kWh usage.
    # LOOKS_KWH: at/over this, the data is clearly kWh-scale — used to warn when
    #   the meter is configured as m3 but is plainly already in kWh.
    LOOKS_M3 = 0.15
    LOOKS_KWH = 0.6

    if raw == "m3":
        is_m3, confirmed = True, True
    elif raw == "kwh":
        is_m3, confirmed = False, True
    elif raw == "auto":
        # explicit opt-in to detection: flip to m3 when the data looks like m3
        is_m3 = (med is not None and med <= LOOKS_M3)
        confirmed = False
    else:  # unset -> conservative default: assume kWh, but flag as unconfirmed
        is_m3, confirmed = False, False

    # Plausibility guard: WARN (never act) when the data looks inconsistent with
    # the chosen interpretation. This catches a mis-set flag loudly rather than
    # silently mispricing by ~11x.
    warning = None
    if med is not None:
        if is_m3 and med >= LOOKS_KWH:
            warning = ("gas configured as m3 but half-hourly median is {:.3f} — "
                       "that looks like kWh already; verify meter type").format(med)
        elif (not is_m3) and med <= LOOKS_M3:
            warning = ("gas configured/defaulted to kWh but half-hourly median is "
                       "{:.3f} — that looks like m3; set gas_units=m3 if SMETS2"
                       ).format(med)

    return {"is_m3": is_m3, "conv": (GAS_M3_TO_KWH if is_m3 else 1.0),
            "declared": declared, "confirmed": confirmed, "warning": warning}


def _load_octopus_cfg():
    # Re-read the file when it changes on disk (mtime). Previously the config was
    # cached in memory for the life of the process and never re-read, so a
    # hand-edit to octopus_config.json (e.g. adding "gas_units":"m3") had no
    # effect until a full restart — a silent, confusing failure. Now any change
    # to the file is picked up on the next access.
    try:
        mtime = OCTOPUS_CFG_FILE.stat().st_mtime
    except OSError:
        mtime = None
    if _octopus_cfg["data"] is not None and _octopus_cfg.get("mtime") == mtime:
        return _octopus_cfg["data"]
    try:
        _octopus_cfg["data"] = json.loads(OCTOPUS_CFG_FILE.read_text())
    except Exception:
        _octopus_cfg["data"] = {}
    _octopus_cfg["mtime"] = mtime
    return _octopus_cfg["data"]


def _save_octopus_cfg(cfg):
    cur = dict(_load_octopus_cfg() or {})
    for k, v in (cfg or {}).items():
        if v is None:
            continue
        if isinstance(v, str):
            v = v.strip()
        cur[k] = v
    _octopus_cfg["data"] = cur
    _octopus_cache["data"] = None
    _octopus_cache["ts"] = 0
    try:
        OCTOPUS_CFG_FILE.write_text(json.dumps(cur))
        # record the mtime we just wrote so the next _load doesn't treat our own
        # in-memory copy as stale and re-read (harmless, but avoids a round-trip)
        try:
            _octopus_cfg["mtime"] = OCTOPUS_CFG_FILE.stat().st_mtime
        except OSError:
            _octopus_cfg["mtime"] = None
    except Exception:
        pass
    return cur


def _octopus_has_config():
    cfg = _load_octopus_cfg() or {}
    return bool(cfg.get("api_key") and cfg.get("elec_mpan") and cfg.get("elec_serial"))


def _octopus_fetch(path, api_key, params=None):
    """GET an Octopus endpoint with Basic auth (key as username, blank pw)."""
    url = OCTOPUS_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "uk-grid-monitor"})
    userpass = base64.b64encode(f"{api_key}:".encode()).decode()
    req.add_header("Authorization", "Basic " + userpass)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _octopus_consumption(kind, point, serial, api_key, hours=336):
    """Half-hourly consumption for the last `hours` (default 14 days). kind is
    'electricity-meter-points' or 'gas-meter-points'. Returns results list."""
    now = datetime.now(timezone.utc)
    params = {
        "period_from": (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "period_to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "page_size": 2000, "order_by": "period",
    }
    path = f"/{kind}/{point}/meters/{serial}/consumption/"
    d = _octopus_fetch(path, api_key, params)
    return d.get("results", []) if isinstance(d, dict) else []


def _agreements_for_fuel(account_doc, fuel):
    """Pull one fuel's dated agreements out of a single account-endpoint response.
    fuel is 'electricity' or 'gas'. Returns a date-ascending list."""
    key = "electricity_meter_points" if fuel == "electricity" else "gas_meter_points"
    out = []
    for prop in (account_doc.get("properties") or []):
        for mp in (prop.get(key) or []):
            for ag in (mp.get("agreements") or []):
                out.append({
                    "tariff_code": ag.get("tariff_code"),
                    "valid_from": ag.get("valid_from"),
                    "valid_to": ag.get("valid_to"),
                })
    out.sort(key=lambda a: a.get("valid_from") or "")
    return out


def _octopus_agreements(account_number, api_key, gas_account_number=None):
    """Resolve the account's tariff history from the REST account endpoint.

    Returns, per fuel, the ordered list of tariff AGREEMENTS actually applied to
    this account — each with its tariff_code and the dates it was in force. This
    is what lets us cost past usage against the rate that applied AT THE TIME,
    rather than one flat typed number. Read-only; the product/rate lookups that
    turn a tariff_code into pence come in a later stage.

    Account model: most customers have ONE account number covering both fuels
    (dual fuel). Some have electricity and gas under SEPARATE accounts. So gas is
    resolved from its own `gas_account_number` when given, otherwise from the
    primary `account_number` alongside electricity.

    Shape:
      {"electricity": [{"tariff_code": "E-1R-VAR-22-11-01-C",
                         "valid_from": "2024-01-01T00:00:00Z",
                         "valid_to":   "2025-06-01T00:00:00Z" | None}, ...],
       "gas": [ ... ],
       "elec_account": "A-XXXXXXXX", "gas_account": "A-YYYYYYYY"}
    A valid_to of None means still in force. Agreements are date-ascending.
    Returns {"error": "..."} on failure rather than raising, so callers can fall
    back to typed rates and label the panel honestly. Per-fuel resolution is
    best-effort: if the gas account fails but electricity succeeds, electricity
    is still returned (and vice versa), with a per-fuel error note.
    """
    if not (account_number and api_key):
        return {"error": "account number and api key required"}

    def _fetch_account(acct):
        try:
            return _octopus_fetch(f"/accounts/{acct}/", api_key), None
        except urllib.error.HTTPError as e:
            return None, f"HTTP {e.code}"
        except Exception as e:
            return None, type(e).__name__

    out = {"electricity": [], "gas": [],
           "elec_account": account_number,
           "gas_account": gas_account_number or account_number}

    # Primary account: electricity, and gas too unless gas has its own account.
    primary_doc, err = _fetch_account(account_number)
    if primary_doc is None:
        # Primary failed entirely — nothing to resolve from it.
        out["error"] = f"account lookup failed: {err}"
        return out
    out["electricity"] = _agreements_for_fuel(primary_doc, "electricity")

    if gas_account_number and gas_account_number != account_number:
        gas_doc, gerr = _fetch_account(gas_account_number)
        if gas_doc is None:
            out["gas_error"] = f"gas account lookup failed: {gerr}"
        else:
            out["gas"] = _agreements_for_fuel(gas_doc, "gas")
    else:
        out["gas"] = _agreements_for_fuel(primary_doc, "gas")

    return out


# ---- Stage 2: tariff-code -> dated rate history (public product endpoints) --
# The account gives us tariff CODES and when each applied. To cost usage we need
# what each tariff CHARGED, as dated periods. Those live on the public product
# endpoints (no auth). Cached in-process keyed by tariff code + payment method,
# since rate history changes rarely (a few times a year for standard tariffs).
_octopus_rates_cache = {}          # {(tariff_code, pay): {"ts":.., "unit":[..], "standing":[..]}}
OCTOPUS_RATES_TTL = 6 * 3600       # rate history is slow-moving; 6h is plenty

_TARIFF_RE = re.compile(r"^([EG])-\d+R-(.+)-[A-P]$")


def _product_from_tariff(tariff_code):
    """E-1R-VAR-22-11-01-C -> ('electricity','VAR-22-11-01'); gas -> 'gas'.
    Returns (fuel_path, product_code) or (None, None) if unparseable."""
    m = _TARIFF_RE.match(tariff_code or "")
    if not m:
        return None, None
    fuel_path = "electricity-tariffs" if m.group(1) == "E" else "gas-tariffs"
    return fuel_path, m.group(2)


def _pick_rate_records(results, pay="DIRECT_DEBIT"):
    """The rate-history endpoints return TWO records per period — one for Direct
    Debit, one for non-DD (via payment_method). Collapse to one series for the
    chosen method, newest-first, as {valid_from, valid_to, p} using inc-VAT."""
    picked = []
    for r in results:
        pm = r.get("payment_method")
        # Records with payment_method None apply to all methods; keep those too.
        if pm is not None and pm != pay:
            continue
        picked.append({
            "valid_from": r.get("valid_from"),
            "valid_to": r.get("valid_to"),
            "p": r.get("value_inc_vat"),
        })
    # If the filter removed everything (e.g. tariff has no per-method split under
    # a different label), fall back to all records so we don't return empty.
    if not picked and results:
        picked = [{"valid_from": r.get("valid_from"), "valid_to": r.get("valid_to"),
                   "p": r.get("value_inc_vat")} for r in results]
    picked.sort(key=lambda x: x.get("valid_from") or "", reverse=True)
    return picked


def _octopus_rate_history(tariff_code, pay="DIRECT_DEBIT"):
    """Fetch dated unit-rate and standing-charge history for one tariff code.

    Returns {"unit":[{valid_from,valid_to,p}], "standing":[...],
             "tariff_code":.., "product":..} or {"error":..}. Public endpoints,
    no auth. Cached for OCTOPUS_RATES_TTL. 'pay' selects the payment-method
    variant (Direct Debit by default — the common domestic case)."""
    if not tariff_code:
        return {"error": "no tariff code"}
    ck = (tariff_code, pay)
    hit = _octopus_rates_cache.get(ck)
    if hit and (time.time() - hit["ts"]) < OCTOPUS_RATES_TTL:
        return hit["data"]

    fuel_path, product = _product_from_tariff(tariff_code)
    if not product:
        return {"error": f"unparseable tariff code: {tariff_code}"}

    base = f"https://api.octopus.energy/v1/products/{product}/{fuel_path}/{tariff_code}/"

    def _fetch_all(kind):
        # Paginate the rate/standing history; usually 1 page for standard tariffs.
        rows, url = [], base + kind + "/"
        for _ in range(6):          # cap pages (Agile could be long; costing
            try:                     # only needs the dated STANDARD periods here)
                req = urllib.request.Request(url, headers={"User-Agent": "uk-grid-monitor"})
                with urllib.request.urlopen(req, timeout=30) as r:
                    d = json.load(r)
            except Exception as e:
                return None, type(e).__name__
            rows.extend(d.get("results", []))
            url = d.get("next")
            if not url:
                break
        return rows, None

    unit_raw, uerr = _fetch_all("standard-unit-rates")
    if unit_raw is None:
        return {"error": f"unit-rate fetch failed: {uerr}"}
    stand_raw, serr = _fetch_all("standing-charges")
    if stand_raw is None:
        return {"error": f"standing-charge fetch failed: {serr}"}

    data = {
        "tariff_code": tariff_code,
        "product": product,
        "pay": pay,
        "unit": _pick_rate_records(unit_raw, pay),
        "standing": _pick_rate_records(stand_raw, pay),
    }
    _octopus_rates_cache[ck] = {"ts": time.time(), "data": data}
    return data


# ---- Stage 3: matched costing (each reading × the rate in force at its time) -
# The honest calculation: rather than total_kWh × one flat rate, cost every
# half-hourly reading against the unit rate that actually applied on its date,
# and sum standing charges per day at the standing rate in force that day. This
# stays correct across price changes and tariff switches. All rates are inc-VAT
# (see _pick_rate_records -> value_inc_vat); do NOT add VAT again downstream.

def _rate_at(periods, iso_t):
    """Unit/standing rate (pence) in force at ISO timestamp iso_t. `periods` is
    the dated list from _pick_rate_records (newest-first): each has valid_from,
    valid_to (None = open), p. Returns pence or None if no period covers t."""
    for pr in periods:                       # newest-first; first match wins
        vf, vt = pr.get("valid_from"), pr.get("valid_to")
        if vf and iso_t < vf:
            continue
        if vt and iso_t >= vt:
            continue
        return pr.get("p")
    return None


def _matched_energy_cost(pts, unit_periods):
    """Sum energy cost (pence) over timestamped kWh points, each at its own-time
    unit rate. Returns (cost_p, uncosted_kwh) — uncosted_kwh is usage that fell
    outside every known rate period (honest: reported, not silently zero-cost)."""
    cost = 0.0
    uncosted = 0.0
    for p in pts:
        rate = _rate_at(unit_periods, p["t"])
        if rate is None:
            uncosted += p["kwh"]
        else:
            cost += p["kwh"] * rate
    return cost, round(uncosted, 4)


def _matched_standing_cost(day_list, standing_periods):
    """Sum standing charge (pence) across the given days, each at the standing
    rate in force that day. day_list is 'YYYY-MM-DD' strings. Days with no known
    rate are skipped and counted as uncovered."""
    cost = 0.0
    uncovered = 0
    for day in day_list:
        # anchor at midday UTC to avoid edge-of-day boundary ambiguity
        rate = _rate_at(standing_periods, day + "T12:00:00Z")
        if rate is None:
            uncovered += 1
        else:
            cost += rate
    return cost, uncovered


def _last_billing_period(end_day, today=None):
    """Given the day-of-month a billing period ENDS on (e.g. 16 -> period runs
    17th of one month to 16th of the next, inclusive of the 16th), return the
    (from_date, to_date) ISO dates of the LAST COMPLETE period — the most recent
    one that has already ended. Both 'YYYY-MM-DD'. None if end_day unusable.
    Clamps end_day to each month's length so short months stay valid."""
    try:
        end_day = int(end_day)
    except (TypeError, ValueError):
        return None
    if not (1 <= end_day <= 31):
        return None
    from calendar import monthrange
    d = today or datetime.now(timezone.utc).date()

    def mkdate(y, m):
        return datetime(y, m, min(end_day, monthrange(y, m)[1])).date()

    cur_end = mkdate(d.year, d.month)
    if cur_end < d:
        end = cur_end
    else:
        pm_y, pm_m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        end = mkdate(pm_y, pm_m)
    sm_y, sm_m = (end.year - 1, 12) if end.month == 1 else (end.year, end.month - 1)
    prev_end = mkdate(sm_y, sm_m)
    start = prev_end + timedelta(days=1)
    return start.isoformat(), end.isoformat()


def _iso_plus_days(iso_date, n):
    """'YYYY-MM-DD' + n days -> 'YYYY-MM-DD'."""
    return (datetime.fromisoformat(iso_date + "T00:00:00+00:00")
            + timedelta(days=n)).date().isoformat()


def _looks_like_m3(results):
    """Heuristic: SMETS2 gas reports m3 (small per-half-hour values); SMETS1 is
    pre-converted to kWh. Median under ~0.35 => almost certainly m3."""
    vals = [r["consumption"] for r in results if r.get("consumption")]
    if not vals:
        return False
    vals.sort()
    return vals[len(vals) // 2] < 0.35


def _summarise_consumption(results, unit_rate_p, standing_p, is_gas=False, gas_is_m3=False,
                           unit_periods=None, standing_periods=None, billing_window=None):
    """Raw half-hourly results -> totals, daily breakdown, cost. unit_rate_p is
    pence/kWh, standing_p pence/day."""
    if not results:
        return None
    conv = GAS_M3_TO_KWH if (is_gas and gas_is_m3) else 1.0
    pts = []
    for r in results:
        c = r.get("consumption")
        if c is None:
            continue
        pts.append({"t": r["interval_start"], "kwh": round(c * conv, 4)})
    if not pts:
        return None
    pts.sort(key=lambda p: p["t"])
    total_kwh = sum(p["kwh"] for p in pts)
    latest_t = pts[-1]["t"]
    # data latency: how far behind 'now' the most recent half-hour is. Octopus
    # consumption is NOT real-time (DCC collection lag is typically most of a
    # day, and irregular), so we report this openly rather than implying live.
    try:
        latest_dt = datetime.fromisoformat(latest_t.replace("Z", "+00:00"))
        lag_h = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600.0
    except Exception:
        lag_h = None
    daily = {}
    day_slots = {}
    for p in pts:
        day = p["t"][:10]
        daily[day] = daily.get(day, 0.0) + p["kwh"]
        day_slots[day] = day_slots.get(day, 0) + 1
    days = sorted(daily)
    # A complete day has 48 half-hour slots. The first and last day in the fetch
    # window are usually partial (data starts/ends mid-day) and would distort
    # day-level stats — a partial day looks artificially "cheap". Full days only
    # for any per-day insight; allow a tiny tolerance for the odd missing slot.
    full_days = [d for d in days if day_slots.get(d, 0) >= 46]
    n_days = max(1, len(days))

    # Costing: if dated rate history is supplied, cost each reading against the
    # rate in force at ITS time and sum standing per-day at that day's rate
    # (stays correct across price changes / tariff switches). Otherwise fall back
    # to the flat typed rate. All matched rates are inc-VAT — do not re-add VAT.
    matched = bool(unit_periods) and bool(standing_periods)
    uncosted_kwh = 0.0
    uncovered_days = 0
    n_unit_periods = 0
    if matched:
        energy_cost_p, uncosted_kwh = _matched_energy_cost(pts, unit_periods)
        standing_cost_p, uncovered_days = _matched_standing_cost(days, standing_periods)
        # count distinct rate periods actually spanned by this usage window
        seen = set()
        for p in pts:
            r = _rate_at(unit_periods, p["t"])
            if r is not None:
                seen.add(r)
        n_unit_periods = len(seen)
        # a single representative "current" rate for insight figures that need a
        # scalar (baseline projection etc.) — the newest period's rate.
        eff_unit_p = unit_periods[0]["p"] if unit_periods else unit_rate_p
        eff_standing_p = standing_periods[0]["p"] if standing_periods else standing_p
    else:
        energy_cost_p = total_kwh * unit_rate_p
        standing_cost_p = standing_p * n_days
        eff_unit_p = unit_rate_p
        eff_standing_p = standing_p

    # ---- deeper insights from the half-hourly shape ----
    from statistics import mean
    insights = {}
    try:
        # average usage by half-hour-of-day slot -> peak & quietest times
        slot = {}
        for p in pts:
            hm = p["t"][11:16]
            slot.setdefault(hm, []).append(p["kwh"])
        slot_avg = {k: mean(v) for k, v in slot.items()}
        if slot_avg:
            peak_hm = max(slot_avg, key=slot_avg.get)
            quiet_hm = min(slot_avg, key=slot_avg.get)
            insights["peak_slot"] = peak_hm
            insights["peak_slot_kwh"] = round(slot_avg[peak_hm], 3)
            insights["quiet_slot"] = quiet_hm
            insights["slot_avg"] = {k: round(v, 4) for k, v in slot_avg.items()}
        # baseline / always-on load: 5th-percentile half-hour draw
        allk = sorted(p["kwh"] for p in pts)
        if allk:
            base_kwh = allk[max(0, len(allk) // 20)]
            insights["baseline_kwh_hh"] = round(base_kwh, 3)
            insights["baseline_watts"] = round(base_kwh * 2000)   # kWh/half-hour -> avg W
            insights["baseline_daily_cost_p"] = round(base_kwh * 48 * eff_unit_p)
        # overnight share (00:00-06:00)
        night = sum(p["kwh"] for p in pts if "00:00" <= p["t"][11:16] < "06:00")
        insights["overnight_pct"] = round(100 * night / total_kwh) if total_kwh else 0
        # weekday vs weekend daily average — full days only
        wd, we = {}, {}
        for d in full_days:
            try:
                dt = datetime.fromisoformat(d + "T00:00:00+00:00")
            except Exception:
                continue
            (we if dt.weekday() >= 5 else wd)[d] = daily[d]
        if wd:
            insights["weekday_avg_kwh"] = round(mean(wd.values()), 2)
        if we:
            insights["weekend_avg_kwh"] = round(mean(we.values()), 2)
        # highest & lowest cost day — full days only (a partial day looks cheap).
        # Under matched costing, each day is priced at the unit+standing rate in
        # force THAT day; otherwise the flat typed rate.
        if matched:
            day_cost = {}
            for d in full_days:
                u = _rate_at(unit_periods, d + "T12:00:00Z")
                s = _rate_at(standing_periods, d + "T12:00:00Z")
                if u is not None and s is not None:
                    day_cost[d] = daily[d] * u + s
        else:
            day_cost = {d: daily[d] * unit_rate_p + standing_p for d in full_days}
        if day_cost:
            hd = max(day_cost, key=day_cost.get); ld = min(day_cost, key=day_cost.get)
            insights["dearest_day"] = {"date": hd, "cost_p": round(day_cost[hd])}
            insights["cheapest_day"] = {"date": ld, "cost_p": round(day_cost[ld])}
        # this week vs previous week (by daily kWh) — full days only
        if len(full_days) >= 8:
            half = len(full_days) // 2
            prev = mean(daily[d] for d in full_days[:half])
            recent = mean(daily[d] for d in full_days[half:])
            insights["week_trend_pct"] = round(100 * (recent - prev) / prev) if prev else 0
        insights["full_days"] = len(full_days)
        # standing charge as share of a typical day + annual projection.
        # Use full days for the "typical day" so partials don't understate it.
        if full_days:
            typ_day_kwh = mean(daily[d] for d in full_days)
        else:
            typ_day_kwh = total_kwh / n_days
        typ_day_cost = typ_day_kwh * eff_unit_p + eff_standing_p
        insights["standing_share_pct"] = round(100 * eff_standing_p / typ_day_cost) if typ_day_cost else 0
        insights["annual_proj_gbp"] = round(typ_day_cost * 365 / 100)
    except Exception:
        insights = {}

    # ---- last-billing-period cost (if a billing window is supplied) ----
    # Costs only the readings within [from, to] (inclusive of the 'to' day),
    # using the same matched-or-flat basis as the headline. Reported separately
    # so the panel can show a real billing-cycle estimate rather than a rolling
    # window. billing_complete flags whether our data actually covers the whole
    # period (a short/new meter history may only partly cover it — stated, not
    # hidden). billing_window is (from_date, to_date) 'YYYY-MM-DD' or None.
    billing = None
    if billing_window:
        bf, bt = billing_window
        # inclusive of the whole 'to' day
        bt_excl = bt + "T23:59:59Z"
        bf_incl = bf + "T00:00:00Z"
        bpts = [p for p in pts if bf_incl <= p["t"] <= bt_excl]
        bdays = sorted({p["t"][:10] for p in bpts})
        if bpts:
            if matched:
                b_energy, b_uncosted = _matched_energy_cost(bpts, unit_periods)
                b_stand, b_uncov = _matched_standing_cost(bdays, standing_periods)
            else:
                b_energy = sum(p["kwh"] for p in bpts) * unit_rate_p
                b_stand = standing_p * len(bdays)
                b_uncosted = b_uncov = 0
            # coverage: did our data reach the period start? (allow 1 day slack)
            have_from = bpts[0]["t"][:10] if bpts else None
            covers_start = have_from is not None and have_from <= _iso_plus_days(bf, 1)
            billing = {
                "from": bf, "to": bt,
                "kwh": round(sum(p["kwh"] for p in bpts), 2),
                "energy_cost_p": round(b_energy),
                "standing_cost_p": round(b_stand),
                "total_cost_p": round(b_energy + b_stand),
                "days_covered": len(bdays),
                "complete": bool(covers_start),
                "uncosted_kwh": round(b_uncosted, 2),
            }

    return {
        "total_kwh": round(total_kwh, 2),
        "n_days": len(days),
        "billing_period": billing,
        "avg_daily_kwh": round(total_kwh / n_days, 2),
        "daily": [{"date": d, "kwh": round(daily[d], 2), "slots": day_slots.get(d, 0)} for d in days],
        "half_hourly": pts[-96:],
        "latest_t": latest_t,
        "lag_hours": round(lag_h, 1) if lag_h is not None else None,
        "unit_rate_p": unit_rate_p,
        "standing_p": standing_p,
        "energy_cost_p": round(energy_cost_p),
        "standing_cost_p": round(standing_cost_p),
        "total_cost_p": round(energy_cost_p + standing_cost_p),
        "daily_cost_p": round((energy_cost_p + standing_cost_p) / n_days),
        "monthly_proj_p": round((energy_cost_p + standing_cost_p) / n_days * 30),
        "gas_unit": ("m3->kWh" if (is_gas and gas_is_m3) else "kWh"),
        # costing provenance — lets the UI label figures honestly (matched vs
        # flat typed fallback) and flag any usage/days the rate history couldn't
        # cover. eff_* are the current-period rates used for scalar projections.
        "cost_matched": matched,
        "eff_unit_p": round(eff_unit_p, 4) if eff_unit_p is not None else None,
        "eff_standing_p": round(eff_standing_p, 4) if eff_standing_p is not None else None,
        "n_rate_periods": n_unit_periods,
        "uncosted_kwh": round(uncosted_kwh, 2),
        "uncovered_days": uncovered_days,
        "insights": insights,
    }


def _resolve_tariff_periods(cfg):
    """Turn the stored config into per-fuel rate history for matched costing.

    Resolves the account's current tariff (Stage 1) then its dated rate history
    (Stage 2), for whichever fuels have an account number available. Returns:
      {"electricity": {"tariff_code":.., "unit":[..], "standing":[..],
                        "current_unit_p":.., "current_standing_p":.., "error":..},
       "gas": {...}, "pay": "DIRECT_DEBIT"|"NON_DIRECT_DEBIT"}
    Any fuel that can't be resolved gets an "error" note and no periods, so the
    caller silently falls back to that fuel's typed rate. Best-effort: an account
    or rate failure never breaks consumption display.
    """
    acct = cfg.get("account_number")
    gas_acct = cfg.get("gas_account_number")
    key = cfg.get("api_key")
    pay = cfg.get("payment_method") or "DIRECT_DEBIT"
    res = {"electricity": {}, "gas": {}, "pay": pay}
    if not (acct and key):
        # No account number entered — matched costing unavailable; typed fallback.
        res["electricity"]["error"] = "no account number"
        res["gas"]["error"] = "no account number"
        return res

    ag = _octopus_agreements(acct, key, gas_acct)
    if "error" in ag and not ag.get("electricity") and not ag.get("gas"):
        res["electricity"]["error"] = ag["error"]
        res["gas"]["error"] = ag["error"]
        return res

    def _latest_code(agreements):
        # newest agreement (list is date-ascending) that has a tariff code
        for a in reversed(agreements or []):
            if a.get("tariff_code"):
                return a["tariff_code"]
        return None

    for fuel in ("electricity", "gas"):
        code = _latest_code(ag.get(fuel))
        if not code:
            res[fuel]["error"] = ag.get("gas_error") if fuel == "gas" else "no tariff agreement"
            continue
        rh = _octopus_rate_history(code, pay)
        if "error" in rh:
            res[fuel]["error"] = rh["error"]
            continue
        res[fuel] = {
            "tariff_code": code,
            "product": rh.get("product"),
            "unit": rh.get("unit", []),
            "standing": rh.get("standing", []),
            "current_unit_p": rh["unit"][0]["p"] if rh.get("unit") else None,
            "current_standing_p": rh["standing"][0]["p"] if rh.get("standing") else None,
        }
    return res


def get_octopus():
    """User's home electricity + gas consumption and cost. Returns
    {'needs_config': True} if not set up, else summarised data. Cached."""
    if not _octopus_has_config():
        return {"needs_config": True}
    c = _octopus_cache
    if c["data"] and time.time() - c["ts"] < OCTOPUS_TTL:
        return c["data"]
    cfg = _load_octopus_cfg()
    key = cfg["api_key"]
    out = {"needs_config": False, "errors": []}
    # Resolve real tariff + dated rate history for matched costing (best-effort;
    # falls back to typed rates per-fuel if unavailable). Attached to output so
    # the UI can show the tariff panels and cost provenance.
    tariffs = _resolve_tariff_periods(cfg)
    out["tariffs"] = tariffs
    # Billing window (last complete period) from the configured end-of-period day.
    bwin = _last_billing_period(cfg.get("billing_end_day")) if cfg.get("billing_end_day") else None
    out["billing_window"] = bwin
    try:
        er = _octopus_consumption("electricity-meter-points",
                                  cfg["elec_mpan"], cfg["elec_serial"], key, hours=1080)
        et = tariffs.get("electricity", {})
        out["electricity"] = _summarise_consumption(
            er, float(cfg.get("elec_unit_p", 22.70)), float(cfg.get("elec_standing_p", 53.76)),
            unit_periods=et.get("unit"), standing_periods=et.get("standing"), billing_window=bwin)
        if out["electricity"] is not None:
            out["electricity"]["_unit_periods"] = et.get("unit")
            out["electricity"]["_standing_periods"] = et.get("standing")
        if out["electricity"] is None and not er:
            out["errors"].append("No electricity readings (smart meter required, or none yet).")
    except urllib.error.HTTPError as e:
        out["electricity"] = None
        out["errors"].append(f"Electricity: HTTP {e.code} (check key/meter IDs).")
    except Exception as e:
        out["electricity"] = None
        out["errors"].append(f"Electricity: {type(e).__name__}")
    if cfg.get("gas_mprn") and cfg.get("gas_serial"):
        try:
            gr = _octopus_consumption("gas-meter-points",
                                      cfg["gas_mprn"], cfg["gas_serial"], key, hours=1080)
            # Unit interpretation is resolved centrally (see _resolve_gas_units).
            # The setting is user-declared; detection only warns, never overrides.
            gres = _resolve_gas_units(cfg, gr)
            gas_m3 = gres["is_m3"]
            gt = tariffs.get("gas", {})
            out["gas"] = _summarise_consumption(
                gr, float(cfg.get("gas_unit_p", 5.56)), float(cfg.get("gas_standing_p", 33.35)),
                is_gas=True, gas_is_m3=gas_m3,
                unit_periods=gt.get("unit"), standing_periods=gt.get("standing"), billing_window=bwin)
            if out["gas"] is not None:
                out["gas"]["_unit_periods"] = gt.get("unit")
                out["gas"]["_standing_periods"] = gt.get("standing")
                out["gas"]["unit_is_m3"] = gas_m3
                out["gas"]["unit_declared"] = gres["declared"]
                out["gas"]["unit_confirmed"] = gres["confirmed"]
                out["gas"]["unit_autodetected"] = (gres["declared"] == "auto")
                if gres["warning"]:
                    out["gas"]["unit_warning"] = gres["warning"]
                    out.setdefault("warnings", []).append("Gas: " + gres["warning"])
            if out["gas"] is None and not gr:
                out["errors"].append("No gas readings yet.")
        except urllib.error.HTTPError as e:
            out["gas"] = None
            out["errors"].append(f"Gas: HTTP {e.code}.")
        except Exception as e:
            out["gas"] = None
            out["errors"].append(f"Gas: {type(e).__name__}")
    e = out.get("electricity"); g = out.get("gas")
    if e or g:
        total_cost = (e["total_cost_p"] if e else 0) + (g["total_cost_p"] if g else 0)
        nd = max((e or {}).get("n_days", 1), (g or {}).get("n_days", 1))

        def _full_days(s):
            """(date -> cost_p) for COMPLETE days only (>=46 of 48 half-hours).
            Partial first/last days of the fetch window are excluded so day and
            week comparisons aren't distorted by incomplete data. Costs each day
            at the rate in force THAT day when matched rate history is available
            (s['_unit_periods']/'_standing_periods'), else the flat typed rate."""
            up = s.get("_unit_periods"); sp = s.get("_standing_periods")
            matched = bool(up) and bool(sp)
            per = {}
            for d in s.get("daily", []):
                if d.get("slots", 0) < 46:
                    continue
                if matched:
                    u = _rate_at(up, d["date"] + "T12:00:00Z")
                    st = _rate_at(sp, d["date"] + "T12:00:00Z")
                    if u is None or st is None:
                        continue   # no known rate that day — omit rather than misprice
                    per[d["date"]] = d["kwh"] * u + st
                else:
                    per[d["date"]] = d["kwh"] * s["unit_rate_p"] + s["standing_p"]
            return per

        def _fuel_periods(s):
            """Per-fuel day / 7-day / month cost blocks (full periods only)."""
            if not s:
                return None
            per = _full_days(s)
            dates = sorted(per)
            blk = {}
            # last full day = second to last date (last usually partial)
            if len(dates) >= 2:
                d = dates[-2]
                prev = dates[-3] if len(dates) >= 3 else None
                blk["day"] = {"cost_p": round(per[d]), "date": d,
                              "delta_p": round(per[d] - per[prev]) if prev else None}
            # last full 7 days
            if len(dates) >= 8:
                r7 = dates[-8:-1]
                p7 = dates[-15:-8] if len(dates) >= 15 else None
                r = sum(per[x] for x in r7)
                blk["week"] = {"cost_p": round(r),
                               "delta_p": round(r - sum(per[x] for x in p7)) if p7 else None}
            # last full calendar month — from the history cache (raw API units)
            mtot = _month_kwh_from_cache(s.get("_kind"))
            if mtot:
                import calendar as _cal
                yy, mm = mtot["ym"]
                ndm = _cal.monthrange(yy, mm)[1]
                # gas cache is raw; convert m3->kWh if this fuel is in m3 mode
                mconv = 1.0
                if s.get("_kind") == "gas":
                    cfg2 = _load_octopus_cfg() or {}
                    mconv = _resolve_gas_units(cfg2)["conv"]
                kwh = mtot["kwh"] * mconv
                prev_kwh = (mtot["prev_kwh"] * mconv) if mtot.get("prev_kwh") is not None else None
                up = s.get("_unit_periods"); sp = s.get("_standing_periods")
                matched = bool(up) and bool(sp)

                def _month_cost(y2, m2, fallback_kwh):
                    """Matched cost for a month: each reading at its own-time rate
                    + per-day standing at that day's rate. Falls back to flat
                    (fallback_kwh * unit + standing*ndm) if no rate history or no
                    per-reading cache."""
                    days_in = _cal.monthrange(y2, m2)[1]
                    if matched:
                        rd = _month_readings_from_cache(s.get("_kind"), y2, m2)
                        if rd:
                            energy = 0.0
                            for iso, v in rd.items():
                                r = _rate_at(up, iso)
                                if r is not None and v is not None:
                                    energy += (v * mconv) * r
                            # standing: each day of the month at that day's rate
                            stot = 0.0
                            for dnum in range(1, days_in + 1):
                                dr = _rate_at(sp, f"{y2:04d}-{m2:02d}-{dnum:02d}T12:00:00Z")
                                if dr is not None:
                                    stot += dr
                            return energy + stot
                    # flat fallback
                    return (fallback_kwh or 0) * s["unit_rate_p"] + s["standing_p"] * days_in

                cost = _month_cost(yy, mm, kwh)
                py, pm = (yy, mm - 1) if mm > 1 else (yy - 1, 12)
                prev_cost = _month_cost(py, pm, prev_kwh) if prev_kwh is not None else None
                blk["month"] = {"cost_p": round(cost), "kwh": round(kwh, 1),
                                "label": datetime(yy, mm, 1).strftime("%b %Y"),
                                "delta_p": (round(cost - prev_cost)
                                            if prev_cost is not None else None)}
            return blk or None

        # tag each summary with its meter kind so the cache lookup knows which file
        if e:
            e["_kind"] = "elec"
        if g:
            g["_kind"] = "gas"
        elec_p = _fuel_periods(e)
        gas_p = _fuel_periods(g)
        # combined last full month = elec month + gas month
        comb_month = None
        em = (elec_p or {}).get("month"); gm = (gas_p or {}).get("month")
        if em or gm:
            comb_month = {"cost_p": (em["cost_p"] if em else 0) + (gm["cost_p"] if gm else 0),
                          "label": (em or gm)["label"]}
        out["combined"] = {
            "total_cost_gbp": round(total_cost / 100, 2),
            "n_days": nd,
            "electricity": elec_p,
            "gas": gas_p,
            "month_total": comb_month,
        }
    c["data"] = out; c["ts"] = time.time()
    return out


def _month_readings_from_cache(kind, yy, mm):
    """The month's raw {iso_start: kwh} readings from the on-disk history cache
    (same files _month_kwh_from_cache sums). Returns the dict or None. Lets the
    month card be matched-costed per-reading rather than rate*total."""
    if not kind:
        return None
    f = OCTOPUS_HIST_DIR / f"{kind}_{yy:04d}-{mm:02d}.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except Exception:
        return None


def _month_kwh_from_cache(kind):
    """Last full calendar month's total kWh (and the month before) for `kind`
    ('elec'|'gas'). Uses the carpet history cache; if a needed month isn't
    cached yet, fetches just those months so the card works before the
    colorgramme has ever been opened. Returns {ym:(y,m), kwh, prev_kwh}|None."""
    if not kind:
        return None
    try:
        now = datetime.now(timezone.utc)
        y, m = now.year, now.month - 1
        if m == 0:
            m = 12; y -= 1
        py, pm = (y, m - 1) if m > 1 else (y - 1, 12)
        cfg = _load_octopus_cfg() or {}
        key = cfg.get("api_key")
        if kind == "gas":
            point, serial, mkind = cfg.get("gas_mprn"), cfg.get("gas_serial"), "gas-meter-points"
        else:
            point, serial, mkind = cfg.get("elec_mpan"), cfg.get("elec_serial"), "electricity-meter-points"

        def _sum(yy, mm):
            f = OCTOPUS_HIST_DIR / f"{kind}_{yy:04d}-{mm:02d}.json"
            if not f.exists():
                # fetch just this month (populates the shared cache)
                if key and point and serial:
                    try:
                        _octopus_history(mkind, point, serial, key, months=1) if (yy, mm) == (now.year, now.month) else None
                    except Exception:
                        pass
                if not f.exists():
                    # targeted single-month fetch
                    _fetch_single_month(mkind, point, serial, key, kind, yy, mm)
            if not f.exists():
                return None
            try:
                return sum(json.loads(f.read_text()).values())
            except Exception:
                return None
        cur = _sum(y, m)
        if cur is None:
            return None
        return {"ym": (y, m), "kwh": cur, "prev_kwh": _sum(py, pm)}
    except Exception:
        return None


def _fetch_single_month(kind, point, serial, api_key, tag, yy, mm):
    """Fetch one calendar month of consumption and write it to the history
    cache file, so month-cost cards work without opening the colorgramme."""
    if not (kind and point and serial and api_key):
        return
    try:
        OCTOPUS_HIST_DIR.mkdir(exist_ok=True)
        start = datetime(yy, mm, 1, tzinfo=timezone.utc)
        nm = datetime(yy + (mm // 12), (mm % 12) + 1, 1, tzinfo=timezone.utc)
        params = {"period_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "period_to": nm.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "page_size": 25000, "order_by": "period"}
        got = {}
        page = _octopus_fetch(f"/{kind}/{point}/meters/{serial}/consumption/", api_key, params)
        while True:
            for r in page.get("results", []):
                if r.get("consumption") is not None:
                    got[r["interval_start"]] = r["consumption"]
            nxt = page.get("next")
            if not nxt:
                break
            req = urllib.request.Request(nxt, headers={"User-Agent": "uk-grid-monitor"})
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{api_key}:".encode()).decode())
            with urllib.request.urlopen(req, timeout=40) as rr:
                page = json.load(rr)
        if got:
            (OCTOPUS_HIST_DIR / f"{tag}_{yy:04d}-{mm:02d}.json").write_text(json.dumps(got))
    except Exception:
        pass


# ---- Octopus historical carpet plots + FFT --------------------------------
# Long-range half-hourly history for colorgramme (carpet) plots: 48 half-hour
# rows x days columns per month, one cell = one half-hour's consumption. Also
# FFT analysis: a periodicity spectrum (which cycles dominate usage) and a
# spectral carpet (how the daily-usage shape evolves). History is immutable, so
# it's cached hard to disk per (fuel, month); only the current month re-fetches.
OCTOPUS_HIST_DIR = Path(__file__).with_name("octopus_history")
_HH_PER_DAY = 48


def _np():
    """numpy if available, else None (pure-Python FFT fallback keeps the server
    stdlib-only when numpy isn't installed)."""
    try:
        import numpy as _n
        return _n
    except Exception:
        return None


def _octopus_history(kind, point, serial, api_key, months=25):
    """Up to `months` of half-hourly consumption, paginated. Returns
    (data, errors) where data is {iso_start: consumption} and errors is a list
    of month keys that failed to fetch. Cached to disk per month; only the
    current month is re-fetched."""
    OCTOPUS_HIST_DIR.mkdir(exist_ok=True)
    now = datetime.now(timezone.utc)
    tag = "elec" if kind.startswith("electricity") else "gas"
    wanted = []
    y, m = now.year, now.month
    for _ in range(months):
        wanted.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
    wanted.reverse()
    cur_month = f"{now.year:04d}-{now.month:02d}"
    merged = {}
    errors = []
    for mk in wanted:
        f = OCTOPUS_HIST_DIR / f"{tag}_{mk}.json"
        if f.exists() and mk != cur_month:
            try:
                merged.update(json.loads(f.read_text()))
                continue
            except Exception:
                pass
        yy, mm = int(mk[:4]), int(mk[5:7])
        start = datetime(yy, mm, 1, tzinfo=timezone.utc)
        nm = datetime(yy + (mm // 12), (mm % 12) + 1, 1, tzinfo=timezone.utc)
        params = {"period_from": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "period_to": min(nm, now).strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "page_size": 25000, "order_by": "period"}
        got = {}
        url = f"/{kind}/{point}/meters/{serial}/consumption/"
        fetch_ok = False
        try:
            page = _octopus_fetch(url, api_key, params)
            fetch_ok = True                # HTTP succeeded (even if 0 rows)
            while True:
                for r in page.get("results", []):
                    if r.get("consumption") is not None:
                        got[r["interval_start"]] = r["consumption"]
                nxt = page.get("next")
                if not nxt:
                    break
                req = urllib.request.Request(nxt, headers={"User-Agent": "uk-grid-monitor"})
                req.add_header("Authorization", "Basic " +
                               base64.b64encode(f"{api_key}:".encode()).decode())
                with urllib.request.urlopen(req, timeout=40) as rr:
                    page = json.load(rr)
        except urllib.error.HTTPError as ex:
            # 404 = no such data for this serial/period (common before enrolment
            # or after a meter swap) — NOT a transient failure.
            if ex.code != 404:
                errors.append(mk)
        except Exception:
            errors.append(mk)              # transient/network — worth retrying
        if got:
            try:
                f.write_text(json.dumps(got))
            except Exception:
                pass
            merged.update(got)
    # trim spurious "errors" that are simply months before the account started:
    # once we know the earliest month with data, anything before it isn't a hole.
    if merged:
        earliest = min(iso[:7] for iso in merged)
        errors = [e for e in errors if e >= earliest]
    return merged, errors


def _carpet_for_month(hist, mk, conv=1.0):
    """48 x N-days grid for month mk ('YYYY-MM'). Cell = kWh; missing = None."""
    import calendar
    yy, mm = int(mk[:4]), int(mk[5:7])
    ndays = calendar.monthrange(yy, mm)[1]
    grid = [[None] * ndays for _ in range(_HH_PER_DAY)]
    total = 0.0; mx = 0.0
    for iso, val in hist.items():
        if not iso.startswith(mk):
            continue
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            continue
        row = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
        col = dt.day - 1
        if 0 <= row < _HH_PER_DAY and 0 <= col < ndays:
            v = val * conv
            grid[row][col] = round(v, 4)
            total += v; mx = max(mx, v)
    return {"grid": grid, "days": ndays, "max": round(mx, 4), "total": round(total, 2)}


def _stack_carpets(carpets):
    """Cell-by-cell mean across month grids (option-a 'stacking') -> 48x31."""
    cols = 31
    acc = [[0.0] * cols for _ in range(_HH_PER_DAY)]
    cnt = [[0] * cols for _ in range(_HH_PER_DAY)]
    for c in carpets:
        g = c["grid"]; nd = len(g[0]) if g else 0
        for r in range(_HH_PER_DAY):
            for col in range(min(nd, cols)):
                v = g[r][col]
                if v is not None:
                    acc[r][col] += v; cnt[r][col] += 1
    grid = [[(round(acc[r][col] / cnt[r][col], 4) if cnt[r][col] else None)
             for col in range(cols)] for r in range(_HH_PER_DAY)]
    mx = max((v for row in grid for v in row if v is not None), default=0.0)
    return {"grid": grid, "days": cols, "max": round(mx, 4), "stacked": len(carpets)}


def _flatten_series(hist, mks, interpolate=True):
    """Even 1-D series over months `mks`. If interpolate, small interior gaps are
    linearly filled; else missing slots become 0. Returns (series, gap_frac).

    Buckets by PARSED timestamp components (not reconstructed '...Z' keys), so
    BST (+01:00) readings are matched as well as GMT ones."""
    import calendar
    mkset = set(mks)
    # index each month's day/half-hour into a flat slot position
    order = sorted(mks)
    offset = {}
    total = 0
    for mk in order:
        yy, mm = int(mk[:4]), int(mk[5:7])
        offset[(yy, mm)] = total
        total += calendar.monthrange(yy, mm)[1] * _HH_PER_DAY
    vals = [None] * total
    for iso, v in hist.items():
        if iso[:7] not in mkset:
            continue
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            continue
        base = offset.get((dt.year, dt.month))
        if base is None:
            continue
        pos = base + (dt.day - 1) * _HH_PER_DAY + dt.hour * 2 + (1 if dt.minute >= 30 else 0)
        if 0 <= pos < total:
            vals[pos] = v
    missing = sum(1 for v in vals if v is None)
    gap_frac = missing / len(vals) if vals else 1.0
    if not interpolate:
        return [(v if v is not None else 0.0) for v in vals], gap_frac
    filled = list(vals); n = len(filled); i = 0
    while i < n:
        if filled[i] is None:
            j = i
            while j < n and filled[j] is None:
                j += 1
            lo = filled[i - 1] if i > 0 else None
            hi = filled[j] if j < n else None
            if lo is not None and hi is not None:
                for k in range(i, j):
                    filled[k] = lo + (hi - lo) * ((k - i + 1) / (j - i + 1))
            else:
                fillv = lo if lo is not None else (hi if hi is not None else 0.0)
                for k in range(i, j):
                    filled[k] = fillv
            i = j
        else:
            i += 1
    return filled, gap_frac


def _fft_power(series):
    """Real FFT power spectrum, DC removed. numpy if present else pure-Python
    DFT. Returns [(period_hours, normalised_power)] ascending by period."""
    import math
    n = len(series)
    if n < 8:
        return []
    mean = sum(series) / n
    x = [v - mean for v in series]
    np = _np()
    if np is not None:
        sp = np.fft.rfft(np.asarray(x, dtype=float))
        power = (np.abs(sp) ** 2).tolist()
        freqs = np.fft.rfftfreq(n, d=0.5).tolist()
    else:
        half = n // 2
        power = [0.0] * (half + 1)
        freqs = [k / (n * 0.5) for k in range(half + 1)]
        for k in range(1, half + 1):
            re = im = 0.0; w = -2 * math.pi * k / n
            for t, v in enumerate(x):
                re += v * math.cos(w * t); im += v * math.sin(w * t)
            power[k] = re * re + im * im
    out = []
    for k in range(1, len(power)):
        if freqs[k] > 0:
            out.append((round(1.0 / freqs[k], 2), power[k]))
    mx = max((p for _, p in out), default=1.0) or 1.0
    return [(ph, round(p / mx, 4)) for ph, p in out]


def _profiles(hist, mks, conv=1.0):
    """Three companion analyses over the selected months, all gap-robust
    (averaging present values only; no interpolation needed):
      - average-day: mean kWh per half-hour-of-day + 10th/90th percentile band
      - load-duration: all half-hours sorted descending (x = % of time)
      - day-of-week: mean daily kWh by weekday (Mon..Sun)
    """
    import calendar
    # collect values keyed by half-hour slot, and per-day totals by weekday
    slot_vals = [[] for _ in range(_HH_PER_DAY)]
    all_vals = []
    day_tot = {}       # date -> kWh total (full days only for weekday means)
    day_slots = {}
    for mk in mks:
        yy, mm = int(mk[:4]), int(mk[5:7])
        nd = calendar.monthrange(yy, mm)[1]
        for iso, val in hist.items():
            if not iso.startswith(mk):
                continue
            try:
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            except Exception:
                continue
            v = val * conv
            row = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
            if 0 <= row < _HH_PER_DAY:
                slot_vals[row].append(v)
            all_vals.append(v)
            dkey = iso[:10]
            day_tot[dkey] = day_tot.get(dkey, 0.0) + v
            day_slots[dkey] = day_slots.get(dkey, 0) + 1

    def _pct(sorted_list, p):
        if not sorted_list:
            return None
        i = min(len(sorted_list) - 1, max(0, int(round(p * (len(sorted_list) - 1)))))
        return sorted_list[i]

    # average-day profile
    avg_day = []
    for row in range(_HH_PER_DAY):
        vals = sorted(slot_vals[row])
        if vals:
            mean = sum(vals) / len(vals)
            avg_day.append({"hh": row, "mean": round(mean, 4),
                            "lo": round(_pct(vals, 0.10), 4),
                            "hi": round(_pct(vals, 0.90), 4)})
        else:
            avg_day.append({"hh": row, "mean": None, "lo": None, "hi": None})

    # load-duration curve: half-hours sorted descending, x = fraction of time
    sv = sorted(all_vals, reverse=True)
    n = len(sv)
    ld = []
    if n:
        step = max(1, n // 200)     # cap ~200 points
        for i in range(0, n, step):
            ld.append({"pct": round(100 * i / n, 2), "kwh": round(sv[i], 4)})
        ld.append({"pct": 100.0, "kwh": round(sv[-1], 4)})

    # day-of-week means (full days only, >=46 slots), kWh per day
    dow_sum = [0.0] * 7; dow_cnt = [0] * 7
    for dkey, tot in day_tot.items():
        if day_slots.get(dkey, 0) < 46:
            continue
        try:
            wd = datetime.fromisoformat(dkey + "T00:00:00+00:00").weekday()
        except Exception:
            continue
        dow_sum[wd] += tot; dow_cnt[wd] += 1
    dow = [{"day": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][i],
            "mean_kwh": (round(dow_sum[i] / dow_cnt[i], 2) if dow_cnt[i] else None),
            "n": dow_cnt[i]} for i in range(7)]

    return {"average_day": avg_day, "load_duration": ld, "day_of_week": dow}


def _rolling_carpet(hist, conv=1.0, months=24):
    """Concatenate the trailing `months` calendar months into one continuous
    48-row carpet (columns = every day, end to end). Returns the grid, the
    global max, and month-boundary metadata for X-axis labelling.

    NB: we PARSE each stored timestamp rather than reconstructing lookup keys,
    because Octopus returns interval_start with a timezone offset (+01:00 in
    BST, +00:00 in GMT), so a reconstructed '...Z' key only matches in winter."""
    import calendar
    now = datetime.now(timezone.utc)
    # ordered trailing window of (year, month), plus a column index for each day
    mks = []
    y, m = now.year, now.month
    for _ in range(months):
        mks.append((y, m)); m -= 1
        if m == 0:
            m = 12; y -= 1
    mks.reverse()
    # map (year, month) -> starting column, and record boundaries
    col_of_month = {}
    boundaries = []
    col = 0
    for (yy, mm) in mks:
        col_of_month[(yy, mm)] = col
        boundaries.append({"label": datetime(yy, mm, 1).strftime("%b"),
                           "year": yy, "month": mm, "col": col})
        col += calendar.monthrange(yy, mm)[1]
    total_cols = col
    grid = [[None] * total_cols for _ in range(_HH_PER_DAY)]
    mx = 0.0
    first_ym = mks[0]
    for iso, val in hist.items():
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        except Exception:
            continue
        # bucket by the wall-clock components of the returned timestamp, exactly
        # as _carpet_for_month does (consistent single-month vs rolling view)
        key = (dt.year, dt.month)
        base = col_of_month.get(key)
        if base is None:
            continue
        c = base + (dt.day - 1)
        r = dt.hour * 2 + (1 if dt.minute >= 30 else 0)
        if 0 <= r < _HH_PER_DAY and 0 <= c < total_cols:
            v = val * conv
            grid[r][c] = round(v, 4)
            if v > mx:
                mx = v
    return {"grid": grid, "days": total_cols, "max": round(mx, 4),
            "boundaries": boundaries,
            "start": mks[0], "end": mks[-1]}


def _rolling_coverage(hist, months=24):
    """Per-month data coverage (% of half-hours present) across the trailing
    window, so months that fetched poorly or predate the account are visible.
    Returns [{month, label, pct, present, expected}]."""
    import calendar
    now = datetime.now(timezone.utc)
    mks = []
    y, m = now.year, now.month
    for _ in range(months):
        mks.append((y, m)); m -= 1
        if m == 0:
            m = 12; y -= 1
    mks.reverse()
    present_by = {}
    for iso in hist:
        present_by[iso[:7]] = present_by.get(iso[:7], 0) + 1
    out = []
    for (yy, mm) in mks:
        mk = f"{yy:04d}-{mm:02d}"
        nd = calendar.monthrange(yy, mm)[1]
        # expected slots: full month, or up to 'now' for the current month
        if (yy, mm) == (now.year, now.month):
            expected = ((now - datetime(yy, mm, 1, tzinfo=timezone.utc)).total_seconds()
                        / 1800)
        else:
            expected = nd * _HH_PER_DAY
        expected = max(1, int(expected))
        got = present_by.get(mk, 0)
        out.append({"month": mk,
                    "label": datetime(yy, mm, 1).strftime("%b %y"),
                    "pct": round(100 * got / expected),
                    "present": got, "expected": expected})
    return out


def _spectral_carpet(hist, mks, fill_gaps=True):
    """FFT each day (48 slots) -> 24 cycles/day bins; stack days as columns.
    Rows = cycles/day (1..24), colour = normalised power. If fill_gaps is False,
    days with ANY missing half-hour are skipped entirely (stricter, no
    interpolation artefacts); if True, small gaps are filled with the day mean."""
    import calendar, math
    np = _np()
    daycols = []
    for mk in mks:
        yy, mm = int(mk[:4]), int(mk[5:7])
        nd = calendar.monthrange(yy, mm)[1]
        for day in range(1, nd + 1):
            day_vals = []
            for hh in range(_HH_PER_DAY):
                dt = datetime(yy, mm, day, hh // 2, 30 * (hh % 2), tzinfo=timezone.utc)
                day_vals.append(hist.get(dt.strftime("%Y-%m-%dT%H:%M:%SZ")))
            present = sum(1 for v in day_vals if v is not None)
            if fill_gaps:
                if present < _HH_PER_DAY * 0.75:
                    continue                     # too incomplete even to fill
                dm = sum(v for v in day_vals if v is not None) / max(1, present)
                dv = [(v if v is not None else dm) - dm for v in day_vals]
            else:
                if present < _HH_PER_DAY:
                    continue                     # strict: any gap -> skip the day
                dm = sum(day_vals) / _HH_PER_DAY
                dv = [v - dm for v in day_vals]
            if np is not None:
                p = (np.abs(np.fft.rfft(np.asarray(dv))) ** 2)[1:25].tolist()
            else:
                p = []
                for k in range(1, 25):
                    re = im = 0.0; w = -2 * math.pi * k / _HH_PER_DAY
                    for t, val in enumerate(dv):
                        re += val * math.cos(w * t); im += val * math.sin(w * t)
                    p.append(re * re + im * im)
            daycols.append(p)
    if not daycols:
        return None
    ncols = len(daycols)
    grid = [[daycols[c][r] for c in range(ncols)] for r in range(24)]
    mx = max((v for row in grid for v in row), default=1.0) or 1.0
    grid = [[round(v / mx, 4) for v in row] for row in grid]
    return {"grid": grid, "days": ncols, "rows": 24}


def get_octopus_carpet(months_sel=None, fuel="electricity", interpolate=True, sc_fill=True):
    """Build carpet + FFT + profiles payload for the requested months.
    interpolate: fill gaps before the periodicity FFT (else zero-fill).
    sc_fill: fill small gaps in the spectral carpet (else skip any gappy day)."""
    if not _octopus_has_config():
        return {"needs_config": True}
    cfg = _load_octopus_cfg()
    key = cfg["api_key"]
    if fuel == "gas":
        if not (cfg.get("gas_mprn") and cfg.get("gas_serial")):
            return {"error": "gas not configured"}
        kind, point, serial = "gas-meter-points", cfg["gas_mprn"], cfg["gas_serial"]
        conv = _resolve_gas_units(cfg)["conv"]
    else:
        kind, point, serial = "electricity-meter-points", cfg["elec_mpan"], cfg["elec_serial"]
        conv = 1.0
    hist, hist_errors = _octopus_history(kind, point, serial, key, months=25)
    avail = sorted({iso[:7] for iso in hist})
    if not months_sel:
        months_sel = avail[-1:] if avail else []
    months_sel = [m for m in months_sel if m in avail]
    conv_hist = {k: v * conv for k, v in hist.items()}
    carpets = {mk: _carpet_for_month(hist, mk, conv) for mk in months_sel}
    stacked = _stack_carpets([carpets[m] for m in months_sel]) if len(months_sel) > 1 else None
    series, gap = _flatten_series(conv_hist, months_sel, interpolate) if months_sel else ([], 1.0)
    spectrum = _fft_power(series) if gap < 0.25 and series else []
    rolling = _rolling_carpet(hist, conv, 24)
    profiles = _profiles(hist, months_sel, conv) if months_sel else None
    # per-month coverage across the rolling window, so under-populated months
    # are visible rather than silently blank
    coverage = _rolling_coverage(hist, 24)
    return {
        "needs_config": False, "fuel": fuel,
        "available_months": avail,
        "selected_months": months_sel,
        "carpets": carpets,
        "stacked": stacked,
        "spectrum": spectrum,
        "rolling": rolling,
        "profiles": profiles,
        "coverage": coverage,
        "fetch_errors": hist_errors,
        "gap_frac": round(gap, 3),
        "too_gappy": gap >= 0.25,
        "interpolate": interpolate,
        "sc_fill": sc_fill,
        "numpy": _np() is not None,
    }


# ---- National Gas: NTS supply, demand, entry-point flows, linepack ----------
# Public REST, no key. One call to instantaneousflow/sites returns the whole
# gas-system picture: supply by entry point + aggregated terminal, total
# supply, demand by category, interconnector exports, total demand, and NTS
# linepack. Captured every 2 min, published every 12 min. Flows are in mcm/day
# (million cubic metres per day); linepack is a stock in mcm.
#
# Honesty notes:
#   * We surface the feed's own publishedTime so staleness is visible — the
#     12-min publish cadence means a value can be up to ~12 min old by design.
#   * Units are labelled mcm/d (flows) and mcm (linepack) throughout; we don't
#     silently convert to energy (GWh) since the feed is volumetric.
#   * Supply/demand rarely balance instantaneously — the difference is linepack
#     change (gas being packed into or drawn from the pipes), which we state
#     rather than hide.
GAS_BASE = "https://api.nationalgas.com/operationaldata/v1"
_gas_cache = {"data": None, "ts": 0}
GAS_TTL = 300        # 5 min; feed publishes every 12 min

# National Gas Published Data API: real hourly-actual linepack history + the
# operator's own Forecast Minimum Linepack (the closest thing to an official
# "floor" — there is no published fixed critical level). Verified publication
# IDs from the live catalogue (publications/catalogue). Hourly data, so a 30-min
# cache is ample. Falls back silently to the locally-logged history if the
# Published Data endpoint is unavailable.
GAS_LP_ACTUAL_ID = "PUBOBJ486"       # Linepack, Hourly Actual, Aggregate, D+1
GAS_LP_MINFC_ID = "PUBOBJ111271"     # Forecast Minimum Linepack
_gas_lp_cache = {"data": None, "ts": 0}
GAS_LP_TTL = 1800    # 30 min


def get_gas_linepack_history(hours=48):
    """Real hourly-actual NTS linepack for the last `hours`, plus the latest
    Forecast Minimum Linepack, from the National Gas Published Data API.
    Returns {"actual": [{t,v}], "min_forecast": float|None, "source": str} or
    None on failure (caller falls back to the local log)."""
    c = _gas_lp_cache
    if c["data"] and time.time() - c["ts"] < GAS_LP_TTL:
        return c["data"]
    try:
        today = datetime.now(timezone.utc).date()
        frm = (today - timedelta(days=3)).isoformat()
        to = today.isoformat()
        d = post_json(f"{GAS_BASE}/publications/gasday", {
            "fromDate": frm, "toDate": to,
            "publicationIds": [GAS_LP_ACTUAL_ID, GAS_LP_MINFC_ID],
            "latestValue": "N"}, timeout=30)
        actual, minfc = [], None
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        for pub in d:
            pid = pub.get("publicationId")
            rows = pub.get("publications") or []
            if pid == GAS_LP_ACTUAL_ID:
                for r in rows:
                    try:
                        t = datetime.fromisoformat(r["applicableAt"])
                        v = float(r["value"])
                    except Exception:
                        continue
                    if t >= cutoff:
                        actual.append({"t": r["applicableAt"], "v": round(v, 1)})
            elif pid == GAS_LP_MINFC_ID:
                # take the most recent forecast-minimum value
                best = None
                for r in rows:
                    try:
                        t = datetime.fromisoformat(r["applicableAt"])
                        v = float(r["value"])
                    except Exception:
                        continue
                    if best is None or t > best[0]:
                        best = (t, v)
                if best:
                    minfc = round(best[1], 1)
        actual.sort(key=lambda p: p["t"])
        if not actual:
            return None
        out = {"actual": actual, "min_forecast": minfc,
               "source": "National Gas Published Data (hourly actual)"}
        c["data"] = out
        c["ts"] = time.time()
        return out
    except Exception:
        return None


# Wholesale gas price history from the same National Gas Published Data API.
# SAP (System Average Price) + SMP buy/sell — the on-day cash-out prices for
# imbalance, and the closest published wholesale reference the portal exposes.
#   PUBOB47  System Average Price (SAP)
#   PUBOB48  System Marginal Price - buy  (SMP buy)
#   PUBOB49  System Marginal Price - sell (SMP sell)
# Honesty note: the API's row carries no explicit unit. National Gas publishes
# these in pence/kWh (values sit around ~5), so we treat `value` as p/kWh and
# derive £/MWh (×10) and p/therm (×29.3071) from that. If a National Gas doc
# ever states a different unit, those two factors are the single place to fix.
GAS_PRICE_SAP_ID = "PUBOB47"
GAS_PRICE_SMP_BUY_ID = "PUBOB48"
GAS_PRICE_SMP_SELL_ID = "PUBOB49"
_gas_price_cache = {"data": None, "ts": 0}
GAS_PRICE_TTL = 1800   # 30 min


def get_gas_price_history(hours=48):
    """Recent SAP / SMP-buy / SMP-sell wholesale gas prices (p/kWh) for the last
    `hours`, from the National Gas Published Data API. Returns
    {"points":[{t,sap,smp_buy,smp_sell}], "latest":{...}, "source":str} or None
    on failure (caller degrades gracefully — the plot is simply hidden)."""
    c = _gas_price_cache
    if c["data"] and time.time() - c["ts"] < GAS_PRICE_TTL:
        return c["data"]
    try:
        today = datetime.now(timezone.utc).date()
        frm = (today - timedelta(days=3)).isoformat()
        to = today.isoformat()
        d = post_json(f"{GAS_BASE}/publications/gasday", {
            "fromDate": frm, "toDate": to,
            "publicationIds": [GAS_PRICE_SAP_ID, GAS_PRICE_SMP_BUY_ID,
                               GAS_PRICE_SMP_SELL_ID],
            "latestValue": "N"}, timeout=30)
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        # merge the three series onto a common timeline keyed by timestamp
        merged = {}
        field = {GAS_PRICE_SAP_ID: "sap", GAS_PRICE_SMP_BUY_ID: "smp_buy",
                 GAS_PRICE_SMP_SELL_ID: "smp_sell"}
        for pub in d:
            key = field.get(pub.get("publicationId"))
            if not key:
                continue
            for r in pub.get("publications") or []:
                ts = r.get("applicableAt")
                try:
                    t = datetime.fromisoformat(ts)
                    v = float(r["value"])
                except Exception:
                    continue
                if t.astimezone(timezone.utc) < cutoff:
                    continue
                merged.setdefault(ts, {"t": ts})[key] = round(v, 4)
        points = [merged[k] for k in sorted(merged)]
        if not points:
            return None
        latest = points[-1]
        out = {"points": points, "latest": latest,
               "unit": "p/kWh (inferred — API carries no unit label)",
               "source": "National Gas Published Data (SAP / SMP)"}
        c["data"] = out
        c["ts"] = time.time()
        return out
    except Exception:
        return None

# Friendly labels + grouping for entry-point supply sites. Interconnector/LNG
# vs domestic-terminal classification drives colouring, like the electricity
# fuel map. Anything unlisted falls back to a piped-terminal default.
GAS_SITE_KIND = {
    "BACTON BBL": ("interconnector", "IC Netherlands (BBL)"),
    "BACTON IC": ("interconnector", "IC Belgium (IUK)"),
    "EASINGTON LANGELED": ("norway", "Langeled (Norway)"),
    "MILFORD HAVEN - DRAGON": ("lng", "Dragon LNG"),
    "MILFORD HAVEN - SOUTH HOOK": ("lng", "South Hook LNG"),
    "GRAIN NTS 1": ("lng", "Isle of Grain LNG 1"),
    "GRAIN NTS 2": ("lng", "Isle of Grain LNG 2"),
    "TEESSIDE CATS": ("terminal", "Teesside CATS"),
    "TEESSIDE PX": ("terminal", "Teesside px"),
    "ALDBROUGH": ("storage", "Aldbrough storage"),
    "HORNSEA": ("storage", "Hornsea storage"),
    "HOLFORD": ("storage", "Holford storage"),
    "HILLTOP": ("storage", "Hilltop storage"),
    "HOLE HOUSE FARM": ("storage", "Hole House Farm storage"),
    "STUBLACH": ("storage", "Stublach storage"),
    "EASINGTON ROUGH ST": ("storage", "Rough storage"),
}
GAS_KIND_COLOUR = {
    "norway": "#5b8def", "interconnector": "#7b6cf0", "lng": "#3fb6c9",
    "terminal": "#f2683c", "storage": "#f5b942", "other": "#9aa7b8",
}


def _gas_num(v):
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _gas_latest(site):
    """Latest flowRate + timestamp from a site's siteGasDetail list."""
    gd = site.get("siteGasDetail") or []
    if not gd:
        return None, None
    last = gd[-1]
    return _gas_num(last.get("flowRate")), last.get("applicableAt")


def get_gas():
    c = _gas_cache
    if c["data"] and not c["data"].get("error") and time.time() - c["ts"] < GAS_TTL:
        return c["data"]
    out = {"published": None, "gas_day": None, "unit": "mcm/d",
           "supply_total": None, "demand_total": None, "linepack": None,
           "supply_sources": [], "demand_categories": [], "exports": [],
           "error": None, "generated": datetime.now(timezone.utc).isoformat()}
    try:
        d = fetch_json(f"{GAS_BASE}/instantaneousflow/sites", timeout=30)
        out["published"] = d.get("publishedTime")
        out["gas_day"] = d.get("currentGasDay")
        groups = d.get("instantaneousFlow") or []
        for grp in groups:
            desc = (grp.get("description") or "").lower()
            sites = grp.get("sites") or []
            if "supply from entry" in desc:
                for s in sites:
                    val, at = _gas_latest(s)
                    if val is None:
                        continue
                    raw = s.get("siteName") or ""
                    kind, label = GAS_SITE_KIND.get(raw, ("terminal", raw.title()))
                    out["supply_sources"].append({
                        "raw": raw, "label": label, "kind": kind,
                        "colour": GAS_KIND_COLOUR.get(kind, GAS_KIND_COLOUR["other"]),
                        "mcm_d": val, "at": at})
            elif desc == "total supply" or "total supply" in desc:
                for s in sites:
                    val, at = _gas_latest(s)
                    out["supply_total"] = val
            elif "demand by category" in desc:
                for s in sites:
                    val, at = _gas_latest(s)
                    if val is None:
                        continue
                    lbl = (s.get("siteName") or "").replace(" Flow", "")
                    if lbl == "LDZ Offtake":
                        lbl = "LDZ Offtake (Domestic)"
                    out["demand_categories"].append({
                        "label": lbl, "mcm_d": val, "at": at})
            elif "demand from interconnector" in desc or "interconnector" in desc and "export" in desc:
                for s in sites:
                    val, at = _gas_latest(s)
                    if val is None:
                        continue
                    out["exports"].append({
                        "label": (s.get("siteName") or "").replace(" Export", ""),
                        "mcm_d": val, "at": at})
            elif desc == "total demand" or "total demand" in desc:
                for s in sites:
                    val, at = _gas_latest(s)
                    out["demand_total"] = val
            elif "linepack" in desc:
                for s in sites:
                    val, at = _gas_latest(s)
                    out["linepack"] = val
                    out["linepack_at"] = at
        # sort supply sources biggest-first
        out["supply_sources"].sort(key=lambda x: -(x["mcm_d"] or 0))
        out["demand_categories"].sort(key=lambda x: -(x["mcm_d"] or 0))
        out["exports"].sort(key=lambda x: -(x["mcm_d"] or 0))
        # supply/demand imbalance = linepack swing (packing vs drawing down)
        if out["supply_total"] is not None and out["demand_total"] is not None:
            out["imbalance"] = round(out["supply_total"] - out["demand_total"], 2)
        # log linepack + supply + demand locally for the balance trend + as a
        # resilient fallback
        local_hist = _log_gas_history(
            out["linepack"], out["supply_total"], out["demand_total"],
            out.get("linepack_at"))
        # prefer REAL hourly-actual linepack history from the Published Data API
        # (backfills the full 48h immediately, vs the local log which accrues
        # over time). Attach the operator's Forecast Minimum Linepack as a floor.
        api_lp = get_gas_linepack_history(48)
        if api_lp and api_lp.get("actual"):
            # Merge policy — LIVE-PRIMARY, archive as gap-fill:
            #  * Wherever we have a locally-logged LIVE sample, use it — that's
            #    the high-resolution, real measured linepack + supply-demand. Over
            #    a full day of running this is most of the window.
            #  * The official hourly-actual archive is used only to FILL STRETCHES
            #    with no live data (typically before local logging began, or gaps
            #    from downtime). This backfills the plot on a fresh start yet lets
            #    the accumulated high-res log take over as it fills in.
            #  * Each point is tagged bridge=True (live/high-res) or False
            #    (archive/hourly) so the frontend can still draw them distinctly
            #    and never present derived-from-archive data as live.
            actual = api_lp["actual"]

            # 1) start from all live-log points (high-res, measured)
            live_pts = []
            live_hours = set()
            for p in local_hist:
                if p.get("lp") is None:
                    continue
                bal = (round(p["s"] - p["d"], 1)
                       if (p.get("s") is not None and p.get("d") is not None) else None)
                live_pts.append({"t": p["t"], "lp": p["lp"], "bal": bal,
                                 "bridge": True})   # live measured, high-res
                live_hours.add(p["t"][:13])         # YYYY-MM-DDTHH bucket

            # 2) add archive hours ONLY where no live sample exists for that hour
            arch_pts = []
            for p in actual:
                if p["t"][:13] in live_hours:
                    continue   # we have live data for this hour — prefer it
                arch_pts.append({"t": p["t"], "lp": p["v"],
                                 "bal": None,        # archive carries no live balance
                                 "bridge": False})   # official hourly-actual

            hist = live_pts + arch_pts
            hist.sort(key=lambda r: r["t"])
            out["history"] = hist
            out["linepack_min_forecast"] = api_lp.get("min_forecast")
            src = api_lp.get("source")
            n_live = len(live_pts)
            n_arch = len(arch_pts)
            if n_live:
                src += f" + {n_live} live pt{'s' if n_live != 1 else ''}"
            out["history_source"] = src
            out["bridge_points"] = n_live
            out["archive_fill_points"] = n_arch
            out["archive_last_t"] = actual[-1]["t"] if actual else None
        else:
            # fallback: locally-logged history (fills over time). All live
            # measured, so every point is a bridge point by definition.
            out["history"] = [{"t": p["t"], "lp": p.get("lp"),
                               "bal": (round(p["s"] - p["d"], 1)
                                       if (p.get("s") is not None and p.get("d") is not None) else None),
                               "bridge": True}
                              for p in local_hist if p.get("lp") is not None]
            out["history_source"] = "local log (accumulating)"
            out["bridge_points"] = len(out["history"])
        # wholesale gas price history (SAP / SMP) — independent of linepack; the
        # plot is simply omitted client-side if this is unavailable.
        out["price_history"] = get_gas_price_history(48)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    c["data"] = out
    c["ts"] = time.time()
    return out


# ---- Geocoder (postcodes.io) for the EA location picker ---------------------
# Free, no key, UK-only, returns a `country` field so we can honestly flag
# non-England locations rather than guessing from a lat/lon box. Tries a full
# postcode first, then a partial-postcode autocomplete, then a place-name query.
GEOCODE_BASE = "https://api.postcodes.io"


def _haversine_km(lat1, lon1, lat2, lon2):
    """Great-circle distance in kilometres between two lat/lon points."""
    if None in (lat1, lon1, lat2, lon2):
        return None
    R = 6371.0  # earth radius, km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.asin(math.sqrt(a))


def _short_place(nm):
    """Trim a compound ward name to its first component so it fits a fixed
    chip: 'Bickleigh & Cornwood' -> 'Bickleigh', 'Cockington with Chelston'
    -> 'Cockington', 'Altarnun, Stoke Climsland' -> 'Altarnun'. Caps length
    as a final guard."""
    if not nm:
        return nm
    for sep in (" & ", " with ", ", ", "/"):
        i = nm.find(sep)
        if i > 0:
            nm = nm[:i]
            break
    nm = nm.strip()
    return nm if len(nm) <= 18 else nm[:17].rstrip() + "\u2026"


def reverse_geocode_bulk(points):
    """Reverse-geocode a list of (lat,lon) to place info in ONE call via
    postcodes.io's bulk endpoint. Returns a list aligned to input order; each
    entry is a dict {name, postcode} (or None where no match). Never raises.

    `name` prefers PARISH, which is finer than admin_ward: EA rainfall gauges
    have no real label of their own (they're all literally 'Rainfall station'),
    so their card name comes entirely from here — and wards are large enough
    that several gauges share one, producing confusing duplicate names. Parish
    separates most of them; the postcode is returned as a further tiebreaker for
    any that still collide (see _dedupe_place_names)."""
    if not points:
        return []
    out = [None] * len(points)
    try:
        body = json.dumps({"geolocations": [
            {"longitude": lon, "latitude": lat, "limit": 1, "radius": 2000}
            for (lat, lon) in points]}).encode()
        req = urllib.request.Request(
            f"{GEOCODE_BASE}/postcodes", data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "uk-grid-monitor/1.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=8).read())
        for i, item in enumerate(d.get("result") or []):
            res = (item.get("result") or [None])
            r = res[0] if res else None
            if r:
                name = (r.get("parish") or r.get("admin_ward")
                        or r.get("admin_district"))
                out[i] = {"name": name, "postcode": r.get("postcode")}
    except Exception:
        pass
    return out


def _postcode_area(pc):
    """Outward code + first inward digit, e.g. 'PL20 6RN' -> 'PL20 6'. A compact,
    geographically meaningful disambiguator for two gauges in the same parish."""
    if not pc:
        return None
    parts = pc.split()
    if len(parts) == 2 and parts[1]:
        return f"{parts[0]} {parts[1][0]}"
    return parts[0] if parts else None


def _dedupe_place_names(records):
    """Make the displayed gauge names distinguishable WITHOUT bloating the name
    itself. EA rainfall gauges have no real name, so several can reverse-geocode
    to the same place. Rather than stuff a suffix into the name (which crowds the
    card and forces mid-word ellipsis), we keep the clean place name and lean on
    the distance shown on the reading line to tell them apart.

      * unique place name  -> leave as-is; card shows bucketed '~N km'.
      * shared place name  -> try a short postcode area suffix IF it actually
        separates them (different districts). Where it doesn't (same parish AND
        same postcode district, as on open moorland), leave the name clean and
        set r['dist_exact']=True so the card shows the gauge's PRECISE distance
        on the reading line — which is always distinct for gauges that aren't
        essentially co-located, and needs no extra name text.
    """
    from collections import defaultdict

    def regroup():
        g = defaultdict(list)
        for r in records:
            g[r.get("place")].append(r)
        return g

    for name, rs in regroup().items():
        if not name or len(rs) < 2:
            continue
        areas = [_postcode_area(r.get("postcode")) for r in rs]
        # postcode areas separate them only if they're all present and distinct
        if len(set(a for a in areas if a)) == len(rs) and all(areas):
            for r, a in zip(rs, areas):
                r["place"] = f"{name} ({a})"
        else:
            # keep the clean name; distinguish by precise distance on the card
            for r in rs:
                r["dist_exact"] = True


def geocode(q):
    q = (q or "").strip()
    if not q:
        return {"error": "empty query", "matches": []}
    out = {"query": q, "matches": [], "error": None}
    looks_postcodey = bool(re.match(r"^[A-Za-z]{1,2}\d", q.replace(" ", "")))

    def _add(lat, lon, label, country, extra=""):
        if lat is None or lon is None:
            return
        out["matches"].append({"lat": float(lat), "lon": float(lon),
                               "label": label, "country": country, "detail": extra})

    try:
        if looks_postcodey:
            # full postcode lookup
            try:
                d = fetch_json(f"{GEOCODE_BASE}/postcodes/"
                               f"{urllib.parse.quote(q)}", timeout=15)
                r = d.get("result")
                if r:
                    _add(r.get("latitude"), r.get("longitude"),
                         r.get("postcode"), r.get("country"),
                         r.get("region") or r.get("admin_district") or "")
            except urllib.error.HTTPError:
                pass
            # partial / outward-code autocomplete
            if not out["matches"]:
                try:
                    d = fetch_json(f"{GEOCODE_BASE}/postcodes?q="
                                   f"{urllib.parse.quote(q)}&limit=5", timeout=15)
                    for r in (d.get("result") or []):
                        _add(r.get("latitude"), r.get("longitude"),
                             r.get("postcode"), r.get("country"),
                             r.get("region") or r.get("admin_district") or "")
                except Exception:
                    pass
        if not out["matches"]:
            # place-name query
            d = fetch_json(f"{GEOCODE_BASE}/places?q="
                           f"{urllib.parse.quote(q)}&limit=6", timeout=15)
            for p in (d.get("result") or []):
                _add(p.get("latitude"), p.get("longitude"),
                     p.get("name_1"), p.get("country"),
                     p.get("county_unitary") or p.get("district_borough") or "")
        if not out["matches"]:
            out["error"] = "No match found. Try a postcode or a nearby town name."
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    return out


# ---- Environment Agency: flood warnings, river levels/flows, rainfall -------
# Open data, no key required, Open Government Licence. Three-tier model that
# mirrors the electricity units feed: stations -> measures -> readings.
#
# Honesty notes baked in:
#   * The EA transfers readings back at varying (often slow) frequencies —
#     typically once or twice a day in calm conditions, more often at flood
#     risk. So a "level" can be hours stale despite 15-min nominal cadence. We
#     surface each reading's own timestamp and let the frontend age-label it.
#   * Each station's stageScale carries its OWN typical range (5%/95% bands) and
#     record highs/lows, so we colour a reading by where it sits in that
#     station's history rather than inventing absolute thresholds.
#   * Readings can be NaN (JSON-illegal), in which case the value field is
#     omitted; we treat missing value as "no reading" not zero.
EA_BASE = "https://environment.data.gov.uk/flood-monitoring"
EA_ATTRIB = "Environment Agency flood and river level data from the real-time data API (Beta)"
# Default focus area: user's locale (London). dist in km. 40km picks up the
# surrounding river network since the tidal Thames itself is sparsely gauged.
EA_DEFAULT_LAT, EA_DEFAULT_LON, EA_DEFAULT_DIST = 51.51, -0.13, 40

_ea_cache = {}            # keyed by (lat,lon,dist) -> {"data":..., "ts":...}
EA_TTL = 300              # 5 min; readings change at most every 15 min
_ea_rain_peak = {}        # measure @id -> [[reading_ts, mm_h], ...]: 2h peak-hold for the card border
EA_PEAK_WINDOW_S = 2 * 3600
_ea_rain_cache = {}       # rain-only overviews (background watcher) — same key
_ea_floods_cache = {"data": None, "ts": 0}
EA_FLOODS_TTL = 300
# Wind (Open-Meteo) has its OWN cache + TTL, independent of the 5-min EA cache.
# Open-Meteo's data only updates every ~15 min and each of the 9 ring points
# counts against its daily quota, so refreshing on the general EA cadence wasted
# the free-tier allowance. 30 min = 2 fetches/hour: still twice the data's own
# update rate (never miss a refresh) but ~6x fewer calls than before.
_ea_wind_cache = {}       # keyed by (round(lat,2), round(lon,2)) -> {"wind":..., "ts":...}
EA_WIND_TTL = 900         # 15 min (4 fetches/hour)
# Wind uses OpenWeather on its OWN daily budget, separate from Resource Conditions.
# Per refresh: the home reading (OC4 current + one-minute nowcast = 2 calls, cached
# 15 min) PLUS the offshore rainfall-nowcast sentinels, which on OC4 are sampled
# from OpenWeather at ONE call per sea point (radar-fed). The ceiling is 600 to
# cover both; the offshore sampler is budget-guarded and falls back to the free
# Open-Meteo model when the day's budget is spent — still under the 900/day cap.
WIND_DAILY_MAX = 600
WIND_BUDGET_FILE = Path(__file__).with_name("wind_budget.json")
_wind_budget = {"date": None, "count": 0}

def _wind_budget_load():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _wind_budget["date"] == today:
        return _wind_budget
    try:
        if WIND_BUDGET_FILE.exists():
            stored = json.loads(WIND_BUDGET_FILE.read_text())
        else:
            stored = {}
    except Exception:
        stored = {}
    if stored.get("date") == today:
        _wind_budget["date"] = today
        _wind_budget["count"] = int(stored.get("count", 0))
    else:
        _wind_budget["date"] = today
        _wind_budget["count"] = 0
        _wind_budget_save()
    return _wind_budget

def _wind_budget_save():
    try:
        WIND_BUDGET_FILE.write_text(json.dumps(_wind_budget))
    except Exception:
        pass

def _wind_budget_remaining():
    return max(0, WIND_DAILY_MAX - _wind_budget_load()["count"])

def _wind_budget_spend(n):
    b = _wind_budget_load()
    b["count"] += n
    _wind_budget_save()


# ---- Pressure tendency (3-hour rolling, survives restarts) -----------------
# Log MSL pressure per location so the 3-hour trend persists across page
# refreshes and server restarts. Tendency bands follow the standard synoptic /
# Met Office 3-hour magnitudes (mb per 3h): steady <0.1, slowly 0.1-1.5,
# (moderate) 1.6-3.5, rapidly >3.5. If <3h of history exists we use the longest
# span available and scale it to a 3-hour-equivalent rate, so a fresh log still
# gives a sensible trend rather than nothing.
PRESSURE_LOG = Path(__file__).with_name("pressure_history.json")
PRESSURE_LOG_HOURS = 4

# Wind-direction history per location, for the dial's fading 3-hour tick ring.
WINDDIR_LOG = Path(__file__).with_name("winddir_history.json")
WINDDIR_LOG_HOURS = 3

def _log_winddir(lat, lon, deg, speed_ms):
    """Append current wind direction for this location, prune to 3h, and return
    a list of {age_s, dir_deg, speed_ms} newest-last for the tick ring. The age
    lets the client fade each tick by how old it is. Never raises."""
    if deg is None:
        return None
    key = f"{round(lat,2)},{round(lon,2)}"
    now = datetime.now(timezone.utc)
    try:
        store = {}
        if WINDDIR_LOG.exists():
            try:
                store = json.loads(WINDDIR_LOG.read_text())
            except Exception:
                store = {}
        series = store.get(key, {})
        series[now.isoformat()] = {"d": round(deg, 1),
                                   "s": round(speed_ms, 1) if speed_ms is not None else None}
        cutoff = now - timedelta(hours=WINDDIR_LOG_HOURS)
        series = {k: v for k, v in series.items() if _keep_after(k, cutoff)}
        store[key] = series
        store = {k: v for k, v in store.items() if v}
        WINDDIR_LOG.write_text(json.dumps(store))
        out = []
        for k, v in sorted(series.items()):
            try:
                age = (now - datetime.fromisoformat(k.replace("Z", "+00:00"))).total_seconds()
            except Exception:
                continue
            out.append({"age_s": round(age), "dir_deg": v.get("d"), "speed_ms": v.get("s")})
        return out
    except Exception as e:
        dbg("winddir log failed:", e)
        return None

def _dir_range(dirs):
    """Angular span (degrees) covered by a set of bearings, handling wrap-around
    (e.g. 350 and 10 span 20, not 340). Returns {min_deg, max_deg, span_deg} or
    None. min/max are the arc endpoints going clockwise from min to max."""
    if not dirs or len(dirs) < 2:
        return None
    ds = sorted(d % 360 for d in dirs)
    # largest gap between consecutive bearings (circular) -> the covered arc is
    # the complement of that gap
    gaps = []
    for i in range(len(ds)):
        a = ds[i]
        b = ds[(i + 1) % len(ds)]
        gap = (b - a) % 360
        gaps.append((gap, a, b))
    biggest = max(gaps, key=lambda g: g[0])
    span = 360 - biggest[0]
    # arc runs clockwise from the bearing after the biggest gap, to the one before
    start = biggest[2]           # just after the gap
    end = biggest[1]             # just before the gap
    return {"min_deg": round(start), "max_deg": round(end), "span_deg": round(span)}

def _log_pressure(lat, lon, hpa):
    """Append current pressure for this location, prune to PRESSURE_LOG_HOURS,
    and return the tendency dict {trend, change_3h, span_h} or None. Never raises."""
    if hpa is None:
        return None
    key = f"{round(lat,2)},{round(lon,2)}"
    now = datetime.now(timezone.utc)
    try:
        store = {}
        if PRESSURE_LOG.exists():
            try:
                store = json.loads(PRESSURE_LOG.read_text())
            except Exception:
                store = {}
        series = store.get(key, {})
        series[now.isoformat()] = round(hpa, 1)
        # prune this location's series
        cutoff = now - timedelta(hours=PRESSURE_LOG_HOURS)
        series = {k: v for k, v in series.items()
                  if _keep_after(k, cutoff)}
        store[key] = series
        # also drop any location series that is now empty
        store = {k: v for k, v in store.items() if v}
        PRESSURE_LOG.write_text(json.dumps(store))
        return _pressure_tendency(series, now)
    except Exception as e:
        dbg("pressure log failed:", e)
        return None

def _keep_after(iso, cutoff):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")) >= cutoff
    except Exception:
        return False

def _pressure_tendency(series, now):
    """Given {iso: hPa} for one location, compute the 3-hour tendency (or the
    longest span available, scaled to a 3h-equivalent). Returns a dict or None."""
    pts = []
    for k, v in series.items():
        try:
            pts.append((datetime.fromisoformat(k.replace("Z", "+00:00")), v))
        except Exception:
            continue
    if len(pts) < 2:
        return None
    pts.sort()
    latest_t, latest_v = pts[-1]
    # find the earliest sample within the last 3h; if none older than a few
    # minutes, we can't say anything yet
    target = latest_t - timedelta(hours=3)
    ref = None
    for t, v in pts:
        if t >= target:
            ref = (t, v)
            break
    if ref is None:
        ref = pts[0]
    ref_t, ref_v = ref
    span_h = (latest_t - ref_t).total_seconds() / 3600.0
    if span_h < 0.25:            # need at least ~15 min of separation
        return None
    raw_change = latest_v - ref_v            # over the actual span
    # For a short span, extrapolating to a full 3h rate can wildly overstate a
    # brief fluctuation (e.g. -1mb in 40min -> "-4.6mb/3h rapidly"). So only scale
    # up modestly: use the raw change directly once we have >=2h of span, and for
    # shorter spans blend toward the raw change rather than the full projection.
    if span_h >= 2.0:
        change_3h = raw_change * (3.0 / span_h)
    else:
        # cap the projection at 1.5x the raw change so short bursts don't inflate
        projected = raw_change * (3.0 / span_h)
        change_3h = max(-abs(raw_change)*1.5, min(abs(raw_change)*1.5, projected)) \
                    if raw_change else 0.0
    mag = abs(change_3h)
    if mag < 0.1:
        word = "Steady"
    elif change_3h > 0:
        word = ("Rising rapidly" if mag > 3.5 else
                "Rising" if mag > 1.5 else "Rising slowly")
    else:
        word = ("Falling rapidly" if mag > 3.5 else
                "Falling" if mag > 1.5 else "Falling slowly")
    return {"trend": word,
            "change_3h": round(change_3h, 1),
            "span_h": round(span_h, 1)}
_ea_station_cache = {}    # keyed by station ref -> {"data":..., "ts":...}
EA_STATION_TTL = 300
# National latest-readings, fetched in ONE call and indexed by measure URI.
# This is the EA's own recommended efficient pattern (a single call every
# ~15 min rather than crawling stations one by one). ~5000 rows, ~1s, small.
#
# Phase-aligned refresh: EA gauges publish on a ~15-min cadence, but at an
# offset we don't know a priori. A blind fixed-interval poll can land just
# BEFORE a publish and serve data ~26–28 min old. Instead we watch the newest
# reading's timestamp on each fetch, learn the publish rhythm, and schedule the
# NEXT refresh to land shortly AFTER the next expected publish — landing us in
# the fresh (1–8 min old) part of the cycle without polling any more often.
# This changes WHEN we poll, not HOW OFTEN, and every displayed age stays the
# measured `dateTime` (honesty preserved — we never imply data is fresher than
# the EA published).
_ea_latest_cache = {"idx": None, "ts": 0, "last_error": None, "stale": False,
                    "next_due": 0,        # monotonic time the next refresh is allowed
                    "last_publish": None, # epoch of the newest reading last seen
                    "period_s": None,     # learned publish period (~900s)
                    "newest_age_s": None} # age of newest reading at last fetch (for UI)
EA_LATEST_TTL = 300            # fallback interval when phase isn't yet learned
EA_LATEST_TTL_MIN = 120        # never refetch more often than this (rate guard)
EA_LATEST_TTL_MAX = 900        # and never wait longer than one full cycle
EA_PUBLISH_PERIOD = 900        # expected EA cadence: 15 min
EA_PHASE_LEAD = 45             # aim to poll this many s AFTER expected publish


def _ea_errstr(e):
    """Compact, human-readable error string for an upstream failure. For an
    HTTPError, include the status code and any USEFUL body text fetch_json
    attached. CDN/proxy error responses (Fastly/Varnish etc.) often carry a full
    HTML error page as the body; we strip tags and boilerplate so the message
    stays a plain textual reason instead of leaking markup onto the page. If no
    meaningful text survives, fall back to the status reason alone."""
    if isinstance(e, urllib.error.HTTPError):
        raw = getattr(e, "grid_body", "") or ""
        base = f"HTTP {e.code} {e.reason}"
        # CDN/proxy error responses often carry a full HTML page. Prefer its
        # <title> (or first <h1>) as a clean one-line reason; strip a leading
        # "Error NNN" that just repeats the status. Only fall back to the raw
        # stripped text for genuinely non-HTML bodies.
        looks_html = "<" in raw and ">" in raw
        body = ""
        if looks_html:
            m = re.search(r"<title[^>]*>(.*?)</title>", raw, re.I | re.S) \
                or re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.I | re.S)
            if m:
                body = re.sub(r"<[^>]*>", " ", m.group(1))
        else:
            body = re.sub(r"<[^>]*>", " ", raw)
        body = " ".join(body.split())
        body = re.sub(r"^Error\s+\d{3}\s*", "", body).strip()[:120]
        return f"{base} — {body}" if body else base
    return f"{type(e).__name__}: {e}"


def _ea_parse_dt(s):
    """Parse an EA ISO dateTime (usually '...Z') to epoch seconds, or None."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _ea_schedule_next(c, now_wall, now_mono, newest_epoch):
    """After a successful index fetch, learn the publish rhythm from the newest
    reading and set c['next_due'] (monotonic) to just after the next expected
    publish. Falls back to the fixed interval until a publish is observed."""
    period = c.get("period_s") or EA_PUBLISH_PERIOD
    if newest_epoch is None:
        # No timestamp to learn from — use the plain fallback interval.
        c["next_due"] = now_mono + EA_LATEST_TTL
        c["newest_age_s"] = None
        return
    age = max(0.0, now_wall - newest_epoch)
    c["newest_age_s"] = age
    # Refine the period estimate if the publish moment moved by ~one period.
    prev = c.get("last_publish")
    if prev and newest_epoch > prev:
        step = newest_epoch - prev
        # accept only plausible ~1-cycle steps to avoid learning from gaps
        if 0.5 * EA_PUBLISH_PERIOD <= step <= 1.5 * EA_PUBLISH_PERIOD:
            # gentle EMA toward the observed step
            c["period_s"] = 0.7 * period + 0.3 * step
    c["last_publish"] = newest_epoch
    period = c.get("period_s") or EA_PUBLISH_PERIOD
    # Next publish is expected ~one period after the newest reading we hold.
    # Aim to poll EA_PHASE_LEAD seconds after that, in monotonic terms.
    secs_to_next_publish = period - age + EA_PHASE_LEAD
    # Clamp so we neither hammer the endpoint nor drift more than a cycle.
    wait = min(max(secs_to_next_publish, EA_LATEST_TTL_MIN), EA_LATEST_TTL_MAX)
    c["next_due"] = now_mono + wait
    dbg(f"ea index: newest {age:.0f}s old, period~{period:.0f}s, "
        f"next refresh in {wait:.0f}s")


def _ea_latest_index():
    """Return {measureURI: (value, dateTime)} for the latest reading of every
    measure nationally, cached. The station 'measures[].@id' join key matches
    the readings 'measure' field. On failure keeps the prior index (labelled
    stale) rather than blanking; records the last error for diagnostics.

    Refresh timing is phase-aligned (see _ea_latest_cache): we refetch when the
    learned schedule says the next publish is due, not on a blind fixed timer."""
    c = _ea_latest_cache
    now_mono = time.monotonic()
    # Phase-aware gate: hold the cached index until its scheduled next_due.
    if c["idx"] is not None and now_mono < c.get("next_due", 0):
        return c["idx"]
    idx = {}
    newest_epoch = None
    try:
        # The single heaviest EA call (~15k rows). This is the most likely one
        # to draw a CDN 503 'Backend fetch failed' under load.
        p = fetch_json(f"{EA_BASE}/data/readings?latest&_limit=15000", timeout=60)
        for r in (p.get("items") or []):
            m = r.get("measure")
            if not m:
                continue
            mid = m.get("@id") if isinstance(m, dict) else m
            dt = r.get("dateTime")
            idx[mid] = (_ea_num(r.get("value")), dt)
            e = _ea_parse_dt(dt)
            if e is not None and (newest_epoch is None or e > newest_epoch):
                newest_epoch = e
        c["last_error"] = None
    except Exception as e:
        c["last_error"] = _ea_errstr(e)
        dbg("ea latest-index failed:", c["last_error"])
        if c["idx"] is not None:
            c["stale"] = True
            # brief retry rather than waiting a whole phase after a failure
            c["next_due"] = now_mono + EA_LATEST_TTL_MIN
            return c["idx"]   # keep prior index rather than blanking on failure
        # cold + failed: try again soon
        c["next_due"] = now_mono + EA_LATEST_TTL_MIN
    else:
        _ea_schedule_next(c, time.time(), now_mono, newest_epoch)
    c["idx"] = idx
    c["ts"] = time.time()
    c["stale"] = False
    return idx


def _ea_num(v):
    """Coerce an EA numeric that may be None, a string, or a list (some
    measures report multi-values). Returns float or None; never raises."""
    if v is None:
        return None
    if isinstance(v, list):
        v = v[-1] if v else None
    try:
        f = float(v)
        return f if f == f else None      # drop NaN
    except (TypeError, ValueError):
        return None


def _ea_first(v):
    """Some EA fields (e.g. townName, riverName) occasionally arrive as a list.
    Return the first scalar or the value itself."""
    if isinstance(v, list):
        return v[0] if v else None
    return v


def get_ea_floods():
    """Current flood warnings/alerts nationally. Severity 1 (severe) .. 4
    (no longer in force). Cheap, cache-friendly national call."""
    c = _ea_floods_cache
    if c["data"] is not None and time.time() - c["ts"] < EA_FLOODS_TTL:
        return c["data"]
    out = {"warnings": [], "counts": {"1": 0, "2": 0, "3": 0, "4": 0},
           "attribution": EA_ATTRIB, "error": None}
    try:
        payload = fetch_json(f"{EA_BASE}/id/floods", timeout=25)
        for it in (payload.get("items") or []):
            lvl = it.get("severityLevel")
            fa = it.get("floodArea") or {}
            w = {
                "severity_level": lvl,
                "severity": it.get("severity"),
                "description": it.get("description"),
                "message": it.get("message"),
                "county": fa.get("county"),
                "river_or_sea": fa.get("riverOrSea"),
                "is_tidal": it.get("isTidal"),
                "time_raised": it.get("timeRaised"),
                "time_changed": it.get("timeSeverityChanged"),
                "area_id": it.get("floodAreaID"),
            }
            out["warnings"].append(w)
            if lvl in (1, 2, 3, 4):
                out["counts"][str(lvl)] += 1
        # worst-first: severe (1) at the top
        out["warnings"].sort(key=lambda w: (w["severity_level"] or 9))
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    c["data"] = out
    c["ts"] = time.time()
    return out


def _ea_local_floods(lat, lon, dist):
    """Flood warnings/alerts in force WITHIN the user's chosen radius, enriched
    with a name and a distance so the dashboard can both highlight them and name
    the nearest local one in the spoken alert ("...including one locally at
    <place>, N km from you"). Two cached calls: the floods spatial query for what
    is in force locally, and the floodAreas spatial query for each area's centroid
    and tidy label. Returns a list of dicts sorted nearest-first; never raises."""
    try:
        furl = f"{EA_BASE}/id/floods?lat={lat}&long={lon}&dist={dist}"
        floods = fetch_json(furl, timeout=20).get("items") or []
    except Exception:
        return []
    inforce = {}
    for it in floods:
        fid = it.get("floodAreaID") or (it.get("floodArea") or {}).get("notation")
        lvl = it.get("severityLevel")
        if fid and lvl in (1, 2, 3):            # 4 = warning no longer in force
            inforce[fid] = {"area_id": fid, "severity_level": lvl,
                            "name": it.get("description"),
                            "river_or_sea": (it.get("floodArea") or {}).get("riverOrSea"),
                            "dist_km": None}
    if not inforce:
        return []
    # centroids + short labels for those areas (single spatial call)
    try:
        aurl = f"{EA_BASE}/id/floodAreas?lat={lat}&long={lon}&dist={dist}&_limit=500"
        areas = fetch_json(aurl, timeout=20).get("items") or []
    except Exception:
        areas = []
    coords = {}
    for a in areas:
        aid = a.get("notation") or a.get("fwdCode")
        if aid:
            coords[aid] = (a.get("label"), _ea_num(a.get("lat")), _ea_num(a.get("long")))
    for fid, rec in inforce.items():
        lbl, flat, flon = coords.get(fid, (None, None, None))
        rec["name"] = lbl or rec.get("name") or rec.get("river_or_sea")
        if flat is not None and flon is not None:
            d = _haversine_km(lat, lon, flat, flon)
            rec["dist_km"] = round(d, 1) if d is not None else None
    out = list(inforce.values())
    out.sort(key=lambda r: (r["dist_km"] is None, r["dist_km"] or 0))
    return out


def _ea_band_position(value, scale):
    """Return where `value` sits relative to a station's own typical range:
    'low' (below typicalRangeLow), 'normal', 'high' (above typicalRangeHigh),
    or None if the scale is unavailable. Honest per-station context rather than
    an absolute threshold."""
    if value is None or not scale:
        return None
    lo = _ea_num(scale.get("typicalRangeLow"))
    hi = _ea_num(scale.get("typicalRangeHigh"))
    if lo is not None and value < lo:
        return "low"
    if hi is not None and value > hi:
        return "high"
    if lo is not None or hi is not None:
        return "normal"
    return None


def get_ea(lat=None, lon=None, dist=None, rain_only=False):
    """Overview for a location: nearby river-level / flow / rainfall stations
    with their latest readings and each station's own typical-range context.
    Cached per (lat,lon,dist).

    rain_only=True skips the (expensive) river-station fetch and returns only
    the rainfall gauges. This is what the dashboard's background rain watcher
    uses when the EA panel is closed — it avoids two _limit=500 station pulls
    per poll for a signal that only needs the rainfall list. A fresh FULL
    overview already contains rainfall, so it satisfies a rain_only request too;
    a rain_only result is cached separately and never served to a full request."""
    lat = EA_DEFAULT_LAT if lat is None else lat
    lon = EA_DEFAULT_LON if lon is None else lon
    dist = EA_DEFAULT_DIST if dist is None else dist
    key = (round(lat, 3), round(lon, 3), round(dist, 1))
    # A fresh full overview serves everything; a rain_only cache entry only
    # serves rain_only callers.
    c = _ea_cache.get(key)
    if c and time.time() - c["ts"] < EA_TTL:
        return c["data"]
    if rain_only:
        rc = _ea_rain_cache.get(key)
        if rc and time.time() - rc["ts"] < EA_TTL:
            return rc["data"]

    out = {"lat": lat, "lon": lon, "dist": dist, "stations": [],
           "rainfall": [], "wind": None, "attribution": EA_ATTRIB, "error": None,
           "rain_only": rain_only, "local_flood_area_ids": [], "local_floods": [],
           # Per-endpoint diagnostics: which of the underlying EA calls failed
           # and why. The dashboard can show this so a partial failure (floods
           # OK, stations/rainfall 503) is visible rather than a blank panel.
           "diag": {"latest_index": None, "stations": None, "rainfall": None,
                    "latest_stale": False, "wind": None},
           "generated": datetime.now(timezone.utc).isoformat()}
    try:
        latest = _ea_latest_index()
        out["diag"]["latest_index"] = _ea_latest_cache.get("last_error")
        out["diag"]["latest_stale"] = bool(_ea_latest_cache.get("stale"))
        out["diag"]["index_age_s"] = _ea_latest_cache.get("newest_age_s")
        if not rain_only:
            _ea_collect_stations(out, lat, lon, dist, latest)
        _ea_collect_rainfall(out, lat, lon, dist, latest)
        if not rain_only:
            _ea_collect_wind(out, lat, lon)
            out["local_floods"] = _ea_local_floods(lat, lon, dist)
            out["local_flood_area_ids"] = [f["area_id"] for f in out["local_floods"]]
    except Exception as e:
        out["error"] = _ea_errstr(e)
    # Roll a concise top-level error from whichever sub-call failed, so existing
    # UI that reads out['error'] still shows something useful.
    if not out["error"]:
        d = out["diag"]
        parts = []
        if d["latest_index"]:
            parts.append(f"latest-readings index: {d['latest_index']}")
        if d["stations"]:
            parts.append(f"river stations: {d['stations']}")
        if d["rainfall"]:
            parts.append(f"rainfall gauges: {d['rainfall']}")
        if parts:
            out["error"] = "; ".join(parts)

    # -- Rainfall-alert diagnostic probe (read-only) --------------------------
    # Runs only on a FULL overview (rain_only requests carry no weather block).
    # Reads what we already fetched (no extra OWM call) and logs the phrase the
    # alert WOULD speak; emits marked, modelled offshore virtual gauges into
    # out['rainfall_model']. Never raises past its own body; never plays a tone.
    if _rain_probe is not None and not rain_only and out.get("wind"):
        try:
            _w = out["wind"]; _cond = _w.get("conditions") or {}
            _hm = _w.get("home") or {}; _spd = _hm.get("speed_ms")
            # Offshore virtual-gauge sampler. When OC4 is live, sample each sea
            # point from OpenWeather (radar/satellite-fed) instead of the modelled
            # Open-Meteo fallback -- ONE OWM call per point, so budget-guarded: if
            # the day's wind budget can't cover the batch, fall back to Open-Meteo.
            _okey = _load_weather_key()
            # NET (sentinels + inner pickets + dither fills): always the free,
            # batched, keyless Open-Meteo -- one call regardless of point count.
            _net_sample = _rain_probe.fetch_om_precip
            # TRACK (mobile cards): OC4 radar-fed quality read, one OWM call per
            # tracked point, budget-guarded; falls back to Open-Meteo when the day's
            # budget can't cover the batch. Only ever called for active detections.
            def _track_sample(pts):
                if not pts:
                    return []
                if (_owm_onecall is not None and _w.get("api") == "OC4" and _okey
                        and _wind_budget_remaining() >= len(pts)):
                    rates = _owm_onecall.fetch_sea_precip(pts, _okey)
                    _wind_budget_spend(len(pts))
                    return rates
                return _rain_probe.fetch_om_precip(pts)   # free Open-Meteo fallback
            _res = _rain_probe.run_probe(
                _RAIN_PROBE_STATE, home=(lat, lon),
                rain_mm_h=_cond.get("rain_1h") or 0.0,
                pressure_hpa=_cond.get("pressure"),
                visibility_m=_cond.get("visibility_m"),
                wind_from=_hm.get("dir_deg"),
                wind_kmh=(_spd * 3.6) if _spd is not None else None,
                gauges=out.get("rainfall") or [],
                flood_active=False,          # floods come from a separate endpoint
                feed_stale=bool(_w.get("stale")),
                forward_precip=out.get("_owm_minute"),
                net_sample_fn=_net_sample, track_sample_fn=_track_sample)
            out["rainfall_model"] = _res.get("virtual_gauges") or []
            out["diag"]["rain_probe"] = _res.get("log")
            out["rain_probe"] = {"would_speak": _res.get("would_speak"),
                                 "signals": _res.get("signals")}
            dbg("rain-probe:", _res.get("log"))
        except Exception as _pe:
            out["diag"]["rain_probe"] = "probe error: " + _ea_errstr(_pe)

    if rain_only:
        _ea_rain_cache[key] = {"data": out, "ts": time.time()}
    else:
        _ea_cache[key] = {"data": out, "ts": time.time()}
    return out


def _ea_collect_stations(out, lat, lon, dist, latest):
    """River-level / flow stations near the point, banded against each station's
    own typical range. Mutates out['stations']. Never raises past its own body
    for the rainfall step's sake — a station failure sets out['error']."""
    try:
        # Stations near the point, full view so we get stageScale typical ranges.
        url = (f"{EA_BASE}/id/stations?lat={lat}&long={lon}&dist={dist}"
               f"&_view=full&_limit=500")
        payload = fetch_json(url, timeout=15)
        items = payload.get("items") or []
        # Latest readings aren't carried on the station's measures[] in
        # _view=full, so join against the national latest-readings index by
        # measure URI (the shared index is passed in — one cached call).
        for it in items:
            measures = it.get("measures") or []
            if isinstance(measures, dict):
                measures = [measures]
            # stageScale/downstageScale are usually inline objects, but for some
            # stations the API returns a bare URI string pointing at the scale
            # resource instead. Treat a non-dict scale as absent (no typical-range
            # band for that station) rather than dereferencing it per-station.
            stage = it.get("stageScale")
            stage = stage if isinstance(stage, dict) else {}
            downstage = it.get("downstageScale")
            downstage = downstage if isinstance(downstage, dict) else {}
            level_val = None      # main "Stage" level — pairs with stageScale
            downstage_val = None  # "Downstream Stage" — pairs with downstageScale
            flow_val = None
            latest_dt = None
            for m in measures:
                if not isinstance(m, dict):
                    continue        # some entries can be bare URI strings
                param = m.get("parameter")
                qual = (m.get("qualifier") or "")
                mid = m.get("@id")
                val, dt = latest.get(mid, (None, None))
                if dt and (latest_dt is None or dt > latest_dt):
                    latest_dt = dt
                if param == "level":
                    # Keep stage and downstream stage apart so each is compared
                    # to its OWN scale — mixing them produced false "high" bands.
                    if "Downstream" in qual:
                        if downstage_val is None:
                            downstage_val = val
                    elif level_val is None:
                        level_val = val
                elif param == "flow" and flow_val is None:
                    flow_val = val
            # If a station only reports downstream stage, fall back to it (paired
            # with the downstage scale for banding below).
            use_downstage_scale = False
            if level_val is None and downstage_val is not None:
                level_val = downstage_val
                use_downstage_scale = True
            # Skip stations with nothing useful
            if level_val is None and flow_val is None:
                continue
            band_scale = downstage if use_downstage_scale else stage
            band = _ea_band_position(level_val, band_scale)
            st = {
                "ref": it.get("stationReference") or it.get("notation"),
                "label": _ea_first(it.get("label")),
                "river": _ea_first(it.get("riverName")),
                "town": _ea_first(it.get("town")),
                "catchment": _ea_first(it.get("catchmentName")),
                "lat": _ea_num(it.get("lat")),
                "lon": _ea_num(it.get("long")),
                "level_m": level_val,
                "flow_cumecs": flow_val,
                "latest_dt": latest_dt,
                "band": band,          # low / normal / high vs station's own range
                "typical_low": _ea_num(band_scale.get("typicalRangeLow")),
                "typical_high": _ea_num(band_scale.get("typicalRangeHigh")),
                "max_on_record": _ea_num((band_scale.get("maxOnRecord") or {}).get("value")),
                "dist_km": _haversine_km(lat, lon, _ea_num(it.get("lat")), _ea_num(it.get("long"))),
                "status": _ea_first(it.get("status")),
            }
            out["stations"].append(st)
        # Sort: flooding-relevant first (high band), then by river name.
        _bandrank = {"high": 0, "normal": 1, "low": 2, None: 3}
        out["stations"].sort(key=lambda s: (_bandrank.get(s["band"], 3),
                                            (s["river"] or "~"), (s["label"] or "")))
    except Exception as e:
        # Record the station failure but let rainfall still be attempted by the
        # caller — a river-endpoint hiccup shouldn't blind the rain watcher.
        out["diag"]["stations"] = _ea_errstr(e)
        dbg("ea stations failed:", out["diag"]["stations"])


def _ea_collect_rainfall(out, lat, lon, dist, latest):
    """Nearby rainfall gauges joined to the shared latest-readings index.
    Mutates out['rainfall']. Rainfall is a nice-to-have for the full overview
    but the primary signal for the background rain watcher, so failures are
    swallowed rather than propagated."""
    # Rainfall: geo-filtering works on the STATIONS endpoint (not readings),
    # so list nearby rainfall stations and join their measures to the same
    # latest index. Values are tips (mm) over the station's period.
    try:
        rurl = (f"{EA_BASE}/id/stations?parameter=rainfall"
                f"&lat={lat}&long={lon}&dist={dist}&_limit=500")
        rp = fetch_json(rurl, timeout=15)
        for it in (rp.get("items") or []):
            ms = it.get("measures") or []
            if isinstance(ms, dict):
                ms = [ms]
            for m in ms:
                if not isinstance(m, dict):
                    continue
                mid = m.get("@id")
                val, dt = latest.get(mid, (None, None))
                if val is None:
                    continue
                # EA rainfall is a bucket TOTAL over the measure's period (15-min
                # tips = 900 s). Everything modelled (OWM/Open-Meteo, sentinels,
                # the intensity bands) is a mm/h RATE, so convert here to a unified
                # rate and carry BOTH: mm_h drives all comparison/colour/alerts,
                # the raw bucket 'mm' stays for honest display.
                _per = m.get("period") or 900
                try:
                    _mmh = round(float(val) * 3600.0 / float(_per), 2) if _per else None
                except (TypeError, ValueError):
                    _mmh = None
                # 2h peak-hold for the card border: the highest rate this gauge
                # has reported in the last two hours, so the border reflects recent
                # activity even after the rain stops. Keyed by reading time (ages out
                # on its own timestamp) and deduped so a reading isn't re-counted
                # across polls.
                _now = time.time()
                _rt = _ea_parse_dt(dt) or _now
                buf = [e for e in _ea_rain_peak.get(mid, []) if _now - e[0] <= EA_PEAK_WINDOW_S]
                if _mmh is not None and (not buf or buf[-1][0] != _rt):
                    buf.append([_rt, _mmh])
                if buf:
                    _ea_rain_peak[mid] = buf
                else:
                    _ea_rain_peak.pop(mid, None)
                _peak = max((e[1] for e in buf), default=_mmh)
                out["rainfall"].append({
                    "ref": it.get("stationReference") or it.get("notation"),
                    "label": _ea_first(it.get("label")),
                    "grid": it.get("gridReference"),
                    "period_s": m.get("period"),
                    "lat": _ea_num(it.get("lat")),
                    "lon": _ea_num(it.get("long")),
                    "mm": val, "mm_h": _mmh, "mm_h_max2h": _peak, "dt": dt,
                })
        # distance from the user's location (local calc, no API), rounded
        # to a ~5-km bucket for a rough "how far" sense.
        for r in out["rainfall"]:
            d = _haversine_km(lat, lon, r.get("lat"), r.get("lon"))
            r["dist_km"] = d
            r["dist_bucket"] = (round(d/5)*5) if d is not None else None
        # nearest first
        out["rainfall"].sort(key=lambda r: (r["dist_km"] is None, r["dist_km"] or 0))
        out["rainfall"] = out["rainfall"][:40]
    except Exception as e:
        # The gauge fetch itself failed — this is the diagnostic that matters.
        out["diag"]["rainfall"] = _ea_errstr(e)
        dbg("ea rainfall failed:", out["diag"]["rainfall"])
        return
    # Naming is a separate, non-critical step: a reverse-geocode failure must
    # NOT be reported as a rainfall-gauge failure, so it's isolated.
    try:
        info = reverse_geocode_bulk([(r["lat"], r["lon"]) for r in out["rainfall"]])
        for r, nm in zip(out["rainfall"], info):
            r["place"] = _short_place(nm["name"]) if nm else None
            r["postcode"] = nm.get("postcode") if nm else None
        # Gauges have no real EA name, so several can reverse-geocode to the same
        # place. Append a short postcode area to any that still collide so no two
        # cards read identically.
        _dedupe_place_names(out["rainfall"])
    except Exception as e:
        dbg("ea rainfall geocode failed (non-fatal):", _ea_errstr(e))


# ---- Local wind field (Open-Meteo, keyless, batched) -----------------------
# EA stations barely carry wind (~12 nationally, none near most users), so a
# local wind field comes from Open-Meteo instead: MODELLED data, keyless, and
# — critically — it returns many points in ONE batched call, so sampling a ring
# around the user's location costs a single request per EA refresh and does not
# touch the OpenWeather daily budget. Uniform coverage everywhere, so any user
# in any area gets wind. Labelled modelled in the payload (honesty convention).
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
WIND_RING_KM = 18.0        # outer sample points ~15-20 km from home
# 8 compass bearings for the outer ring (home is the 9th, central point).
WIND_BEARINGS = [("N", 0), ("NE", 45), ("E", 90), ("SE", 135),
                 ("S", 180), ("SW", 225), ("W", 270), ("NW", 315)]


def _offset_latlon(lat, lon, bearing_deg, dist_km):
    """Point dist_km from (lat,lon) along a compass bearing. Small-distance
    equirectangular offset — plenty accurate at ~18 km."""
    R = 6371.0
    b = math.radians(bearing_deg)
    dlat = (dist_km * math.cos(b)) / R
    dlon = (dist_km * math.sin(b)) / (R * math.cos(math.radians(lat)))
    return (lat + math.degrees(dlat), lon + math.degrees(dlon))


def _stale_wind(cached, reason=None):
    """Return a copy of a cached wind reading marked stale, with its age in
    seconds computed from the cache timestamp. Used whenever block 4 falls back
    to a cached OpenWeather reading (TTL hit, budget/key unavailable, or a failed
    fetch) so the dashboard can show the reading's age and a stale marker rather
    than presenting an old sky observation as if it were live. Honesty over
    plausibility: an unlabelled stale reading (e.g. '99% overcast' hours after it
    cleared) is exactly the failure this guards against."""
    if not cached or not cached.get("wind"):
        return None
    w = dict(cached["wind"])
    ts = cached.get("ts")
    w["stale"] = True
    if ts is not None:
        w["stale_age_s"] = round(time.time() - ts)
    if reason:
        w["stale_reason"] = reason
    return w


_om_cloud_cache = {}          # keyed by (round(lat,2), round(lon,2)) -> {"data":..., "ts":...}
OM_CLOUD_TTL = 900            # 15 min: matches Open-Meteo's own update cadence

# WMO weather-code -> (main, description). Descriptions use the SAME vocabulary
# family as OpenWeather ("clear sky", "…clouds", "rain") so the dashboard's
# colour logic and labels keep working unchanged. Cloud-only codes (0-3) get a
# more precise description from the numeric cloud % below.
_WMO = {
    0: ("Clear", "clear sky"), 1: ("Clouds", "mainly clear"),
    2: ("Clouds", "partly cloudy"), 3: ("Clouds", "overcast clouds"),
    45: ("Fog", "fog"), 48: ("Fog", "depositing rime fog"),
    51: ("Drizzle", "light drizzle"), 53: ("Drizzle", "drizzle"),
    55: ("Drizzle", "dense drizzle"),
    56: ("Drizzle", "freezing drizzle"), 57: ("Drizzle", "dense freezing drizzle"),
    61: ("Rain", "light rain"), 63: ("Rain", "rain"), 65: ("Rain", "heavy rain"),
    66: ("Rain", "freezing rain"), 67: ("Rain", "heavy freezing rain"),
    71: ("Snow", "light snow"), 73: ("Snow", "snow"), 75: ("Snow", "heavy snow"),
    77: ("Snow", "snow grains"),
    80: ("Rain", "light rain showers"), 81: ("Rain", "rain showers"),
    82: ("Rain", "violent rain showers"),
    85: ("Snow", "snow showers"), 86: ("Snow", "heavy snow showers"),
    95: ("Thunderstorm", "thunderstorm"),
    96: ("Thunderstorm", "thunderstorm with hail"),
    99: ("Thunderstorm", "thunderstorm with heavy hail"),
}

def _cloud_band_desc(pct):
    """Map a cloud-cover percentage to an OpenWeather-style description.
    Bands mirror OWM's own (few 11-25, scattered 25-50, broken 51-84,
    overcast 85+) so labels read consistently across sources."""
    if pct is None:
        return None
    if pct <= 10:  return "clear sky"
    if pct <= 25:  return "few clouds"
    if pct <= 50:  return "scattered clouds"
    if pct <= 84:  return "broken clouds"
    return "overcast clouds"

def _openmeteo_cloud(lat, lon, timeout=12):
    """Fetch current cloud cover + weather code from Open-Meteo (keyless).
    Returns {clouds_pct, cond_main, cond_desc, source} or None on failure.
    Used as the PRIMARY source for block 4's cloud/description because OWM's
    Current Weather cloud field has proved unreliable for this location; OWM
    remains the backup when Open-Meteo is unavailable."""
    key = (round(lat, 2), round(lon, 2))
    c = _om_cloud_cache.get(key)
    if c and time.time() - c["ts"] < OM_CLOUD_TTL:
        return c["data"]
    try:
        url = ("https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               "&current=cloud_cover,weather_code")
        d = fetch_json(url, timeout=timeout)
        cur = (d or {}).get("current") or {}
        pct = cur.get("cloud_cover")
        code = cur.get("weather_code")
        main, wdesc = _WMO.get(code, (None, None))
        # For plain cloud codes, prefer the numeric-band description (finer than
        # the code's coarse label); for weather codes (rain/fog/etc) keep the code.
        if code in (0, 1, 2, 3) or code is None:
            desc = _cloud_band_desc(pct) or wdesc
            main = "Clear" if (pct is not None and pct <= 10) else "Clouds"
        else:
            desc = wdesc
        data = {"clouds_pct": pct, "cond_main": main, "cond_desc": desc,
                "source": "Open-Meteo"}
        _om_cloud_cache[key] = {"data": data, "ts": time.time()}
        return data
    except Exception as e:
        dbg("open-meteo cloud fetch failed:", _ea_errstr(e))
        return None


def _ea_collect_wind(out, lat, lon):
    """Attach the local wind reading (home point only) from OpenWeather's Current
    Weather Data endpoint to out['wind']. Cached on its OWN 15-min TTL
    (EA_WIND_TTL, 4 fetches/hour) and its OWN daily budget (WIND_DAILY_MAX,
    separate from Resource Conditions). Non-fatal: a failure leaves the last good
    cached reading in place rather than blanking the dials.

    Note: OpenWeather's current-weather endpoint is single-point only (no batched
    multi-coordinate call), so the former 8-point Open-Meteo area-spread ring is
    not available here — the ring is omitted and the dials show the home point."""
    wkey = (round(lat, 2), round(lon, 2))
    cached = _ea_wind_cache.get(wkey)
    if cached and time.time() - cached["ts"] < EA_WIND_TTL:
        out["wind"] = cached["wind"]
        return
    key = _load_weather_key()
    if not key:
        out["diag"]["wind"] = "no OpenWeather key"
        if cached:
            out["wind"] = _stale_wind(cached, "no OpenWeather key")
        return
    if _wind_budget_remaining() < 1:
        out["diag"]["wind"] = "wind daily budget reached"
        if cached:
            out["wind"] = _stale_wind(cached, "daily fetch budget reached")
        return
    try:
        data, _owm_tier, _owm_minute = _fetch_owm(lat, lon, key)
        _wind_budget_spend(2 if (_owm_tier == "OC4" and _owm_minute is not None) else 1)
        out["_owm_minute"] = _owm_minute
        w = (data or {}).get("wind") or {}
        home = {
            "speed_ms": w.get("speed"),
            "dir_deg": w.get("deg"),
            "gust_ms": w.get("gust"),
            "time": datetime.now(timezone.utc).isoformat(),
        }
        # Everything below comes free in the SAME response we already fetch for
        # wind — no extra API cost. Dew point is not supplied by this endpoint,
        # so it's DERIVED from temp+humidity (Magnus formula) and tagged derived.
        m = (data or {}).get("main") or {}
        clouds = (data or {}).get("clouds") or {}
        rain = (data or {}).get("rain") or {}
        snow = (data or {}).get("snow") or {}
        wx = ((data or {}).get("weather") or [{}])[0]
        sysd = (data or {}).get("sys") or {}
        temp = m.get("temp")
        rh = m.get("humidity")
        dew = None
        if temp is not None and rh:
            try:
                import math as _math
                a, b = 17.62, 243.12
                g_ = (a * temp) / (b + temp) + _math.log(max(rh, 1) / 100.0)
                dew = round((b * g_) / (a - g_), 1)
            except Exception:
                dew = None
        cond = {
            "temp": temp,
            "feels_like": m.get("feels_like"),
            "temp_min": m.get("temp_min"),
            "temp_max": m.get("temp_max"),
            "pressure": m.get("pressure"),          # hPa == mB
            "humidity": rh,
            "dew_point": dew,                        # derived
            "dew_point_derived": dew is not None,
            "clouds_pct": clouds.get("all"),
            "visibility_m": (data or {}).get("visibility"),
            "rain_1h": rain.get("1h"),
            "snow_1h": snow.get("1h"),
            "cond_main": wx.get("main"),
            "cond_desc": wx.get("description"),
            "sunrise": sysd.get("sunrise"),          # Unix UTC
            "sunset": sysd.get("sunset"),
            "tz_offset": (data or {}).get("timezone"),  # location's UTC offset (s)
        }
        # Cloud + description: OWM's Current Weather cloud field has proved
        # unreliable for this location (repeatedly reporting overcast against a
        # clear sky and satellite). Open-Meteo is the PRIMARY source for these two
        # fields; OWM values above stay only as the backup when OM is unavailable.
        # cloud_source records which one actually supplied the shown value, so the
        # dashboard can stay honest about provenance. Temp/pressure/wind remain OWM.
        om = _openmeteo_cloud(lat, lon)
        if om and om.get("clouds_pct") is not None:
            cond["clouds_pct"] = om["clouds_pct"]
            cond["cond_main"] = om["cond_main"]
            cond["cond_desc"] = om["cond_desc"]
            cond["cloud_source"] = "Open-Meteo"
        else:
            cond["cloud_source"] = "OpenWeather (Open-Meteo unavailable)"
        # 3-hour pressure tendency (persisted per location so it survives refresh)
        try:
            cond["pressure_tendency"] = _log_pressure(lat, lon, m.get("pressure"))
        except Exception:
            cond["pressure_tendency"] = None
        wind = {
            "home": home,
            "ring": [],                    # no ring: OpenWeather can't batch points
            "ring_km": None,
            "modelled": False,             # this is a station-model current obs
            "source": "OpenWeather " + ("One Call 4.0" if _owm_tier == "OC4" else "Current Weather 2.5"),
            "api": _owm_tier,
            "generated": datetime.now(timezone.utc).isoformat(),
            "conditions": cond,
        }
        # 3-hour direction history for the dial's fading tick ring (persisted)
        try:
            hist = _log_winddir(lat, lon, w.get("deg"), w.get("speed"))
            wind["dir_history"] = hist
            if hist:
                dirs = [h["dir_deg"] for h in hist if h.get("dir_deg") is not None]
                wind["dir_range"] = _dir_range(dirs)
        except Exception:
            wind["dir_history"] = None
        # classify calm / variable / normal now dir_range (if any) is known
        _span = (wind.get("dir_range") or {}).get("span_deg")
        home["wind_state"] = _wind_state(w.get("speed"), w.get("deg"), _span)
        out["wind"] = wind
        _ea_wind_cache[wkey] = {"wind": wind, "ts": time.time()}
    except Exception as e:
        out["diag"]["wind"] = _ea_errstr(e)
        dbg("ea wind (openweather) failed:", out["diag"]["wind"])
        # reuse the last good reading if we have one, so a transient failure or a
        # hit daily limit doesn't blank the dials — clearly marked as the cached
        # (stale) copy so an old sky reading can't masquerade as live.
        if cached:
            out["wind"] = _stale_wind(cached, "last fetch failed")


def _wind_state(speed_ms, deg, dir_span_deg):
    """Classify the home wind so the dial can be honest when direction is
    undefined rather than drawing a firm arrow.
      calm     — speed below WMO calm threshold (0.5 m/s); direction meaningless.
      variable — OWM omitted the bearing (deg is None) with a real speed, OR the
                 logged 3h direction has swung widely (>=135 deg) at low speed
                 (<3 m/s) — a genuinely shifting light wind, not a steady breeze.
      normal   — a definite bearing to draw.
    """
    if speed_ms is not None and speed_ms < 0.5:
        return "calm"
    if speed_ms is not None and speed_ms > 0 and deg is None:
        return "variable"
    if (dir_span_deg is not None and dir_span_deg >= 135
            and speed_ms is not None and speed_ms < 3):
        return "variable"
    return "normal"


def get_ea_station(ref):
    """Drill-down for one station: its measures plus a recent readings trace
    (last ~day) for each, and the station's typical-range scale for context."""
    if not ref:
        return {"error": "no station reference", "readings": {}}
    c = _ea_station_cache.get(ref)
    if c and time.time() - c["ts"] < EA_STATION_TTL:
        return c["data"]
    out = {"ref": ref, "label": None, "river": None, "town": None,
           "measures": [], "series": {}, "scale": {}, "downscale": {},
           "attribution": EA_ATTRIB, "error": None}
    try:
        # Station metadata + scales
        meta = fetch_json(f"{EA_BASE}/id/stations/{urllib.parse.quote(ref)}",
                          timeout=25)
        it = meta.get("items") or {}
        if isinstance(it, list):
            it = it[0] if it else {}
        out["label"] = _ea_first(it.get("label"))
        out["river"] = _ea_first(it.get("riverName"))
        out["town"] = _ea_first(it.get("town"))
        out["scale"] = it.get("stageScale") if isinstance(it.get("stageScale"), dict) else {}
        out["downscale"] = it.get("downstageScale") if isinstance(it.get("downstageScale"), dict) else {}
        # Recent readings for all this station's measures (last 24h), sorted.
        rd = fetch_json(
            f"{EA_BASE}/id/stations/{urllib.parse.quote(ref)}/readings"
            f"?_sorted&_limit=400", timeout=30)
        for r in (rd.get("items") or []):
            meas = r.get("measure")
            val = _ea_num(r.get("value"))
            dt = r.get("dateTime")
            if meas is None or val is None or dt is None:
                continue
            out["series"].setdefault(meas, []).append({"t": dt, "v": val})
        # measures list summary
        for meas, pts in out["series"].items():
            pts.sort(key=lambda p: p["t"])
            # measure id string encodes parameter + qualifier; expose the tail
            tail = meas.rsplit("/", 1)[-1]
            out["measures"].append({"measure": meas, "id": tail,
                                    "n": len(pts),
                                    "latest": pts[-1]["v"] if pts else None,
                                    "latest_t": pts[-1]["t"] if pts else None})
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
    _ea_station_cache[ref] = {"data": out, "ts": time.time()}
    return out


# ---- Alert engine -----------------------------------------------------------
# Redesigned to behave more like a real control-room alarm list: multiple
# factors, each judged against the semantics a system operator actually cares
# about, then ranked by severity so the most serious condition sits at the top.
#
# Guiding rules (consistent with the dashboard's "honesty over plausibility"
# stance):
#   * Every trigger is backed by a field the snapshot genuinely carries. We do
#     not invent conditions we can't measure.
#   * Frequency is judged from the timestamped TRACE (rate-of-change + how long
#     it has dwelt outside a band), not a single instantaneous sample, because a
#     one-cycle blip is not the same as a sustained excursion — and RoCoF is the
#     first signature of a large infeed loss.
#   * Factors are considered together where the physics couples them (frequency
#     already low AND reserve thin is worse than either alone).
#   * Data we cannot see is itself an alarm: a stale frequency feed is surfaced,
#     not hidden behind the last good reading.
#
# Thresholds map to GB operational reality where possible:
#   ±0.2 Hz  operational limit (normal working band is tighter)
#   ±0.5 Hz  statutory limit
#   49.5 Hz  Low Frequency Demand Disconnection schemes begin to arm/act
# They remain named constants so they can be tuned in one place.
FREQ_NOMINAL = 50.0
FREQ_OP_LIMIT = 0.20      # Hz off nominal -> outside normal operational band
FREQ_STAT_LIMIT = 0.50    # Hz off nominal -> statutory limit
FREQ_LFDD = 49.5          # Hz -> demand-disconnection territory
FREQ_DWELL_S = 60         # must stay outside the band this long to escalate
# The public frequency feed is ~15s cadence, which CANNOT resolve true RoCoF
# (the protection-relevant rate of change happens over ~0.5-2s). What we can
# honestly measure is a sustained slew across several samples — a persistent
# trend, not an instantaneous rate. Thresholds are set for that coarser signal
# and the alert wording says so, rather than implying protection-grade RoCoF.
SLEW_WARN = 0.010         # Hz/s trend over ~30s -> notable persistent drift
SLEW_ALERT = 0.025        # Hz/s trend over ~30s -> fast persistent slew
FREQ_STALE_S = 300        # frequency feed older than this = we're flying blind

MARGIN_WARN_MW = 4000     # indicated margin below this = tightening
MARGIN_ALERT_MW = 2000    # below this = seriously tight

DEMAND_RAMP_WARN_MW = 3000  # >3 GW swing in an hour is a steep national ramp
IMPORT_SHARE_WARN = 25.0    # net imports supplying >25% of demand = concentrated
IMPORT_SHARE_ALERT = 35.0   # >35% = interconnector trip is a very large infeed
# Export-side mirror: when GB is a large NET EXPORTER, domestic generation runs
# well above demand and the interconnectors are carrying a big share of that
# surplus out. Same magnitudes as the import thresholds (share is negative when
# exporting, so we compare the magnitude).
EXPORT_SHARE_WARN = 25.0    # exports absorbing >25% of demand-equivalent
EXPORT_SHARE_ALERT = 35.0   # >35% = losing export capability sheds a large surplus

# Severity ranking so the bar is ordered worst-first, matching how an alarm
# list is triaged. Higher = more urgent.
_SEV = {"critical": 3, "warning": 2, "notice": 1, "ok": 0}

# Warning-type keywords that indicate genuine capacity/supply stress.
# Matched on WORD BOUNDARIES, not as bare substrings: a naive substring test
# fired "NISM" (Notification of Inadequate System Margin) on the "nism" inside
# "Balancing Mechanism", promoting a routine IT-outage notice to a critical
# alert. "MARGIN" likewise must not match "marginal". A trailing plural "s" is
# allowed so "DISCONNECTION" still catches "disconnections".
STRESS_KEYWORDS = ("CAPACITY MARKET", "MARGIN", "ELECTRICITY MARGIN", "NISM",
                   "NEGATIVE RESERVE", "DEMAND CONTROL", "EMERGENCY", "HIGH RISK",
                   "INADEQUATE", "DEMAND REDUCTION", "VOLTAGE REDUCTION",
                   "DISCONNECTION", "LOSS OF SUPPLY")
STRESS_PATTERNS = tuple(
    re.compile(r"\b" + re.escape(k) + r"S?\b", re.IGNORECASE)
    for k in STRESS_KEYWORDS)


def _is_stress_warning(wt, txt):
    """True if a NESO warning's type or text signals genuine capacity/supply
    stress, matching keywords on word boundaries to avoid false positives like
    'mechaNISM' or 'MARGINal'."""
    hay = f"{wt}\n{txt}"
    return any(p.search(hay) for p in STRESS_PATTERNS)


def _a(level, title, detail, tag=None):
    a = {"level": level, "title": title, "detail": detail}
    if tag:
        a["tag"] = tag
    return a


def _parse_iso(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _freq_dynamics(freq):
    """Derive rate-of-change (Hz/s) and how long the frequency has been outside
    the operational band, from the timestamped trace. Returns a dict with
    rocof_hz_s, secs_outside_band, age_s (of the latest sample) and the trend
    sign. All values best-effort: a sparse or short trace just yields None for
    the parts it can't support, which the caller treats as 'unknown, don't
    escalate on this factor'."""
    out = {"rocof_hz_s": None, "secs_outside_band": None, "age_s": None,
           "trend": None}
    pts = freq.get("trace_points") or []
    # latest-sample age
    lt = _parse_iso(freq.get("time") or "")
    if lt:
        out["age_s"] = (datetime.now(timezone.utc) - lt).total_seconds()
    # need at least two timestamped points for a rate
    parsed = [(_parse_iso(p.get("t")), p.get("hz")) for p in pts]
    parsed = [(t, hz) for t, hz in parsed if t and hz is not None]
    if len(parsed) >= 2:
        parsed.sort()
        # RoCoF over the last ~30s of samples (robust to single-sample jitter):
        # regress the tail rather than differencing two adjacent noisy points.
        tail = [p for p in parsed if (parsed[-1][0] - p[0]).total_seconds() <= 30]
        if len(tail) >= 2:
            t0 = tail[0][0]
            xs = [(t - t0).total_seconds() for t, _ in tail]
            ys = [hz for _, hz in tail]
            n = len(xs); sx = sum(xs); sy = sum(ys)
            sxx = sum(x * x for x in xs); sxy = sum(x * y for x, y in zip(xs, ys))
            denom = (n * sxx - sx * sx)
            if denom:
                slope = (n * sxy - sx * sy) / denom     # Hz per second
                out["rocof_hz_s"] = slope
                out["trend"] = 1 if slope > 0 else (-1 if slope < 0 else 0)
        # dwell: how long frequency has been continuously outside the
        # operational band, measured from the latest sample back to the most
        # recent in-band sample (i.e. the start of the current excursion). If
        # the whole trace is outside the band we can only say "at least the
        # trace length", which is still a lower bound the caller can act on.
        latest_t = parsed[-1][0]
        if abs(parsed[-1][1] - FREQ_NOMINAL) < FREQ_OP_LIMIT:
            out["secs_outside_band"] = 0.0
        else:
            excursion_start = parsed[0][0]          # assume from trace start...
            for t, hz in reversed(parsed):          # ...unless we find an in-band point
                if abs(hz - FREQ_NOMINAL) < FREQ_OP_LIMIT:
                    excursion_start = t
                    break
            out["secs_outside_band"] = (latest_t - excursion_start).total_seconds()
    return out


def build_alerts(snap):
    alerts = []

    # ---- 1. Frequency: value + rate-of-change + dwell, from the trace -------
    freq = snap.get("frequency")
    if freq and freq.get("hz") is not None:
        hz = freq["hz"]
        dev = abs(hz - FREQ_NOMINAL)
        dyn = _freq_dynamics(freq)
        rocof = dyn["rocof_hz_s"]
        dwell = dyn["secs_outside_band"]
        age = dyn["age_s"]

        # (a) feed staleness — we can't alarm on frequency we can't see
        if age is not None and age > FREQ_STALE_S:
            mins = int(age // 60)
            alerts.append(_a("warning", "Frequency feed stale",
                f"No fresh frequency reading for {mins} min (last {hz:.3f} Hz). "
                "Frequency-based alerting is running blind until the feed "
                "recovers.", tag="DATA"))

        # (b) statutory / disconnection territory — always critical
        if hz <= FREQ_LFDD or dev >= FREQ_STAT_LIMIT:
            alerts.append(_a("critical", "Frequency outside statutory limit",
                f"Grid frequency {hz:.3f} Hz ({dev:.3f} Hz off nominal). "
                "At/below 49.5 Hz automatic demand disconnection can arm. "
                "This is a severe supply/demand imbalance.", tag="FREQ"))
        # (c) sustained excursion outside the operational band
        elif dev >= FREQ_OP_LIMIT and (dwell is None or dwell >= FREQ_DWELL_S):
            held = f" held ~{int(dwell)}s" if dwell else ""
            alerts.append(_a("warning", "Sustained frequency excursion",
                f"Grid frequency {hz:.3f} Hz, {dev:.3f} Hz off nominal{held} — "
                "outside the normal operational band.", tag="FREQ"))
        # (d) brief excursion — note only, likely a transient being corrected
        elif dev >= FREQ_OP_LIMIT:
            alerts.append(_a("notice", "Brief frequency excursion",
                f"Grid frequency {hz:.3f} Hz, {dev:.3f} Hz off nominal — brief, "
                "likely a transient under correction.", tag="FREQ"))

        # (e) sustained slew — a persistent trend across samples. NOT true
        # RoCoF (the 15s feed can't resolve that); this catches a frequency
        # walking steadily away from nominal, which is worth flagging even when
        # the current value is still inside the band.
        if rocof is not None:
            a_ro = abs(rocof)
            arrow = "falling" if rocof < 0 else "rising"
            if a_ro >= SLEW_ALERT:
                alerts.append(_a("warning", "Frequency slewing",
                    f"Frequency has been {arrow} steadily (~{rocof*60:+.2f} Hz/min "
                    "trend over the last ~30s of samples). Note: the public feed "
                    "is 15s cadence, so this is a trend, not protection-grade "
                    "RoCoF.", tag="SLEW"))
            elif a_ro >= SLEW_WARN:
                alerts.append(_a("notice", "Frequency drifting",
                    f"Frequency is {arrow} gently (~{rocof*60:+.2f} Hz/min trend). "
                    "Watch for a developing imbalance.", tag="SLEW"))

    # ---- 2. Reserve vs largest infeed (N-1 security), coupled to frequency --
    r = snap.get("reserve")
    freq_low = bool(freq and freq.get("hz") is not None
                    and freq["hz"] < FREQ_NOMINAL - 0.10)
    if (r and r.get("spinning_reserve_mw") is not None and r.get("largest_infeed_mw")
            and not r.get("stale")):
        spin, infeed = r["spinning_reserve_mw"], r["largest_infeed_mw"]
        if spin < infeed:
            extra = (" Frequency is already below nominal, so a trip now would "
                     "bite immediately." if freq_low else "")
            alerts.append(_a("critical", "System not secured against largest loss",
                f"Spinning reserve {spin:,} MW is below the largest single infeed "
                f"({infeed:,} MW). A trip of that unit could not be fully covered "
                f"by running plant.{extra}", tag="N-1"))
        elif spin < infeed * 1.5:
            # thin reserve is a warning, but escalate to critical if frequency
            # is also sagging — the two together are a genuine pre-event state
            lvl = "critical" if freq_low else "warning"
            alerts.append(_a(lvl, "Thin reserve against largest loss",
                f"Spinning reserve {spin:,} MW is only {r.get('cover_ratio')}× the "
                f"largest single infeed ({infeed:,} MW)."
                + (" Compounded by below-nominal frequency." if freq_low else ""),
                tag="N-1"))
    elif r and r.get("stale"):
        alerts.append(_a("notice", "Reserve figure stale",
            "Operating-reserve calculation is using a cached value this cycle "
            "(insufficient live unit coverage).", tag="DATA"))

    # ---- 3. Capacity margin (forecast trough) -------------------------------
    m = snap.get("margin")
    if m and m.get("min_mw") is not None:
        if m["min_mw"] <= MARGIN_ALERT_MW:
            alerts.append(_a("critical", "Low capacity margin",
                f"Indicated margin drops to {m['min_mw']:,} MW around "
                f"{_hhmm(m['min_time'])}. Headroom is very tight — the window "
                "where power cuts become a risk.", tag="MARGIN"))
        elif m["min_mw"] <= MARGIN_WARN_MW:
            alerts.append(_a("warning", "Tightening margin",
                f"Indicated margin falls to {m['min_mw']:,} MW around "
                f"{_hhmm(m['min_time'])}. Watch for further tightening.",
                tag="MARGIN"))

    # ---- 4. Steep national demand ramp into a tight margin ------------------
    dem = snap.get("demand")
    if dem and dem.get("delta_1h") is not None:
        dd = dem["delta_1h"]
        margin_tight = bool(m and m.get("min_mw") is not None
                            and m["min_mw"] <= MARGIN_WARN_MW * 1.5)
        if abs(dd) >= DEMAND_RAMP_WARN_MW and margin_tight:
            direction = "rising" if dd > 0 else "falling"
            alerts.append(_a("warning", "Steep demand ramp",
                f"National demand is {direction} fast ({dd:+,} MW over the past "
                "hour) while margin is already tightening — dispatch is working "
                "hard to keep pace.", tag="DEMAND"))

    # ---- 5. Import / export dependence (interconnector concentration) -------
    ss = snap.get("supply_stack")
    if ss and ss.get("import_share_pct") is not None:
        share = ss["import_share_pct"]        # +ve = importing, -ve = exporting
        infeed = (r or {}).get("largest_infeed_mw")
        if share >= IMPORT_SHARE_ALERT:
            alerts.append(_a("warning", "High import dependence",
                f"Net imports are meeting {share:.0f}% of demand "
                f"({ss.get('net_imports_mw', 0):,} MW). Interconnector loss would "
                "be a very large single infeed to absorb.", tag="IMPORT"))
        elif share >= IMPORT_SHARE_WARN:
            alerts.append(_a("notice", "Notable import dependence",
                f"Net imports are supplying {share:.0f}% of demand "
                f"({ss.get('net_imports_mw', 0):,} MW).", tag="IMPORT"))
        elif share <= -EXPORT_SHARE_ALERT:
            exp_mw = -ss.get("net_imports_mw", 0)
            alerts.append(_a("warning", "High export level",
                f"GB is a large net exporter — exports equal {abs(share):.0f}% of "
                f"demand ({exp_mw:,} MW). Domestic generation is running well above "
                "demand; loss of export capability would leave a large surplus to "
                "shed.", tag="EXPORT"))
        elif share <= -EXPORT_SHARE_WARN:
            exp_mw = -ss.get("net_imports_mw", 0)
            alerts.append(_a("notice", "Notable net export",
                f"GB is net exporting — exports equal {abs(share):.0f}% of demand "
                f"({exp_mw:,} MW).", tag="EXPORT"))

    # ---- 6. Official NESO system warnings (layered on top) ------------------
    for w in (snap.get("warnings") or []):
        wt = (w.get("type") or "").upper()
        txt = (w.get("text") or "").upper()
        if _is_stress_warning(wt, txt):
            alerts.append(_a("critical", f"NESO warning: {w.get('type')}",
                             w.get("text") or "NESO system warning in force.",
                             tag="NESO"))

    # De-dupe by (level,title), then order worst-first for an alarm-list feel.
    seen, uniq = set(), []
    for a in alerts:
        k = (a["level"], a["title"])
        if k not in seen:
            seen.add(k); uniq.append(a)
    uniq.sort(key=lambda a: _SEV.get(a["level"], 0), reverse=True)

    # If nothing of substance fired, say so — but still surface any 'notice'
    # items (e.g. import dependence, stale caches) alongside the reassurance,
    # rather than suppressing them.
    if not any(a["level"] in ("critical", "warning") for a in uniq):
        uniq.insert(0, _a("ok", "System nominal",
            "No frequency, reserve, margin or warning triggers active. Grid "
            "operating within normal parameters.", tag="OK"))
    return uniq


def _log_alerts(alerts):
    """Journal alert state transitions to disk. Compares this cycle's active
    alerts against the set active last cycle (persisted in the log file) and
    appends a 'raised' event for anything new and a 'cleared' event (with the
    duration it was active) for anything that has gone away. Never raises — a
    logging failure must not take down the snapshot.

    On-disk shape:
        {
          "active": { "<level>|<title>": {"level","title","tag","raised"} , ...},
          "events": [ {"event":"raised"/"cleared", "level","title","tag",
                       "time", ["raised","duration_s"]}, ... ]
        }
    Events are the durable record; 'active' is bookkeeping so the next cycle can
    detect transitions across process restarts too.
    """
    try:
        now = datetime.now(timezone.utc)
        now_iso = now.isoformat()

        store = {"active": {}, "events": []}
        if ALERT_LOG.exists():
            try:
                loaded = json.loads(ALERT_LOG.read_text())
                if isinstance(loaded, dict):
                    store["active"] = loaded.get("active", {}) or {}
                    store["events"] = loaded.get("events", []) or []
            except Exception:
                pass  # corrupt/empty file: start fresh, don't lose the cycle

        prev_active = store["active"]
        # Build this cycle's active set, keyed (level|title), skipping 'ok'.
        cur_active = {}
        for a in alerts:
            if a["level"] in ALERT_LOG_SKIP_LEVELS:
                continue
            key = f"{a['level']}|{a['title']}"
            cur_active[key] = {"level": a["level"], "title": a["title"],
                               "tag": a.get("tag"), "detail": a.get("detail")}

        events = store["events"]

        # Newly raised: in current, not in previous.
        for key, a in cur_active.items():
            if key not in prev_active:
                ev = {"event": "raised", "time": now_iso,
                      "level": a["level"], "title": a["title"],
                      "tag": a.get("tag")}
                if a.get("detail"):
                    ev["detail"] = a["detail"]   # store full text for the history view
                events.append(ev)
                a["raised"] = now_iso            # stamp so we can compute duration on clear
            else:
                # carry forward the original raised time; keep the detail we
                # first logged rather than the (possibly re-worded) live one
                a["raised"] = prev_active[key].get("raised", now_iso)
                if not a.get("detail"):
                    a["detail"] = prev_active[key].get("detail")

        # Cleared: in previous, not in current.
        for key, a in prev_active.items():
            if key not in cur_active:
                raised = a.get("raised")
                dur = None
                if raised:
                    try:
                        dur = round((now - datetime.fromisoformat(raised)).total_seconds())
                    except Exception:
                        dur = None
                ev = {"event": "cleared", "time": now_iso,
                      "level": a["level"], "title": a["title"],
                      "tag": a.get("tag"), "raised": raised}
                if a.get("detail"):
                    ev["detail"] = a["detail"]
                if dur is not None:
                    ev["duration_s"] = dur
                events.append(ev)

        # Prune events older than the retention window.
        cutoff = now - timedelta(days=ALERT_LOG_DAYS)
        pruned = []
        for ev in events:
            try:
                if datetime.fromisoformat(ev["time"]) >= cutoff:
                    pruned.append(ev)
            except Exception:
                pruned.append(ev)  # keep unparseable rather than silently drop

        store = {"active": cur_active, "events": pruned}
        tmp = ALERT_LOG.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(store))
        tmp.replace(ALERT_LOG)   # atomic-ish: write to temp then rename
    except Exception:
        pass


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    n = len(xs)
    if n == 0:
        return None
    m = n // 2
    return xs[m] if n % 2 else (xs[m - 1] + xs[m]) / 2


def alert_history_stats(days=None):
    """Aggregate the alert event log into per-tag and per-title statistics:
    fire counts (number of 'raised' events) and median active durations (from
    'cleared' events that carry duration_s). Optionally restrict to the last
    `days`. Never raises.

    Returns:
      {
        "window_days": int|None,
        "total_raised": int, "total_cleared": int, "currently_active": int,
        "first_event": iso|None, "last_event": iso|None,
        "by_tag": [ {tag, level, raised, cleared, median_duration_s,
                     max_duration_s, longest_title} ... ],   # worst/most first
        "by_title": [ {title, tag, level, raised, cleared,
                       median_duration_s, last_raised} ... ],
      }
    """
    out = {"window_days": days, "total_raised": 0, "total_cleared": 0,
           "currently_active": 0, "first_event": None, "last_event": None,
           "by_tag": [], "by_title": [], "error": None}
    try:
        loaded = json.loads(ALERT_LOG.read_text())
    except Exception:
        # No log yet, or unreadable — return a valid empty stats object.
        return out
    if not isinstance(loaded, dict):
        return out
    events = loaded.get("events", []) or []
    active = loaded.get("active", {}) or {}
    out["currently_active"] = len(active)

    if days:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        kept = []
        for e in events:
            t = _parse_iso(e.get("time") or "")
            if t is None or t >= cutoff:
                kept.append(e)
        events = kept

    if events:
        times = sorted(e.get("time", "") for e in events if e.get("time"))
        if times:
            out["first_event"], out["last_event"] = times[0], times[-1]

    # accumulate per tag and per title
    tag_acc = {}      # tag -> {level, raised, cleared, durs:[], titles:set}
    title_acc = {}    # (title) -> {tag, level, raised, cleared, durs:[], last_raised}
    for e in events:
        ev = e.get("event")
        tag = e.get("tag") or "—"
        title = e.get("title") or "(untitled)"
        level = e.get("level")
        ta = tag_acc.setdefault(tag, {"level": level, "raised": 0, "cleared": 0,
                                      "durs": [], "titles": {}})
        tia = title_acc.setdefault(title, {"tag": tag, "level": level,
                                           "raised": 0, "cleared": 0, "durs": [],
                                           "last_raised": None})
        # keep the most severe level seen for the tag
        if level and (_SEV.get(level, 0) > _SEV.get(ta["level"], 0)):
            ta["level"] = level
        if ev == "raised":
            out["total_raised"] += 1
            ta["raised"] += 1
            tia["raised"] += 1
            if not tia["last_raised"] or (e.get("time") or "") > tia["last_raised"]:
                tia["last_raised"] = e.get("time")
        elif ev == "cleared":
            out["total_cleared"] += 1
            ta["cleared"] += 1
            tia["cleared"] += 1
            d = e.get("duration_s")
            if isinstance(d, (int, float)):
                ta["durs"].append(d)
                tia["durs"].append(d)
                ta["titles"][title] = max(ta["titles"].get(title, 0), d)

    for tag, a in tag_acc.items():
        longest_title = None
        if a["titles"]:
            longest_title = max(a["titles"].items(), key=lambda kv: kv[1])[0]
        out["by_tag"].append({
            "tag": tag, "level": a["level"], "raised": a["raised"],
            "cleared": a["cleared"],
            "median_duration_s": _median(a["durs"]),
            "max_duration_s": max(a["durs"]) if a["durs"] else None,
            "longest_title": longest_title,
        })
    for title, a in title_acc.items():
        out["by_title"].append({
            "title": title, "tag": a["tag"], "level": a["level"],
            "raised": a["raised"], "cleared": a["cleared"],
            "median_duration_s": _median(a["durs"]),
            "last_raised": a["last_raised"],
        })
    # order: most-fired first, then by severity
    out["by_tag"].sort(key=lambda x: (-x["raised"], -_SEV.get(x["level"], 0)))
    out["by_title"].sort(key=lambda x: (-x["raised"], -_SEV.get(x["level"], 0)))
    return out


def read_alert_history(limit=200, level=None):
    """Return the most recent alert events (newest first), optionally filtered
    by level. Used by the /api/alert-history endpoint. Never raises."""
    try:
        loaded = json.loads(ALERT_LOG.read_text())
        events = loaded.get("events", []) if isinstance(loaded, dict) else []
    except Exception:
        events = []
    if level:
        events = [e for e in events if e.get("level") == level]
    events = sorted(events, key=lambda e: e.get("time", ""), reverse=True)
    return events[:limit]


def _hhmm(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%H:%M UTC %d %b")
    except Exception:
        return iso


# Remembers the last successful reading of volatile sources so a single failed
# fetch doesn't blank the panel.
_last_good = {}

# Snapshot build tuning. Sources are fetched in parallel (each already caches
# its own result, so most builds are cheap); the overall deadline caps how long
# a single build can block the /api/grid request even if an upstream hangs.
SNAP_BUILD_DEADLINE = 25       # seconds; return partial after this
SNAP_POOL_WORKERS = 8
# One shared pool so background workers from a timed-out build can still finish
# and warm their caches for the next request rather than being cancelled.
_snap_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=SNAP_POOL_WORKERS, thread_name_prefix="snap")

SNAP_SOURCES = [
    ("generation", "get_generation"), ("frequency", "get_frequency"),
    ("demand", "get_demand"), ("margin", "get_margin"),
    ("warnings", "get_warnings"), ("carbon", "get_carbon"),
    ("weather", "get_weather"), ("reserve", "get_operating_reserve"),
    ("solar", "get_solar"), ("price", "get_price"),
    ("battery", "get_battery"),
]


def build_snapshot():
    snap = {"generated": datetime.now(timezone.utc).isoformat(),
            "backend_version": "2026-08-07a",   # bump when adding data fields
            "server_build": SERVER_BUILD,       # shown in the dashboard footer
            "features": ["solar", "freq_trace_points", "weather_batch",
                         "operating_reserve", "supply_stack", "weather_openweather",
                         "weather_resource_sites", "generator_units"],
            "sources_ok": {}, "errors": []}

    # Fetch all sources in parallel. Each has its own cache and timeouts; the
    # per-host throttle in fetch_json keeps parallelism from hammering any one
    # upstream. We wait only up to SNAP_BUILD_DEADLINE, then return a PARTIAL
    # snapshot marking any laggards — their futures keep running in the shared
    # pool and warm their caches for the next request.
    futures = {_snap_pool.submit(globals()[fn]): key for key, fn in SNAP_SOURCES}
    deadline = time.monotonic() + SNAP_BUILD_DEADLINE
    pending = set(futures)
    for key, _ in SNAP_SOURCES:
        snap[key] = None
        snap["sources_ok"][key] = False
    try:
        for fut in concurrent.futures.as_completed(
                futures, timeout=SNAP_BUILD_DEADLINE):
            key = futures[fut]
            pending.discard(fut)
            try:
                val = fut.result()
                snap[key] = val
                snap["sources_ok"][key] = val is not None
            except Exception as e:
                snap["errors"].append(f"{key}: {_ea_errstr(e) if 'HTTPError' in type(e).__name__ else e}")
    except concurrent.futures.TimeoutError:
        pass   # deadline hit — whatever's still pending is reported below
    # Anything not done by the deadline is a soft timeout, not a hard failure:
    # leave its value None (post-processing below falls back to last-good where
    # it can) and record it so the UI can show which source lagged this cycle.
    for fut in pending:
        if not fut.done():
            key = futures[fut]
            snap["errors"].append(f"{key}: timed out (> {SNAP_BUILD_DEADLINE}s this cycle)")

    # Solar isn't in FUELINST; splice the PVLive estimate into the fuel list so
    # the generation mix reflects it (as Gridwatch does). Marked estimated.
    gen = snap.get("generation")
    solar = snap.get("solar")
    if gen and solar and solar.get("mw"):
        # replace any zero SOLAR placeholder, else append
        fuels = gen["fuels"]
        existing = next((f for f in fuels if f["code"] == "SOLAR"), None)
        if existing:
            existing["mw"] = solar["mw"]
            existing["estimated"] = True
        else:
            name, cat, colour = FUEL_META.get("SOLAR", ("Solar", "renewable", "#f5c542"))
            fuels.append({"code": "SOLAR", "name": name, "category": cat,
                          "colour": colour, "mw": solar["mw"], "delta_1h": None,
                          "estimated": True})
        fuels.sort(key=lambda f: f["mw"], reverse=True)
        gen["generation_total_mw"] = round(gen["generation_total_mw"] + solar["mw"])
        gen["solar_estimated"] = True

    # Battery isn't in FUELINST either; splice net battery output (from PN, via
    # the Terravolt classification) as a two-way 'storage' source like pumped
    # storage. Positive = discharging (supply), negative = charging (demand).
    battery = snap.get("battery")
    if gen and battery and battery.get("net_mw") is not None:
        fuels = gen["fuels"]
        name, cat, colour = FUEL_META.get("BATTERY", ("Battery", "storage", "#e0b0ff"))
        existing = next((f for f in fuels if f["code"] == "BATTERY"), None)
        entry = {"code": "BATTERY", "name": name, "category": cat, "colour": colour,
                 "mw": battery["net_mw"], "delta_1h": None,
                 "discharge_mw": battery.get("discharge_mw"),
                 "charge_mw": battery.get("charge_mw"), "two_way": True}
        if existing:
            existing.update(entry)
        else:
            fuels.append(entry)
        fuels.sort(key=lambda f: f["mw"], reverse=True)
        # only positive (discharge) contributes to generation total, matching
        # how pumped storage discharge counts as supply
        if battery["net_mw"] > 0:
            gen["generation_total_mw"] = round(gen["generation_total_mw"] + battery["net_mw"])
        gen["battery_included"] = True

    # Derived: demand vs generation, renewable share
    if gen:
        renew = sum(f["mw"] for f in gen["fuels"]
                    if f["category"] == "renewable" and f["mw"] > 0)
        low_carbon = sum(f["mw"] for f in gen["fuels"]
                         if (f["category"] == "renewable" or f["code"] == "NUCLEAR")
                         and f["mw"] > 0)
        tot = gen["generation_total_mw"] or 1
        snap["renewable_pct"] = round(100 * renew / tot, 1)
        snap["low_carbon_pct"] = round(100 * low_carbon / tot, 1)

    # Weather: if this cycle failed, fall back to the last good reading so the
    # panel doesn't blank out on a single transient failure. But preserve the
    # key-status flags (needs_key/bad_key) so the dashboard can still prompt.
    wx = snap.get("weather")
    if (not wx) or wx.get("error") or wx.get("avg_wind_100m_ms") is None:
        if _last_good.get("weather"):
            stale = dict(_last_good["weather"])
            stale["stale"] = True
            if wx and wx.get("error"):
                stale["error"] = wx["error"]
            for flag in ("needs_key", "bad_key"):
                if wx and wx.get(flag):
                    stale[flag] = wx[flag]
            snap["weather"] = stale
            snap["sources_ok"]["weather"] = True
    else:
        _last_good["weather"] = wx     # remember this good reading

    # Inferred: does actual wind output match the wind resource?
    wx = snap.get("weather")
    if gen and wx and wx.get("avg_wind_100m_ms") is not None:
        wind_mw = next((f["mw"] for f in gen["fuels"] if f["code"] == "WIND"), None)
        wind_d = next((f.get("delta_1h") for f in gen["fuels"] if f["code"] == "WIND"), None)
        note, w = None, wx["avg_wind_100m_ms"]
        if wind_mw is not None:
            if w >= 10 and wind_mw < 6000 and (wind_d is not None and wind_d <= 0):
                note = ("Strong hub-height wind but metered wind output is flat/low — "
                        "possible curtailment or transmission constraint.")
            elif w < 5:
                note = "Low wind resource — expect wind generation to stay subdued."
        snap["wind_context"] = {"metered_wind_mw": wind_mw,
                                "avg_wind_100m_ms": w, "note": note}

    # Supply stack: how demand is actually being met right now, imports included.
    # domestic generation + net interconnector imports + net storage ≈ demand.
    # This makes imports an explicit, first-class part of supply (question: "do
    # the plots account for imports?" — here, yes, as their own band).
    m, dem = snap.get("margin"), snap.get("demand")
    if gen and dem and dem.get("transmission_mw"):
        demand_mw = dem["transmission_mw"]
        fuels = gen["fuels"]
        # Transmission-metered balance excludes embedded solar (it isn't on the
        # transmission system — it shows up as reduced demand, not as supply).
        domestic = sum(f["mw"] for f in fuels
                       if f["category"] not in ("interconnector", "storage")
                       and not f.get("estimated") and f["mw"] > 0)
        imports = sum(f["mw"] for f in fuels
                      if f["category"] == "interconnector" and f["mw"] > 0)
        exports = -sum(f["mw"] for f in fuels
                       if f["category"] == "interconnector" and f["mw"] < 0)   # positive number
        storage = sum(f["mw"] for f in fuels if f["category"] == "storage")     # +discharge/-charge
        net_imports = imports - exports
        total_supply = domestic + net_imports + max(storage, 0)
        embedded_solar = snap.get("solar", {}).get("mw") if snap.get("solar") else None
        snap["supply_stack"] = {
            "demand_mw": round(demand_mw),
            "domestic_mw": round(domestic),
            "imports_mw": round(imports),
            "exports_mw": round(exports),
            "net_imports_mw": round(net_imports),
            "storage_mw": round(storage),
            "total_supply_mw": round(total_supply),
            "import_share_pct": round(100 * net_imports / demand_mw, 1) if demand_mw else None,
            "embedded_solar_mw": embedded_solar,   # served behind the meter, not in transmission balance
        }

        # Reconstructed "national consumption" — a Gridwatch-comparable estimate
        # of what the country is actually using, versus the transmission-metered
        # figure. Gridwatch's demand adds embedded (distribution-connected)
        # generation and net imports back onto transmission demand. We can add
        # embedded SOLAR (from PVLive) and net imports, but embedded WIND is not
        # available from Elexon/PVLive alone, so this will still read slightly
        # BELOW Gridwatch on windy days. Labelled an estimate and its exclusions
        # are stated so it isn't mistaken for a metered value.
        recon = demand_mw + (embedded_solar or 0) + max(net_imports, 0)
        snap["national_consumption"] = {
            "estimate_mw": round(recon),
            "basis_mw": round(demand_mw),                 # transmission demand we start from
            "added_solar_mw": round(embedded_solar or 0),
            "added_net_imports_mw": round(max(net_imports, 0)),
            "includes_embedded_wind": False,              # not available from current feeds
            "estimated": True,
        }
    # Spare headroom (theoretical): MELNGC is the sum of unit Maximum Export
    # Limits minus demand — i.e. capacity that COULD be dispatched, not what is
    # running. Kept separate and clearly labelled so it isn't confused with the
    # live supply stack above.
    if m:
        snap["spare_headroom"] = {
            "margin_mw": m.get("current_mw"),
            "min_mw": m.get("min_mw"),
            "min_time": m.get("min_time"),
            "delta_1h": m.get("ahead_delta_1h"),
            "basis": "sum of unit Maximum Export Limits minus demand (theoretical)",
        }
    # Log current margin to disk and attach the accumulated -24h history so the
    # dashboard can show past-and-forecast around a "now" line.
    hist = _log_margin_history(m)
    if m is not None:
        m["history"] = [{"time": t, "margin_mw": v} for t, v in hist]
    snap["alerts"] = build_alerts(snap)
    _log_alerts(snap["alerts"])
    snap["alert_level"] = ("critical" if any(a["level"] == "critical" for a in snap["alerts"])
                           else "warning" if any(a["level"] == "warning" for a in snap["alerts"])
                           else "ok")
    return snap


# ---- HTTP server ------------------------------------------------------------
HTML_PATH = Path(__file__).with_name("grid_dashboard.html")
_cache = {"snap": None, "ts": 0}
CACHE_TTL = 55  # seconds


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def handle_one_request(self):
        # A browser closing the socket mid-response (refresh, tab close, the
        # 60s auto-refresh superseding an in-flight request) raises a connection
        # error here or in wfile.write. On Windows that's ConnectionAbortedError
        # (WinError 10053); on Unix, BrokenPipeError. These are expected, not
        # faults — swallow them quietly instead of letting ThreadingHTTPServer
        # print a full traceback for a dead client.
        try:
            super().handle_one_request()
        except (ConnectionError, BrokenPipeError):
            self.close_connection = True

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            if self.path.startswith("/api/weather-key"):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    payload = {}
                key = (payload.get("key") or "").strip()
                if not key:
                    self._send_json({"ok": False, "error": "empty key"}, 400)
                    return
                _save_weather_key(key)
                # Invalidate the weather cache + backoff so the next snapshot
                # refetches immediately with the new key.
                _weather_cache.update({"ts": 0, "backoff_until": 0,
                                       "fails": 0, "err": None})
                _cache["ts"] = 0     # force a fresh snapshot too
                self._send_json({"ok": True})
            elif self.path.startswith("/api/octopus-config"):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    payload = {}
                # accept known fields only; the API key + meter IDs + tariff rates
                allowed = {"api_key", "account_number", "gas_account_number",
                           "payment_method", "billing_end_day", "elec_mpan", "elec_serial",
                           "gas_mprn", "gas_serial", "elec_unit_p", "elec_standing_p",
                           "gas_unit_p", "gas_standing_p", "gas_units"}
                cfg = {k: payload[k] for k in allowed if k in payload}
                if not cfg.get("api_key") and not _octopus_has_config():
                    self._send_json({"ok": False, "error": "api key required"}, 400)
                    return
                _save_octopus_cfg(cfg)
                self._send_json({"ok": True})
            else:
                self.send_error(404)
        except (ConnectionError, BrokenPipeError):
            self.close_connection = True

    def do_GET(self):
        try:
            self._route()
        except (ConnectionError, BrokenPipeError):
            # Client went away mid-write — nothing to serve, nothing to log.
            self.close_connection = True

    def _route(self):
        if self.path.startswith("/api/weather-key"):
            # Status only — never returns the key itself, just whether one is set.
            self._send_json({"has_key": bool(_load_weather_key())})
        elif self.path.startswith("/api/octopus-carpet"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                fuel = qs.get("fuel", ["electricity"])[0]
                msel = qs.get("months", [""])[0]
                months = [m for m in msel.split(",") if m] or None
                interp = qs.get("interp", ["1"])[0] != "0"
                scfill = qs.get("scfill", ["1"])[0] != "0"
                self._send_json(get_octopus_carpet(months, fuel, interp, scfill))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif self.path.startswith("/api/octopus-config"):
            cfg = _load_octopus_cfg() or {}
            self._send_json({
                "has_config": _octopus_has_config(),
                "has_key": bool(cfg.get("api_key")),
                "account_number": cfg.get("account_number", ""),
                "gas_account_number": cfg.get("gas_account_number", ""),
                "payment_method": cfg.get("payment_method", "DIRECT_DEBIT"),
                "billing_end_day": cfg.get("billing_end_day", ""),
                "elec_mpan": cfg.get("elec_mpan", ""),
                "elec_serial": cfg.get("elec_serial", ""),
                "gas_mprn": cfg.get("gas_mprn", ""),
                "gas_serial": cfg.get("gas_serial", ""),
                "elec_unit_p": cfg.get("elec_unit_p", ""),
                "elec_standing_p": cfg.get("elec_standing_p", ""),
                "gas_unit_p": cfg.get("gas_unit_p", ""),
                "gas_standing_p": cfg.get("gas_standing_p", ""),
                # Report the raw stored value; empty means unset (the resolver
                # treats unset as kWh-but-unconfirmed). This matches the actual
                # runtime behaviour rather than the old misleading "auto".
                "gas_units": cfg.get("gas_units", ""),
            })
        elif self.path.startswith("/api/octopus"):
            # On-demand home consumption data (the pop-out fetches when opened).
            try:
                self._send_json(get_octopus())
            except Exception as e:
                self._send_json({"needs_config": False,
                                 "errors": [f"{type(e).__name__}: {e}"]}, 500)
        elif self.path.startswith("/api/frequency"):
            # Lightweight, phase-learned frequency feed for the fast dial poll.
            # Cheap between due times (serves cache); only hits upstream when a
            # new 15s sample is expected. Keeps the rest of the page on 60s.
            try:
                self._send_json(get_frequency_fast() or {"error": "no frequency data"})
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}"}, 500)
        elif self.path.startswith("/api/units"):
            # Generator drill-down data, served on demand (the pop-out fetches
            # this only when opened, so it doesn't bloat the 60s snapshot).
            try:
                self._send_json(get_units())
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}", "stations": []}, 500)
        elif self.path.startswith("/api/gas"):
            try:
                self._send_json(get_gas())
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}",
                                 "supply_sources": []}, 500)
        elif self.path.startswith("/api/geocode"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                q = qs.get("q", [""])[0]
                self._send_json(geocode(q))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}",
                                 "matches": []}, 500)
        elif self.path.startswith("/api/ea-station"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                ref = qs.get("ref", [None])[0]
                self._send_json(get_ea_station(ref))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}",
                                 "series": {}}, 500)
        elif self.path.startswith("/api/ea-floods"):
            try:
                self._send_json(get_ea_floods())
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}",
                                 "warnings": []}, 500)
        elif self.path.startswith("/api/ea"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                lat = qs.get("lat", [None])[0]
                lon = qs.get("lon", [None])[0]
                dist = qs.get("dist", [None])[0]
                rain_only = qs.get("rain", ["0"])[0] in ("1", "true", "yes")
                self._send_json(get_ea(
                    float(lat) if lat else None,
                    float(lon) if lon else None,
                    float(dist) if dist else None,
                    rain_only=rain_only))
            except Exception as e:
                # get_ea is internally resilient (returns partial data with a
                # soft 'error' field); this outer guard only trips on a total
                # failure. Serve empty-but-shaped data, not a bare 500 blank.
                self._send_json({"error": f"{type(e).__name__}: {e}",
                                 "stations": [], "rainfall": [], "floods": None,
                                 "partial": True}, 200)
        elif self.path.startswith("/api/alert-stats"):
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                days = qs.get("days", [None])[0]
                self._send_json(alert_history_stats(int(days) if days else None))
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}",
                                 "by_tag": [], "by_title": []}, 500)
        elif self.path.startswith("/api/alert-history"):
            # Journalled alert raised/cleared events, newest first. Optional
            # ?limit= and ?level= query params. Read-only view of ALERT_LOG.
            try:
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                limit = int(qs.get("limit", ["200"])[0])
                level = qs.get("level", [None])[0]
                self._send_json({"events": read_alert_history(limit=limit, level=level)})
            except Exception as e:
                self._send_json({"error": f"{type(e).__name__}: {e}", "events": []}, 500)
        elif self.path.startswith("/api/grid"):
            now = time.time()
            if not _cache["snap"] or now - _cache["ts"] > CACHE_TTL:
                _cache["snap"] = build_snapshot()
                _cache["ts"] = now
            body = json.dumps(_cache["snap"]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path in ("/", "/index.html", "/grid_dashboard.html"):
            if HTML_PATH.exists():
                body = HTML_PATH.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                # The dashboard file changes often during development; without
                # this a browser can serve a stale cached copy after the file is
                # updated (e.g. wiring that "used to work" appears broken).
                self.send_header("Cache-Control", "no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404, "grid_dashboard.html not found next to server")
        else:
            self.send_error(404)


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8412)
    ap.add_argument("--once", action="store_true", help="write snapshot.json and exit")
    ap.add_argument("--debug", action="store_true",
                    help="verbose per-fetch logging to stderr (URL, status, timing, "
                         "error body); also enabled via GRIDMON_DEBUG=1")
    args = ap.parse_args()
    if args.debug:
        DEBUG = True

    if args.once:
        snap = build_snapshot()
        Path("snapshot.json").write_text(json.dumps(snap, indent=2))
        print(json.dumps(snap["alerts"], indent=2))
        print(f"\nWrote snapshot.json  (alert level: {snap['alert_level']})")
        return

    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"GB Energy Monitor running: http://localhost:{args.port}")
    if DEBUG:
        print("Debug logging ON (per-fetch diagnostics to stderr).")
    print("Ctrl-C to stop.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
