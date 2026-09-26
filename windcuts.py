# windcuts.py — wind vs power-cuts correlation tracker for GB Energy Monitor.
#
# Accumulates, over time, the relationship between wind (METAR gusts, per region) and the
# UNPLANNED power-cut arrival rate, so the dashboard can show a learned response curve and a
# live "given the current gust, expect roughly N ..." prediction.
#
# Design (agreed):
#   * Response = the FLOW, not the stock. We correlate the rate of NEW unplanned cuts per
#     hour, because wind causes faults; the standing customers-off level lags through
#     restoration. Two metrics are kept per region:
#         faults  — new unplanned INCIDENTS/hr   (every operator, incl. SPEN → SPEN native)
#         off     — new unplanned CUSTOMERS-off/hr (SPEN contributes 0, no counts → excluded)
#   * Regionalised: each incident is placed in its licence area + country (point-in-polygon)
#     so an isolated storm only drives its own region's curve, never the whole country.
#   * Lag: a region's samples are binned by the TRAILING-MAX gust over ~1 h, not the instant.
#   * Honest accumulation: 5-min samples are autocorrelated, so we keep running per-(region,
#     gust-bin) sufficient statistics (n, Σ, Σ²) → mean ± spread per band, O(1) to update and
#     robust to the mass of calm samples. Calm (zero-rate) samples ARE logged — they define
#     the baseline, so a background fault level can't masquerade as a low-wind peak.
#
# Storage (all beside this module):
#   windcuts_binstats.json  running (n, Σ, Σ²) per region × metric × gust-bin  (the model)
#   windcuts_state.json     per-region last incident-ids, last ts, recent-gust rings, start
#   windcuts-YYYY-MM.jsonl   append-only corpus (audit / re-binning)
#
# Build 260926.1  (version = YYMMDD.N in UT; bump on every change to this file)
# Change history: CHANGELOG.md
#
# Python 3, stdlib only. Uses metar.py for wind.

from __future__ import annotations
import json, os, time, math, datetime, threading

import metar

_DIR = os.path.dirname(os.path.abspath(__file__))
BINSTATS_FILE = os.path.join(_DIR, "windcuts_binstats.json")
STATE_FILE = os.path.join(_DIR, "windcuts_state.json")
HIST_PREFIX = os.path.join(_DIR, "windcuts-")

BIN_KT = 5                 # gust bin width (knots)
GUST_RING_S = 3600         # trailing window for the max-gust used to bin (1 h, lag-aware)
MAX_INTERVAL_S = 1800      # samples farther apart than this are a gap → reseed, don't bin
MIN_N_LOW = 3              # bin sample count: below this = "learning"
MIN_N_GOOD = 8             #                    at/above this = "good" confidence

# Operators whose whole footprint is a single licence area + country. Their outages are
# definitionally in that region, so the operator identity — not the postcode geocode —
# fixes the region: an NIE fault is in Northern Ireland even if its postcode never resolved
# to a coordinate, so it never falls through to ALL-only ("unknown"). Names must match
# region_polys.json exactly.  {dno: (area, country)}
OP_REGION = {
    "NIE": ("Northern Ireland", "Northern Ireland"),
    "ESB": ("Republic of Ireland", "Republic of Ireland"),
}

_lock = threading.Lock()


# ───────────────────────── small json helpers ───────────────────────────────
def _load(path, default):
    try:
        return json.loads(open(path, encoding="utf-8").read())
    except Exception:
        return default


def _save(path, obj):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def _hist_path(ts):
    m = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m")
    return HIST_PREFIX + m + ".jsonl"


