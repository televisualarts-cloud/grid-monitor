# rain_probe.py — read-only rainfall-alert DIAGNOSTIC probe for GB Energy Monitor
#
# Purpose: each refresh cycle, evaluate the rain signals we have (model-at-home,
# physical EA gauges, and a movable offshore Open-Meteo arc) and LOG the exact
# phrase the alert system WOULD speak — with no tone, no Web Speech, no state
# forced onto the live alert layer. It is deliberately side-effect-free apart
# from the ProbeState it is handed, so it can be watched against real weather
# for a week or two before any of it earns a tone. ("Honesty over plausibility":
# everything modelled is labelled modelled; nothing modelled counts as measured
# confirmation.)
#
# Python 3.13, stdlib only. No import of the live server; call run_probe() from
# the EA collect path and hand it data already fetched (see INTEGRATION notes).

from __future__ import annotations
import json, math, time, random, os, threading, urllib.request, urllib.parse, urllib.error
from dataclasses import dataclass, field, asdict

# ───────────────────────── tunables (all in one place) ──────────────────────
# Rainfall intensity bands, mm/h (OWM rain.1h is already an instantaneous rate).
INTENSITY = [(0.05, "dry"), (0.5, "drizzle"), (2.0, "light"),
             (10.0, "moderate"), (50.0, "heavy"), (float("inf"), "violent")]

TREND_WINDOW_S   = 30 * 60     # look back ~30 min for the rate slope
TREND_DEADBAND   = 0.6         # mm/h change over the window before we call it
TREND_FAST       = 3.0         # mm/h change over the window = "quickly"

PRESS_FALL       = 1.0         # hPa/hr: notable fall
PRESS_FALL_FAST  = 2.0         # hPa/hr: rapid
PRESS_FALL_STORM = 3.5         # hPa/hr: vigorous / stormy
PRESS_RISE       = 1.0         # hPa/hr: clearing

VIS_LOW_M        = 5000        # visibility below this is "low"
VIS_DROP_M       = 3000        # a fall of this much over the window = "dropping"
VIS_WINDOW_S     = 45 * 60

# Warm-up + dropout guards. Derived signals (trend, pressure, visibility) need a
# minimum SPAN of history before they mean anything; otherwise a cold start or a
# gap yields a wild slope (e.g. +30 hPa/h from two samples a minute apart). A
# series whose newest sample is stale is treated as unknown, not extrapolated.
MIN_TREND_SPAN_S = 15 * 60     # rain-rate slope needs >= this much history
MIN_PRESS_SPAN_S = 30 * 60     # pressure tendency needs >= this much history
MIN_VIS_SPAN_S   = 20 * 60     # visibility trend needs >= this much history
PRESS_RATE_SANE  = 5.0         # hPa/hr beyond this is unphysical -> treat as noise
STALE_SAMPLE_S   = 20 * 60     # a series whose newest point is older than this is stale

# Forward nowcast (OWM One Call 4.0 one-minute timeline: 60 x per-minute mm/h).
FORWARD_HORIZON_MIN = 30       # how far into the minute nowcast we look ahead
FORWARD_ONSET_MMH   = 0.5      # forecast rate that counts as onset (light rain)

# Offshore movable arc (Open-Meteo, modelled, keyless, batched). Points are placed
# ONLY where they are genuinely over sea; land points are suppressed (real gauges
# already cover the land, and skipping them saves the precip sample). Azimuths are
# free — sentinels sit at whatever bearings are open water, not fixed cardinals.
SENTINEL_KM      = 40                 # permanent OUTER sentinel ring
ARC_SENTINEL_KM  = SENTINEL_KM        # (compat alias, still used by the sea mask)
PICKET_KM        = 20                 # permanent INNER picket ring — bridges the 40->home gap
NET_STEP_DEG     = 20                 # azimuth spacing of the sentinel ring (~14 km apart at 40 km)
PICKET_STEP_DEG  = 40                 # inner pickets sparser, sitting between sentinel spokes
NET_RANGES       = [40, 30, 20, 10, 5]  # ranges the sea-mask tests (net rings + mobile-band sea test)
NET_DITHER_DEG   = 10                 # under-hood azimuth jitter for the detection-only fill points
NET_FILL_RANGES  = [30, 10]           # mid ranges the dithered fill sweeps for coverage between rings
ARC_DETECT_MMH   = 0.3                # model rate that counts as "rain on this point"
# Frugality: each net point counts as one free-tier Open-Meteo call, so sample the
# slow-moving offshore model at most this often and reuse the readings in between;
# keep the hidden fill coords stable for an epoch so identical points are re-sampled
# (cacheable) rather than a fresh random set each cycle that multiplies the call count.
NET_SAMPLE_TTL_S   = 15 * 60          # sample the offshore net at most this often
NET_DITHER_EPOCH_S = 60 * 60          # keep the dithered fill azimuths stable this long
OC4_FALLBACK_MAX   = 4                # on Open-Meteo failure, sample <= this many sentinels via OC4
SEA_MASK_TTL     = 24 * 3600          # land/sea geography is static; refresh ~daily
GEOCODE_BASE     = "https://api.postcodes.io"

# Mobile tracker cards: spawned on a NET detection, they live in the 5-35 km band
# (40 km stays the sentinels'), chase the cell inward (~1.6x wind), jump back now
# and then to sense what follows, then loiter -> retreat to 35 km -> vanish. Their
# QUALITY reads come from OC4 (track_sample_fn), so OpenWeather budget is spent
# only on real detections; the wide net rides the free batched Open-Meteo.
MOBILE_BAND_MIN  = 5
MOBILE_BAND_MAX  = 35
MOBILE_MAX       = 4                  # budget cap on concurrent trackers
MOBILE_SEP_DEG   = 25                 # don't spawn a new mobile this close to an existing one
MOBILE_STEP_MIN_KM = 3.0              # minimum inward probe step per cycle
MOBILE_STEP_MAX_KM = 8.0              # cap on a single cycle's step
MOBILE_JUMPBACK_EVERY = 3             # every Nth wet cycle, jump back instead of chasing in
MOBILE_LOITER_CYCLES = 2              # dry cycles held before retreat begins
MOBILE_RETREAT_KM = 8.0               # outward step per cycle while retreating (-> 35 -> vanish)
MOBILE_EDGE_WINDOW_S = 45 * 60        # history window for the measured front speed

# "Smells wrong" speed-trust gate (shared by the sea arc and the land front). A
# measured front speed is SPOKEN only if it is physically sane, roughly consistent
# with the wind that drives it, and stable across cycles; otherwise the figure is
# withheld and the alert falls back to a qualitative approach. Honesty over a
# plausible-but-wrong number (e.g. two fronts read as one giving 300 mph).
KMH_PER_MPH   = 1.609344
SPEED_MIN_MPH = 5                     # below this: quasi-stationary, no ETA number
SPEED_MAX_MPH = 75                    # above this: almost no UK rain band -> distrust
SPEED_WIND_LO = 0.4                   # trust only if measured >= this * (1.6 x wind) prior
SPEED_WIND_HI = 3.0                   # ...and <= this * prior
SPEED_STABLE_TOL = 0.35               # successive measures must agree within +/-35%

# Approach ETA: system motion tends to run faster than the 10 m wind.
ADVECT_FAST_FACTOR = 1.6
ETA_MIN_MIN      = 15
ETA_MAX_MIN      = 90

GAUGE_CONFIRM_KM = 8.0         # a physical gauge this close counts as "at your location"
UPWIND_HALF_ANGLE = 60        # a gauge within +/- this of wind-from is "upwind"

# Land-front tracking: give the PHYSICAL upwind gauges the same treatment as the
# sea arc — bin the leading wet edge into range rings, measure its inward speed
# ring-to-ring across cycles, and watch whether the front is strengthening or
# weakening as it closes in (so a fizzling shower is called out, not just an ETA).
LAND_RINGS        = [30, 20, 10]      # km boundaries mirroring the sea arc's inner rings
LAND_TRACK_MAX_KM = 35                # a wet upwind gauge beyond this is too far to track yet
LAND_TRACK_WINDOW_S = 45 * 60         # history window for edge speed + intensity slope
LAND_MIN_SPAN_S   = 8 * 60            # need this much history before calling a trend/speed
FRONT_STRENGTHEN  = 1.0               # mm/h rise of the leading edge over the window = building
FRONT_WEAKEN      = -1.0              # mm/h fall over the window = easing
FRONT_FIZZLE_MMH  = 1.0               # weakening AND peak below this = likely to fizzle out
ARC_WEAKEN_MMH    = 0.5               # sea edge intensity drop that counts as "weakening"

# Open-Meteo forecast endpoint. The free public host is rate-limited PER IP and shared,
# so a busy/CGNAT/cloud IP can sit permanently "daily limit exceeded". Set OPEN_METEO_BASE
# (e.g. a self-hosted Open-Meteo: http://localhost:8080/v1) to use a private quota instead.
OM_BASE = os.environ.get("OPEN_METEO_BASE", "https://api.open-meteo.com/v1").rstrip("/")
OPEN_METEO_URL = OM_BASE + "/forecast"


# ───────────────────────── API call metering (diagnostic) ───────────────────
# Every REAL (non-cached) upstream call is logged with a tag naming what asked for
# it, so we can see exactly where the OM / OWM budgets go. Two files next to this
# module: a rolling JSONL event log (api_calls.jsonl) and a per-UTC-day tally
# (api_usage_daily.json, reset at 00:00 UTC, persisted across restarts).
METER_ENABLED = True
_METER_DIR  = os.path.dirname(os.path.abspath(__file__))
METER_LOG   = os.path.join(_METER_DIR, "api_calls.jsonl")
METER_TALLY = os.path.join(_METER_DIR, "api_usage_daily.json")
METER_LOG_MAX = 5 * 1024 * 1024                 # rotate the event log past ~5 MB
_meter_lock = threading.Lock()
_meter_day  = {"date": None, "counts": {}}

def _utc_day():
    return time.strftime("%Y-%m-%d", time.gmtime())

def _meter_load_day():
    d = _utc_day()
    if _meter_day["date"] == d:
        return
    _meter_day["date"] = d; _meter_day["counts"] = {}
    try:
        blob = json.loads(open(METER_TALLY, encoding="utf-8").read())
        if blob.get("date") == d:
            _meter_day["counts"] = dict(blob.get("counts") or {})
    except Exception:
        pass

def _meter_save_day():
    try:
        with open(METER_TALLY, "w", encoding="utf-8") as fh:
            json.dump({"date": _meter_day["date"], "counts": _meter_day["counts"]}, fh)
    except Exception:
        pass