# ───────────────────────── running bin statistics ───────────────────────────
# binstats[regionKey][metric][binStr] = [n, sum, sumsq]
def _bin_key(gust):
    return str(int(gust // BIN_KT) * BIN_KT)     # lower edge of the 5-kt band, as a string


def _accum(binstats, region, metric, gust, rate):
    d = binstats.setdefault(region, {}).setdefault(metric, {})
    b = _bin_key(gust)
    cell = d.get(b)
    if cell is None:
        cell = d[b] = [0, 0.0, 0.0]
    cell[0] += 1
    cell[1] += rate
    cell[2] += rate * rate


# ───────────────────────── observe one sample ───────────────────────────────
def _region_keys_for(area, country):
    keys = ["ALL"]
    if country:
        keys.append("country:" + country)
    if area:
        keys.append("area:" + area)
    return keys


def observe(pc_snapshot, now=None):
    """Join the current wind to this power-cuts snapshot and fold it into the running model.
    Called once per powercuts sample. Never raises."""
    try:
        with _lock:
            now = now or pc_snapshot.get("generated") or time.time()
            st = metar.stations()
            wind = metar.live(st)                       # region key -> {gust_kt,...}
            state = _load(STATE_FILE, {})
            binstats = _load(BINSTATS_FILE, {})
            if "start_ts" not in state:
                state["start_ts"] = now
            last_ts = state.get("last_ts")
            rings = state.get("gust_rings", {})         # region -> [[ts,gust],...]
            last_ids = state.get("last_ids", {})        # region -> [ids]

            # 1) current unplanned incidents grouped by region key
            cur_ids = {}          # region -> set(ids)
            cur_off = {}          # region -> {id: customers_off}
            for i in (pc_snapshot.get("incidents") or []):
                if i.get("planned"):
                    continue                            # weather correlation is unplanned only
                iid = i.get("id")
                if not iid:
                    continue
                oa, oc = OP_REGION.get(i.get("dno"), (None, None))
                if oc:
                    # single-area operator (NIE/ESB): region is fixed by the operator, so an
                    # unresolved postcode still counts to its country + area, not "unknown"
                    area, country = oa, oc
                else:
                    lat, lon = i.get("lat"), i.get("lon")
                    if lat is None or lon is None:
                        area, country = None, None
                    else:
                        area, country = metar.classify(lat, lon)
                for k in _region_keys_for(area, country):
                    cur_ids.setdefault(k, set()).add(iid)
                    cur_off.setdefault(k, {})[iid] = (i.get("customers_off") or 0)

            # 2) update trailing gust rings, compute per-region binning gust (trailing max)
            tgust = {}
            allkeys = set(rings) | set(cur_ids) | set(wind)
            for k in allkeys:
                g = (wind.get(k) or {}).get("gust_kt")
                ring = [p for p in rings.get(k, []) if now - p[0] <= GUST_RING_S]
                if g is not None:
                    ring.append([now, g])
                rings[k] = ring
                gs = [p[1] for p in ring]
                tgust[k] = max(gs) if gs else None

            # 3) new (unplanned) cuts per region since last sample → rate → bin
            interval_h = ((now - last_ts) / 3600.0) if last_ts else None
            row = {"ts": now, "iso": _iso(now), "regions": {}}
            can_bin = interval_h is not None and 0 < (now - last_ts) <= MAX_INTERVAL_S
            for k in allkeys:
                cur = cur_ids.get(k, set())
                prev = set(last_ids.get(k, []))
                new_ids = cur - prev
                new_faults = len(new_ids)
                new_off = sum(cur_off.get(k, {}).get(i, 0) for i in new_ids)
                g = tgust.get(k)
                if can_bin and g is not None:
                    _accum(binstats, k, "faults", g, new_faults / interval_h)
                    _accum(binstats, k, "off",    g, new_off / interval_h)
                # corpus row (compact: only regions doing something or windy)
                if new_faults or (g and g >= 20) or cur:
                    row["regions"][k] = {"g": round(g, 1) if g is not None else None,
                                         "nf": new_faults, "noff": new_off, "act": len(cur)}

            # 4) persist
            state["last_ts"] = now
            state["last_ids"] = {k: sorted(v) for k, v in cur_ids.items()}
            state["gust_rings"] = rings
            state["samples"] = state.get("samples", 0) + 1
            _save(STATE_FILE, state)
            _save(BINSTATS_FILE, binstats)
            try:
                with open(_hist_path(now), "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row) + "\n")
            except Exception:
                pass
            return True
    except Exception:
        return False


def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ───────────────────────── model + prediction (read) ────────────────────────
def _curve(binstats, region, metric):
    """Return sorted bins [{g, mean, sd, n}] for a region+metric, plus baseline (mean of the
    lowest populated bin) and the total sample count."""
    d = (binstats.get(region) or {}).get(metric) or {}
    bins = []
    ntot = 0
    for b, (n, s, ss) in sorted(((int(k), v) for k, v in d.items())):
        if n <= 0:
            continue
        mean = s / n
        var = max(0.0, ss / n - mean * mean)
        bins.append({"g": b, "mean": round(mean, 3), "sd": round(math.sqrt(var), 3), "n": n})
        ntot += n
    baseline = bins[0]["mean"] if bins else 0.0
    return bins, baseline, ntot


def predict(region, gust, metric="faults", binstats=None):
    """Given a live gust, read the expected rate off the accumulated curve for this region."""
    if gust is None:
        return None
    binstats = binstats if binstats is not None else _load(BINSTATS_FILE, {})
    bins, baseline, _ = _curve(binstats, region, metric)
    if not bins:
        return {"gust": gust, "expected": None, "conf": "learning"}
    b = int(gust // BIN_KT) * BIN_KT
    # nearest populated bin at or below the gust; else the lowest we have
    cand = [x for x in bins if x["g"] <= b] or bins
    hit = cand[-1]
    conf = "good" if hit["n"] >= MIN_N_GOOD else ("low" if hit["n"] >= MIN_N_LOW else "learning")
    return {"gust": gust, "bin": hit["g"], "expected": hit["mean"],
            "lo": round(max(0.0, hit["mean"] - hit["sd"]), 3),
            "hi": round(hit["mean"] + hit["sd"], 3),
            "n": hit["n"], "baseline": round(baseline, 3), "conf": conf}


def snapshot(regions=None):
    """Everything the dashboard's wind-vs-cuts panel needs: live wind per region, the learned
    curves + baselines, a live prediction per region, and a maturity summary."""
    binstats = _load(BINSTATS_FILE, {})
    state = _load(STATE_FILE, {})
    st = metar.stations()
    wind = metar.live(st)
    keys = regions or (["ALL"]
                       + sorted(k for k in wind if k.startswith("country:"))
                       + sorted(k for k in wind if k.startswith("area:")))
    out = {"generated": time.time(), "bin_kt": BIN_KT, "metrics": ["faults", "off"],
           "regions": {}, "wind": wind, "metar": metar.status()}
    for k in keys:
        g = (wind.get(k) or {}).get("gust_kt")
        entry = {"wind": wind.get(k) or {}, "curves": {}, "predict": {}}
        for m in ("faults", "off"):
            bins, base, ntot = _curve(binstats, k, m)
            entry["curves"][m] = {"bins": bins, "baseline": round(base, 3), "n": ntot}
            entry["predict"][m] = predict(k, g, m, binstats)
        out["regions"][k] = entry
    start = state.get("start_ts")
    out["maturity"] = {
        "samples": state.get("samples", 0),
        "span_days": round((time.time() - start) / 86400.0, 2) if start else 0,
        "start_iso": _iso(start) if start else None,
    }
    return out


if __name__ == "__main__":
    # Realistic self-test: gusts vary SLOWLY (a random walk with occasional storms), and the
    # unplanned-cut arrival rate rises steeply above ~30 kt on top of a small calm baseline.
    # Verifies the learned curve rises with wind and the low-wind bins sit near baseline.
    import random
    for p in (BINSTATS_FILE, STATE_FILE):
        try:
            os.remove(p)
        except OSError:
            pass
    random.seed(3)
    base_ts = time.time() - 4000 * 300

    class FakeMetar:
        g = 10.0
        def stations(self):
            return [{"id": "X", "lat": 52.5, "lon": -1.5, "wspd_kt": self.g, "wgst_kt": self.g,
                     "gust_kt": self.g, "obs": 0, "area": "West Midlands", "country": "England"}]
        def live(self, st=None):
            v = {"gust_kt": self.g, "wind_kt": self.g, "n_stations": 1}
            return {"ALL": dict(v), "country:England": dict(v), "area:West Midlands": dict(v)}
        def classify(self, lat, lon):
            return "West Midlands", "England"
        def status(self):
            return {"stations": 1, "age_s": 0, "error": None}
    fm = FakeMetar()
    metar.stations = fm.stations; metar.live = fm.live
    metar.classify = fm.classify; metar.status = fm.status

    nid = 0
    g = 12.0
    for i in range(4000):
        # slow gust random walk, clamped; occasional storm surges
        g += random.gauss(0, 1.2)
        if random.random() < 0.004:
            g += random.uniform(15, 35)          # storm onset
        g = max(4.0, min(70.0, g * 0.98 + 12 * 0.02))   # mean-revert to ~12
        fm.g = round(g)
        # arrival: small calm baseline + steep rise above 30 kt (Poisson-ish per 5 min)
        lam = 0.03 + 0.9 * max(0.0, (g - 30) / 10.0) ** 1.6
        k = 0
        p = math.exp(-lam)
        cum = p
        u = random.random()
        while u > cum and k < 12:
            k += 1
            p *= lam / k
            cum += p
        incs = []
        for _ in range(k):
            nid += 1
            incs.append({"id": "i%d" % nid, "lat": 52.5, "lon": -1.5,
                         "customers_off": random.randint(20, 800), "planned": False})
        observe({"generated": base_ts + i * 300, "incidents": incs}, now=base_ts + i * 300)

    s = snapshot(["ALL"])
    print("maturity:", s["maturity"])
    print("baseline faults/hr:", s["regions"]["ALL"]["curves"]["faults"]["baseline"])
    print("  gust band     faults/hr (mean ± sd)   n")
    for b in s["regions"]["ALL"]["curves"]["faults"]["bins"]:
        print(f"  {b['g']:>2}-{b['g']+BIN_KT:<2} kt : {b['mean']:6.2f} ± {b['sd']:5.2f}   {b['n']}")
    print("predict @ 15 kt:", predict("ALL", 15, "faults"))
    print("predict @ 50 kt:", predict("ALL", 50, "faults"))