def meter_api(api, tag, n=1, note=""):
    """Record n real upstream calls to `api` (OM/OWM/EA/...) made for `tag`. Appends a
    JSONL event and bumps the per-UTC-day tally. Never raises; safe from many threads."""
    if not METER_ENABLED or not n or n <= 0:
        return
    try:
        with _meter_lock:
            _meter_load_day()
            key = str(api) + "/" + str(tag)
            _meter_day["counts"][key] = _meter_day["counts"].get(key, 0) + int(n)
            _meter_save_day()
            try:
                if os.path.exists(METER_LOG) and os.path.getsize(METER_LOG) > METER_LOG_MAX:
                    os.replace(METER_LOG, METER_LOG + ".1")
            except Exception:
                pass
            rec = {"iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "ts": round(time.time(), 1), "api": api, "tag": tag, "n": int(n)}
            if note:
                rec["note"] = note
            with open(METER_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass

def api_usage_today():
    """Per-UTC-day call tally for surfacing: {date, counts:{'OM/offshore_net':N,...}, total}."""
    try:
        with _meter_lock:
            _meter_load_day()
            counts = dict(_meter_day["counts"])
        return {"date": _meter_day["date"], "counts": counts, "total": sum(counts.values())}
    except Exception:
        return {"date": None, "counts": {}, "total": 0}


# ───────────────────────── small geo/number helpers ─────────────────────────
def _rad(d): return d * math.pi / 180.0

def haversine_km(la1, lo1, la2, lo2):
    if None in (la1, lo1, la2, lo2): return None
    R = 6371.0
    dla, dlo = _rad(la2 - la1), _rad(lo2 - lo1)
    a = (math.sin(dla / 2) ** 2 +
         math.cos(_rad(la1)) * math.cos(_rad(la2)) * math.sin(dlo / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))

def bearing_deg(la1, lo1, la2, lo2):
    """Compass bearing FROM (la1,lo1) TO (la2,lo2), 0=N,90=E."""
    p1, p2, dl = _rad(la1), _rad(la2), _rad(lo2 - lo1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.atan2(y, x) * 180 / math.pi + 360) % 360

def offset_latlon(lat, lon, bearing, dist_km):
    """Point dist_km from (lat,lon) along a compass bearing (equirectangular,
    fine at tens of km)."""
    R = 6371.0
    b = _rad(bearing)
    dlat = (dist_km * math.cos(b)) / R
    dlon = (dist_km * math.sin(b)) / (R * math.cos(_rad(lat)))
    return (lat + math.degrees(dlat), lon + math.degrees(dlon))

_COMPASS = [(0, "N", "north"), (45, "NE", "north-east"), (90, "E", "east"),
            (135, "SE", "south-east"), (180, "S", "south"),
            (225, "SW", "south-west"), (270, "W", "west"),
            (315, "NW", "north-west")]

def compass(bearing, spoken=False):
    if bearing is None: return None
    best = min(_COMPASS, key=lambda c: min(abs(bearing - c[0]), 360 - abs(bearing - c[0])))
    return best[2] if spoken else best[1]

def _round5(x): return int(round(x / 5.0) * 5)


def _gmm(g):
    """Unified precip RATE (mm/h) for a physical gauge. EA gauges arrive with a
    15-minute bucket total in 'mm' and the same value already converted to a rate
    in 'mm_h'; prefer the rate so gauge, sentinel and model points are all compared
    on one scale (mm/h). Falls back to raw 'mm' for any caller predating the
    conversion. Everything here (INTENSITY, ARC_DETECT_MMH, FRONT_*) is mm/h, so
    this is the single read for gauge intensity."""
    v = g.get("mm_h")
    return v if v is not None else g.get("mm")


# ───────────────────────── persistent state ─────────────────────────────────
@dataclass
class ProbeState:
    """Held by the caller across cycles (in memory, or JSON round-tripped).
    Everything the probe needs to remember between refreshes lives here."""
    rain_hist: list = field(default_factory=list)      # [(ts, mm_h)]
    press_hist: list = field(default_factory=list)     # [(ts, hPa)]
    vis_hist: list = field(default_factory=list)        # [(ts, metres)]
    arc_mode: str = "scan"                              # "scan" | "track"
    arc_hits: int = 0                                   # consecutive offshore detections
    arc_edge_km: float | None = None                   # nearest wet range while tracking
    arc_edge_ts: float | None = None
    arc_edge_mm: float | None = None                   # leading-edge intensity last cycle (fizzle watch)
    arc_lock_ts: float | None = None
    arc_track_az: list = field(default_factory=list)   # (legacy, unused by the two-layer net)
    mobiles: list = field(default_factory=list)        # active mobile tracker agents (dicts)
    mobile_seq: int = 0                                # id counter for spawned mobiles
    net_edge_hist: list = field(default_factory=list)  # [[ts, nearest_wet_net_km, peak_mm]] — front-speed source
    net_speed_kmh: float | None = None                 # last measured net-edge closing speed
    net_cache: dict = field(default_factory=dict)      # (bearing,range)->{mm,snow,src}: last net sample, reused within TTL
    net_cache_ts: float = 0.0                          # when the net was last actually sampled (Open-Meteo)
    net_dither: dict = field(default_factory=dict)     # az->stable jitter (regenerated per dither epoch)
    net_dither_ts: float = 0.0                         # when the dither pattern was last regenerated
    net_feed: str = "ok"                               # offshore feed state: ok | degraded | exhausted | down
    net_feed_reason: "str | None" = None               # human reason (e.g. daily limit) when not ok
    gauge_hist: dict = field(default_factory=dict)     # id -> [[ts, mm_h], ...] rolling per-gauge history
    sit_state: str = "clear"                           # last situational base state (for dwell)
    sit_state_since: float = 0.0
    sit_group: str = "clear"                           # coarse group for dwell (here/showers/continuous/...)
    sit_group_since: float = 0.0
    sit_announced: dict = field(default_factory=dict)  # key -> {"phrase","next"} cadence schedule
    sit_last_snow: bool = False
    sit_episode_spoke: bool = False                    # did the current precip episode actually announce anything?
    sit_dir_sect: "int | None" = None                  # last announced direction sector (8ths) for hysteresis
    sit_tracks: list = field(default_factory=list)     # tracked precip clusters (continuity across sectors)
    sit_track_seq: int = 0
    sit_probes: list = field(default_factory=list)     # [{bearing,dist_km,mm,snow,ts}] land model-probe confirmations
    sit_probe_ts: float = 0.0                          # last probe-deployment time
    sit_probe_phase: int = 0                           # rotates so consecutive probes cover new ground
    sit_probe_used: int = 0                            # probe SAMPLES spent this episode (budget cap)
    sit_probe_used_ts: float = 0.0                     # when the episode budget was last touched
    sit_last_showery_ts: float = 0.0                   # last time the situation was showery (episode memory)
    sit_last_centroid: "list | None" = None            # [bearing, dist] of the last active cluster
    sea_pts: list = field(default_factory=list)         # [[az,range],...] that are sea
    sea_home: list | None = None
    sea_ts: float = 0.0
    land_hist: list = field(default_factory=list)      # [(ts, edge_km, peak_mm, dir)] — approaching land front
    land_active: bool = False                          # was a land front approaching last cycle?
    land_last_dir: str | None = None                   # spoken direction of the tracked land front
    announced: dict = field(default_factory=dict)      # key -> last-announced phrase (edge-triggered)
    was_active: bool = False                            # was the situation non-calm last cycle?

    def to_json(self): return json.dumps(asdict(self))
    @classmethod
    def from_json(cls, s): return cls(**json.loads(s)) if s else cls()


def _trim(hist, now, window):
    return [(t, v) for (t, v) in hist if now - t <= window]


def _span(h):
    """Elapsed seconds covered by a history list (0 if fewer than 2 points)."""
    return (h[-1][0] - h[0][0]) if len(h) >= 2 else 0.0


def _fresh(h, now):
    """True if the newest sample is recent enough to reason from (dropout guard)."""
    return bool(h) and (now - h[-1][0]) <= STALE_SAMPLE_S


# ───────────────────────── signal computations ──────────────────────────────
def classify_intensity(mm_h):
    if mm_h is None: return None
    for hi, name in INTENSITY:
        if mm_h < hi: return name
    return "violent"

def compute_trend(hist, now):
    """Rate change over the trend window. Returns one of steady/rising/
    rising_fast/easing/easing_fast, plus the delta (mm/h)."""
    h = _trim(hist, now, TREND_WINDOW_S)
    if len(h) < 2 or _span(h) < MIN_TREND_SPAN_S or not _fresh(h, now):
        return "steady", 0.0
    # smooth the two ends a little to resist single-sample model jitter
    early = sum(v for _, v in h[:2]) / len(h[:2])
    late = sum(v for _, v in h[-2:]) / len(h[-2:])
    d = late - early
    if d >= TREND_FAST:   return "rising_fast", d
    if d >= TREND_DEADBAND:  return "rising", d
    if d <= -TREND_FAST:  return "easing_fast", d
    if d <= -TREND_DEADBAND: return "easing", d
    return "steady", d

def pcls_from_change3h(change_3h):
    """Classify pressure tendency from the AUTHORITATIVE 3-hour change (hPa/3h) that the
    weather panel already computes from its persisted, restart-surviving log. The spoken
    pressure alert uses this single figure, so the voice and the panel can never disagree.
    (This replaces a second, weaker in-memory endpoint slope that had its own volatile
    history and could point the opposite way to the panel.)"""
    if change_3h is None:
        return 0.0, "steady"
    rate = change_3h / 3.0             # hPa per hour, comparable to the thresholds below
    if abs(rate) > PRESS_RATE_SANE:
        return rate, "steady"          # unphysical = bad data; report, do not alarm
    if rate <= -PRESS_FALL_STORM: cls = "falling_storm"
    elif rate <= -PRESS_FALL_FAST: cls = "falling_fast"
    elif rate <= -PRESS_FALL: cls = "falling"
    elif rate >= PRESS_RISE: cls = "rising"
    else: cls = "steady"
    return rate, cls

def visibility_state(hist, now, vis_now):
    h = _trim(hist, now, VIS_WINDOW_S)
    dropping = False
    if (len(h) >= 2 and _span(h) >= MIN_VIS_SPAN_S and _fresh(h, now)
            and h[0][1] is not None and vis_now is not None):
        dropping = (h[0][1] - vis_now) >= VIS_DROP_M
    low = vis_now is not None and vis_now < VIS_LOW_M
    return {"low": low, "dropping": dropping, "m": vis_now}


_BANDRANK = {"dry": 0, "drizzle": 1, "light": 2, "moderate": 3, "heavy": 4, "violent": 5}

def compute_forward(minute, now, current_band):
    """From the forward one-minute precipitation series, detect upcoming onset
    (when dry now) or intensification ahead (when already raining). minute is
    [{'dt': unix, 'mm_h': rate}, ...]. Returns a dict or None."""
    if not minute:
        return None
    fut = [(m["dt"], m["mm_h"]) for m in minute
           if m.get("dt") and m.get("mm_h") is not None
           and now < m["dt"] <= now + FORWARD_HORIZON_MIN * 60]
    if not fut:
        return None
    peak = max(v for _, v in fut)
    raining = current_band not in (None, "dry")
    out = {"onset_eta_min": None, "intensify": False, "peak_mmh": round(peak, 2)}
    if not raining:
        for dt, v in fut:
            if v >= FORWARD_ONSET_MMH:
                out["onset_eta_min"] = max(0, _round5((dt - now) / 60.0))
                break
    elif _BANDRANK.get(classify_intensity(peak), 0) > _BANDRANK.get(current_band, 0):
        out["intensify"] = True
    return out


def eta_from_speed(dist_km, speed_kmh, measured=False):
    """Arrival window from distance and a closing speed. Returns
    (text, lo_min, hi_min) or (None, None, None) if it should be dropped.
    measured=False treats speed as a surface-wind proxy (the system runs faster,
    so the fast edge applies ADVECT_FAST_FACTOR); measured=True treats it as an
    already-observed front speed and brackets it symmetrically (+/-20%)."""
    if not dist_km or not speed_kmh or speed_kmh <= 0.5:
        return None, None, None
    if measured:
        mid = dist_km / speed_kmh * 60.0
        lo, hi = _round5(mid * 0.8), _round5(mid * 1.2)
    else:
        slow = dist_km / speed_kmh * 60.0               # surface wind = slow edge
        fast = dist_km / (speed_kmh * ADVECT_FAST_FACTOR) * 60.0
        lo, hi = _round5(fast), _round5(slow)
    if hi > ETA_MAX_MIN:                                # too far out for a claim
        return None, None, None
    if lo < ETA_MIN_MIN:
        return "within the next fifteen minutes", lo, hi
    return f"in {lo} to {hi} minutes", lo, hi


def eta_window(dist_km, wind_kmh):
    """Back-compat wrapper: arrival window from a surface-wind proxy speed."""
    return eta_from_speed(dist_km, wind_kmh, measured=False)


def gauge_approach(gauges, home, wind_from, wind_kmh):
    """Physical-gauge (measured) approach signal. gauges: list of dicts with
    lat/lon/mm/dist_km. Returns dict or None. Also reports nearest wet gauge and
    whether any wet gauge is within confirmation range (measured 'at home')."""
    wet = [g for g in gauges if (_gmm(g) or 0) > 0
           and g.get("lat") is not None and g.get("lon") is not None
           and not g.get("modelled")]
    if not wet:
        return None
    for g in wet:
        g["_brg"] = bearing_deg(home[0], home[1], g["lat"], g["lon"])
        g["_d"] = g.get("dist_km") or haversine_km(home[0], home[1], g["lat"], g["lon"])
    nearest = min(wet, key=lambda g: g["_d"] or 9e9)
    confirmed = (nearest["_d"] or 9e9) <= GAUGE_CONFIRM_KM
    upwind = []
    if wind_from is not None:
        for g in wet:
            diff = abs((g["_brg"] - wind_from + 180) % 360 - 180)
            if diff <= UPWIND_HALF_ANGLE:
                upwind.append(g)
    out = {"nearest_km": round(nearest["_d"], 1) if nearest["_d"] else None,
           "confirmed_at_home": confirmed, "n_wet": len(wet),
           "approach": None}
    if len(upwind) >= 2:
        near_up = min(upwind, key=lambda g: g["_d"] or 9e9)
        txt, lo, hi = eta_window(near_up["_d"], wind_kmh)
        out["approach"] = {"dir": compass(wind_from, spoken=True),
                           "eta_text": txt, "eta_lo": lo, "eta_hi": hi,
                           "n_up": len(upwind), "edge_km": round(near_up["_d"], 1)}
    return out


# ───────────────────────── land-front range-ring tracker ────────────────────
def _upwind_wet(gauges, home, wind_from):
    """Wet, non-modelled physical gauges lying upwind of home (within
    UPWIND_HALF_ANGLE of the wind-from bearing). Each is tagged with _brg/_d."""
    wet = [g for g in gauges if (_gmm(g) or 0) > 0
           and g.get("lat") is not None and g.get("lon") is not None
           and not g.get("modelled")]
    for g in wet:
        g["_brg"] = bearing_deg(home[0], home[1], g["lat"], g["lon"])
        g["_d"] = g.get("dist_km") or haversine_km(home[0], home[1], g["lat"], g["lon"])
    if wind_from is None:
        return []
    return [g for g in wet
            if abs((g["_brg"] - wind_from + 180) % 360 - 180) <= UPWIND_HALF_ANGLE
            and g["_d"] is not None]


def _ring_of(edge_km):
    """Coarsest range ring the leading wet edge has crossed (30/20/10 km); None
    if still beyond the outer ring. Used to key ring-crossing announcements."""
    if edge_km is None:
        return None
    band = None
    for r in sorted(LAND_RINGS, reverse=True):          # 30, 20, 10
        if edge_km <= r:
            band = r
    return band


def _front_trend(hist, now):
    """Intensity slope of the approaching front over the tracking window, from
    the leading-edge peak mm/h. Returns (label, delta_mm) where label is
    strengthening / weakening / steady."""
    h = [(t, mm) for (t, e, mm, d) in hist
         if now - t <= LAND_TRACK_WINDOW_S and mm is not None]
    if len(h) < 2 or (h[-1][0] - h[0][0]) < LAND_MIN_SPAN_S:
        return "steady", 0.0
    early = sum(v for _, v in h[:2]) / len(h[:2])
    late = sum(v for _, v in h[-2:]) / len(h[-2:])
    d = late - early
    if d >= FRONT_STRENGTHEN: return "strengthening", d
    if d <= FRONT_WEAKEN:     return "weakening", d
    return "steady", d


def _front_speed(hist, now):
    """Measured inward closing speed (km/h) of the leading edge over the window,
    or None if there isn't yet inbound motion to measure."""
    h = [(t, e) for (t, e, mm, d) in hist
         if now - t <= LAND_TRACK_WINDOW_S and e is not None]
    if len(h) < 2:
        return None
    dt_hr = (h[-1][0] - h[0][0]) / 3600.0
    moved = h[0][1] - h[-1][1]                           # positive = closing in
    if dt_hr <= 0 or moved <= 0:
        return None
    return moved / dt_hr


def land_front(state, gauges, home, wind_from, wind_kmh, now):
    """Track the approaching PHYSICAL-gauge front the way the sea arc tracks the
    offshore edge: nearest upwind wet gauge = leading edge, binned into range
    rings; measured inward speed (falling back to wind when motion isn't yet
    measurable); and an intensity trend so a fizzling shower is called out.
    Read-only apart from appending to state.land_hist. Returns a dict."""
    dir_spoken = compass(wind_from, spoken=True) if wind_from is not None else None
    out = {"active": False, "dir": dir_spoken, "edge_km": None, "ring": None,
           "speed_kmh": None, "measured": False, "eta_text": None,
           "eta_lo": None, "eta_hi": None, "intensity_trend": "steady",
           "fizzling": False, "peak_mm": None}
    uw = _upwind_wet(gauges, home, wind_from)
    if not uw:
        return out

    edge = min(g["_d"] for g in uw)
    peak = max((_gmm(g) or 0) for g in uw)
    out["edge_km"], out["peak_mm"] = round(edge, 1), round(peak, 2)

    state.land_hist.append((now, edge, peak, dir_spoken))
    state.land_hist = [(t, e, m, dd) for (t, e, m, dd) in state.land_hist
                       if now - t <= LAND_TRACK_WINDOW_S]

    trend, dmm = _front_trend(state.land_hist, now)
    spd = _front_speed(state.land_hist, now)
    measured = spd is not None
    if not measured:
        spd = wind_kmh
    txt, lo, hi = eta_from_speed(edge, spd, measured=measured)

    out["intensity_trend"] = trend
    out["speed_kmh"] = round(spd, 1) if spd else None
    out["measured"] = measured
    out["eta_text"], out["eta_lo"], out["eta_hi"] = txt, lo, hi
    out["ring"] = _ring_of(edge)
    out["fizzling"] = (trend == "weakening" and peak < FRONT_FIZZLE_MMH)
    # "active" = a front genuinely approaching: beyond the confirm radius (else
    # it's arriving and the band/confirm messages take over), within track range,
    # and corroborated — either two-plus upwind wet gauges, or a single one that
    # has since shown measured inbound motion (temporal confirmation). This keeps
    # one stray wet gauge from raising an approach on its first appearance.
    n_up = len(uw)
    out["n_up"] = n_up
    out["active"] = ((GAUGE_CONFIRM_KM < edge <= LAND_TRACK_MAX_KM)
                     and (n_up >= 2 or measured))
    return out


# ───────────────────────── movable offshore arc ─────────────────────────────
def fetch_landsea(points, timeout=8):
    """Batched land/sea test via postcodes.io: a coordinate with no nearby postcode
    is taken to be over sea (or outside GB). Returns a list of bools (True = sea)
    aligned to points; None entries mean the lookup failed. Never raises."""
    if not points:
        return []
    try:
        body = json.dumps({"geolocations": [
            {"longitude": p["lon"], "latitude": p["lat"], "limit": 1, "radius": 2000}
            for p in points]}).encode()
        req = urllib.request.Request(GEOCODE_BASE + "/postcodes", data=body,
              headers={"Content-Type": "application/json", "User-Agent": "uk-grid-monitor/1.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        out = [not (item.get("result")) for item in (d.get("result") or [])]
        while len(out) < len(points):
            out.append(False)
        return out
    except Exception:
        return [None] * len(points)


def ensure_sea_mask(state, home, now, landsea_fn):
    """Determine and cache which (azimuth,range) points around home are sea. Static
    geography, so computed rarely (SEA_MASK_TTL) and reused. On a lookup failure we
    keep whatever cache we have rather than guess a coastline."""
    hk = [round(home[0], 2), round(home[1], 2)]
    if state.sea_home == hk and (now - state.sea_ts) < SEA_MASK_TTL and state.sea_pts:
        return
    ranges = NET_RANGES
    cand = []
    for a in range(0, 360, NET_STEP_DEG):
        for r in ranges:
            la, lo = offset_latlon(home[0], home[1], a, r)
            cand.append({"az": a, "range_km": r, "lat": la, "lon": lo})
    flags = landsea_fn(cand)
    if not flags or any(f is None for f in flags):
        return                              # lookup failed: keep existing cache
    state.sea_pts = [[c["az"], c["range_km"]] for c, f in zip(cand, flags) if f]
    state.sea_home, state.sea_ts = hk, now


def _sea_set(state):
    return {(a, r) for a, r in state.sea_pts}

OM_BACKOFF_S = 90               # after a minute/hour rate-limit, pause Open-Meteo this long
# Open-Meteo's free tier is enforced PER IP and shared, so the "Daily API request limit
# exceeded — try again tomorrow" message can appear early in the UTC day and regardless
# of THIS app's own call count (a shared/CGNAT/cloud IP is drained by everyone on it;
# reproducibly seen returning 429 from a fresh IP that had made zero calls). OM itself
# says "try again tomorrow", so after a DAILY rejection we hold off until just after the
# next 00:00 UTC reset — but keep at most an hourly probe in case a shared pool frees up
# sooner. Non-daily (minute/hour) limits use the short pause. OC4 covers the gap meanwhile.
OM_DAILY_PROBE_S = 900          # while daily-capped, re-probe every 15 min. This is also
                                # how quickly it recovers by itself after you CHANGE IP
                                # (new IP = fresh OM quota): the next probe succeeds and
                                # the warning clears with no restart. Shorter = faster
                                # recovery but more probing at a genuinely stuck IP.
_om_backoff_until = 0.0
_om_daily_until = 0.0            # until when the last rejection was a DAILY cap (for messaging)

def _om_is_ratelimit(reason):
    r = (reason or "").lower()
    return any(k in r for k in ("limit", "429", "too many", "rate", "minutely", "hourly", "quota"))

def _om_is_daily(reason):
    """True only for Open-Meteo's DAILY-cap message, not the minute/hour ones."""
    r = (reason or "").lower()
    return "daily" in r or "per day" in r or "tomorrow" in r

def _next_utc_midnight(now):
    import datetime as _dt
    dtn = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    nxt = (dtn + _dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp() + 120.0        # +2 min so OM's counter has definitely rolled

def _om_backoff_for(reason, now):
    """Set the global Open-Meteo cooldown. A daily-cap rejection holds until the real
    00:00 UTC reset (probing at most hourly meanwhile); other rate-limits use a short pause."""
    global _om_backoff_until, _om_daily_until
    if _om_is_daily(reason):
        _om_daily_until = _next_utc_midnight(now)
        _om_backoff_until = min(now + OM_DAILY_PROBE_S, _om_daily_until)
    else:
        _om_backoff_until = now + OM_BACKOFF_S

def fetch_om_precip(points, timeout=8):
    """Batched Open-Meteo current precipitation (mm) for many coords in ONE call.
    Modelled. Never raises — returns rates aligned to points (None on failure). A
    rate-limit trips a short GLOBAL backoff: we stop calling Open-Meteo entirely during
    its cooldown (respecting its "try again in one minute"), so we neither hammer it nor
    rack up failed calls — the OC4 fallback covers the gap."""
    global _om_backoff_until
    if not points:
        return []
    now = time.time()
    if now < _om_backoff_until:
        msg = ("daily limit reached — backing off until it resets"
               if now < _om_daily_until else "rate-limited, backing off")
        return [{"mm": None, "snow": False, "src": "OM",
                 "err": msg, "backoff": True}] * len(points)
    try:
        q = {"latitude": ",".join(f"{p['lat']:.4f}" for p in points),
             "longitude": ",".join(f"{p['lon']:.4f}" for p in points),
             "current": "precipitation,rain,showers"}
        url = OPEN_METEO_URL + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"User-Agent": "uk-grid-monitor/1.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        if isinstance(d, dict) and d.get("error"):
            # Open-Meteo signals failures (limit exceeded etc.) as a 200 body with
            # error:true -- surface it, and back off if it is a rate-limit (longer for
            # a daily-cap rejection). The request WAS issued, so callers may count it.
            reason = str(d.get("reason") or "Open-Meteo error")
            if _om_is_ratelimit(reason):
                _om_backoff_for(reason, now)
            return [{"mm": None, "snow": False, "src": "OM", "err": reason, "sent": True}] * len(points)
        blocks = d if isinstance(d, list) else [d]
        out = []
        for b in blocks:
            cur = b.get("current") or {}
            tot = cur.get("precipitation")           # total water-equiv, incl. snow
            if tot is None:
                out.append({"mm": None, "snow": False, "src": "OM", "sent": True}); continue
            rain = (cur.get("rain") or 0.0) + (cur.get("showers") or 0.0)
            snow_we = tot - rain                     # snow water-equivalent
            out.append({"mm": tot, "snow": bool(snow_we > 0.05 and snow_we >= rain),
                        "src": "OM", "sent": True})
        return out
    except urllib.error.HTTPError as e:
        reason = None
        try:
            reason = (json.loads(e.read() or b"{}") or {}).get("reason")
        except Exception:
            pass
        if getattr(e, "code", None) == 429 or _om_is_ratelimit(reason):
            _om_backoff_for(reason or ("HTTP " + str(getattr(e, "code", ""))), now)
        # An HTTP response came back (request reached Open-Meteo) -> mark as sent.
        return [{"mm": None, "snow": False, "src": "OM",
                 "err": reason or ("HTTP " + str(e.code)), "sent": True}] * len(points)
    except Exception as e:
        return [{"mm": None, "snow": False, "src": "OM", "err": "fetch failed: " + str(e)[:80]}] * len(points)


def _apply_sample(pt, rt):
    """Normalise a sampler result onto a point: {mm, snow, src}. Tolerates a bare
    float for back-compat."""
    if isinstance(rt, dict):
        pt["mm"] = rt.get("mm"); pt["snow"] = bool(rt.get("snow")); pt["src"] = rt.get("src")
    else:
        pt["mm"] = rt; pt["snow"] = False; pt["src"] = None


def _is_sea(seaset, az, range_km):
    """Sea test for an arbitrary (azimuth, range): snap to the nearest mask cell."""
    a = int(round((az % 360) / NET_STEP_DEG) * NET_STEP_DEG) % 360
    r = min(NET_RANGES, key=lambda rr: abs(rr - range_km))
    return (a, r) in seaset


def _range_speed(hist, now, window=MOBILE_EDGE_WINDOW_S):
    """Inward closing speed (km/h) of a wet EDGE from a [[ts, range_km, mm], ...]
    history: positive = closing on home. None if not yet measurable."""
    h = [e for e in hist if now - e[0] <= window]
    if len(h) < 2:
        return None
    dt_hr = (h[-1][0] - h[0][0]) / 3600.0
    moved = h[0][1] - h[-1][1]
    if dt_hr <= 0 or moved <= 0:
        return None
    return moved / dt_hr


def speed_trust(measured_kmh, wind_kmh, prev_kmh=None):
    """The shared "smells wrong" gate. Returns (mph_or_None, trusted_bool). A speed
    is trusted only if it is in a physical band, roughly consistent with 1.6x the
    driving wind, and stable versus the previous measure. Used by BOTH the sea arc
    and the land front so there is one definition of a believable front speed."""
    if not measured_kmh or measured_kmh <= 0:
        return None, False
    mph = measured_kmh / KMH_PER_MPH
    if mph < SPEED_MIN_MPH or mph > SPEED_MAX_MPH:      # physical band
        return round(mph), False
    if wind_kmh:                                        # consistency with the driving wind
        prior = ADVECT_FAST_FACTOR * wind_kmh
        if prior > 0 and not (SPEED_WIND_LO * prior <= measured_kmh <= SPEED_WIND_HI * prior):
            return round(mph), False
    if prev_kmh:                                        # 2-cycle stability
        rel = abs(measured_kmh - prev_kmh) / max(prev_kmh, 1e-6)
        if rel > SPEED_STABLE_TOL:
            return round(mph), False
    return round(mph), True


def _net_points(state, home, seaset, sentinel_az):
    """Build the NET sample set: stable DISPLAYED sentinels (40 km) + inner pickets
    (20 km), plus hidden DITHER fill points (jittered azimuth, mid ranges) that give
    detection coverage in the gaps without adding a visible, jittering card. All ride
    the one free Open-Meteo batch. Returns (displayed, fills)."""
    disp = []
    for a in sentinel_az:
        la, lo = offset_latlon(home[0], home[1], a, SENTINEL_KM)
        disp.append({"lat": la, "lon": lo, "range_km": SENTINEL_KM, "bearing": a, "kind": "sentinel"})
    for a in sentinel_az:
        if a % PICKET_STEP_DEG == 0 and _is_sea(seaset, a, PICKET_KM):
            la, lo = offset_latlon(home[0], home[1], a, PICKET_KM)
            disp.append({"lat": la, "lon": lo, "range_km": PICKET_KM, "bearing": a, "kind": "picket"})
    fills = []
    for a in sentinel_az:
        ja = (a + state.net_dither.get(a, 0.0)) % 360      # stable within a dither epoch
        for r in NET_FILL_RANGES:
            if _is_sea(seaset, ja, r):
                la, lo = offset_latlon(home[0], home[1], ja, r)
                fills.append({"lat": la, "lon": lo, "range_km": r, "bearing": ja, "kind": "fill"})
    return disp, fills


def _mobile_move(m, now, wind_kmh, net_speed_kmh, trusted):
    """Move a WET mobile: mostly chase inward one probe step (~1.6x wind, or the
    measured front speed when trusted), and every Nth wet cycle jump BACK to sense
    whether heavier/lighter/no rain is following. Clamped to the 5-35 km band."""
    dt_hr = None
    if m.get("last_move_ts"):
        dt_hr = (now - m["last_move_ts"]) / 3600.0
    spd = net_speed_kmh if (trusted and net_speed_kmh) else (ADVECT_FAST_FACTOR * (wind_kmh or 0))
    base = (spd * dt_hr) if (spd and dt_hr) else MOBILE_STEP_MIN_KM
    step = max(MOBILE_STEP_MIN_KM, min(MOBILE_STEP_MAX_KM, base or MOBILE_STEP_MIN_KM))
    m["probe_ct"] = m.get("probe_ct", 0) + 1
    if m["probe_ct"] % MOBILE_JUMPBACK_EVERY == 0:
        m["range_km"] = min(MOBILE_BAND_MAX, m["range_km"] + step * 2.0)   # jump back to sense what follows
        m["probe_phase"] = "back"
    else:
        m["range_km"] = max(MOBILE_BAND_MIN, m["range_km"] - step)         # chase inward
        m["probe_phase"] = "in"
    m["last_move_ts"] = now


def _update_mobiles(state, home, now, track_sample_fn, wind_kmh, net_speed_kmh, trusted):
    """Advance every existing mobile one cycle: take a quality (OC4) read at its
    position; if wet, chase/jump-back and keep it alive; if dry, loiter briefly then
    retreat outward to 35 km and vanish. A dry mobile that goes wet again re-locks."""
    if not state.mobiles:
        return
    pts = []
    for m in state.mobiles:
        la, lo = offset_latlon(home[0], home[1], m["bearing"], m["range_km"])
        pts.append({"lat": la, "lon": lo})
    rates = track_sample_fn(pts) if pts else []
    survivors = []
    for m, rt in zip(state.mobiles, rates):
        _apply_sample(m, rt)
        m["confirmed"] = (m.get("src") == "OC4") and (m.get("mm") is not None)
        wet = (m.get("mm") or 0) >= ARC_DETECT_MMH
        if wet:
            m["last_wet_ts"] = now
            m["dry_cycles"] = 0
            _mobile_move(m, now, wind_kmh, net_speed_kmh, trusted)
            m["state"] = "hunt"
        else:
            m["dry_cycles"] = m.get("dry_cycles", 0) + 1
            if m["dry_cycles"] <= MOBILE_LOITER_CYCLES:
                m["state"] = "loiter"                      # hold position a little
            else:
                m["state"] = "retreat"
                m["range_km"] = min(MOBILE_BAND_MAX, m["range_km"] + MOBILE_RETREAT_KM)
                if m["range_km"] >= MOBILE_BAND_MAX - 0.01:
                    continue                               # reached 35 km -> vanish
        survivors.append(m)
    state.mobiles = survivors


def _spawn_mobiles(state, now, detections):
    """Seed a mobile on any net detection not already covered by an existing mobile
    (within MOBILE_SEP_DEG of azimuth), up to MOBILE_MAX. Strongest detection first."""
    for p in sorted(detections, key=lambda d: -(d.get("mm") or 0)):
        if len(state.mobiles) >= MOBILE_MAX:
            break
        az = p["bearing"]
        covered = any(abs((m["bearing"] - az + 180) % 360 - 180) <= MOBILE_SEP_DEG
                      for m in state.mobiles)
        if covered:
            continue
        state.mobile_seq += 1
        state.mobiles.append({
            "id": state.mobile_seq, "bearing": az,
            "range_km": max(MOBILE_BAND_MIN, min(MOBILE_BAND_MAX, p["range_km"])),
            "state": "hunt", "born_ts": now, "last_wet_ts": now, "dry_cycles": 0,
            "mm": p.get("mm"), "snow": bool(p.get("snow")), "confirmed": False,
            "probe_ct": 0, "last_move_ts": now,
        })


def _fallback_sentinels(disp, n=OC4_FALLBACK_MAX):
    """Pick a FEW sentinels spread evenly across the open arc, to sample via the
    budgeted OC4 sampler when the free Open-Meteo net has failed. Returns references
    into `disp` so applying a sample updates the real net points."""
    sents = [p for p in disp if p.get("kind") == "sentinel"]
    if not sents or n <= 0:
        return []
    k = min(n, len(sents))
    step = len(sents) / k
    return [sents[int(i * step)] for i in range(k)]


def arc_update(state, home, now, net_sample_fn=fetch_om_precip, track_sample_fn=None,
               landsea_fn=fetch_landsea, wind_kmh=None, sample_fn=None, net_ttl_mult=1.0):
    """Two-layer offshore rain detector.

      * NET (free) — permanent sentinels at 40 km + inner pickets at 20 km +
        under-hood dithered fill points, ALL sampled through the batched keyless
        Open-Meteo (net_sample_fn): one call per cycle regardless of point count.
        The displayed sentinel/picket cards are stable; the dither is detection-only
        and fills the gaps a shower could otherwise slip through.
      * MOBILES (budgeted) — on a net detection, up to MOBILE_MAX tracker cards
        spawn in the 5-35 km band, chase the cell inward, jump back to sense what
        follows, then loiter -> retreat to 35 km -> vanish. Their quality reads come
        from track_sample_fn (OC4), so OpenWeather budget is spent only on real
        detections.

    Front speed is measured from the STATIONARY net's nearest-wet range over time and
    passed through the shared speed_trust() gate, so a believable figure feeds the ETA
    and a jumpy one is withheld. Returns (info, vgauges). Back-compat: pass sample_fn
    to use one sampler for both layers."""
    if sample_fn is not None:
        if net_sample_fn is fetch_om_precip:
            net_sample_fn = sample_fn
        if track_sample_fn is None:
            track_sample_fn = sample_fn
    if track_sample_fn is None:
        track_sample_fn = net_sample_fn

    ensure_sea_mask(state, home, now, landsea_fn)
    seaset = _sea_set(state)
    sentinel_az = sorted({a for (a, r) in seaset if r == SENTINEL_KM})
    info = {"detected": False, "dir_spoken": None, "snow": False, "weakening": False,
            "edge_km": None, "speed_kmh": None, "speed_mph": None, "speed_trusted": False,
            "eta_text": None, "n_mobile": 0, "dropout": False}
    if not sentinel_az:                          # no open water around here
        state.mobiles = []
        return info, []

    # ---- dither epoch: keep the hidden fill azimuths stable for a while so the same
    # coordinates are re-sampled (cacheable) rather than a fresh random set each cycle
    # that multiplies the free-tier daily call count.
    if not state.net_dither or (now - (state.net_dither_ts or 0)) >= NET_DITHER_EPOCH_S:
        state.net_dither = {a: random.uniform(-NET_DITHER_DEG, NET_DITHER_DEG) for a in sentinel_az}
        state.net_dither_ts = now
    else:
        for a in sentinel_az:
            state.net_dither.setdefault(a, random.uniform(-NET_DITHER_DEG, NET_DITHER_DEG))

    # ---- NET: sentinels + pickets (shown) + dither fills (hidden). The offshore model
    # moves slowly, so SAMPLE at most once per NET_SAMPLE_TTL_S and reuse the cached
    # readings in between -- the biggest cut to the Open-Meteo daily-call budget.
    disp, fills = _net_points(state, home, seaset, sentinel_az)
    net_pts = disp + fills
    due = (now - (state.net_cache_ts or 0)) >= NET_SAMPLE_TTL_S * max(1.0, net_ttl_mult) or not state.net_cache
    if due:
        rates = net_sample_fn(net_pts) if net_pts else []
        for pt, rt in zip(net_pts, rates):
            _apply_sample(pt, rt)
        # Meter the locations Open-Meteo actually CHARGED for: every point in a request
        # that reached its servers (returned data OR an over-limit error), but not the
        # ones skipped locally during a backoff. Open-Meteo weights per location, so this
        # tracks the true budget footprint rather than only the readings we kept.
        _sent = sum(1 for rt in rates if isinstance(rt, dict)
                    and (rt.get("sent") or rt.get("mm") is not None))
        meter_api("OM", "offshore_net", _sent)
        errs = [rt.get("err") for rt in rates if isinstance(rt, dict) and rt.get("err")]
        all_none = bool(net_pts) and all(p.get("mm") is None for p in net_pts)
        if errs and all_none:
            reason = errs[0]
            _r = (reason or "").lower()
            daily = _om_is_daily(reason)
            transient = (not daily) and any(k in _r for k in ("backing off", "minute", "minutely", "hour", "hourly", "429", "rate", "too many", "quota"))
            # OC4 fallback: sample a few key sentinels through the budgeted track sampler
            # so offshore detection degrades gracefully instead of going blind.
            #
            # Judge COVERAGE by whether each read reached the source, not by whether it
            # found rain: a successful DRY read (mm == 0.0, or sent=True) is coverage,
            # not a blind spot. Counting only mm-not-None previously mislabelled a dry
            # OC4 fallback as "exhausted"/blind on a rain-free arc, even though OC4 had
            # answered and correctly reported no rain. (Honesty over plausibility.)
            covered = 0
            if track_sample_fn is not None and track_sample_fn is not net_sample_fn:
                fb = _fallback_sentinels(disp)
                for pt, rt in zip(fb, track_sample_fn(fb) if fb else []):
                    if isinstance(rt, dict) and (rt.get("sent") or rt.get("mm") is not None):
                        _apply_sample(pt, rt); covered += 1
            if covered:      state.net_feed = "degraded"    # OC4 covering the gap (may be dry)
            elif daily:      state.net_feed = "exhausted"   # daily cap AND no fallback coverage
            elif transient:  state.net_feed = "throttled"   # minute/hour rate-limit: recovers shortly
            else:            state.net_feed = "down"
            state.net_feed_reason = reason
        else:
            state.net_feed = "ok"; state.net_feed_reason = None
        state.net_cache = {(round(p["bearing"]), p["range_km"]):
                           {"mm": p.get("mm"), "snow": p.get("snow"), "src": p.get("src")}
                           for p in net_pts}
        state.net_cache_ts = now
    else:
        for pt in net_pts:
            cp = state.net_cache.get((round(pt["bearing"]), pt["range_km"]))
            if cp:
                pt["mm"] = cp.get("mm"); pt["snow"] = cp.get("snow"); pt["src"] = cp.get("src")
            else:
                pt["mm"] = None; pt["snow"] = False; pt["src"] = None
    net_valid = [p for p in net_pts if p.get("mm") is not None]
    info["dropout"] = bool(net_pts) and not net_valid
    info["net_feed"] = state.net_feed
    info["net_feed_reason"] = state.net_feed_reason
    info["net_sampled_age_s"] = int(now - state.net_cache_ts) if state.net_cache_ts else None
    detections = [p for p in net_pts if (p.get("mm") or 0) >= ARC_DETECT_MMH]

    # ---- front speed from the stationary net's leading wet range over time ------
    net_edge = min((p["range_km"] for p in detections), default=None)
    net_peak = max((p.get("mm") or 0) for p in detections) if detections else None
    if net_edge is not None:
        state.net_edge_hist.append([now, net_edge, net_peak])
    state.net_edge_hist = [e for e in state.net_edge_hist if now - e[0] <= MOBILE_EDGE_WINDOW_S]
    meas = _range_speed(state.net_edge_hist, now)
    mph, trusted = speed_trust(meas, wind_kmh, state.net_speed_kmh) if meas else (None, False)
    if meas:
        state.net_speed_kmh = meas
    # weakening: leading-edge peak fading over the window
    weakening = False
    hp = [e[2] for e in state.net_edge_hist if e[2] is not None]
    if len(hp) >= 2 and hp[-1] is not None and hp[-1] <= hp[0] - ARC_WEAKEN_MMH and hp[-1] < 2.0:
        weakening = True

    # ---- MOBILES: advance existing, then spawn from uncovered detections --------
    _update_mobiles(state, home, now, track_sample_fn, wind_kmh, meas, trusted)
    _spawn_mobiles(state, now, detections)

    # ---- summarise for the alert layer (leading wet mobile, else nearest net) ---
    wet_mob = [m for m in state.mobiles if (m.get("mm") or 0) >= ARC_DETECT_MMH]
    lead = min(wet_mob, key=lambda m: m["range_km"], default=None)
    lead_bearing = lead_range = lead_snow = None
    if lead is not None:
        lead_bearing, lead_range, lead_snow = lead["bearing"], lead["range_km"], lead.get("snow")
    elif detections:
        nd = min(detections, key=lambda p: p["range_km"])
        lead_bearing, lead_range, lead_snow = nd["bearing"], nd["range_km"], nd.get("snow")
    if lead_bearing is not None:
        info["detected"] = True
        info["dir_spoken"] = compass(lead_bearing, spoken=True)
        info["snow"] = bool(lead_snow)
        info["edge_km"] = round(lead_range, 1)
        info["weakening"] = weakening
        if meas:
            info["speed_kmh"], info["speed_mph"], info["speed_trusted"] = round(meas, 1), mph, trusted
            if trusted:
                txt, _, _ = eta_from_speed(lead_range, meas, measured=True)
                info["eta_text"] = txt
    info["n_mobile"] = len(state.mobiles)

    # ---- vgauges: stable sentinels/pickets + live mobiles ----------------------
    def _spd(m):
        return (info["speed_mph"] if (m is lead and info["speed_trusted"]) else None)
    vgauges = []
    for p in disp:
        vgauges.append({
            "modelled": True, "source": "OM", "kind": p["kind"],
            "name": f"{compass(p['bearing'])} sea · {p['bearing']:.0f}°",
            "lat": p["lat"], "lon": p["lon"], "bearing": p["bearing"],
            "dist_km": p["range_km"], "mm": p.get("mm"), "snow": bool(p.get("snow")),
            "confirmed": False, "model_ts": now,
        })
    for m in state.mobiles:
        la, lo = offset_latlon(home[0], home[1], m["bearing"], m["range_km"])
        vgauges.append({
            "modelled": True, "source": ("OC4" if m.get("confirmed") else "OM"),
            "kind": "mobile", "state": m.get("state"),
            "name": f"{compass(m['bearing'])} sea · {m['bearing']:.0f}°",
            "lat": la, "lon": lo, "bearing": m["bearing"], "dist_km": round(m["range_km"], 1),
            "mm": m.get("mm"), "snow": bool(m.get("snow")), "confirmed": bool(m.get("confirmed")),
            "speed_mph": _spd(m), "speed_trusted": bool(m is lead and info["speed_trusted"]),
            "model_ts": now,
        })
    return info, vgauges


# ───────────────────────── template catalogue ───────────────────────────────
# id -> (tier, spoken text with {slots}). Spoken style: no brackets, no digits
# read aloud beyond a plain time window. Precise figures live in screen text.
TEMPLATES = {
    "int_drizzle":   ("notice",  "Drizzle is starting at your location."),
    "int_light":     ("notice",  "Light rain is starting at your location."),
    "int_moderate":  ("notice",  "Rain is falling at your location."),
    "int_heavy":     ("warning", "Heavy rain starting at your location, getting heavier soon."),
    "int_violent":   ("warning", "Very heavy rain at your location. Localised flooding is possible."),
    "trend_heavier": ("notice",  "The rain at your location is picking up."),
    "trend_easing":  ("notice",  "The rain is easing, but hasn't cleared yet."),
    "trend_intermittent": ("notice", "Rain at your location is intermittent."),
    "rain_stopped":  ("notice",  "The rain at your location has stopped."),
    "press_falling": ("notice",  "Pressure is falling steadily. A change in the weather is likely over the next few hours."),
    "press_storm":   ("warning", "Pressure is falling very rapidly. Stormy weather is possible. Secure anything loose outdoors."),
    "press_rising":  ("notice",  "Pressure is rising and the rain is easing. Better weather should be approaching soon."),
    "vis_fog":       ("notice",  "Visibility is dropping quickly. Mist or fog may be forming."),
    "vis_clear":     ("notice",  "Visibility is improving."),
    "approach_win":  ("notice",  "Rain is approaching from the {dir} and may arrive {eta}."),
    "approach_imm":  ("notice",  "Rain is approaching from the {dir} and may arrive within the next fifteen minutes."),
    "approach_soft": ("notice",  "Rain is present to the {dir} and could move your way."),
    "approach_miss": ("notice",  "Rain is passing to the {dir} and is unlikely to reach you."),
    "approach_strengthen":     ("notice", "Rain is approaching from the {dir} and getting stronger. It may arrive {eta}."),
    "approach_strengthen_soft": ("notice", "Rain is building to the {dir} and moving your way."),
    "approach_weaken":         ("notice", "Rain approaching from the {dir} is easing as it nears. It may arrive {eta}, but lighter than before."),
    "approach_fizzle":         ("notice", "Rain approaching from the {dir} is fading and may fizzle out before it reaches you."),
    "approach_gone":           ("notice", "The rain that was approaching from the {dir} has faded and is no longer likely to reach you."),
    "sea_approach":  ("notice",  "Rain may be moving in from the sea to the {dir}. There are no gauges out there to confirm it."),
    "sea_weaken":    ("notice",  "Rain out to the {dir} over the sea is weakening and may not reach the coast."),
    "sea_snow_approach": ("notice", "Wintry showers may be moving in from the sea to the {dir}. There are no gauges out there to confirm it."),
    "sea_speed":     ("notice",  "Rain is moving in from the sea to the {dir} at around {mph} miles per hour. It may reach the coast {eta}."),
    "sea_snow_weaken":   ("notice", "Snow out to the {dir} over the sea is weakening and may not reach the coast."),
    "model_unconf":  ("notice",  "The model suggests rain at your location. This is not yet confirmed by rain gauges in the area."),
    "gauge_confirm": ("notice",  "Rain at your location is now confirmed by nearby gauges."),
    "compound_wet":  ("warning", "Pressure is falling quickly and the wind is increasing. Heavy rain has started and is likely to persist."),
    "flood_corrob":  ("warning", "Heavy rain here, and a flood alert is in force for the area. Keep an eye on local water levels."),
    "nowcast_soon":  ("notice",  "Visibility is dropping and conditions are deteriorating. Rain expected soon."),
    "nowcast_heavier": ("notice", "The rain is expected to become heavier shortly."),
    "fwd_onset":      ("notice", "Rain is expected within about {eta} minutes."),
    "fwd_onset_soon": ("notice", "Rain is expected within the next few minutes."),
    "settled":       ("notice",  "Conditions have settled. No rain is expected in the near term."),
    "feed_stale":    ("notice",  "Weather data is out of date. Rainfall monitoring is paused for now."),
}

# Candidate keys that represent an actual rain threat — rain falling at home, or a
# front / minute-ahead onset arriving. Only these arm the "settled" all-clear, so
# it follows a real rain episode rather than dry-weather pressure/fog chatter.
# Deliberately EXCLUDES: "press"/"clearing" (dry-sky pressure notes), "nowcast"/"vis"
# (fog/visibility). "settled" itself is the all-clear, not an active signal.
RAIN_ACTIVE_KEYS = {
    "compound", "flood", "band", "confirm", "modelonly",
    "trend", "approach", "sea", "forward",
}


def select(situation):
    """Turn the computed situation dict into an ordered list of candidate
    announcements (highest priority first): [(key, tier, phrase), ...].
    'key' is the throttle identity (transition-only announcing keys on it)."""
    band   = situation["band"]
    trend  = situation["trend"]
    pcls   = situation["press_cls"]
    vis    = situation["vis"]
    appr   = situation["approach"]
    sea    = situation["sea"]
    conf   = situation["confirmed"]
    flood  = situation["flood_active"]
    cands = []

    raining = band not in (None, "dry")

    # compound severe first
    if band in ("heavy", "violent") and pcls in ("falling_fast", "falling_storm"):
        cands.append(("compound", *TEMPLATES["compound_wet"]))
    if band in ("heavy", "violent") and flood:
        cands.append(("flood", *TEMPLATES["flood_corrob"]))

    # at-home intensity (only announce the band itself on entry)
    if band == "violent":  cands.append(("band", *TEMPLATES["int_violent"]))
    elif band == "heavy":  cands.append(("band", *TEMPLATES["int_heavy"]))
    elif band == "moderate": cands.append(("band", *TEMPLATES["int_moderate"]))
    elif band == "light": cands.append(("band", *TEMPLATES["int_light"]))
    elif band == "drizzle": cands.append(("band", *TEMPLATES["int_drizzle"]))

    if raining and conf: cands.append(("confirm", *TEMPLATES["gauge_confirm"]))
    if raining and situation["model_only"]:
        cands.append(("modelonly", *TEMPLATES["model_unconf"]))

    # trend
    if raining and trend in ("rising", "rising_fast"):
        cands.append(("trend", *TEMPLATES["trend_heavier"]))
    elif raining and trend in ("easing", "easing_fast"):
        cands.append(("trend", *TEMPLATES["trend_easing"]))

    # pressure / clearing
    if pcls == "falling_storm": cands.append(("press", *TEMPLATES["press_storm"]))
    elif pcls == "falling" and not raining: cands.append(("press", *TEMPLATES["press_falling"]))
    if pcls == "rising" and (trend in ("easing", "easing_fast") or not raining):
        cands.append(("clearing", *TEMPLATES["press_rising"]))

    # visibility
    if vis["dropping"] and vis["low"]:
        if not raining:
            cands.append(("nowcast", *TEMPLATES["nowcast_soon"]))
        else:
            cands.append(("vis", *TEMPLATES["vis_fog"]))

    # directional approach (physical land gauges) — ring-tracked, with a strength
    # trend so a building front, an easing one, and a fizzling shower differ.
    land = situation.get("land")
    if land and land.get("active"):
        d = land.get("dir") or "nearby"
        if land.get("fizzling"):
            cands.append(("approach", TEMPLATES["approach_fizzle"][0],
                          TEMPLATES["approach_fizzle"][1].format(dir=d)))
        elif land.get("intensity_trend") == "weakening":
            eta = land.get("eta_text") or "soon"
            cands.append(("approach", TEMPLATES["approach_weaken"][0],
                          TEMPLATES["approach_weaken"][1].format(dir=d, eta=eta)))
        elif land.get("intensity_trend") == "strengthening":
            if land.get("eta_text"):
                cands.append(("approach", TEMPLATES["approach_strengthen"][0],
                              TEMPLATES["approach_strengthen"][1].format(dir=d, eta=land["eta_text"])))
            else:
                cands.append(("approach", TEMPLATES["approach_strengthen_soft"][0],
                              TEMPLATES["approach_strengthen_soft"][1].format(dir=d)))
        else:  # steady
            if land.get("eta_text") and land.get("eta_lo") is not None and land["eta_lo"] < ETA_MIN_MIN:
                cands.append(("approach", TEMPLATES["approach_imm"][0],
                              TEMPLATES["approach_imm"][1].format(dir=d)))
            elif land.get("eta_text"):
                cands.append(("approach", TEMPLATES["approach_win"][0],
                              TEMPLATES["approach_win"][1].format(dir=d, eta=land["eta_text"])))
            else:
                cands.append(("approach", TEMPLATES["approach_soft"][0],
                              TEMPLATES["approach_soft"][1].format(dir=d)))

    # offshore modelled approach — with the same fizzle awareness
    if sea and sea.get("detected"):
        d = sea.get("dir_spoken") or "the sea"
        snowing = sea.get("snow")
        spd = sea.get("speed_mph") if sea.get("speed_trusted") else None
        eta = sea.get("eta_text")
        if sea.get("weakening"):
            tkey = "sea_snow_weaken" if snowing else "sea_weaken"
            cands.append(("sea", TEMPLATES[tkey][0], TEMPLATES[tkey][1].format(dir=d)))
        elif snowing:
            cands.append(("sea", TEMPLATES["sea_snow_approach"][0],
                          TEMPLATES["sea_snow_approach"][1].format(dir=d)))
        elif spd and eta:
            cands.append(("sea", TEMPLATES["sea_speed"][0],
                          TEMPLATES["sea_speed"][1].format(dir=d, mph=spd, eta=eta)))
        else:
            cands.append(("sea", TEMPLATES["sea_approach"][0],
                          TEMPLATES["sea_approach"][1].format(dir=d)))

    # forward nowcast (OWM one-minute timeline)
    fwd = situation.get("forward")
    if fwd and not raining and fwd.get("onset_eta_min") is not None:
        eta = fwd["onset_eta_min"]
        if eta <= 5:
            cands.append(("forward", *TEMPLATES["fwd_onset_soon"]))
        else:
            cands.append(("forward", TEMPLATES["fwd_onset"][0],
                          TEMPLATES["fwd_onset"][1].format(eta=eta)))
    elif fwd and raining and fwd.get("intensify"):
        cands.append(("forward", *TEMPLATES["nowcast_heavier"]))

    # "settled" (all-clear) is NOT emitted here — it is a one-shot handled in
    # run_probe on the active->calm transition, so it never repeats or fires out
    # of a clear sky.
    return cands


# ───────────────────────── main entry ───────────────────────────────────────
# ───────────────── unified situational engine (WEATHER_ALERT_DESIGN.md, phase A/B) ──
# Read-only for now: builds a spatial picture (per-gauge history -> character ->
# aggregate -> situation state) and exposes it as `situational`. It does NOT yet
# drive any alert; the voice migrates onto it in a later phase.
SIT_HIST_WINDOW_S     = 90 * 60
SIT_RECENCY_S         = 40 * 60      # "still active" = wet within this window (persists through shower gaps)
SIT_WET_MMH           = ARC_DETECT_MMH      # 0.3 mm/h counts as wet
SIT_HERE_KM           = 5
SIT_NEAR_KM           = 20
SIT_MID_KM            = 40
SIT_SHOWERY_MIN_TRANS = 2                   # dry<->wet flips in window to look showery
SIT_STEADY_WETFRAC    = 0.75               # >= this with few flips = steady
SIT_WIDESPREAD_COVER  = 0.35               # wet fraction of the <=MID field for "widespread"
SIT_CONTINUOUS_COVER  = 0.5

_SIT_BAND_NAMES = ["dry", "light", "moderate", "heavy", "very heavy"]


def _iso_ts(s):
    """EA ISO dateTime ('...Z') -> epoch seconds, or None."""
    if not s:
        return None
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _sit_band(mm):
    if mm is None or mm < SIT_WET_MMH: return 0
    if mm < 2:  return 1
    if mm < 5:  return 2
    if mm < 10: return 3
    return 4


def _push_ghist(store, key, ts, mm, window=SIT_HIST_WINDOW_S):
    """Append a [ts, mm] sample to a gauge's rolling history, trimmed to `window`
    and deduped by timestamp so a reading isn't re-counted across polls."""
    buf = [e for e in store.get(key, []) if ts - e[0] <= window]
    if mm is not None and (not buf or buf[-1][0] != ts):
        buf.append([ts, mm])
    if buf:
        store[key] = buf
    else:
        store.pop(key, None)
    return buf


def gauge_character(series, now):
    """Per-gauge character over the history window: wetness, intermittency, band,
    and a dry/steady/showery/wet label."""
    h = [e for e in (series or []) if now - e[0] <= SIT_HIST_WINDOW_S]
    out = {"n": len(h), "wet_fraction": 0.0, "n_transitions": 0, "spikiness": 0.0,
           "cur_band": 0, "peak_band": 0, "wet": False, "recent_wet": False, "label": "dry"}
    if not h:
        return out
    rates = [max(0.0, e[1] or 0.0) for e in h]
    flags = [1 if r >= SIT_WET_MMH else 0 for r in rates]
    out["wet_fraction"] = round(sum(flags) / len(flags), 2)
    out["n_transitions"] = sum(1 for i in range(1, len(flags)) if flags[i] != flags[i-1])
    mean = sum(rates) / len(rates)
    peak = max(rates)
    out["spikiness"] = round(peak / mean, 2) if mean > 0 else 0.0
    out["cur_band"] = _sit_band(rates[-1])
    out["peak_band"] = _sit_band(peak)
    out["wet"] = rates[-1] >= SIT_WET_MMH
    out["recent_wet"] = any((e[1] or 0) >= SIT_WET_MMH for e in h if now - e[0] <= SIT_RECENCY_S)
    wf, nt = out["wet_fraction"], out["n_transitions"]
    if wf < 0.05:
        out["label"] = "dry"
    elif wf >= SIT_STEADY_WETFRAC and nt <= 1:
        out["label"] = "steady"
    elif nt >= SIT_SHOWERY_MIN_TRANS:
        out["label"] = "showery"
    else:
        out["label"] = "wet"
    return out


# ---- phase (d): cluster continuity — track a system as ONE object as it drifts
# across sectors (nearest-match each cycle), with a re-centring approach + ETA. ----
SIT_CLUSTER_LINK_KM = 18            # active gauges within this of a cluster member join it
SIT_TRACK_MATCH_KM  = 22           # a cluster within this of a track's last centre = same track
SIT_TRACK_MAX_MISS  = 2            # cycles a track survives unmatched before it is dropped
SIT_TRACK_WINDOW_S  = 45 * 60      # motion (closing speed) measured over this

def _polar_xy(bearing, dist):
    r = math.radians(bearing)
    return dist * math.sin(r), dist * math.cos(r)

def _cluster(active, link_km):
    """Single-linkage spatial clustering of active gauges (home-relative x,y)."""
    pts = [dict(p) for p in active]
    for p in pts:
        p["_x"], p["_y"] = _polar_xy(p["bearing"], p["dist_km"])
    used = [False] * len(pts)
    clusters = []
    for i in range(len(pts)):
        if used[i]:
            continue
        stack = [i]; used[i] = True; members = []
        while stack:
            j = stack.pop(); members.append(pts[j])
            for k in range(len(pts)):
                if not used[k] and math.hypot(pts[j]["_x"] - pts[k]["_x"], pts[j]["_y"] - pts[k]["_y"]) <= link_km:
                    used[k] = True; stack.append(k)
        clusters.append(members)
    return clusters

def _cluster_summary(members):
    wx = wy = w = 0.0; pk = 0; snow_ct = 0
    for m in members:
        ww = (m["char"]["peak_band"] or 0) + 0.5
        x, y = _polar_xy(m["bearing"], m["dist_km"])
        wx += ww * x; wy += ww * y; w += ww
        pk = max(pk, m["char"]["peak_band"])
        if m.get("snow"): snow_ct += 1
    cx, cy = wx / w, wy / w
    return {"bearing": math.degrees(math.atan2(cx, cy)) % 360,
            "dist_km": math.hypot(cx, cy), "x": cx, "y": cy,
            "peak_band": pk, "snow": snow_ct >= max(1, len(members) // 2), "n": len(members)}

def _ang_diff(a, b):
    """Smallest absolute angular difference (0-180 deg) between two bearings."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)

SIT_DIR_TOL    = 75.0    # deg: a track heading this far off the wind flow is distrusted
SIT_UPWIND_TOL = 90.0    # deg: a track must be within this of UPWIND to count as approaching

def _track_motion(t, now, wind_from=None):
    h = [x for x in t["hist"] if now - x[0] <= SIT_TRACK_WINDOW_S]
    out = {"approaching": False, "speed_kmh": None, "eta_text": None,
           "eta_lo": None, "eta_hi": None,
           "move_kmh": None, "move_deg": None, "move_trusted": False}
    if len(h) < 2:
        return out
    # weather advects roughly DOWNWIND; flow_to is the direction the air moves TOWARD.
    flow_to = ((wind_from + 180.0) % 360.0) if wind_from is not None else None
    dt_hr = (h[-1][0] - h[0][0]) / 3600.0
    closing = h[0][2] - h[-1][2]                 # centre distance decreasing = closing on home
    if dt_hr > 0 and closing > 0:
        spd = closing / dt_hr
        mph, trusted = speed_trust(spd, None, None)   # physical band only (no wind here)
        out["speed_kmh"] = round(spd, 1)
        # A cell can only genuinely be APPROACHING if it sits UPWIND of home (roughly
        # opposite the airflow). A downwind cell whose centroid appears to close is a
        # gauge artifact, not a real approach — reject it against the wind.
        upwind_ok = (wind_from is None) or (_ang_diff(t["bearing"], wind_from) <= SIT_UPWIND_TOL)
        if trusted and upwind_ok and t["dist_km"] > SIT_HERE_KM:
            out["approaching"] = True
            txt, elo, ehi = eta_from_speed(t["dist_km"], spd, measured=True)
            out["eta_text"] = txt; out["eta_lo"] = elo; out["eta_hi"] = ehi
    # motion vector: HEADING of travel, CROSS-CHECKED against the wind. A heading that
    # disagrees with the flow by more than the tolerance is distrusted (centroid jitter):
    # we then show the wind-implied direction, unconfirmed, rather than a bogus one, and
    # withhold a speed we don't believe.
    if dt_hr > 0:
        b0, d0 = math.radians(h[0][1]), h[0][2]
        b1, d1 = math.radians(h[-1][1]), h[-1][2]
        dx = d1 * math.sin(b1) - d0 * math.sin(b0)
        dy = d1 * math.cos(b1) - d0 * math.cos(b0)
        mspd = math.hypot(dx, dy) / dt_hr
        if mspd >= 1.0:
            obs_deg = math.degrees(math.atan2(dx, dy)) % 360.0
            _mph, mtr = speed_trust(mspd, None, None)
            dir_ok = (flow_to is None) or (_ang_diff(obs_deg, flow_to) <= SIT_DIR_TOL)
            out["move_trusted"] = bool(mtr and dir_ok)
            out["move_deg"] = round(obs_deg if dir_ok else flow_to)
            out["move_kmh"] = round(mspd, 1) if out["move_trusted"] else None
    return out

def _next_track_id(tracks):
    """Lowest positive id not currently used by a live track, so a number frees up
    for reuse once its cell has dropped off — ids stay small instead of climbing
    forever. (Uniqueness only matters among concurrent tracks.)"""
    used = {t.get("id") for t in tracks}
    i = 1
    while i in used:
        i += 1
    return i


def update_tracks(points, pstate, now, wind_from=None):
    """Cluster active gauges and match to persistent tracks by nearest centre, so a
    system keeps ONE id as it drifts across sectors. Returns a list of track summaries
    (bearing, dist, intensity, snow, approaching, eta)."""
    active = [p for p in points if p["char"]["wet"] or p["char"].get("recent_wet")]
    clusters = [_cluster_summary(m) for m in _cluster(active, SIT_CLUSTER_LINK_KM)] if active else []
    tracks = pstate.sit_tracks
    for t in tracks:
        t["_matched"] = False
    for cs in clusters:
        best, bd = None, SIT_TRACK_MATCH_KM
        for t in tracks:
            if t["_matched"]:
                continue
            d = math.hypot(cs["x"] - t["_x"], cs["y"] - t["_y"])
            if d < bd:
                bd, best = d, t
        if best is not None:
            best["_matched"] = True
            best["hist"].append([now, cs["bearing"], cs["dist_km"], cs["peak_band"], cs["snow"]])
            best["hist"] = [x for x in best["hist"] if now - x[0] <= SIT_TRACK_WINDOW_S]
            best.update({"bearing": cs["bearing"], "dist_km": cs["dist_km"], "peak_band": cs["peak_band"],
                         "snow": cs["snow"], "n": cs["n"], "last": now, "miss": 0, "_x": cs["x"], "_y": cs["y"]})
        else:
            tracks.append({"id": _next_track_id(tracks), "bearing": cs["bearing"], "dist_km": cs["dist_km"],
                           "peak_band": cs["peak_band"], "snow": cs["snow"], "n": cs["n"],
                           "hist": [[now, cs["bearing"], cs["dist_km"], cs["peak_band"], cs["snow"]]],
                           "first": now, "last": now, "miss": 0, "_matched": True,
                           "_x": cs["x"], "_y": cs["y"]})
    survivors = []
    for t in tracks:
        if t["_matched"]:
            survivors.append(t)
        else:
            t["miss"] = t.get("miss", 0) + 1
            if t["miss"] <= SIT_TRACK_MAX_MISS:
                survivors.append(t)
    pstate.sit_tracks = survivors
    out = []
    for t in survivors:
        mot = _track_motion(t, now, wind_from)
        out.append({"id": t["id"], "bearing": round(t["bearing"]), "dist_km": round(t["dist_km"], 1),
                    "peak_band": t["peak_band"], "snow": bool(t["snow"]), "n": t["n"],
                    "approaching": mot["approaching"], "speed_kmh": mot["speed_kmh"],
                    "eta_text": mot["eta_text"], "eta_lo": mot["eta_lo"], "eta_hi": mot["eta_hi"],
                    "age_s": int(now - t["first"]),
                    "move_kmh": mot["move_kmh"], "move_deg": mot["move_deg"],
                    "move_trusted": mot["move_trusted"]})
    return out


# ---- frugal land model-probes: confirm a showery airmass BETWEEN the gauges ----
# Scattered land showers often sit between EA gauges, so gauge-only detection can read
# "clear" while showers continue. When a showery episode is live we deploy a FEW free
# Open-Meteo probe points around the last cluster (and downwind, where the next cell
# would be) — sparingly (a periodic check, plus one confirmation before any all-clear) —
# so the airmass is read from the model, not guessed. Free/keyless, one batched call.
SIT_PROBE_MAX         = 2            # at most this many probe points per deployment
SIT_PROBE_INTERVAL_S  = 30 * 60      # slow heartbeat while gauges are wet (the clear-check is the key deploy)
SIT_PROBE_DOWNWIND_KM = 12           # (legacy) downwind offset for the old cluster-line placement
SIT_PROBE_UPWIND_KM   = [9.0, 16.0]  # sample the incoming corridor UPWIND OF HOME at these ranges
SIT_PROBE_OFFSHORE_MAX = 5.0         # a probe may sit at most this far over the sea (near-shore gap)
SIT_PROBE_JITTER_DEG  = 12           # rotate the bearing between deployments to cover new ground
SIT_PROBE_EPISODE_CAP = 8            # hard cap on probe SAMPLES per showery episode
SIT_PROBE_EPISODE_S   = 90 * 60      # a showery episode stays "live" this long after the last showery read
SIT_PROBE_FRESH_S     = 30 * 60      # a probe reading older than this can't confirm anything
SIT_PROBE_WET_MMH     = ARC_DETECT_MMH

def _probe_offshore_cap(home, bearing, want_km, seaset, off=SIT_PROBE_OFFSHORE_MAX):
    """If the upwind point is over sea, cap its distance so it sits at most `off` km
    beyond the coastline along that bearing. Inland corridors keep the full desired
    distance. (Sea mask is ~5 km granular, so coast is approximate.)"""
    if not seaset:
        return want_km
    first_sea = None
    for r in sorted(NET_RANGES):            # [5,10,20,30,40]
        if _is_sea(seaset, bearing, r):
            first_sea = r; break
    if first_sea is None:                   # land all the way out -> desired distance
        return want_km
    coast = max(0.0, first_sea - 2.5)       # rough coastline
    return max(3.0, min(want_km, coast + off))

def _land_probe_points(home, centroid, wind_from, seaset=None, phase=0):
    """Home-relevant probe placement: sample the approach corridor UPWIND OF HOME (the
    direction weather arrives from) so a probe sees rain that is about to reach you. If
    the upwind corridor is over water it is allowed up to SIT_PROBE_OFFSHORE_MAX offshore
    to catch a shower before landfall. The bearing rotates a little each deployment
    (phase) so repeated probes cover new ground rather than the same line. Falls back to
    the last cluster's direction only when the wind is unknown."""
    if wind_from is not None:
        base_brg = wind_from                # upwind = toward the wind source
    elif centroid is not None:
        base_brg = centroid[0]
    else:
        return []
    jit = ((phase % 3) - 1) * SIT_PROBE_JITTER_DEG    # -12, 0, +12 rotating
    pts = []
    for i, want in enumerate(SIT_PROBE_UPWIND_KM[:SIT_PROBE_MAX]):
        # keep the pair distinct even when both are offshore-capped: the near point sits
        # centred and closer to the coast, the far point is fanned and reaches further.
        brg = (base_brg + jit + (0.0 if i == 0 else 8.0)) % 360
        off = SIT_PROBE_OFFSHORE_MAX * (0.5 if i == 0 else 1.0)
        d = _probe_offshore_cap(home, brg, want, seaset, off)
        la, lo = offset_latlon(home[0], home[1], brg, d)
        pts.append({"lat": la, "lon": lo,
                    "bearing": bearing_deg(home[0], home[1], la, lo),
                    "dist_km": haversine_km(home[0], home[1], la, lo)})
    return pts

def run_land_probes(state, sit, home, wind_from, now, sampler, cadence_mult=1.0, seaset=None):
    """Deploy the confirmation probes SPARINGLY and SMARTLY, UPWIND OF HOME. Returns
    {active, fresh_clear, probes}.

    When to fire, in priority order:
      * clear-check (the key deploy) — gauges have just gone dry in a live showery
        episode: sample the upwind corridor to answer "is more coming, or has it cleared?"
        Uses the near+far pair.
      * slow heartbeat — while gauges are still wet, an occasional SINGLE upwind look for
        what is next (the gauges already confirm the current rain, so this is light).
    A per-episode sample budget (SIT_PROBE_EPISODE_CAP) caps the spend, the bearing
    rotates each deployment, and probing stops entirely outside a showery episode."""
    if sit["base_state"] in ("isolated_showers", "widespread_showers"):
        state.sit_last_showery_ts = now
        ib, idm = sit["intensity"]["bearing"], sit["intensity"]["dist_km"]
        if ib is not None and idm is not None:
            state.sit_last_centroid = [ib, idm]
    episode = (now - (state.sit_last_showery_ts or 0)) <= SIT_PROBE_EPISODE_S
    if not episode or (wind_from is None and state.sit_last_centroid is None):
        state.sit_probes = []
        return {"active": False, "fresh_clear": False, "probes": []}
    # reset the per-episode budget after a long gap (a fresh episode)
    if now - (state.sit_probe_used_ts or 0) > SIT_PROBE_EPISODE_S:
        state.sit_probe_used = 0
    gauges_wet = sit["n_wet"] > 0
    since = now - (state.sit_probe_ts or 0)
    clear_check = (not gauges_wet) and since >= SIT_PROBE_FRESH_S
    heartbeat = gauges_wet and since >= SIT_PROBE_INTERVAL_S * max(1.0, cadence_mult)
    if (clear_check or heartbeat) and state.sit_probe_used < SIT_PROBE_EPISODE_CAP:
        pts = _land_probe_points(home, state.sit_last_centroid, wind_from, seaset, state.sit_probe_phase)
        if heartbeat and not clear_check:
            pts = pts[:1]                       # a heartbeat needs only one upwind look
        rates = sampler(pts) if pts else []
        # Count locations Open-Meteo charged for (sent), not just the ones that returned data.
        meter_api("OM", "land_probe", sum(1 for rt in rates if isinstance(rt, dict)
                                          and (rt.get("sent") or rt.get("mm") is not None)))
        probes = []
        for pt, rt in zip(pts, rates):
            mm = rt.get("mm") if isinstance(rt, dict) else rt
            probes.append({"bearing": round(pt["bearing"]), "dist_km": round(pt["dist_km"], 1),
                           "mm": mm, "snow": bool(rt.get("snow")) if isinstance(rt, dict) else False,
                           "ts": now})
        state.sit_probes = probes
        state.sit_probe_ts = now
        state.sit_probe_used += len(pts); state.sit_probe_used_ts = now
        state.sit_probe_phase += 1
    recent = [pr for pr in state.sit_probes if now - pr["ts"] <= SIT_PROBE_FRESH_S]
    active = any((pr["mm"] or 0) >= SIT_PROBE_WET_MMH for pr in recent)
    fresh_clear = bool(recent) and not active
    return {"active": active, "fresh_clear": fresh_clear, "probes": state.sit_probes}


# ---- phase (c): cadence + wording, one voice for the situation state ----
CAD_HERE_HEAVY   = 10 * 60          # heartbeat while heavy+ rain is AT home
CAD_HERE_LIGHT   = 20 * 60          # heartbeat while light/moderate at home
CAD_SHOWERS      = 40 * 60          # heartbeat while showers persist in the area
CAD_CONTINUOUS   = 60 * 60          # heartbeat while continuous rain persists
SIT_DWELL_S      = 9 * 60           # a state must persist this long before an AREA announcement

def _sit_group(sit):
    if sit["here"]:
        return "here"
    b = sit["base_state"]
    if b in ("isolated_showers", "widespread_showers"): return "showers"
    if b == "continuous": return "continuous"
    if b == "approaching": return "approaching"
    if b == "wet": return "wet"          # active but uncharacterised: silent, but NOT clear
    return "clear"

def _cap(s): return s[0].upper() + s[1:] if s else s

def _sit_here_phrase(sit):
    noun = "snow" if sit["snow"] else "rain"
    lead = {"very heavy": "Very heavy", "heavy": "Heavy", "moderate": "",
            "light": "Light", "dry": ""}.get(sit["intensity"]["name"], "")
    body = (lead + " " + noun).strip()
    if sit["snow"]:
        return _cap(body) + " is falling at your location."
    return _cap(body) + " at your location."

def _sit_showers_phrase(sit):
    noun = "wintry showers" if sit["snow"] else "showers"
    kind = "widespread " if sit["base_state"] == "widespread_showers" else "isolated "
    inten = sit["intensity"]["name"]
    heavy = inten in ("heavy", "very heavy")
    dist = sit["intensity"]["dist_km"]
    d = sit.get("_dir_word")
    if d is None:
        b = sit["intensity"]["bearing"]
        d = compass(b, spoken=True) if b is not None else None
    lead = kind + ((inten + " ") if heavy else "") + noun
    where = (" to the " + d) if d else " in the area"
    tail = ", which may be with you soon" if (heavy and dist is not None and dist <= 20) else ""
    return _cap(lead) + where + tail + "."

def _sit_approach_phrase(sit, tr):
    noun = "snow" if tr["snow"] else "rain"
    inten = _SIT_BAND_NAMES[tr["peak_band"]]
    lead = ((inten + " ") if inten in ("heavy", "very heavy") else "") + noun
    d = sit.get("_dir_word") or compass(tr["bearing"], spoken=True)
    eta = tr.get("eta_text") or "soon"
    return _cap(lead) + " is approaching from the " + d + ", " + eta + "."

def _sit_continuous_phrase(sit):
    noun = "snow" if sit["snow"] else "rain"
    inten = sit["intensity"]["name"]
    lead = "continuous " + ((inten + " ") if inten in ("heavy", "very heavy") else "") + noun
    return _cap(lead) + " has set in."

def _sector_hyst(prev_sect, brg, margin=12.0):
    """Bin a bearing to an 8th, but keep the previous sector until the bearing is
    clearly (half a sector + margin) past its centre — stops a system on a boundary
    from flip-flopping its announced direction."""
    if brg is None:
        return prev_sect
    new_sect = int(round((brg % 360) / 45.0)) % 8
    if prev_sect is None or new_sect == prev_sect:
        return new_sect
    center = prev_sect * 45.0
    diff = abs((brg - center + 180) % 360 - 180)
    return new_sect if diff > (22.5 + margin) else prev_sect


def schedule_situational(sit, pstate, now, mult=1.0):
    """Turn the situation into cadence-scheduled spoken alerts. One voice: an area
    state is announced once it is certain (dwell), then on a slow heartbeat and on any
    material change; rain AT home is always announced immediately and repeats faster;
    clearing is a one-shot. `mult` (Normal 1.0 / Low ~2.0) stretches the heartbeats.
    Mutates pstate scheduling only. Returns [{tier,key,phrase}]."""
    out = []
    grp = _sit_group(sit)
    prev_grp = pstate.sit_group
    if grp != prev_grp:
        pstate.sit_group = grp
        pstate.sit_group_since = now
    stable = now - (pstate.sit_group_since or now)
    if grp not in ("showers", "approaching"):
        pstate.sit_dir_sect = None

    # clearing one-shot: only when we transition to CLEAR *and* this episode actually
    # announced something (so a silent onset blip never says "clearing").
    if grp == "clear":
        if pstate.sit_episode_spoke:
            noun = "snow" if pstate.sit_last_snow else "rain"
            out.append({"tier": "notice", "key": "sit_clear", "phrase": "The " + noun + " is clearing."})
        pstate.sit_episode_spoke = False
        pstate.sit_announced = {}
        return out

    key = tier = phrase = None
    hb = 3600.0
    if grp == "here":
        b = sit["intensity"]["peak_band"]
        key, tier = "here", ("warn" if b >= 3 else "notice")
        phrase = _sit_here_phrase(sit)
        hb = (CAD_HERE_HEAVY if b >= 3 else CAD_HERE_LIGHT) * mult
    elif grp == "showers" and stable >= SIT_DWELL_S:
        key, tier = "showers", "notice"
        stable_sect = _sector_hyst(pstate.sit_dir_sect, sit["intensity"]["bearing"])
        pstate.sit_dir_sect = stable_sect
        sit = dict(sit)                    # don't mutate the diagnostic object
        sit["_dir_word"] = compass(stable_sect * 45, spoken=True) if stable_sect is not None else None
        sit["_dir_sect"] = stable_sect
        phrase = _sit_showers_phrase(sit)
        hb = CAD_SHOWERS * mult
    elif grp == "continuous" and stable >= SIT_DWELL_S:
        key, tier = "continuous", "notice"
        phrase = _sit_continuous_phrase(sit)
        hb = CAD_CONTINUOUS * mult
    elif grp == "approaching" and sit.get("approach_track") and stable >= SIT_DWELL_S:
        tr = sit["approach_track"]
        key, tier = "approach", "notice"
        stable_sect = _sector_hyst(pstate.sit_dir_sect, tr["bearing"])
        pstate.sit_dir_sect = stable_sect
        sit = dict(sit)
        sit["_dir_word"] = compass(stable_sect * 45, spoken=True) if stable_sect is not None else None
        sit["_dir_sect"] = stable_sect
        phrase = _sit_approach_phrase(sit, tr)
        hb = CAD_CONTINUOUS * mult

    if key:
        pstate.sit_last_snow = bool(sit["snow"])
        # signature: re-announce on a MATERIAL change (state, intensity band, snow,
        # and — for showers — the heaviest cell's sector), or when the heartbeat is
        # due. Direction jitter and wording tweaks never re-trigger.
        pk = sit["intensity"]["peak_band"]
        if key in ("showers", "approach"):
            _sect = sit.get("_dir_sect")
            sig = [key, sit["base_state"], pk, bool(sit["snow"]), (_sect if _sect is not None else -1)]
        else:
            sig = [key, pk, bool(sit["snow"])]
        prev = pstate.sit_announced.get(key)
        # a signature change re-announces only when it is NOT an intensity downgrade
        # (so the peak decaying as history ages doesn't announce "easing" then clear);
        # heartbeat and first-appearance always fire.
        sig_changed = (prev is None) or (prev.get("sig") != sig)
        not_downgrade = (prev is None) or (pk >= prev.get("pk", 0))
        due = (prev is None) or (now >= prev.get("next", 0)) or (sig_changed and not_downgrade)
        if due:
            out.append({"tier": tier, "key": "sit_" + key, "phrase": phrase})
            pstate.sit_episode_spoke = True
            pstate.sit_announced = {key: {"sig": sig, "pk": pk, "next": now + hb}}
        else:
            pstate.sit_announced = {key: {"sig": prev.get("sig"), "pk": prev.get("pk", pk),
                                          "next": prev.get("next", now + hb)}}
    return out


def build_situational(points, home, wind_from, land, sea, now):
    """Aggregate per-gauge characters into a situation state + locus + intensity.
    points: [{bearing, dist_km, char, snow, kind}]. Read-only diagnostic.

    State is driven by RECENT ACTIVITY over the history window (so an intermittent
    shower doesn't read "clear" during its dry phase), and coverage is measured over
    the PHYSICAL land field only (the dry sea sentinels must not dilute it)."""
    field = [p for p in points if p.get("dist_km") is not None and p["dist_km"] <= SIT_MID_KM]
    phys = [p for p in field if p.get("kind") == "phys"]

    def _active(p):
        c = p["char"]
        return c["wet"] or c.get("recent_wet", False)      # rain now, or within the recency window

    active = [p for p in field if _active(p)]
    active_phys = [p for p in phys if _active(p)]
    wet_now = [p for p in field if p["char"]["wet"]]
    n_phys = len(phys)
    coverage = round(len(active_phys) / n_phys, 2) if n_phys else (
        round(len(active) / len(field), 2) if field else 0.0)
    # steady/showery CHARACTER comes only from physical tipping-bucket gauges — the
    # model net points are continuous by nature and have no real intermittency signal.
    showery = [p for p in active if p.get("kind") == "phys" and p["char"]["label"] == "showery"]
    steady = [p for p in active if p.get("kind") == "phys" and p["char"]["label"] == "steady"]
    shower_conf = round(sum(max(0.0, 1.0 - p["dist_km"] / SIT_MID_KM) for p in showery), 2)
    peak_band = max((p["char"]["peak_band"] for p in active), default=0)
    heavy = max(active, key=lambda p: (p["char"]["peak_band"], -p["dist_km"])) if active else None
    # intensity-weighted CENTROID bearing/distance of the active cluster: a stable
    # direction that doesn't flip when the single heaviest gauge jumps across a
    # sector boundary (a system spanning sectors reads as one, centred smoothly).
    _cx = _cy = _cd = _cw = 0.0
    for _p in active:
        _w = (_p["char"]["peak_band"] or 0) + 0.5
        _rb = math.radians(_p["bearing"])
        _cx += _w * math.sin(_rb); _cy += _w * math.cos(_rb); _cd += _w * _p["dist_km"]; _cw += _w
    cbrg = (math.degrees(math.atan2(_cx, _cy)) % 360) if _cw > 0 else None
    cdist = (_cd / _cw) if _cw > 0 else None
    nearest = min(wet_now, key=lambda p: p["dist_km"]) if wet_now else None
    here = bool(nearest and nearest["dist_km"] < SIT_HERE_KM)
    snow_wet = [p for p in active if p.get("snow")]
    snow = bool(active) and len(snow_wet) >= max(1, len(active) // 2)
    approach = bool((land and land.get("active")) or (sea and sea.get("detected")))

    # state — activity-based, with an absolute count guard so "widespread" needs a
    # genuinely broad field, not just "all of my two gauges are wet".
    if not active and not approach:
        stt = "clear"
    elif not active and approach:
        stt = "approaching"
    elif coverage >= SIT_CONTINUOUS_COVER and len(steady) >= max(1, len(showery)):
        stt = "continuous"
    elif len(showery) >= 2 and coverage >= SIT_WIDESPREAD_COVER and len(active) >= 4:
        stt = "widespread_showers"
    elif showery:
        stt = "isolated_showers"
    else:
        stt = "wet"                                        # active but uncharacterised (onset / short history)

    if here:
        loc = {"here": True, "nearest_km": round(nearest["dist_km"], 1)}
    elif nearest:
        loc = {"here": False, "nearest_km": round(nearest["dist_km"], 1),
               "bearing": round(nearest["bearing"])}
    elif heavy:
        loc = {"here": False, "bearing": round(heavy["bearing"]), "dist_km": round(heavy["dist_km"], 1)}
    elif approach:
        d = (sea.get("dir_spoken") if sea and sea.get("detected") else (land.get("dir") if land else None))
        loc = {"here": False, "approach_dir": d}
    else:
        loc = None
    return {
        "state": stt + ("_snow" if snow and stt != "clear" else ""),
        "base_state": stt, "snow": snow,
        "n_field": len(field), "n_phys": n_phys, "n_active": len(active), "n_wet": len(wet_now),
        "nearest_wet_km": (round(nearest["dist_km"], 1) if nearest else None),
        "coverage": coverage, "shower_confidence": shower_conf,
        "n_showery": len(showery), "n_steady": len(steady),
        "intensity": {"peak_band": peak_band, "name": _SIT_BAND_NAMES[peak_band],
                      "bearing": (round(cbrg) if cbrg is not None else None),
                      "dist_km": (round(cdist, 1) if cdist is not None else None)},
        "locus": loc, "here": here, "approaching": approach,
    }


def run_probe(state: ProbeState, *, home, rain_mm_h, pressure_hpa, visibility_m,
              wind_from, wind_kmh, gauges, flood_active=False, feed_stale=False,
              now=None, sample_fn=None, net_sample_fn=fetch_om_precip,
              track_sample_fn=None, landsea_fn=fetch_landsea, forward_precip=None,
              cadence_mult=1.0, sampling_mult=1.0, pressure_change_3h=None):
    """Evaluate everything for one cycle. Read-only: mutates only `state`.
    Returns a diagnostic dict incl. `would_speak` (transitions this cycle) and
    `virtual_gauges` (marked, modelled). Nothing here plays a tone."""
    now = now or time.time()

    if feed_stale:
        return {"ts": now, "feed_stale": True,
                "would_speak": [{"tier": "notice", "key": "stale",
                                 "phrase": TEMPLATES["feed_stale"][1]}],
                "virtual_gauges": [], "log": "feed stale — monitoring paused"}

    # update rolling histories
    if rain_mm_h is not None: state.rain_hist.append((now, rain_mm_h))
    if visibility_m is not None: state.vis_hist.append((now, visibility_m))
    state.rain_hist = _trim(state.rain_hist, now, TREND_WINDOW_S)
    state.vis_hist = _trim(state.vis_hist, now, VIS_WINDOW_S)

    band = classify_intensity(rain_mm_h)
    trend, trend_d = compute_trend(state.rain_hist, now)
    # pressure tendency comes from the panel's authoritative persisted 3h figure
    # (single source of truth) rather than a second in-memory slope.
    prate, pcls = pcls_from_change3h(pressure_change_3h)
    warm = pressure_change_3h is None
    vis = visibility_state(state.vis_hist, now, visibility_m)
    appr = gauge_approach(gauges, home, wind_from, wind_kmh)
    land = land_front(state, gauges, home, wind_from, wind_kmh, now)
    sea, vgauges = arc_update(state, home, now, net_sample_fn=net_sample_fn,
                              track_sample_fn=track_sample_fn, landsea_fn=landsea_fn,
                              wind_kmh=wind_kmh, sample_fn=sample_fn, net_ttl_mult=sampling_mult)
    forward = compute_forward(forward_precip, now, band)

    # ---- situational engine (read-only diagnostic, WEATHER_ALERT_DESIGN.md phase A/B) --
    _sit_pts = []
    for _g in (gauges or []):
        if _g.get("lat") is None or _g.get("lon") is None:
            continue
        _k = "g:" + str(_g.get("ref") or _g.get("label") or id(_g))
        _mm = _g.get("mm_h"); _mm = _mm if _mm is not None else _g.get("mm")
        _push_ghist(state.gauge_hist, _k, _iso_ts(_g.get("dt")) or now, _mm)
        _sit_pts.append({"bearing": bearing_deg(home[0], home[1], _g["lat"], _g["lon"]),
                         "dist_km": _g.get("dist_km") or haversine_km(home[0], home[1], _g["lat"], _g["lon"]),
                         "char": gauge_character(state.gauge_hist.get(_k), now),
                         "snow": False, "kind": "phys",
                         "name": (_g.get("label") or _g.get("ref") or "gauge"),
                         "mm": _mm, "last_ts": _iso_ts(_g.get("dt"))})
    for _vg in vgauges:
        if _vg.get("kind") not in ("sentinel", "picket"):
            continue
        _k = f"n:{_vg['bearing']:.0f}:{_vg['dist_km']}"
        _push_ghist(state.gauge_hist, _k, now, _vg.get("mm"))
        _sit_pts.append({"bearing": _vg["bearing"], "dist_km": _vg["dist_km"],
                         "char": gauge_character(state.gauge_hist.get(_k), now),
                         "snow": bool(_vg.get("snow")), "kind": "net",
                         "name": _vg.get("name"), "mm": _vg.get("mm"), "last_ts": now})
    situational = build_situational(_sit_pts, home, wind_from, land, sea, now)
    if situational["base_state"] != state.sit_state:
        state.sit_state = situational["base_state"]; state.sit_state_since = now
    situational["stable_s"] = int(now - (state.sit_state_since or now))
    # per-gauge points for the engine debug view (bearing, distance, character)
    situational["points"] = [{"b": round(p["bearing"]), "d": round(p["dist_km"], 1),
                              "kind": p["kind"], "label": p["char"]["label"],
                              "band": p["char"]["peak_band"], "cur": p["char"]["cur_band"],
                              "wet": bool(p["char"]["wet"]), "snow": bool(p.get("snow")),
                              "name": p.get("name"), "trans": p["char"]["n_transitions"],
                              "mm": (round(p["mm"], 2) if p.get("mm") is not None else None),
                              "age_s": (int(now - p["last_ts"]) if p.get("last_ts") else None)}
                             for p in _sit_pts]
    # at-home reading that drives the nowcast "at your location" voice (model/nearest
    # sensor rain rate) — surfaced so the view can SHOW what caused an at-home alert.
    situational["home"] = {"mm": (round(rain_mm_h, 2) if rain_mm_h is not None else None),
                           "band": (_sit_band(rain_mm_h) if rain_mm_h is not None else 0),
                           "name": (classify_intensity(rain_mm_h) or "dry")}
    situational["ts"] = now
    situational["net_feed"] = sea.get("net_feed")
    situational["net_feed_reason"] = sea.get("net_feed_reason")
    situational["net_sampled_age_s"] = sea.get("net_sampled_age_s")
    # active offshore mobile trackers (spawn only on sea detections) — so they appear
    # on the plan view the moment they are deployed.
    situational["mobiles"] = [{"b": round(m["bearing"]), "d": m["dist_km"], "mm": m.get("mm"),
                               "snow": bool(m.get("snow")), "confirmed": bool(m.get("confirmed")),
                               "speed_mph": m.get("speed_mph"), "state": m.get("state")}
                              for m in vgauges if m.get("kind") == "mobile"]
    # phase (d): cluster continuity — track systems across sectors; if home is dry but
    # a tracked cluster is closing, surface it as ONE approaching system (dir + ETA).
    _tracks = update_tracks(_sit_pts, state, now, wind_from)
    situational["tracks"] = _tracks
    _appr = [t for t in _tracks if t.get("approaching")]
    _appr_tr = min(_appr, key=lambda t: t["dist_km"]) if _appr else None
    situational["approach_track"] = _appr_tr
    # promote to "approaching" while the tracked cluster's CENTRE is still beyond
    # NEAR (smooth: uses the track distance, not the flappy nearest-wet gauge) and
    # nothing is right at home.
    if _appr_tr and not situational["here"] and _appr_tr["dist_km"] > SIT_NEAR_KM:
        situational["base_state"] = "approaching"
        situational["snow"] = bool(_appr_tr["snow"])
        situational["state"] = "approaching" + ("_snow" if _appr_tr["snow"] else "")
        pb = max(situational["intensity"]["peak_band"], _appr_tr["peak_band"])
        situational["intensity"]["peak_band"] = pb
        situational["intensity"]["name"] = _SIT_BAND_NAMES[pb]
        situational["locus"] = {"here": False, "bearing": _appr_tr["bearing"],
                                "dist_km": _appr_tr["dist_km"],
                                "approach_dir": compass(_appr_tr["bearing"], spoken=True),
                                "eta_text": _appr_tr["eta_text"]}
        if situational["base_state"] != state.sit_state:
            state.sit_state = situational["base_state"]; state.sit_state_since = now
    # frugal land-probe corroboration: hold showers through gauge gaps while the model
    # shows the airmass is live, and gate the all-clear on a fresh model confirmation.
    _probe = run_land_probes(state, situational, home, wind_from, now, net_sample_fn,
                             cadence_mult=sampling_mult, seaset=_sea_set(state))
    situational["probe"] = {"active": _probe["active"], "fresh_clear": _probe["fresh_clear"],
                            "n": len(_probe["probes"])}
    situational["probe_points"] = _probe["probes"]
    if situational["base_state"] == "clear" and (now - (state.sit_last_showery_ts or 0)) <= SIT_PROBE_EPISODE_S:
        if _probe["active"]:
            _pw = [pr for pr in _probe["probes"] if (pr["mm"] or 0) >= SIT_PROBE_WET_MMH]
            _snow = bool(_pw) and sum(1 for pr in _pw if pr["snow"]) >= max(1, len(_pw) // 2)
            situational["base_state"] = "isolated_showers"; situational["snow"] = _snow
            situational["state"] = "isolated_showers" + ("_snow" if _snow else "")
            if _pw:
                _near = min(_pw, key=lambda x: x["dist_km"])
                _pb = max(_sit_band(pr["mm"]) for pr in _pw)
                situational["intensity"]["peak_band"] = max(situational["intensity"]["peak_band"], _pb)
                situational["intensity"]["name"] = _SIT_BAND_NAMES[situational["intensity"]["peak_band"]]
                situational["intensity"]["bearing"] = _near["bearing"]
                situational["intensity"]["dist_km"] = _near["dist_km"]
        elif not _probe["fresh_clear"]:
            # gauges dry but no fresh model confirmation yet -> HOLD, don't clear
            situational["base_state"] = "isolated_showers"
            situational["state"] = "isolated_showers" + ("_snow" if situational["snow"] else "")
        if situational["base_state"] != state.sit_state:
            state.sit_state = situational["base_state"]; state.sit_state_since = now
    situational_speak = schedule_situational(situational, state, now, mult=cadence_mult)

    confirmed = bool(appr and appr.get("confirmed_at_home"))
    raining = band not in (None, "dry")
    model_only = raining and not confirmed and not (appr and appr.get("n_wet"))

    situation = {"band": band, "trend": trend, "press_cls": pcls, "vis": vis,
                 "approach": appr, "land": land, "sea": sea, "confirmed": confirmed,
                 "model_only": model_only, "flood_active": flood_active,
                 "wind_from": wind_from, "forward": forward}

    cands = select(situation)

    # Edge-triggered announcing: one message per key (highest priority first),
    # spoken only when that key first appears or its phrase changes. A key absent
    # this cycle is dropped, so the same condition can speak again if it recurs.
    # A persistent state is therefore announced ONCE, never on a repeating timer.
    would = []
    now_keys = {}
    for key, tier, phrase in cands:
        if key not in now_keys:
            now_keys[key] = (tier, phrase)
    for key, (tier, phrase) in now_keys.items():
        if state.announced.get(key) != phrase:
            would.append({"tier": tier, "key": key, "phrase": phrase})
    state.announced = {k: v[1] for k, v in now_keys.items()}

    # Land-front fade: a ONE-SHOT when a front that was approaching weakens or
    # veers away before arriving (and hasn't simply arrived — the band/confirm
    # messages cover a real arrival). Announced with its last tracked direction.
    land_active_now = bool(land and land.get("active"))
    if state.land_active and not land_active_now and not raining and not confirmed:
        would.append({"tier": "notice", "key": "land_fade",
                      "phrase": TEMPLATES["approach_gone"][1].format(
                          dir=(state.land_last_dir or "that direction"))})
    state.land_active = land_active_now
    if land_active_now and land.get("dir"):
        state.land_last_dir = land["dir"]

    # All-clear: a ONE-SHOT, spoken only on the transition from an active
    # RAIN situation to calm — never on a calm-from-start day, never repeated. A
    # land-fade this cycle already IS the all-clear for that front, so settled
    # only speaks when nothing else did.
    #
    # Only genuine rain signals (present rain at home, or a front/onset arriving)
    # arm the all-clear. Benign dry-weather chatter — pressure rising or falling
    # under a dry sky, or a fog/visibility note — must NOT arm it, or a pressure
    # blip flickering across a threshold on a dry day fires a bogus "no rain
    # expected in the near term" every time it falls away.
    rain_active = any(k in RAIN_ACTIVE_KEYS for k in now_keys)
    if rain_active:
        state.was_active = True
    elif state.was_active:
        if not would:
            would.append({"tier": "notice", "key": "settled",
                          "phrase": TEMPLATES["settled"][1]})
        state.was_active = False

    log = (f"band={band} trend={trend}({trend_d:+.1f}) "
           f"press={prate:+.2f}hPa/h[{pcls}{'/warmup' if warm else ''}] vis={'low' if vis['low'] else 'ok'}"
           f"{'/drop' if vis['dropping'] else ''} "
           f"arc={'det' if sea.get('detected') else 'clear'}"
           + (f" edge={sea['edge_km']}km" if sea.get("edge_km") else "")
           + (f" mob={sea['n_mobile']}" if sea.get("n_mobile") else "")
           + (f" spd={sea['speed_mph']}mph{'*' if sea.get('speed_trusted') else '?'}" if sea.get("speed_mph") else "")
           + (" dropout" if sea.get("dropout") else "")
           + (" arc_weak" if sea.get("weakening") else "")
           + (f" | land {land['dir']} edge={land['edge_km']}km ring={land['ring']}"
              f" {land['intensity_trend']}{'/fizzle' if land.get('fizzling') else ''}"
              f"{'(measured)' if land.get('measured') else '(wind)'}"
              f" eta={land['eta_text']}"
              if land and land.get("active") else "")
           + (f" fwd_onset={forward['onset_eta_min']}min" if forward and forward.get("onset_eta_min") is not None else "")
           + (" fwd_heavier" if forward and forward.get("intensify") else "")
           + (" | WOULD SPEAK: " + " || ".join(w["phrase"] for w in would) if would else "")
           + f" | SIT {situational['state']} cov={situational['coverage']} n_wet={situational['n_wet']}"
             f" shc={situational['shower_confidence']} peak={situational['intensity']['name']}")

    return {"ts": now, "situation": situation, "situational": situational,
            "situational_speak": situational_speak, "would_speak": would,
            "virtual_gauges": vgauges, "log": log,
            "signals": {"band": band, "trend": trend, "trend_delta": round(trend_d, 2),
                        "press_rate": round(prate, 2), "press_cls": pcls,
                        "vis": vis, "arc": sea, "confirmed": confirmed,
                        "model_only": model_only, "warming_up": warm}}
