# rain_probe.py — read-only rainfall-alert DIAGNOSTIC probe for GB Energy Monitor
#
# Build 260930.1  (version = YYMMDD.N in UT; bump on every change to this file)
# Change history: CHANGELOG.md
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
OC4_FALLBACK_MAX   = 4                # on Open-Meteo failure, sample <= this many sea points via OC4
NET_FB_UPWIND      = 2                # ...of which this many are the points nearest the flow's source;
                                      # the rest rotate round the other sentinels/pickets each sample
NET_OC4_KEEP_S     = 60 * 60          # a kept OC4 sea reading counts (can detect) for this long...
NET_OC4_DROP_S     = 3 * 3600         # ...is shown as stale after that, and dropped after this
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
GAUGE_LATE_S     = 75 * 60     # an EA gauge whose newest reading is older than this is LATE: it
                               # says nothing about now, so it must not read as "dry". Normal EA lag
                               # (15-min readings, published ~20-30 min late, plus our index refresh
                               # and cache) reaches ~60 min just before each batch, so 75 min only
                               # catches genuinely delayed data.
GAUGE_FRESH_S    = 60 * 60     # hysteresis: once ALL gauges are late (EA treated as unavailable,
                               # home ring stands in), EA counts again only when the newest reading
                               # is younger than this -- no flip-flopping around each EA batch
UPWIND_HALF_ANGLE = 60        # a gauge within +/- this of wind-from is "upwind"

# Steering flow. Showers move with the ~850 hPa wind, not the 10 m wind. One keyless
# Open-Meteo pressure-level read at home covers the whole field; it is the ONE flow used
# wherever the probe asks "where is rain coming from, and how fast" (approach cone, land
# front, gauge approach, track checks, land-probe placement, sea-tracker chase, sea speed).
# Without it the flow is surface wind x ADVECT_FAST_FACTOR — exactly the pre-steering
# behaviour, so an Open-Meteo outage changes nothing.
STEER_TTL_S       = 60 * 60     # re-read the 850 hPa wind at most this often
STEER_RETRY_S     = 15 * 60     # after a failed read, retry this soon
STEER_KEEP_S      = 3 * 3600    # keep a failed-to-refresh 850 hPa read this long (marked old)
STEER_RATIO_LO    = 0.5         # measured speed must be >= this x steering speed...
STEER_RATIO_HI    = 1.5         # ...and <= this x steering speed
STEER_HEADING_TOL = 40.0        # measured heading within +/- this of the steering flow
SURF_HEADING_TOL  = 75.0        # looser heading check when the flow is only a surface estimate
TRACK_MIN_SPAN_S  = 10 * 60     # a track needs a gauge hand-over across at least this long

# Stationary / suspect gauges: a gauge wet for hours in one place (static drizzle, or a
# stuck/faulty bucket) must not look like an approaching cell.
GAUGE_STAT_WINDOW_S  = 3 * 3600  # history judged
GAUGE_STAT_MIN_READS = 10        # readings needed before judging
GAUGE_STAT_SET       = 0.75      # wet (> 0) in >= this fraction -> flagged
GAUGE_STAT_CLEAR     = 0.50      # flag clears below this fraction
GAUGE_CHECK_S        = 60 * 60   # hourly free Open-Meteo spot-check of flagged gauges
GAUGE_CHECK_WET_MMH  = 0.05      # model rate at the gauge that counts as "model wet"

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
# OPEN_METEO_MODELS (optional, e.g. icon_eu) pins every request to a named model. A
# self-hosted instance only holds the models you synced, and its default best_match
# picks others, returning all-null values -- so set this when self-hosting. Unset =
# no models= parameter (public host behaviour unchanged).
OM_MODELS = os.environ.get("OPEN_METEO_MODELS", "").strip()
# LOCAL-FIRST with automatic fallback. When OPEN_METEO_BASE points somewhere other than
# the public host, every request tries that local instance first (short timeout, with
# OPEN_METEO_MODELS). If it is unreachable, errors, or answers with nothing but nulls,
# the SAME request goes to the public host instead (no models= -- exactly the old
# behaviour), local is marked down for OM_LOCAL_RETRY_S, then retried; it recovers on
# its own. OPEN_METEO_BASE unset = public only, and none of this runs.
OM_PUBLIC_BASE = "https://api.open-meteo.com/v1"
OM_PUBLIC_URL = OM_PUBLIC_BASE + "/forecast"
OM_LOCAL = OM_BASE != OM_PUBLIC_BASE
OM_LOCAL_TIMEOUT_S = 3.0         # a dead local server must never stall a refresh
OM_LOCAL_RETRY_S = 300           # after a local failure, go straight to public this long
_om_local = {"down_until": 0.0, "reason": None, "since": None,
             "last_host": None, "last_ts": None}
_om_local_lock = threading.Lock()
_om_tls = threading.local()      # host that served THIS thread's last OM request (metering)


class OMBackoff(Exception):
    """Public Open-Meteo needed but still cooling down after a rate-limit."""


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
    if api == "OM" and getattr(_om_tls, "host", None) == "local":
        api = "OM-local"         # served by the self-hosted instance: not public budget
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


# ───────────────────────── Open-Meteo response debug log ────────────────────
# The call meter (above) tells us HOW MANY OM calls we made; it cannot tell us
# WHY OM rejected them. This second log captures the raw evidence for every OM
# HTTP attempt — exact error text, HTTP status, and any rate-limit / Retry-After
# headers OM returns — so that when "OM down" shows, we can read back which limit
# (minute/hour/day) actually tripped instead of guessing. One JSONL file next to
# this module (om_debug.jsonl); om_diag.py reads it + api_calls.jsonl and reports.
OM_DEBUG_ENABLED = True
OM_DEBUG_LOG = os.path.join(_METER_DIR, "om_debug.jsonl")
OM_DEBUG_MAX = 5 * 1024 * 1024                  # rotate past ~5 MB

def _om_pick_headers(hdrs):
    """Keep only the headers useful for diagnosing a limit: dates, retry hints,
    and anything mentioning rate/limit/quota. Free-tier OM may send none of these;
    an empty dict is itself evidence (we then rely on timestamp reconstruction)."""
    out = {}
    try:
        items = list(hdrs.items()) if hdrs is not None and hasattr(hdrs, "items") else []
        for k, v in items:
            lk = str(k).lower()
            if (lk in ("date", "server", "retry-after", "x-request-id", "content-type", "age")
                    or "rate" in lk or "limit" in lk or "retry" in lk or "quota" in lk):
                out[str(k)] = str(v)
    except Exception:
        pass
    return out

def _om_debug(rec):
    """Append one OM-attempt record. Never raises; safe across threads."""
    if not OM_DEBUG_ENABLED:
        return
    try:
        with _meter_lock:
            try:
                if os.path.exists(OM_DEBUG_LOG) and os.path.getsize(OM_DEBUG_LOG) > OM_DEBUG_MAX:
                    os.replace(OM_DEBUG_LOG, OM_DEBUG_LOG + ".1")
            except Exception:
                pass
            r = dict(rec)
            r.setdefault("iso", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            r.setdefault("ts", round(time.time(), 2))
            with open(OM_DEBUG_LOG, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass


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
    net_src: str = ""                                   # 'RV' radar or 'OM' model: who served the last net sample
    ea_all_late: bool = False                          # EA gauges all late (with GAUGE_FRESH_S hysteresis)
    net_dither: dict = field(default_factory=dict)     # az->stable jitter (regenerated per dither epoch)
    net_dither_ts: float = 0.0                         # when the dither pattern was last regenerated
    net_feed: str = "ok"                               # offshore feed state: ok | degraded | exhausted | down
    net_feed_reason: "str | None" = None               # human reason (e.g. daily limit) when not ok
    ring_cache: list = field(default_factory=list)     # home-ring backup: [{bearing,range_km,mm,snow,src}] last sample
    ring_cache_ts: float = 0.0                         # when the home ring was last sampled (OC4)
    ring_feed: str = "off"                             # home-ring state: off | active | exhausted
    ring_feed_reason: "str | None" = None              # human reason when the ring can't sample
    ring_src: "str | None" = None                      # which source fed the ring: OM | OC4 | None
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
    # ---- approach episode (the single owner of "rain approaching" speech) ----
    appr_ep: dict = field(default_factory=dict)        # live episode: {state,id,cls,dir,alert_ts,...}; {} = idle
    appr_seq: int = 0                                  # episode id counter
    appr_obs_ts: float = 0.0                           # newest field READING already processed (15-min buckets)
    appr_present_reads: int = 0                        # readings in a row with a threat in the cone (gap-tolerant)
    appr_empty_reads: int = 0                          # readings in a row with the cone empty
    appr_first_edge: "float | None" = None             # threat edge when first seen (receding check)
    appr_supp: dict = field(default_factory=dict)      # post-fizzle suppression {cls, edge_km, ts}; {} = none
    appr_cand: dict = field(default_factory=dict)      # live shadow candidate (every threat, alerted or not)
    appr_cand_seq: int = 0
    appr_quiet_until: float = 0.0                      # area shower notes stay quiet until then after a fizzle
    # ---- steering flow (850 hPa) ----
    steer_from: "float | None" = None                  # last good 850 hPa wind-from (deg)
    steer_kmh: "float | None" = None                   # last good 850 hPa speed (km/h)
    steer_ts: "float | None" = None                    # when it was read
    steer_try_ts: float = 0.0                          # last attempt (success or not)
    steer_err: "str | None" = None                     # last failure reason
    # ---- stationary / suspect gauges ----
    gauge_stat: dict = field(default_factory=dict)     # "g:<name>" -> [[reading_ts, mm_h], ...] over 3 h
    gauge_flags: dict = field(default_factory=dict)    # "g:<name>" -> {flag, verified, set_ts, check_ts, ...}
    # ---- sea-net OC4 fallback ----
    net_oc4: dict = field(default_factory=dict)        # "bearing:range" -> {mm, snow, ts} last OC4 read per point
    net_fb_rot: int = 0                                # rotation index for the non-upwind fallback picks

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


def gauge_approach(gauges, home, wind_from, wind_kmh, flow=None):
    """Physical-gauge (measured) approach signal. gauges: list of dicts with
    lat/lon/mm/dist_km. Returns dict or None. Also reports nearest wet gauge and
    whether any wet gauge is within confirmation range (measured 'at home').
    `flow` (steering) replaces the surface wind for "upwind" and the ETA when given."""
    if flow and flow.get("from_deg") is not None:
        wind_from = flow["from_deg"]
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
        txt, lo, hi = (_flow_eta(near_up["_d"], flow, wind_kmh) if flow
                       else eta_window(near_up["_d"], wind_kmh))
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


def land_front(state, gauges, home, wind_from, wind_kmh, now, flow=None):
    """Track the approaching PHYSICAL-gauge front the way the sea arc tracks the
    offshore edge: nearest upwind wet gauge = leading edge, binned into range
    rings; measured inward speed (falling back to wind when motion isn't yet
    measurable); and an intensity trend so a fizzling shower is called out.
    Read-only apart from appending to state.land_hist. Returns a dict.
    With `flow`, "upwind" is the steering flow's source and a measured speed must pass
    speed_plausible(); a withheld speed falls back to the flow ETA with a `speed_note`."""
    if flow and flow.get("from_deg") is not None:
        wind_from = flow["from_deg"]
    dir_spoken = compass(wind_from, spoken=True) if wind_from is not None else None
    out = {"active": False, "dir": dir_spoken, "edge_km": None, "ring": None,
           "speed_kmh": None, "measured": False, "eta_text": None,
           "eta_lo": None, "eta_hi": None, "intensity_trend": "steady",
           "fizzling": False, "peak_mm": None, "speed_verified": False, "speed_note": None}
    uw = _upwind_wet(gauges, home, wind_from)
    if not uw:
        return out

    lead_g = min(uw, key=lambda g: g["_d"])
    edge = lead_g["_d"]
    peak = max((_gmm(g) or 0) for g in uw)
    out["edge_km"], out["peak_mm"] = round(edge, 1), round(peak, 2)

    state.land_hist.append((now, edge, peak, dir_spoken))
    state.land_hist = [(t, e, m, dd) for (t, e, m, dd) in state.land_hist
                       if now - t <= LAND_TRACK_WINDOW_S]

    trend, dmm = _front_trend(state.land_hist, now)
    spd = _front_speed(state.land_hist, now)
    measured = spd is not None
    if measured and flow:
        # the front moves from the lead gauge toward home: heading = its bearing + 180
        ok, note = speed_plausible(spd, (lead_g["_brg"] + 180.0) % 360.0, flow)
        out["speed_note"] = note
        if not ok:
            measured = False
    out["speed_verified"] = measured
    if measured:
        txt, lo, hi = eta_from_speed(edge, spd, measured=True)
    elif flow:
        spd = flow.get("kmh")
        txt, lo, hi = _flow_eta(edge, flow, wind_kmh)
    else:
        spd = wind_kmh
        txt, lo, hi = eta_from_speed(edge, spd, measured=False)

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


def _om_local_ready(now):
    """True when a local instance is configured and not in its post-failure cooldown."""
    return OM_LOCAL and now >= _om_local["down_until"]


def _om_all_null(d):
    """True if an OM response carries values but every one of them is null -- a local
    instance that is up but holds no data for the request (sync stopped, wrong model)."""
    seen = False
    for b in (d if isinstance(d, list) else [d]):
        if not isinstance(b, dict):
            continue
        for sec in ("current", "hourly", "minutely_15", "daily"):
            blk = b.get(sec)
            if not isinstance(blk, dict):
                continue
            for k, v in blk.items():
                if k in ("time", "interval"):
                    continue
                for x in (v if isinstance(v, list) else [v]):
                    seen = True
                    if x is not None:
                        return False
    return seen


def _om_mark_local(ok, reason=None, now=None):
    """Record a local success/failure; logs only the up/down TRANSITIONS to om_debug."""
    now = now or time.time()
    with _om_local_lock:
        if ok:
            if _om_local["reason"] is not None:
                _om_debug({"outcome": "local_up", "host": "local",
                           "was_down_s": round(now - (_om_local["since"] or now)),
                           "reason": "recovered (was: " + str(_om_local["reason"]) + ")"})
            _om_local.update(down_until=0.0, reason=None, since=None)
        else:
            if _om_local["reason"] is None:
                _om_debug({"outcome": "local_down", "host": "local", "reason": reason,
                           "retry_in_s": OM_LOCAL_RETRY_S})
                _om_local["since"] = now
            _om_local.update(down_until=now + OM_LOCAL_RETRY_S, reason=reason)


def _om_request(q, timeout):
    """One Open-Meteo forecast GET, local-first. Returns (http_code, headers, raw, host)
    with host "local" or "public". Raises OMBackoff if public is needed but cooling
    down; public HTTPError / network errors propagate unchanged so callers' existing
    rate-limit handling still applies. Local failures never raise -- they fall through."""
    now = time.time()
    if _om_local_ready(now):
        lq = dict(q)
        if OM_MODELS:
            lq["models"] = OM_MODELS
        reason = None
        try:
            req = urllib.request.Request(OPEN_METEO_URL + "?" + urllib.parse.urlencode(lq),
                                         headers={"User-Agent": "uk-grid-monitor/1.0"})
            resp = urllib.request.urlopen(req, timeout=min(timeout, OM_LOCAL_TIMEOUT_S))
            raw = resp.read()
            code = getattr(resp, "status", None) or getattr(resp, "code", None) or 200
            d = json.loads(raw)
            if isinstance(d, dict) and d.get("error"):
                reason = "error: " + str(d.get("reason") or "?")[:100]
            elif _om_all_null(d):
                reason = "no data (all values null) - sync stopped or OPEN_METEO_MODELS wrong?"
            else:
                _om_mark_local(True, now=now)
                _om_tls.host = "local"
                _om_local.update(last_host="local", last_ts=time.time())
                return code, _om_pick_headers(getattr(resp, "headers", None)), raw, "local"
        except urllib.error.HTTPError as e:
            reason = "HTTP " + str(e.code)
        except Exception as e:
            reason = "unreachable: " + str(e)[:100]
        _om_mark_local(False, reason, now)
    _om_tls.host = "public"
    if now < _om_backoff_until:
        raise OMBackoff("public Open-Meteo backing off")
    pq = dict(q)
    if OM_MODELS and not OM_LOCAL:
        pq["models"] = OM_MODELS           # explicitly pinned public use (no local configured)
    req = urllib.request.Request((OM_PUBLIC_URL if OM_LOCAL else OPEN_METEO_URL)
                                 + "?" + urllib.parse.urlencode(pq),
                                 headers={"User-Agent": "uk-grid-monitor/1.0"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    raw = resp.read()
    code = getattr(resp, "status", None) or getattr(resp, "code", None) or 200
    _om_local.update(last_host="public", last_ts=time.time())
    return code, _om_pick_headers(getattr(resp, "headers", None)), raw, "public"


def om_get_json(q, timeout=12):
    """Local-first Open-Meteo GET for other modules (grid_server's cloud read).
    Returns (data, host). Raises on any failure, including OMBackoff and an error body
    (a public rate-limit body also trips the shared public backoff)."""
    code, hdrs, raw, host = _om_request(q, timeout)
    d = json.loads(raw)
    if isinstance(d, dict) and d.get("error"):
        reason = str(d.get("reason") or "Open-Meteo error")
        if host == "public" and _om_is_ratelimit(reason):
            _om_backoff_for(reason, time.time())
        raise RuntimeError(reason)
    return d, host


def om_status(now=None):
    """Which Open-Meteo is serving, for the dashboard footer. mode: "local" (self-hosted,
    healthy), "public_fallback" (local configured but down -> public), or "public"."""
    now = now or time.time()
    st = {"configured_local": OM_LOCAL, "last_host": _om_local["last_host"],
          "last_ts": _om_local["last_ts"],
          "public_backoff": now < _om_backoff_until,
          "public_daily_limit": now < _om_daily_until}
    if not OM_LOCAL:
        st["mode"] = "public"
    elif _om_local["reason"] is not None:
        st.update(mode="public_fallback", reason=_om_local["reason"],
                  down_since=_om_local["since"],
                  retry_in_s=max(0, round(_om_local["down_until"] - now)))
    else:
        st["mode"] = "local"
    return st


def fetch_om_precip(points, timeout=8):
    """Batched Open-Meteo current precipitation RATE (mm/h) for many coords in ONE call.
    Open-Meteo's "current" values are backward-looking sums over current.interval
    seconds (900 = the preceding 15 minutes, confirmed on the self-hosted instance
    29 Sep 2026), so each amount is converted to mm/h: amount x 3600 / interval.
    Modelled. Never raises — returns rates aligned to points (None on failure). A
    rate-limit trips a short GLOBAL backoff: we stop calling Open-Meteo entirely during
    its cooldown (respecting its "try again in one minute"), so we neither hammer it nor
    rack up failed calls — the OC4 fallback covers the gap.

    (_om_backoff_until is only READ here; the cooldown is SET in _om_backoff_for,
    so this function needs no `global` declaration.)"""
    if not points:
        return []
    now = time.time()
    if now < _om_backoff_until and not _om_local_ready(now):
        daily = now < _om_daily_until
        msg = ("daily limit reached — backing off until it resets"
               if daily else "rate-limited, backing off")
        # Log the self-suppression too: it distinguishes "OM is rejecting us right
        # now" from "we are still sitting out a cooldown from an earlier rejection".
        _om_debug({"n_points": len(points), "outcome": "backoff_skip",
                   "daily": bool(daily), "resume_in_s": round(_om_backoff_until - now)})
        return [{"mm": None, "snow": False, "src": "OM",
                 "err": msg, "backoff": True}] * len(points)
    t0 = time.time()
    base = {"n_points": len(points)}
    try:
        q = {"latitude": ",".join(f"{p['lat']:.4f}" for p in points),
             "longitude": ",".join(f"{p['lon']:.4f}" for p in points),
             "current": "precipitation,rain,showers"}
        code, hdrs, raw, host = _om_request(q, timeout)
        base["host"] = host
        d = json.loads(raw)
        if isinstance(d, dict) and d.get("error"):
            # Open-Meteo signals failures (limit exceeded etc.) as a 200 body with
            # error:true -- surface it, and back off if it is a rate-limit (longer for
            # a daily-cap rejection). The request WAS issued, so callers may count it.
            reason = str(d.get("reason") or "Open-Meteo error")
            rl = _om_is_ratelimit(reason)
            _om_debug({**base, "outcome": "error_body", "http": code, "reason": reason,
                       "is_ratelimit": bool(rl), "is_daily": _om_is_daily(reason),
                       "headers": hdrs, "elapsed_ms": round((time.time() - t0) * 1000)})
            if rl:
                _om_backoff_for(reason, now)
            return [{"mm": None, "snow": False, "src": "OM", "err": reason, "sent": True}] * len(points)
        _om_debug({**base, "outcome": "ok", "http": code, "headers": hdrs,
                   "elapsed_ms": round((time.time() - t0) * 1000)})
        blocks = d if isinstance(d, list) else [d]
        out = []
        for b in blocks:
            cur = b.get("current") or {}
            tot = cur.get("precipitation")           # total water-equiv, incl. snow
            if tot is None:
                out.append({"mm": None, "snow": False, "src": "OM", "sent": True}); continue
            try:
                _iv = float(cur.get("interval") or 900)
            except (TypeError, ValueError):
                _iv = 900.0
            _k = 3600.0 / _iv if _iv > 0 else 4.0   # amount over the interval -> mm/h
            tot = tot * _k
            rain = ((cur.get("rain") or 0.0) + (cur.get("showers") or 0.0)) * _k
            snow_we = tot - rain                     # snow water-equivalent
            out.append({"mm": tot, "snow": bool(snow_we > 0.05 and snow_we >= rain),
                        "src": "OM", "sent": True})
        return out
    except OMBackoff:
        # local was tried and failed, and public is still cooling down
        daily = now < _om_daily_until
        _om_debug({**base, "outcome": "backoff_skip", "daily": bool(daily),
                   "resume_in_s": round(_om_backoff_until - now)})
        return [{"mm": None, "snow": False, "src": "OM", "backoff": True,
                 "err": ("daily limit reached — backing off until it resets"
                         if daily else "rate-limited, backing off")}] * len(points)
    except urllib.error.HTTPError as e:
        reason = None
        body = b""
        try:
            body = e.read() or b""
        except Exception:
            pass
        try:
            reason = (json.loads(body or b"{}") or {}).get("reason")
        except Exception:
            pass
        hdrs = _om_pick_headers(getattr(e, "headers", None))
        code = getattr(e, "code", None)
        rl = (code == 429) or _om_is_ratelimit(reason)
        _om_debug({**base, "outcome": "http_error", "http": code,
                   "reason": reason or ("HTTP " + str(code)),
                   "is_ratelimit": bool(rl), "is_daily": _om_is_daily(reason),
                   "headers": hdrs,
                   "body": (body[:200].decode("utf-8", "replace") if body else ""),
                   "elapsed_ms": round((time.time() - t0) * 1000)})
        if rl:
            _om_backoff_for(reason or ("HTTP " + str(code)), now)
        # An HTTP response came back (request reached Open-Meteo) -> mark as sent.
        return [{"mm": None, "snow": False, "src": "OM",
                 "err": reason or ("HTTP " + str(e.code)), "sent": True}] * len(points)
    except Exception as e:
        _om_debug({**base, "outcome": "exception", "http": None,
                   "reason": "fetch failed: " + str(e)[:120],
                   "elapsed_ms": round((time.time() - t0) * 1000)})
        return [{"mm": None, "snow": False, "src": "OM", "err": "fetch failed: " + str(e)[:80]}] * len(points)



# ───────────────────────── measured radar for the sea net (RainViewer) ─────────────
# The net used to sample ONLY a weather model (Open-Meteo "current" precipitation), which
# can miss or misplace a narrow band entirely (29 Sep 2026: radar showed 1-9 mm/h on the
# 40 km south/south-west sentinels at 16:50 UT while the model net read dry). RainViewer's
# free API (personal/educational use; since 1 Jan 2026: past radar only, 10-min frames for
# 2 h, max zoom 7, "Universal Blue" colours, 100 req/IP/min) gives MEASURED reflectivity.
# One zoom-7 512px tile (~0.4 km/px here) covers the whole net, so a sample costs 1-2
# tile fetches. Pixel colour -> dBZ (table below) -> mm/h by Marshall-Palmer Z = 200 R^1.6.
# A transparent pixel is "no echo" (dry). Any failure returns None so the caller falls
# back to the model. Stdlib only: the PNG is decoded with zlib.
RADAR_ENABLED   = os.environ.get("RAIN_RADAR", "1") != "0"
RV_MAPS_URL     = "https://api.rainviewer.com/public/weather-maps.json"
RV_ZOOM, RV_SIZE, RV_COLOR = 7, 512, 2        # zoom cap 7; Universal Blue = scheme 2
RV_MAPS_TTL_S   = 5 * 60                      # re-read the frame list at most this often
RV_MAX_AGE_S    = 30 * 60                     # newest frame older than this -> radar unusable
RV_NET_TTL_S    = 10 * 60                     # net sampling interval while radar serves (frames are 10-min)
RV_MIN_DBZ      = 10.0                        # below this: clutter / non-precipitating echo -> dry
_RV_TABLE = ",".join([
    "-10:636159,-9:66635a,-8:69665c,-7:6c685d,-6:6f6b5f,-5:726e61,-4:757062,-3:787364,"
    "-2:7c7565,-1:7f7867,0:827b69,1:857d6a,2:88806c,3:8b826d,4:8e856f,5:928871,"
    "6:9e9375,7:aa9e79,8:b6a97e,9:c2b482,10:cec087,11:d2c48b,12:d6c88f,13:dacc93,"
    "14:ded097,15:88ddee,16:6cd1eb,17:51c5e8,18:36bae5,19:1baee2,20:00a3e0,21:009ad5,"
    "22:0091ca,23:0088bf,24:007fb4,25:0077aa,26:0070a3,27:00699c,28:006295,29:005b8e,"
    "30:005588,31:005180,32:004e78,33:004a70,34:004768,35:ffee00,36:ffe000,37:ffd200,"
    "38:ffc500,39:ffb700,40:ffaa00,41:ff9f00,42:ff9500,43:ff8b00,44:ff8100,45:ff4400,"
    "46:f23600,47:e62800,48:d91b00,49:cd0d00,50:c10000,51:a80000,52:8f0000,53:760000,"
    "54:5d0000,55:ffaaff,56:ff9fff,57:ff95ff,58:ff8bff,59:ff81ff,60:ff77ff,61:ff6cff,"
    "62:ff62ff,63:ff58ff,64:ff4eff,65:ffffff"
])
_RV_DBZ = {k: float(d) for d, k in (e.split(":") for e in _RV_TABLE.split(","))}
_RV_RGB = [(int(k[0:2], 16), int(k[2:4], 16), int(k[4:6], 16), d) for k, d in _RV_DBZ.items()]
_rv = {"maps": None, "maps_ts": 0.0, "tiles": {}, "last_err": None, "frame_ts": None}


def _png_rgba(b):
    """Minimal PNG decoder (8-bit RGBA/RGB/palette, non-interlaced) -> (w, h, px(x, y))."""
    import struct, zlib
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    i, w, h, ct, pal, trns, idat = 8, 0, 0, None, None, None, b""
    while i < len(b):
        n = struct.unpack(">I", b[i:i + 4])[0]; t = b[i + 4:i + 8]; data = b[i + 8:i + 8 + n]; i += 12 + n
        if t == b"IHDR":
            w, h, bd, ct = struct.unpack(">IIBB", data[:10])
            if bd != 8 or data[12] != 0:
                raise ValueError("unsupported PNG (bit depth / interlace)")
        elif t == b"PLTE":
            pal = [data[k:k + 3] for k in range(0, len(data), 3)]
        elif t == b"tRNS":
            trns = data
        elif t == b"IDAT":
            idat += data
    bpp = {6: 4, 2: 3, 3: 1}[ct]
    raw = zlib.decompress(idat); stride = w * bpp; rows = []; prev = bytearray(stride); p = 0
    for _y in range(h):
        f = raw[p]; line = bytearray(raw[p + 1:p + 1 + stride]); p += 1 + stride
        if f:
            for x in range(stride):
                a = line[x - bpp] if x >= bpp else 0; up = prev[x]; c = prev[x - bpp] if x >= bpp else 0
                if f == 1:   line[x] = (line[x] + a) & 255
                elif f == 2: line[x] = (line[x] + up) & 255
                elif f == 3: line[x] = (line[x] + ((a + up) >> 1)) & 255
                elif f == 4:
                    pp = a + up - c; pa, pb, pc = abs(pp - a), abs(pp - up), abs(pp - c)
                    line[x] = (line[x] + (a if pa <= pb and pa <= pc else (up if pb <= pc else c))) & 255
        rows.append(bytes(line)); prev = line

    def px(x, y):
        L = rows[y]
        if ct == 6: return tuple(L[4 * x:4 * x + 4])
        if ct == 2: return tuple(L[3 * x:3 * x + 3]) + (255,)
        k = L[x]; a = trns[k] if trns and k < len(trns) else 255
        return tuple(pal[k]) + (a,)
    return w, h, px


def _rv_dbz(r, g, b):
    d = _RV_DBZ.get("%02x%02x%02x" % (r, g, b))
    if d is not None:
        return d
    best = min(_RV_RGB, key=lambda e: (e[0] - r) ** 2 + (e[1] - g) ** 2 + (e[2] - b) ** 2)
    return best[3]


def _rv_newest_frame(now):
    if _rv["maps"] is None or now - _rv["maps_ts"] >= RV_MAPS_TTL_S:
        req = urllib.request.Request(RV_MAPS_URL, headers={"User-Agent": "gb-energy-monitor"})
        with urllib.request.urlopen(req, timeout=15) as r:
            _rv["maps"] = json.loads(r.read()); _rv["maps_ts"] = now
        meter_api("RV", "radar_index", 1)
    past = ((_rv["maps"] or {}).get("radar") or {}).get("past") or []
    if not past:
        raise RuntimeError("no radar frames")
    fr = max(past, key=lambda f: f.get("time") or 0)
    if now - (fr.get("time") or 0) > RV_MAX_AGE_S:
        raise RuntimeError("newest radar frame is %d min old" % ((now - fr["time"]) // 60))
    return _rv["maps"].get("host") or "https://tilecache.rainviewer.com", fr


def fetch_radar_precip(points, now=None):
    """Measured radar rain rate (mm/h) at each point, from the newest RainViewer frame.
    Returns a list shaped like fetch_om_precip's ({mm, snow, src:'RV', radar_ts}), or
    None on any failure (caller falls back to the model). Never raises."""
    if not RADAR_ENABLED or not points:
        return None
    now = now or time.time()
    try:
        host, fr = _rv_newest_frame(now)
        n = 2 ** RV_ZOOM; out = []
        for p in points:
            la, lo = float(p["lat"]), float(p["lon"])
            xf = (lo + 180.0) / 360.0 * n
            yf = (1 - math.log(math.tan(math.radians(la)) + 1 / math.cos(math.radians(la))) / math.pi) / 2 * n
            key = (fr["path"], int(xf), int(yf))
            if key not in _rv["tiles"]:
                url = f"{host}{fr['path']}/{RV_SIZE}/{RV_ZOOM}/{int(xf)}/{int(yf)}/{RV_COLOR}/0_0.png"
                req = urllib.request.Request(url, headers={"User-Agent": "gb-energy-monitor"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    _rv["tiles"] = {k: v for k, v in _rv["tiles"].items() if k[0] == fr["path"]}  # keep this frame only
                    _rv["tiles"][key] = _png_rgba(r.read())
                meter_api("RV", "radar_tile", 1)
            w, h, px = _rv["tiles"][key]
            r_, g_, b_, a_ = px(min(w - 1, int((xf % 1) * w)), min(h - 1, int((yf % 1) * h)))
            if a_ == 0:
                mmh = 0.0                                    # no echo
            else:
                dbz = _rv_dbz(r_, g_, b_)
                mmh = 0.0 if dbz < RV_MIN_DBZ else round((10 ** (dbz / 10.0) / 200.0) ** (1 / 1.6), 2)
            out.append({"mm": mmh, "snow": False, "src": "RV", "radar_ts": fr.get("time")})
        _rv["last_err"] = None; _rv["frame_ts"] = fr.get("time")
        return out
    except Exception as e:
        _rv["last_err"] = f"{type(e).__name__}: {e}"
        return None


def radar_status():
    return {"enabled": RADAR_ENABLED, "frame_ts": _rv.get("frame_ts"), "last_err": _rv.get("last_err")}


def fetch_net_precip(points):
    """Sea net / home ring / land probes: measured radar first, the model if radar fails."""
    r = fetch_radar_precip(points)
    return r if r is not None else fetch_om_precip(points)


def _om_charged(rates):
    """Locations Open-Meteo charged for in a sampler result (radar reads cost it nothing)."""
    return sum(1 for rt in rates if isinstance(rt, dict) and rt.get("src") != "RV"
               and (rt.get("sent") or rt.get("mm") is not None))


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


def _flow_eta(dist_km, flow, wind_kmh=None):
    """Arrival window from the steering flow. A real 850 hPa speed is already steering-level,
    so it is bracketed symmetrically; the surface fallback keeps the original surface-wind
    bracket (surface = slow edge, x ADVECT_FAST_FACTOR = fast edge). Never multiplies a real
    850 hPa speed by ADVECT_FAST_FACTOR."""
    if flow and flow.get("src") == "850hPa" and flow.get("kmh"):
        return eta_from_speed(dist_km, flow["kmh"], measured=True)
    surf = (flow or {}).get("surface_kmh") if flow else None
    return eta_from_speed(dist_km, surf if surf is not None else wind_kmh, measured=False)


def speed_plausible(kmh, heading_deg, flow):
    """The steering-flow gate for a MEASURED speed. Returns (ok, note). A speed is plausible
    only if it is in the physical band, 0.5-1.5x the 850 hPa speed, and heading within 40 deg
    of the flow. When the flow is only the surface estimate (no 850 hPa read), the original
    looser test applies (0.4-3.0x of 1.6x surface wind, heading within 75 deg). `note` says
    why a speed was withheld, in plain words for the engine view."""
    if not kmh or kmh <= 0:
        return False, None
    mph = kmh / KMH_PER_MPH
    if mph < SPEED_MIN_MPH or mph > SPEED_MAX_MPH:
        return False, f"{mph:.0f} mph is outside {SPEED_MIN_MPH}-{SPEED_MAX_MPH} mph"
    if not flow:
        return True, None
    steer = flow.get("kmh")
    real = flow.get("src") == "850hPa"
    lo, hi, tol = ((STEER_RATIO_LO, STEER_RATIO_HI, STEER_HEADING_TOL) if real
                   else (SPEED_WIND_LO, SPEED_WIND_HI, SURF_HEADING_TOL))
    if steer and not (lo * steer <= kmh <= hi * steer):
        what = "steering" if real else "surface-wind estimate"
        return False, f"{mph:.0f} mph vs {what} {steer / KMH_PER_MPH:.0f} mph"
    if heading_deg is not None and flow.get("from_deg") is not None:
        to = (flow["from_deg"] + 180.0) % 360.0
        if _ang_diff(heading_deg, to) > tol:
            return False, f"heading {compass(heading_deg)} vs flow toward {compass(to)}"
    return True, None


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


def _mobile_move(m, now, wind_kmh, net_speed_kmh, trusted, flow_kmh=None):
    """Move a WET mobile: mostly chase inward one probe step (~1.6x wind, or the
    measured front speed when trusted), and every Nth wet cycle jump BACK to sense
    whether heavier/lighter/no rain is following. Clamped to the 5-35 km band."""
    dt_hr = None
    if m.get("last_move_ts"):
        dt_hr = (now - m["last_move_ts"]) / 3600.0
    # flow_kmh is already steering-level (850 hPa, or 1.6x surface in the fallback)
    spd = net_speed_kmh if (trusted and net_speed_kmh) else (flow_kmh or ADVECT_FAST_FACTOR * (wind_kmh or 0))
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


def _update_mobiles(state, home, now, track_sample_fn, wind_kmh, net_speed_kmh, trusted, flow_kmh=None):
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
        # measured = radar (RV) or OpenWeather (OC4); the reading is dated by its radar
        # frame when it has one, so its true age is known downstream (approach ETA)
        m["confirmed"] = (m.get("src") in ("OC4", "RV")) and (m.get("mm") is not None)
        m["ts"] = ((rt.get("radar_ts") if isinstance(rt, dict) else None) or now) if m.get("mm") is not None else m.get("ts")
        wet = (m.get("mm") or 0) >= ARC_DETECT_MMH
        if wet:
            m["last_wet_ts"] = now
            m["dry_cycles"] = 0
            _mobile_move(m, now, wind_kmh, net_speed_kmh, trusted, flow_kmh)
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
            "src": p.get("src"), "ts": p.get("ts"),
            "probe_ct": 0, "last_move_ts": now,
        })


def _net_key(p):
    return f"{round(p['bearing'])}:{p['range_km']}"


def _fallback_pick(disp, flow_from, state, n=OC4_FALLBACK_MAX):
    """Choose the sea points to read through the budgeted OC4 sampler when the free
    Open-Meteo net has failed: NET_FB_UPWIND points nearest the flow's source (where rain
    will come from), then the rest ROTATE round the other sentinels and pickets on each
    sample, so every point is read regularly rather than the same few forever. Returns
    references into `disp`. Same call count as before (n per sample)."""
    cands = [p for p in disp if p.get("kind") in ("sentinel", "picket")]
    if not cands or n <= 0:
        return []
    n = min(n, len(cands))
    chosen = []
    if flow_from is not None:
        up = sorted(cands, key=lambda p: (_ang_diff(p["bearing"], flow_from), -p["range_km"]))
        chosen = up[:min(NET_FB_UPWIND, n)]
    rest = sorted((p for p in cands if p not in chosen), key=lambda p: (p["bearing"], p["range_km"]))
    k = n - len(chosen)
    if rest and k > 0:
        start = (state.net_fb_rot or 0) % len(rest)
        chosen += [rest[(start + i) % len(rest)] for i in range(min(k, len(rest)))]
        state.net_fb_rot = (start + k) % len(rest)
    return chosen


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


# ───────────────────────── steering flow (850 hPa) ─────────────────────────
def fetch_om_steering(home, timeout=8, now=None):
    """One keyless Open-Meteo read of the 850 hPa wind at home for the current UTC hour.
    Returns {"from_deg", "kmh", "sent": True} or {"err", "sent"}. Honours the shared
    Open-Meteo backoff (no call while it is cooling down) and logs every attempt to
    om_debug.jsonl, like the net. Never raises."""
    now = now or time.time()
    base = {"n_points": 1, "tag": "steering"}
    if now < _om_backoff_until and not _om_local_ready(now):
        _om_debug({**base, "outcome": "backoff_skip", "daily": bool(now < _om_daily_until),
                   "resume_in_s": round(_om_backoff_until - now)})
        return {"err": "Open-Meteo backing off", "sent": False}
    t0 = time.time()
    try:
        q = {"latitude": f"{home[0]:.4f}", "longitude": f"{home[1]:.4f}",
             "hourly": "wind_speed_850hPa,wind_direction_850hPa",
             "wind_speed_unit": "kmh", "forecast_days": 1, "timezone": "GMT"}
        code, hdrs, raw, host = _om_request(q, timeout)
        base["host"] = host
        d = json.loads(raw)
        if isinstance(d, dict) and d.get("error"):
            reason = str(d.get("reason") or "Open-Meteo error")
            _om_debug({**base, "outcome": "error_body", "http": code, "reason": reason,
                       "is_ratelimit": _om_is_ratelimit(reason), "is_daily": _om_is_daily(reason),
                       "headers": hdrs, "elapsed_ms": round((time.time() - t0) * 1000)})
            if _om_is_ratelimit(reason):
                _om_backoff_for(reason, now)
            return {"err": reason, "sent": True}
        h = d.get("hourly") or {}
        times = h.get("time") or []
        key = time.strftime("%Y-%m-%dT%H:00", time.gmtime(now))
        if key not in times:
            _om_debug({**base, "outcome": "no_hour", "http": code, "reason": "hour " + key + " not in response"})
            return {"err": "no 850 hPa value for this hour", "sent": True}
        i = times.index(key)
        spd = (h.get("wind_speed_850hPa") or [None] * len(times))[i]
        drn = (h.get("wind_direction_850hPa") or [None] * len(times))[i]
        if spd is None or drn is None:
            _om_debug({**base, "outcome": "no_value", "http": code, "reason": "850 hPa value missing"})
            return {"err": "no 850 hPa value for this hour", "sent": True}
        _om_debug({**base, "outcome": "ok", "http": code, "headers": hdrs,
                   "elapsed_ms": round((time.time() - t0) * 1000)})
        return {"from_deg": float(drn) % 360.0, "kmh": float(spd), "sent": True}
    except OMBackoff:
        _om_debug({**base, "outcome": "backoff_skip", "daily": bool(now < _om_daily_until),
                   "resume_in_s": round(_om_backoff_until - now)})
        return {"err": "Open-Meteo backing off", "sent": False}
    except urllib.error.HTTPError as e:
        reason = None
        try:
            reason = (json.loads(e.read() or b"{}") or {}).get("reason")
        except Exception:
            pass
        reason = reason or ("HTTP " + str(e.code))
        rl = (e.code == 429) or _om_is_ratelimit(reason)
        _om_debug({**base, "outcome": "http_error", "http": e.code, "reason": reason,
                   "is_ratelimit": bool(rl), "is_daily": _om_is_daily(reason),
                   "headers": _om_pick_headers(getattr(e, "headers", None)),
                   "elapsed_ms": round((time.time() - t0) * 1000)})
        if rl:
            _om_backoff_for(reason, now)
        return {"err": reason, "sent": True}
    except Exception as e:
        _om_debug({**base, "outcome": "exception", "http": None,
                   "reason": "fetch failed: " + str(e)[:120],
                   "elapsed_ms": round((time.time() - t0) * 1000)})
        return {"err": "fetch failed: " + str(e)[:80], "sent": False}


def steering_update(state, home, now, wind_from, wind_kmh, fetch_fn=fetch_om_steering):
    """The ONE flow for the whole probe: {from_deg, kmh, src, stale, age_s, surface_kmh,
    reason}. src "850hPa" = a real steering read (re-read hourly, retried 15 min after a
    failure, kept up to 3 h marked stale); src "surface" = surface wind x 1.6, which is the
    pre-steering behaviour. None only when there is no wind at all."""
    fresh = state.steer_ts is not None and (now - state.steer_ts) < STEER_TTL_S
    if not fresh and (now - (state.steer_try_ts or 0)) >= STEER_RETRY_S and fetch_fn is not None:
        state.steer_try_ts = now
        r = fetch_fn(home) or {}
        if r.get("sent"):
            meter_api("OM", "steering", 1)
        if r.get("kmh") is not None and r.get("from_deg") is not None:
            state.steer_from, state.steer_kmh, state.steer_ts = r["from_deg"], r["kmh"], now
            state.steer_err = None
        else:
            state.steer_err = r.get("err") or "no reading"
    if state.steer_ts is not None and (now - state.steer_ts) <= STEER_KEEP_S and state.steer_kmh:
        age = now - state.steer_ts
        stale = age > (STEER_TTL_S + STEER_RETRY_S)
        return {"from_deg": state.steer_from, "kmh": state.steer_kmh, "src": "850hPa",
                "stale": stale, "age_s": int(age), "surface_kmh": wind_kmh,
                "reason": (state.steer_err if stale else None)}
    if wind_from is None:
        return None
    return {"from_deg": float(wind_from),
            "kmh": (wind_kmh * ADVECT_FAST_FACTOR) if wind_kmh else None,
            "src": "surface", "stale": False, "age_s": None, "surface_kmh": wind_kmh,
            "reason": state.steer_err}


def _steer_public(flow):
    """The flow as the engine view / API sees it (rounded, no internals)."""
    if not flow:
        return None
    return {"from_deg": (round(flow["from_deg"]) if flow.get("from_deg") is not None else None),
            "kmh": (round(flow["kmh"], 1) if flow.get("kmh") else None),
            "src": flow["src"], "stale": bool(flow.get("stale")), "age_s": flow.get("age_s"),
            "reason": flow.get("reason")}


# ───────────────────────── stationary / suspect gauges ─────────────────────────
def _gkey(g):
    """Stable identity of a physical gauge: its EA station reference. (EA labels are not
    unique — every rainfall gauge is labelled "Rainfall station" — so a label can't be used.)"""
    return "g:" + str(g.get("ref") or g.get("grid") or g.get("label") or
                       f"{g.get('lat')},{g.get('lon')}")


def _gname(g):
    """Display name of a physical gauge: the place the server resolved for it, else its
    grid reference, else the EA label/reference."""
    return g.get("place") or g.get("grid") or g.get("label") or g.get("ref") or "gauge"


def update_gauge_flags(state, gauges, now, sampler=None):
    """Flag gauges that have been wet for hours in one place. A gauge wet (> 0) in >= 75 %
    of >= 10 readings over 3 h is flagged; the flag clears below 50 %. Flagged gauges are
    left out of tracks and the approach threat but stay on screen and in the area state.
    One hourly, batched, free Open-Meteo spot-check at the flagged gauges labels each one
    `stationary` (model wet: static drizzle) or `suspect` (model dry: possible fault);
    until checked it is `stationary`, unverified. Returns the public flag list."""
    live = {}
    for g in (gauges or []):
        if g.get("modelled") or g.get("lat") is None or g.get("lon") is None:
            continue
        key = _gkey(g)
        ts = _iso_ts(g.get("dt")) or now
        mm = _gmm(g)
        buf = [e for e in state.gauge_stat.get(key, []) if now - e[0] <= GAUGE_STAT_WINDOW_S]
        if mm is not None and (not buf or buf[-1][0] != ts):      # count READINGS, not polls
            buf.append([ts, mm])
        state.gauge_stat[key] = buf
        live[key] = (g, buf)
    for key in list(state.gauge_stat):                            # gauges that stopped reporting
        if key not in live:
            buf = [e for e in state.gauge_stat[key] if now - e[0] <= GAUGE_STAT_WINDOW_S]
            if buf:
                state.gauge_stat[key] = buf
            else:
                state.gauge_stat.pop(key, None)
                state.gauge_flags.pop(key, None)
    for key, (g, buf) in live.items():
        n = len(buf)
        frac = (sum(1 for e in buf if (e[1] or 0) > 0) / n) if n else 0.0
        f = state.gauge_flags.get(key)
        if f is None and n >= GAUGE_STAT_MIN_READS and frac >= GAUGE_STAT_SET:
            f = state.gauge_flags[key] = {"flag": "stationary", "verified": False, "set_ts": now,
                                          "check_ts": 0.0, "model_mm": None}
        elif f is not None and frac < GAUGE_STAT_CLEAR:
            state.gauge_flags.pop(key, None)
            f = None
        if f is not None:
            f.update({"lat": g["lat"], "lon": g["lon"], "wet_frac": round(frac, 2), "n": n,
                      "name": _gname(g), "ref": g.get("ref"), "grid": g.get("grid")})
    due = [k for k, f in state.gauge_flags.items()
           if now - (f.get("check_ts") or 0) >= GAUGE_CHECK_S and f.get("lat") is not None]
    if due and sampler is not None:
        pts = [{"lat": state.gauge_flags[k]["lat"], "lon": state.gauge_flags[k]["lon"]} for k in due]
        rates = sampler(pts) or []
        meter_api("OM", "gauge_check", sum(1 for rt in rates if isinstance(rt, dict)
                                            and (rt.get("sent") or rt.get("mm") is not None)))
        for k, rt in zip(due, rates):
            f = state.gauge_flags[k]
            mm = rt.get("mm") if isinstance(rt, dict) else rt
            if mm is None:
                # no reading (e.g. Open-Meteo capped): try again after the short retry
                f["check_ts"] = now - GAUGE_CHECK_S + STEER_RETRY_S
                continue
            f["check_ts"] = now
            f["model_mm"] = round(mm, 2)
            f["verified"] = True
            f["flag"] = "stationary" if mm >= GAUGE_CHECK_WET_MMH else "suspect"
    return [{"id": k, "name": f.get("name") or k[2:], "ref": f.get("ref"), "grid": f.get("grid"),
             "flag": f["flag"], "verified": bool(f.get("verified")),
             "wet_frac": f.get("wet_frac"), "n": f.get("n"), "since_s": int(now - f["set_ts"]),
             "model_mm": f.get("model_mm"),
             "checked_age_s": (int(now - f["check_ts"]) if f.get("verified") else None)}
            for k, f in sorted(state.gauge_flags.items())]


# ───────────────────────── home-ring OWM backup (OM-down fallback) ───────────
# When the free Open-Meteo feed is unreachable (daily/rate-limit backoff, or a
# hard failure), the wide offshore net cannot run. This backup stands in: a compact,
# HOME-CENTRED set of OWM/OC4 virtual gauges arranged as TWO concentric rings — 4 on
# a 20 km ring (N/E/S/W) and 4 on a 10 km ring (NE/SE/SW/NW), offset so together they
# cover all eight compass points across two radii (8 probes total). It gives a working
# local rain picture in place of the absent offshore net AND the (often co-absent) EA
# land gauges, and it fully REPLACES the offshore sentinels while active — the
# sentinels are not kept or re-sampled, so no OpenWeather quota is wasted on the dead
# net. Sea-independent, so an inland location with no offshore arc still gets a full
# backup. Sampled through the SAME budgeted OC4 sampler as the mobiles, so it respects
# the daily OWM budget and reports "exhausted" honestly when the budget can't cover a
# cycle. Reverts automatically the moment Open-Meteo is reachable again. (Honesty over
# plausibility: still modelled, marked.)
RING_OUTER_KM       = 20.0                       # outer ring radius (km)
RING_INNER_KM       = 10.0                       # inner ring radius (km)
RING_OUTER_BEARINGS = [0, 90, 180, 270]          # N, E, S, W   on the 20 km ring
RING_INNER_BEARINGS = [45, 135, 225, 315]        # NE, SE, SW, NW on the 10 km ring (offset)
# Sample the backup ring SPARINGLY. Each sample spends OWM/OC4 budget on all 8 probes
# (8 calls), and while Open-Meteo is daily-capped the ring may be the fallback all day,
# so a short interval drains the OpenWeather quota fast. 30 min => at most ~8 calls per
# half hour (~384/day before the active-forecast-window quiet factor stretches it
# further) — well under the 600/day wind budget it shares. Raise this to spend less; a
# broad, slow, MODELLED rain field does not need frequent resampling.
RING_SAMPLE_TTL_S   = 30 * 60
# The last ring-sample time is persisted here so a server RESTART (ProbeState is
# in-memory only) doesn't reset the throttle and immediately re-sample all 8 probes.
_RING_TS_FILE       = os.path.join(_METER_DIR, "ring_sample_ts.json")


def _ring_ts_load():
    try:
        return float(json.loads(open(_RING_TS_FILE, encoding="utf-8").read()).get("ts", 0))
    except Exception:
        return 0.0


def _ring_ts_save(ts):
    try:
        with open(_RING_TS_FILE, "w", encoding="utf-8") as fh:
            json.dump({"ts": ts}, fh)
    except Exception:
        pass


def om_in_backoff(now=None):
    """True while Open-Meteo is in a global cooldown (daily/minute/hour rate-limit).
    This is the signal that the free OM feed is effectively down for everyone on this
    IP — used to switch the offshore net over to the home-ring OWM backup."""
    now = now or time.time()
    return now < _om_backoff_until


def _home_ring(home):
    """Two concentric rings of virtual-gauge points around home: 4 on the 20 km ring
    at N/E/S/W and 4 on the 10 km ring at NE/SE/SW/NW (offset), so together they sample
    all eight compass points across two radii — 8 probes total."""
    pts = []
    for az in RING_OUTER_BEARINGS:
        la, lo = offset_latlon(home[0], home[1], az, RING_OUTER_KM)
        pts.append({"lat": la, "lon": lo, "range_km": RING_OUTER_KM, "bearing": az, "kind": "ring"})
    for az in RING_INNER_BEARINGS:
        la, lo = offset_latlon(home[0], home[1], az, RING_INNER_KM)
        pts.append({"lat": la, "lon": lo, "range_km": RING_INNER_KM, "bearing": az, "kind": "ring"})
    return pts


def home_ring_backup(state, home, now, net_sample_fn, track_sample_fn, active, om_ok=True, ttl_mult=1.0):
    """Deploy (or clear) the home-ring backup that stands in for the EA LAND gauges when
    they're unavailable. Sampled from the FREE Open-Meteo net (net_sample_fn) when OM is
    reachable (om_ok=True) — one batched call, no budget cost — else from the budgeted OC4
    sampler (track_sample_fn). Reuses cached readings within RING_SAMPLE_TTL_S. When not
    active, clears the ring and returns []."""
    if not active:
        # keep ring_cache: if the ring is needed again within its TTL it reuses the readings
        state.ring_feed = "off"
        state.ring_feed_reason = None
        return []
    sampler = net_sample_fn if om_ok else track_sample_fn
    if sampler is None:
        state.ring_feed = "off"; state.ring_feed_reason = None; state.ring_src = None
        return []
    pts = _home_ring(home)
    # Seed the last-sample time from disk so a restart doesn't force an immediate
    # re-sample within the interval. (Note: we do NOT force a sample just because the
    # in-memory cache is empty — that would re-spend budget on every restart.)
    if not state.ring_cache_ts:
        state.ring_cache_ts = _ring_ts_load()
    due = (now - (state.ring_cache_ts or 0)) >= RING_SAMPLE_TTL_S * max(1.0, ttl_mult)
    # Nothing usable cached (first activation, or after a restart): sample now when the
    # sampler is the FREE one (radar / Open-Meteo). Only the budgeted OC4 path waits.
    if not due and om_ok and not any(cp.get("mm") is not None for cp in (state.ring_cache or [])):
        due = True
    if due:
        rates = sampler(pts)
        covered = 0
        for p, rt in zip(pts, rates):
            _apply_sample(p, rt)
            if isinstance(rt, dict) and (rt.get("sent") or rt.get("mm") is not None):
                covered += 1
        state.ring_feed = "active" if covered else "exhausted"
        state.ring_feed_reason = None if covered else ("Open-Meteo unavailable" if om_ok else "OWM budget/limit")
        _rv_used = any(isinstance(rt, dict) and rt.get("src") == "RV" for rt in rates)
        state.ring_src = ("RV" if _rv_used else ("OM" if om_ok else "OC4")) if covered else None
        state.ring_cache = [{"bearing": p["bearing"], "range_km": p["range_km"],
                             "mm": p.get("mm"), "snow": p.get("snow"), "src": p.get("src")}
                            for p in pts]
        state.ring_cache_ts = now
        _ring_ts_save(now)
    else:
        for p in pts:
            for cp in state.ring_cache:
                if cp["bearing"] == p["bearing"] and cp["range_km"] == p["range_km"]:
                    p["mm"] = cp["mm"]; p["snow"] = cp["snow"]; p["src"] = cp["src"]
                    break
        if any(p.get("mm") is not None for p in pts):          # re-activated on cached readings
            state.ring_feed = "active"; state.ring_feed_reason = None
            state.ring_src = next((p.get("src") for p in pts if p.get("src")), state.ring_src)
    # Don't render blank cards: if nothing was actually read (budget spent), show
    # nothing — honestly nothing to see — rather than 8 empty gauges.
    if not any(p.get("mm") is not None for p in pts):
        return []
    vg = []
    for p in pts:
        src = p.get("src") or state.ring_src or "OC4"
        srcname = {"OM": "Open-Meteo", "RV": "radar"}.get(src, "OpenWeather")
        vg.append({
            # "modelled" means VIRTUAL (not an EA gauge) to every consumer, incl. the dashboard;
            # "measured" says whether the reading itself is radar/OpenWeather rather than a model
            "modelled": True, "measured": src in ("RV", "OC4"), "source": src, "kind": "ring",
            "name": f"{compass(p['bearing'])} · {srcname} backup",
            "lat": p["lat"], "lon": p["lon"], "bearing": p["bearing"],
            "dist_km": p["range_km"], "mm": p.get("mm"), "snow": bool(p.get("snow")),
            "confirmed": (src in ("OC4", "RV") and p.get("mm") is not None),
            "model_ts": now,
        })
    return vg


def arc_update(state, home, now, net_sample_fn=fetch_om_precip, track_sample_fn=None,
               landsea_fn=fetch_landsea, wind_kmh=None, sample_fn=None, net_ttl_mult=1.0,
               net_oc4_fallback=True, flow=None):
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
            "eta_text": None, "n_mobile": 0, "dropout": False, "speed_note": None}
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
    _ttl = RV_NET_TTL_S if getattr(state, "net_src", None) == "RV" else NET_SAMPLE_TTL_S   # radar frames are 10-min
    # The quiet-hours multiplier is for PAID sampling. Radar is free and is read on every
    # 10-min frame around the clock (29 Sep: a 42-min radar gap during an evening onset).
    _mult = 1.0 if getattr(state, "net_src", None) == "RV" else max(1.0, net_ttl_mult)
    due = (now - (state.net_cache_ts or 0)) >= _ttl * _mult or not state.net_cache
    if due:
        rates = net_sample_fn(net_pts) if net_pts else []
        for pt, rt in zip(net_pts, rates):
            _apply_sample(pt, rt)
            # a radar reading is dated by its FRAME time (true age); a model read by now
            _rts = rt.get("radar_ts") if isinstance(rt, dict) else None
            pt["ts"] = (_rts or now) if pt.get("mm") is not None else None
        state.net_src = "RV" if any(isinstance(rt, dict) and rt.get("src") == "RV" for rt in rates) else "OM"
        # Meter the locations Open-Meteo actually CHARGED for: every point in a request
        # that reached its servers (returned data OR an over-limit error), but not the
        # ones skipped locally during a backoff. Open-Meteo weights per location, so this
        # tracks the true budget footprint rather than only the readings we kept.
        _sent = _om_charged(rates)
        if _sent:
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
            if net_oc4_fallback and track_sample_fn is not None and track_sample_fn is not net_sample_fn:
                fb = _fallback_pick(disp, (flow or {}).get("from_deg"), state)
                for pt, rt in zip(fb, track_sample_fn(fb) if fb else []):
                    if isinstance(rt, dict) and (rt.get("sent") or rt.get("mm") is not None):
                        _apply_sample(pt, rt); covered += 1
                        if pt.get("mm") is not None:
                            pt["ts"] = now
                            state.net_oc4[_net_key(pt)] = {"mm": pt["mm"], "snow": bool(pt.get("snow")),
                                                           "ts": now}
                # Points not read this sample keep their last OC4 reading (with its age)
                # instead of going blank; kept readings older than NET_OC4_DROP_S are dropped.
                state.net_oc4 = {k: v for k, v in state.net_oc4.items() if now - v["ts"] <= NET_OC4_DROP_S}
                for pt in disp:
                    if pt.get("mm") is None:
                        kept = state.net_oc4.get(_net_key(pt))
                        if kept:
                            pt["mm"], pt["snow"], pt["src"], pt["ts"] = kept["mm"], kept["snow"], "OC4", kept["ts"]
            if covered:      state.net_feed = "degraded"    # OC4 covering the gap (may be dry)
            elif daily:      state.net_feed = "exhausted"   # daily cap AND no fallback coverage
            elif transient:  state.net_feed = "throttled"   # minute/hour rate-limit: recovers shortly
            else:            state.net_feed = "down"
            # A "degraded" feed is WORKING on the OC4 backup, so don't attach the raw
            # "daily limit ... try again tomorrow" error — that reads as a failure and
            # contradicts the "tracking on OC4 fallback" badge. Keep the reason only when
            # the feed is genuinely NOT covered (exhausted / throttled / down).
            state.net_feed_reason = None if covered else reason
        else:
            state.net_feed = "ok"; state.net_feed_reason = None
        state.net_cache = {(round(p["bearing"]), p["range_km"]):
                           {"mm": p.get("mm"), "snow": p.get("snow"), "src": p.get("src"),
                            "ts": p.get("ts")}
                           for p in net_pts}
        state.net_cache_ts = now
    else:
        for pt in net_pts:
            cp = state.net_cache.get((round(pt["bearing"]), pt["range_km"]))
            if cp:
                pt["mm"] = cp.get("mm"); pt["snow"] = cp.get("snow"); pt["src"] = cp.get("src")
                pt["ts"] = cp.get("ts")
            else:
                pt["mm"] = None; pt["snow"] = False; pt["src"] = None; pt["ts"] = None
    # A reading older than NET_OC4_KEEP_S is shown (marked stale) but can't detect.
    for pt in net_pts:
        pt["stale"] = bool(pt.get("ts") is not None and now - pt["ts"] > NET_OC4_KEEP_S)
    net_valid = [p for p in net_pts if p.get("mm") is not None]
    info["dropout"] = bool(net_pts) and not net_valid
    info["net_feed"] = state.net_feed
    info["net_feed_reason"] = state.net_feed_reason
    info["net_sampled_age_s"] = int(now - state.net_cache_ts) if state.net_cache_ts else None
    detections = [p for p in net_pts if (p.get("mm") or 0) >= ARC_DETECT_MMH and not p.get("stale")]

    # ---- front speed from the stationary net's leading wet range over time ------
    net_edge = min((p["range_km"] for p in detections), default=None)
    net_peak = max((p.get("mm") or 0) for p in detections) if detections else None
    if net_edge is not None:
        state.net_edge_hist.append([now, net_edge, net_peak])
    state.net_edge_hist = [e for e in state.net_edge_hist if now - e[0] <= MOBILE_EDGE_WINDOW_S]
    meas = _range_speed(state.net_edge_hist, now)
    if meas and flow:
        # physical band + 2-cycle stability, then the steering-flow gate (the sea edge
        # closes on home from the nearest wet point's bearing: heading = bearing + 180)
        mph, trusted = speed_trust(meas, None, state.net_speed_kmh)
        _nd = min(detections, key=lambda p: p["range_km"]) if detections else None
        ok, note = speed_plausible(meas, ((_nd["bearing"] + 180.0) % 360.0) if _nd else None, flow)
        trusted = bool(trusted and ok)
        info["speed_note"] = note
    else:
        mph, trusted = speed_trust(meas, wind_kmh, state.net_speed_kmh) if meas else (None, False)
    if meas:
        state.net_speed_kmh = meas
    # weakening: leading-edge peak fading over the window
    weakening = False
    hp = [e[2] for e in state.net_edge_hist if e[2] is not None]
    if len(hp) >= 2 and hp[-1] is not None and hp[-1] <= hp[0] - ARC_WEAKEN_MMH and hp[-1] < 2.0:
        weakening = True

    # ---- MOBILES: advance existing, then spawn from uncovered detections --------
    _update_mobiles(state, home, now, track_sample_fn, wind_kmh, meas, trusted,
                    (flow or {}).get("kmh"))
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
        # label each point by where its reading actually came from, with its real age
        _src = p.get("src") or "OM"
        vgauges.append({
            "modelled": True, "measured": _src in ("RV", "OC4"), "source": _src, "kind": p["kind"],
            "name": f"{compass(p['bearing'])} sea · {p['bearing']:.0f}°",
            "lat": p["lat"], "lon": p["lon"], "bearing": p["bearing"],
            "dist_km": p["range_km"], "mm": p.get("mm"), "snow": bool(p.get("snow")),
            "confirmed": bool(_src in ("OC4", "RV") and p.get("mm") is not None),
            "model_ts": p.get("ts"), "stale": bool(p.get("stale")),
            "age_s": (int(now - p["ts"]) if p.get("ts") is not None else None),
        })
    for m in state.mobiles:
        la, lo = offset_latlon(home[0], home[1], m["bearing"], m["range_km"])
        vgauges.append({
            "modelled": True, "source": (m.get("src") or "OM"),
            "kind": "mobile", "state": m.get("state"),
            "name": f"{compass(m['bearing'])} sea · {m['bearing']:.0f}°",
            "lat": la, "lon": lo, "bearing": m["bearing"], "dist_km": round(m["range_km"], 1),
            "mm": m.get("mm"), "snow": bool(m.get("snow")), "confirmed": bool(m.get("confirmed")),
            "speed_mph": _spd(m), "speed_trusted": bool(m is lead and info["speed_trusted"]),
            "model_ts": m.get("ts") or now,
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
# select() still computes these for diagnostics, but they no longer speak: the approach
# episode (run_approach_episode) owns all approach speech.
APPR_RETIRED_KEYS = {"approach", "sea"}

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
            "peak_band": pk, "snow": snow_ct >= max(1, len(members) // 2), "n": len(members),
            # hand-over is judged on the gauges wet NOW: a gauge that has dried counts as
            # "lost" at once, not only after the 40-min recency window lets it go
            "ids": sorted({m.get("key") or m.get("name") or "" for m in members if m["char"]["wet"]}),
            # centre of the gauges wet NOW: motion is measured from this, because the full
            # cluster keeps recently-wet gauges and so lags behind a moving band
            "wet_c": _wet_centre(members)}

def _wet_centre(members):
    """[bearing, dist] of the intensity-weighted centre of the members wet now, or None."""
    wx = wy = w = 0.0
    for m in members:
        if not m["char"]["wet"]:
            continue
        ww = (m["char"]["cur_band"] or 0) + 0.5
        x, y = _polar_xy(m["bearing"], m["dist_km"])
        wx += ww * x; wy += ww * y; w += ww
    if w <= 0:
        return None
    cx, cy = wx / w, wy / w
    return [math.degrees(math.atan2(cx, cy)) % 360, math.hypot(cx, cy)]


def _ang_diff(a, b):
    """Smallest absolute angular difference (0-180 deg) between two bearings."""
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)

SIT_DIR_TOL    = 75.0    # deg: a track heading this far off the wind flow is distrusted
SIT_UPWIND_TOL = 90.0    # deg: a track must be within this of UPWIND to count as approaching

def _track_motion(t, now, wind_from=None, flow=None):
    """Motion of one tracked cluster. HONEST motion only: a fixed gauge can't move, and a
    cluster that only grows shifts its centroid without real motion, so a speed/heading is
    measured only when the cluster has both GAINED and LOST member gauges across at least
    TRACK_MIN_SPAN_S (rain handing over from gauge to gauge). A measured motion must then
    pass speed_plausible() against the steering flow. An unverified track is never
    approaching; a stale track (kept through a missed cycle) is never approaching."""
    if flow and flow.get("from_deg") is not None:
        wind_from = flow["from_deg"]
    h = [x for x in t["hist"] if now - x[0] <= SIT_TRACK_WINDOW_S]
    stale = bool(t.get("miss"))
    out = {"approaching": False, "speed_kmh": None, "eta_text": None,
           "eta_lo": None, "eta_hi": None,
           "move_kmh": None, "move_deg": None, "move_trusted": False,
           "speed_verified": False, "speed_note": None, "stale": stale}
    flow_to = ((wind_from + 180.0) % 360.0) if wind_from is not None else None
    if flow_to is not None:
        out["move_deg"] = round(flow_to)            # wind-implied direction, unconfirmed
    if stale:
        out["speed_note"] = "no active gauges this cycle"
        return out
    if len(h) < 2:
        return out
    span = h[-1][0] - h[0][0]
    ids0 = set(h[0][5]) if len(h[0]) > 5 else set()
    ids1 = set(h[-1][5]) if len(h[-1]) > 5 else set()
    handover = span >= TRACK_MIN_SPAN_S and bool(ids1 - ids0) and bool(ids0 - ids1)
    if not handover:
        out["speed_note"] = ("no gauge hand-over yet" if span >= TRACK_MIN_SPAN_S
                             else "under 10 min of history")
        return out
    dt_hr = span / 3600.0
    def _c(x):                                   # wet-now centre when known, else cluster centre
        return x[6] if (len(x) > 6 and x[6]) else [x[1], x[2]]
    c0, c1 = _c(h[0]), _c(h[-1])
    b0, d0 = math.radians(c0[0]), c0[1]
    b1, d1 = math.radians(c1[0]), c1[1]
    dx = d1 * math.sin(b1) - d0 * math.sin(b0)
    dy = d1 * math.cos(b1) - d0 * math.cos(b0)
    mspd = math.hypot(dx, dy) / dt_hr
    if mspd < 1.0:
        out["speed_note"] = "not moving"
        return out
    obs_deg = math.degrees(math.atan2(dx, dy)) % 360.0
    ok, note = speed_plausible(mspd, obs_deg, flow)
    out["speed_note"] = note
    if not ok:
        return out
    out["speed_verified"] = out["move_trusted"] = True
    out["move_deg"] = round(obs_deg)
    out["move_kmh"] = round(mspd, 1)
    closing = d0 - d1                            # wet-centre distance decreasing = closing on home
    if closing > 0:
        spd = closing / dt_hr
        out["speed_kmh"] = round(spd, 1)
        # A cell can only genuinely be APPROACHING if it sits UPWIND of home (roughly
        # opposite the airflow); a downwind cell whose centroid appears to close is not.
        upwind_ok = (wind_from is None) or (_ang_diff(t["bearing"], wind_from) <= SIT_UPWIND_TOL)
        if upwind_ok and d1 > SIT_HERE_KM:
            txt, elo, ehi = eta_from_speed(d1, spd, measured=True)
            out["approaching"] = True
            out["eta_text"] = txt; out["eta_lo"] = elo; out["eta_hi"] = ehi
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


def update_tracks(points, pstate, now, wind_from=None, flow=None):
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
            best["hist"].append([now, cs["bearing"], cs["dist_km"], cs["peak_band"], cs["snow"], cs["ids"], cs["wet_c"]])
            best["hist"] = [x for x in best["hist"] if now - x[0] <= SIT_TRACK_WINDOW_S]
            best.update({"bearing": cs["bearing"], "dist_km": cs["dist_km"], "peak_band": cs["peak_band"],
                         "snow": cs["snow"], "n": cs["n"], "last": now, "miss": 0, "_x": cs["x"], "_y": cs["y"]})
        else:
            tracks.append({"id": _next_track_id(tracks), "bearing": cs["bearing"], "dist_km": cs["dist_km"],
                           "peak_band": cs["peak_band"], "snow": cs["snow"], "n": cs["n"],
                           "hist": [[now, cs["bearing"], cs["dist_km"], cs["peak_band"], cs["snow"], cs["ids"], cs["wet_c"]]],
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
        mot = _track_motion(t, now, wind_from, flow)
        out.append({"id": t["id"], "bearing": round(t["bearing"]), "dist_km": round(t["dist_km"], 1),
                    "peak_band": t["peak_band"], "snow": bool(t["snow"]), "n": t["n"],
                    "approaching": mot["approaching"], "speed_kmh": mot["speed_kmh"],
                    "eta_text": mot["eta_text"], "eta_lo": mot["eta_lo"], "eta_hi": mot["eta_hi"],
                    "age_s": int(now - t["first"]),
                    "move_kmh": mot["move_kmh"], "move_deg": mot["move_deg"],
                    "move_trusted": mot["move_trusted"],
                    "speed_verified": mot["speed_verified"], "speed_note": mot["speed_note"],
                    "stale": mot["stale"]})
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
# Relaxed limits while a SELF-HOSTED Open-Meteo is serving (no quota to protect). They
# change only how often / how far the probes sample, never what a reading means
# (SIT_PROBE_FRESH_S, the wet threshold and the episode gating are unchanged). The
# moment requests fall back to the public host, the frugal limits above apply again.
SIT_PROBE_UPWIND_KM_LOCAL  = [9.0, 16.0, 25.0]   # 25 km bridges the inland gap to the 30 km net ring
SIT_PROBE_INTERVAL_LOCAL_S = 15 * 60             # heartbeat: as often as the model's values change
SIT_PROBE_CLEAR_LOCAL_S    = 15 * 60             # clear-check spacing
SIT_PROBE_FAN_DEG          = [0.0, 8.0, -8.0]    # bearing offsets per point: keep points distinct

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

def _land_probe_points(home, centroid, wind_from, seaset=None, phase=0, ranges=None):
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
    if ranges is None:
        ranges = SIT_PROBE_UPWIND_KM[:SIT_PROBE_MAX]
    for i, want in enumerate(ranges):
        # keep the points distinct even when offshore-capped: the near point sits centred
        # and closer to the coast, further points are fanned (+8, then -8) and reach further.
        brg = (base_brg + jit + SIT_PROBE_FAN_DEG[i % len(SIT_PROBE_FAN_DEG)]) % 360
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
    rotates each deployment, and probing stops entirely outside a showery episode.

    While a self-hosted Open-Meteo is serving (_om_local_ready) there is no quota to
    save: no episode cap, 15-min heartbeat and clear-check, every heartbeat uses all
    points, three ranges (9/16/25 km), and the quiet-hours cadence stretch is ignored."""
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
    local = _om_local_ready(now)            # self-hosted OM serving -> relaxed limits
    if local:
        clear_gap, beat_gap, ranges = SIT_PROBE_CLEAR_LOCAL_S, SIT_PROBE_INTERVAL_LOCAL_S, SIT_PROBE_UPWIND_KM_LOCAL
        within_cap = True
    else:
        clear_gap = SIT_PROBE_FRESH_S
        beat_gap = SIT_PROBE_INTERVAL_S * max(1.0, cadence_mult)
        ranges = SIT_PROBE_UPWIND_KM[:SIT_PROBE_MAX]
        within_cap = state.sit_probe_used < SIT_PROBE_EPISODE_CAP
    clear_check = (not gauges_wet) and since >= clear_gap
    heartbeat = gauges_wet and since >= beat_gap
    if (clear_check or heartbeat) and within_cap:
        pts = _land_probe_points(home, state.sit_last_centroid, wind_from, seaset,
                                 state.sit_probe_phase, ranges=ranges)
        if heartbeat and not clear_check and not local:
            pts = pts[:1]                       # public: a heartbeat needs only one upwind look
        rates = sampler(pts) if pts else []
        # Count locations Open-Meteo charged for (sent), not just the ones that returned data.
        if _om_charged(rates):
            meter_api("OM", "land_probe", _om_charged(rates))
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
    return {"active": active, "fresh_clear": fresh_clear, "probes": state.sit_probes,
            "limits": "local" if local else "public"}


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


def schedule_situational(sit, pstate, now, mult=1.0, appr_quiet=False):
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
    elif (grp == "showers" and stable >= SIT_DWELL_S
          and not (sit["base_state"] == "isolated_showers" and sit["intensity"]["peak_band"] <= 1
                   and (sit["intensity"]["dist_km"] or 0) > SIT_NEAR_KM)
          and not (appr_quiet and sit["intensity"]["peak_band"] < 3)):
        # Isolated LIGHT showers beyond SIT_NEAR_KM are detection, not announcement: they
        # rarely survive the trip. And while an approach episode is live (or in its post-
        # fizzle cooldown) the episode IS the narrative, so non-heavy area shower notes stay
        # quiet — otherwise "showers to the south-west" interleaves with, or contradicts,
        # the approach -> fizzle sequence.
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
    # grp == "approaching" is deliberately SILENT here: approach speech is owned by
    # run_approach_episode (one alert, optional confirm, one fizzle). The state is still
    # computed and shown on the engine view.

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
    # LATE physical gauges (newest reading older than GAUGE_LATE_S) say nothing about now:
    # left out of the field so they neither read as "clear" nor dilute the coverage.
    n_late = sum(1 for p in points if p.get("kind") == "phys" and p.get("late"))
    field = [p for p in points if p.get("dist_km") is not None and p["dist_km"] <= SIT_MID_KM
             and not (p.get("kind") == "phys" and p.get("late"))]
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
        "n_field": len(field), "n_phys": n_phys, "n_late": n_late, "n_active": len(active), "n_wet": len(wet_now),
        "nearest_wet_km": (round(nearest["dist_km"], 1) if nearest else None),
        "coverage": coverage, "shower_confidence": shower_conf,
        "n_showery": len(showery), "n_steady": len(steady),
        "intensity": {"peak_band": peak_band, "name": _SIT_BAND_NAMES[peak_band],
                      "bearing": (round(cbrg) if cbrg is not None else None),
                      "dist_km": (round(cdist, 1) if cdist is not None else None)},
        "locus": loc, "here": here, "approaching": approach,
    }


# ───────────────── approach episode — ONE owner for "rain approaching" speech ─────────
# Replaces the four overlapping approach voices (land-front phrase edge-trigger, sea-arc
# phrase, cluster-track "approaching" state, and the would->situational bridge), which
# re-spoke on every ETA wobble and could never say whether a forecast came true.
#
# Each cycle, EVERY wet cell in the upwind approach cone is gathered from all sources —
# physical gauges, tracked clusters, sea net points and mobiles — and summarised as one
# THREAT. The threat is classed:
#   * organised  — a front / band: >= APPR_ORG_MIN_CELLS wet cells spread >= APPR_ORG_WIDTH_KM
#                  ACROSS the wind (a band is wide; one shower drifting downwind wets a line
#                  of gauges ALONG the wind), or a cell at >= APPR_ORG_PEAK_MMH, or >= 2 cells
#                  with pressure falling. Track member counts are NOT used: a cluster keeps
#                  recently-wet gauges, so a single shower's trail would read as a band.
#   * isolated   — anything less: one or two light showers.
# Isolated showers typically live 30-60 min, so a light one far out usually dies before it
# arrives. It must therefore be CLOSE (APPR_ISO_ALERT_KM) and due soon (APPR_ISO_MAX_ETA_MIN)
# before it may alert; an organised front may alert out to APPR_ORG_ALERT_KM.
#
# TIMING IS COUNTED IN READINGS, NOT MINUTES. EA gauges post 15-minute bucket totals and the
# offshore net is sampled every 15 min, while the probe polls every ~5 min — so a minute-based
# dwell could be satisfied by one bucket seen on two polls. A new "reading" is a newer field
# observation timestamp (newest gauge dt / net sample); persistence, confirmation and fizzle
# all count those, and one missed reading does not reset a run.
#
# Episode sequence (never more than three messages, usually two):
#   idle -> ALERT (threat in >= APPR_PERSIST_READS readings) -> optional CONFIRM (once: a
#   later reading shows it clearly closer, not weakening) -> ARRIVED (silent: the at-home
#   voice owns arrival) or FIZZLE (one message: cone empty for APPR_FIZZLE_READS readings,
#   or long past its ETA window without getting materially closer).
# After a fizzle the same threat is SUPPRESSED until the cone clears: it may only alert again
# if it upgrades isolated -> organised, or comes clearly closer than where it fizzled — so a
# stalled band can't re-alert on a timer.
#
# SHADOW LOG (learning groundwork): every threat run — alerted or not — is recorded with
# location-agnostic features per reading and its outcome (did rain reach home?) in
# rain_threats-YYYY-MM.jsonl. Episode transitions go to rain_episodes-YYYY-MM.jsonl.
APPR_CONE_DEG        = UPWIND_HALF_ANGLE   # +/- deg of wind-from that counts as "coming at us"
APPR_WET_MMH         = SIT_WET_MMH         # a cell must be at least this wet (NOT merely > 0)
APPR_MAX_KM          = 40.0                # cells beyond this are ignored entirely
APPR_GAUGE_MAX_AGE_S = 45 * 60             # a gauge reading older than this can't be a threat
APPR_ORG_MIN_CELLS   = 3                   # wet land OR sea cells in the cone...
APPR_ORG_WIDTH_KM    = 15.0                # ...spread at least this far ACROSS the wind -> organised
APPR_ORG_PEAK_MMH    = 2.0                 # any cell at/above this (moderate+) -> organised
APPR_ORG_ALERT_KM    = 35.0                # organised may alert from this far
APPR_ISO_ALERT_KM    = 12.0                # isolated must be this close...
APPR_ISO_MAX_ETA_MIN = 40                  # ...and (when a speed is known) due within this (window midpoint)
APPR_PERSIST_READS   = 2                   # threat present in this many readings before the first alert
APPR_FIZZLE_READS    = 2                   # cone empty this many readings in a row = fizzled / cleared
APPR_RECEDE_KM       = 2.0                 # edge moved OUT by more than this since first seen = receding
APPR_CONFIRM_FRAC    = 0.25                # confirm needs the edge closed by >= this fraction...
APPR_CONFIRM_MIN_KM  = 3.0                 # ...and at least this many km (also the re-alert bar)
APPR_OVERDUE_GRACE_S = 20 * 60             # past the ETA window by this, not materially closer = fizzled
APPR_OVERDUE_DEFAULT_MIN = 45              # ETA ceiling assumed when the alert had no ETA
APPR_EPISODE_MAX_S   = 3 * 3600            # safety: an episode older than this expires silently
APPR_SUPP_MAX_S      = 6 * 3600            # safety: post-fizzle suppression never outlives this
APPR_CAND_MAX_S      = 4 * 3600            # safety: a shadow candidate is resolved as 'expired' after this
APPR_CAND_MAX_ROUNDS = 24                  # cap on per-reading snapshots kept in one candidate record
_BAND_MM = {0: 0.0, 1: 1.0, 2: 3.5, 3: 7.0, 4: 12.0}   # track peak band -> representative mm/h

EPISODE_LOG_ENABLED = True


def _jsonl_append(prefix, rec):
    """Append one record to <prefix>-YYYY-MM.jsonl beside this module. Never raises."""
    if not EPISODE_LOG_ENABLED:
        return
    try:
        r = dict(rec)
        r.setdefault("iso", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(r.get("ts") or time.time())))
        fn = os.path.join(_METER_DIR, prefix + "-" + time.strftime("%Y-%m", time.gmtime()) + ".jsonl")
        with _meter_lock:
            with open(fn, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(r) + "\n")
    except Exception:
        pass


def _episode_log(rec):
    _jsonl_append("rain_episodes", rec)


def _in_cone(brg, wind_from):
    return wind_from is not None and _ang_diff(brg, wind_from) <= APPR_CONE_DEG


def cone_geometry(home, gauges, wind_from):
    """Location-agnostic description of THIS install's gauge layout in the approach cone:
    how many physical gauges sit in it (8-40 km) and their typical spacing (mean nearest-
    neighbour distance). Lets a learner compare a dense network, a sparse one and a coast."""
    pts = []
    for g in (gauges or []):
        if g.get("modelled") or g.get("lat") is None or g.get("lon") is None:
            continue
        d = g.get("dist_km") or haversine_km(home[0], home[1], g["lat"], g["lon"])
        b = bearing_deg(home[0], home[1], g["lat"], g["lon"])
        if d is not None and GAUGE_CONFIRM_KM < d <= APPR_MAX_KM and _in_cone(b, wind_from):
            pts.append(_polar_xy(b, d))
    nn = []
    for i, (x, y) in enumerate(pts):
        ds = [math.hypot(x - x2, y - y2) for j, (x2, y2) in enumerate(pts) if j != i]
        if ds:
            nn.append(min(ds))
    return {"n_gauges_cone": len(pts),
            "gauge_spacing_km": (round(sum(nn) / len(nn), 1) if nn else None)}


def approach_threat(*, home, gauges, wind_from, wind_kmh, tracks, vgauges, land, sea,
                    press_cls, now, flow=None):
    """Gather EVERY wet cell in the upwind approach cone (all sources) and summarise it as
    one threat, or None if the cone is empty. Read-only. The cone is centred on the
    steering flow; `gauges` should already exclude stationary/suspect gauges."""
    if flow and flow.get("from_deg") is not None:
        wind_from = flow["from_deg"]
    cells = []
    # physical EA gauges — wet (>= APPR_WET_MMH), fresh, beyond the at-home radius
    for g in (gauges or []):
        if g.get("modelled") or g.get("lat") is None or g.get("lon") is None:
            continue
        mm = _gmm(g) or 0.0
        if mm < APPR_WET_MMH:
            continue
        ts = _iso_ts(g.get("dt"))
        if ts is not None and now - ts > APPR_GAUGE_MAX_AGE_S:
            continue
        d = g.get("dist_km") or haversine_km(home[0], home[1], g["lat"], g["lon"])
        b = bearing_deg(home[0], home[1], g["lat"], g["lon"])
        if d is None or not (GAUGE_CONFIRM_KM < d <= APPR_MAX_KM) or not _in_cone(b, wind_from):
            continue
        cells.append({"src": "gauge", "b": round(b), "d": round(d, 1), "mm": round(mm, 2), "snow": False,
                      "age_s": (int(now - ts) if ts is not None else None)})
    # modelled sea net (sentinels/pickets), home-ring backup points, and live mobiles
    for v in (vgauges or []):
        if v.get("kind") not in ("sentinel", "picket", "mobile", "ring"):
            continue
        if v.get("stale"):
            continue                                   # an old kept reading can't be a threat
        mm = v.get("mm") or 0.0
        if mm < ARC_DETECT_MMH:
            continue
        d, b = v.get("dist_km"), v.get("bearing")
        if d is None or b is None or not (GAUGE_CONFIRM_KM < d <= APPR_MAX_KM) or not _in_cone(b, wind_from):
            continue
        src = ("ring" if v["kind"] == "ring" else "sea_" + v["kind"])
        _vts = v.get("model_ts")
        cells.append({"src": src, "b": round(b), "d": round(d, 1), "mm": round(mm, 2),
                      "snow": bool(v.get("snow")),
                      "age_s": (int(max(0.0, now - _vts)) if _vts else None)})
    # tracked clusters the tracker itself judges to be approaching (already upwind-gated)
    trk = [t for t in (tracks or []) if t.get("approaching") and SIT_HERE_KM < (t.get("dist_km") or 0) <= APPR_MAX_KM]
    for t in trk:
        cells.append({"src": "track", "b": t["bearing"], "d": t["dist_km"],
                      "mm": _BAND_MM.get(t.get("peak_band") or 0, 0.0), "snow": bool(t.get("snow"))})
    if not cells:
        return None
    n_land = sum(1 for c in cells if c["src"] == "gauge")
    n_model = sum(1 for c in cells if c["src"].startswith("sea_") or c["src"] == "ring")
    peak = max(c["mm"] for c in cells)
    lead = min(cells, key=lambda c: c["d"])
    press_falling = press_cls in ("falling", "falling_fast", "falling_storm")
    # cross-wind width of the wet POINT cells: offset of each perpendicular to the wind axis
    # through home. A band is wide across the wind; a lone shower is narrow.
    pts = [c for c in cells if c["src"] != "track"]
    cross = [c["d"] * math.sin(math.radians(c["b"] - wind_from)) for c in pts] if wind_from is not None else []
    width = round(max(cross) - min(cross), 1) if len(cross) >= 2 else 0.0
    why = []
    if (n_land + n_model) >= APPR_ORG_MIN_CELLS and width >= APPR_ORG_WIDTH_KM: why.append("wide band")
    if peak >= APPR_ORG_PEAK_MMH: why.append("intensity")
    if press_falling and (n_land + n_model) >= 2: why.append("pressure")
    cls = "organised" if why else "isolated"
    # closing speed: a MEASURED figure that passed the steering gate where we have one,
    # else the steering flow (850 hPa, or the surface estimate). speed_src says which.
    spd, measured, speed_src = None, False, None
    if sea and sea.get("detected") and sea.get("speed_trusted") and sea.get("speed_kmh"):
        spd, measured, speed_src = sea["speed_kmh"], True, "sea"
    elif land and land.get("measured") and land.get("speed_kmh"):
        spd, measured, speed_src = land["speed_kmh"], True, "land"     # verified in land_front
    if spd is None:
        tk = [t for t in trk if t.get("speed_kmh")]
        if tk:
            spd, measured, speed_src = min(tk, key=lambda t: t["dist_km"])["speed_kmh"], True, "track"
    eta_txt = eta_lo = eta_hi = None
    if spd is not None:                                   # measured + gated
        eta_txt, eta_lo, eta_hi = eta_from_speed(lead["d"], spd, measured=True)
    elif flow and flow.get("kmh"):                        # steering flow (or its surface estimate)
        spd, speed_src = flow["kmh"], ("steering" if flow["src"] == "850hPa" else "surface")
        eta_txt, eta_lo, eta_hi = _flow_eta(lead["d"], flow, wind_kmh)
    elif wind_kmh:                                        # no flow object: original wind proxy
        spd, speed_src = wind_kmh, "surface"
        eta_txt, eta_lo, eta_hi = eta_from_speed(lead["d"], spd, measured=False)
    too_far = bool(spd) and eta_txt is None
    # The lead cell was seen where it was when its reading was VALID, not now: the rain
    # has kept moving since (radar ~10-20 min, EA gauges ~60 min). Take that age off the
    # arrival window; if it is used up, the rain may already be arriving.
    data_age_s = lead.get("age_s") or 0
    if eta_lo is not None and eta_hi is not None and data_age_s >= 60:
        eta_txt, eta_lo, eta_hi = _eta_age_corrected(eta_lo, eta_hi, data_age_s)
    weakening = bool((land and land.get("active") and land.get("intensity_trend") == "weakening")
                     or (sea and sea.get("detected") and sea.get("weakening")))
    trend = ("weakening" if weakening else
             (land.get("intensity_trend") if (land and land.get("active")) else "steady"))
    snow_cells = [c for c in cells if c.get("snow")]
    return {"cls": cls, "why": why, "edge_km": lead["d"], "bearing": lead["b"],
            "data_age_s": (int(data_age_s) if data_age_s else 0),
            "dir": compass(lead["b"], spoken=True), "peak_mm": round(peak, 2),
            "n_cells": len(cells), "n_land": n_land, "n_model": n_model, "n_track": len(trk),
            "width_km": width, "sources": sorted({c["src"] for c in cells}),
            "speed_kmh": round(spd, 1) if spd else None, "measured": measured,
            "speed_src": speed_src,
            "eta_text": eta_txt, "eta_lo": eta_lo, "eta_hi": eta_hi, "too_far": too_far,
            "weakening": weakening, "trend": trend, "press_cls": press_cls,
            "snow": len(snow_cells) > len(cells) / 2,
            "cells": sorted(cells, key=lambda c: c["d"])[:12]}


def _eta_age_corrected(lo, hi, age_s):
    """Arrival window with the lead reading's age taken off (minutes, rounded to 5).
    Wording follows eta_from_speed; a window used up by the data's age reads
    'any time now' (the alert says it 'may arrive any time now')."""
    a = age_s / 60.0
    lo2, hi2 = max(0, _round5(lo - a)), max(0, _round5(hi - a))
    if hi2 <= 0:
        return "any time now", 0, 0
    if hi2 <= 5:
        return "within a few minutes", lo2, hi2
    if lo2 < ETA_MIN_MIN:
        return "within the next fifteen minutes", lo2, hi2
    return f"in {lo2} to {hi2} minutes", lo2, hi2


def _appr_qualifies(th, supp=None):
    """(ok, reason) — may this threat raise an ALERT (ignoring persistence)?"""
    if th["too_far"]:
        return False, "eta beyond window"
    if supp:
        upgraded = supp.get("cls") == "isolated" and th["cls"] == "organised"
        se = supp.get("edge_km") or 0
        closer = th["edge_km"] <= se - max(APPR_CONFIRM_MIN_KM, APPR_CONFIRM_FRAC * se)
        if not (upgraded or closer):
            return False, "suppressed after fizzle (not upgraded or closer)"
    if th["cls"] == "organised":
        if th["edge_km"] > APPR_ORG_ALERT_KM:
            return False, "organised but beyond %g km" % APPR_ORG_ALERT_KM
        return True, "organised"
    if th["weakening"]:
        return False, "isolated and weakening"
    if th["edge_km"] > APPR_ISO_ALERT_KM:
        return False, "isolated beyond %g km" % APPR_ISO_ALERT_KM
    if th["eta_hi"] is not None and (th["eta_lo"] + th["eta_hi"]) / 2 > APPR_ISO_MAX_ETA_MIN:
        return False, "isolated, eta beyond %d min" % APPR_ISO_MAX_ETA_MIN
    return True, "isolated, close"


def _appr_noun(ep_or_th):
    return "snow" if ep_or_th.get("snow") else "rain"


def _appr_subject(ep):
    """What the episode is about, as spoken: 'rain'/'snow' for a band, 'shower' for an isolated one."""
    if ep.get("cls") == "isolated":
        return "wintry shower" if ep.get("snow") else "shower"
    return _appr_noun(ep)


def _appr_alert_phrase(th):
    noun = _appr_noun(th)
    d = th["dir"] or "nearby"
    if th["cls"] == "organised":
        if th["eta_text"]:
            return f"A band of {noun} is approaching from the {d} and may arrive {th['eta_text']}."
        return f"A band of {noun} is moving in from the {d}."
    what = "A wintry shower" if th.get("snow") else "A shower"
    return f"{what} is approaching from the {d} and may reach you {th['eta_text'] or 'soon'}."


def _appr_confirm_phrase(ep, th):
    tail = f", and may arrive {th['eta_text']}." if th.get("eta_text") else ", and is getting closer."
    return f"Confirmed: the {_appr_subject(ep)} approaching from the {ep['dir']} is still on its way" + tail


def _appr_fizzle_phrase(ep):
    return f"The {_appr_subject(ep)} that was approaching from the {ep['dir']} has fizzled out before reaching you."


_SNAP_KEYS = ("cls", "why", "edge_km", "bearing", "dir", "peak_mm", "n_cells", "n_land", "n_model",
              "n_track", "width_km", "sources", "speed_kmh", "measured", "speed_src", "eta_lo", "eta_hi",
              "data_age_s",
              "weakening", "trend", "press_cls")


def _snap(th, cells=False):
    if not th:
        return None
    d = {k: th.get(k) for k in _SNAP_KEYS}
    if cells:
        d["cells"] = th.get("cells")
    return d


def _cand_resolve(state, now, outcome, truth=None):
    c = state.appr_cand
    if not c:
        return
    c["outcome"] = outcome
    c["truth"] = truth
    c["resolved_ts"] = now
    c["dur_s"] = int(now - c["first_ts"])
    c["alerted"] = c.get("alerted_ep") is not None
    _jsonl_append("rain_threats", dict(c, ts=now))
    state.appr_cand = {}


def run_approach_episode(state, th, *, arrived, arrived_src=None, obs_ts=None, now,
                         home=None, gauges=None, wind_from=None, wind_kmh=None,
                         cone_from=None, steering=None, n_flagged=0):
    """Advance the approach episode (and its shadow candidate) one cycle. Returns
    [{tier,key,phrase}] (0 or 1 item) and mutates state. `th` is approach_threat() output
    (or None); `obs_ts` is the newest field-reading timestamp — state machine transitions
    that need 'another reading' only happen when it advances; `arrived` is True when rain
    is measured at home (the at-home voice announces that, so arrival is silent here)."""
    out = []
    new_read = obs_ts is not None and obs_ts > (state.appr_obs_ts or 0)
    if new_read:
        state.appr_obs_ts = obs_ts
        if th:
            state.appr_present_reads += 1
            state.appr_empty_reads = 0
            if state.appr_first_edge is None:
                state.appr_first_edge = th["edge_km"]
        else:
            state.appr_empty_reads += 1
            if state.appr_empty_reads >= APPR_FIZZLE_READS:   # one gap is tolerated; two clears the run
                state.appr_present_reads = 0
                state.appr_first_edge = None
    cleared = state.appr_empty_reads >= APPR_FIZZLE_READS
    supp = state.appr_supp or {}
    if supp and (cleared or now - supp.get("ts", now) > APPR_SUPP_MAX_S):
        state.appr_supp = supp = {}
    receding = bool(th) and state.appr_first_edge is not None and \
        th["edge_km"] > state.appr_first_edge + APPR_RECEDE_KM
    ep = state.appr_ep or {}
    st = ep.get("state", "idle")
    decision = None

    # ---- shadow candidate: open on a threat, snapshot each new reading, resolve on outcome
    if th and new_read and not state.appr_cand:
        state.appr_cand_seq += 1
        geo = cone_geometry(home, gauges, cone_from if cone_from is not None else wind_from) if home else {}
        tm = time.gmtime(now)
        state.appr_cand = {"cand": state.appr_cand_seq, "first_ts": now,
                           "home": ([round(home[0], 2), round(home[1], 2)] if home else None),
                           "wind_from": (round(wind_from) if wind_from is not None else None),
                           "wind_kmh": (round(wind_kmh, 1) if wind_kmh else None),
                           "steering": steering, "n_flagged": n_flagged,
                           "month": tm.tm_mon, "hour_utc": tm.tm_hour, **geo,
                           "alerted_ep": None, "rounds": []}
    cur_round = None
    c = state.appr_cand
    if c and th and new_read and len(c["rounds"]) < APPR_CAND_MAX_ROUNDS:
        cur_round = _snap(th)
        cur_round["t"] = int(now - c["first_ts"])
        cur_round["decision"] = None
        c["rounds"].append(cur_round)

    if arrived:
        if cur_round is not None:
            cur_round["decision"] = "arrived"

        if st != "idle":
            _episode_log({"ts": now, "event": "arrived", "ep": ep.get("id"), "truth": arrived_src,
                          "after_s": int(now - ep.get("alert_ts", now)),
                          "confirmed": st == "confirmed", "threat": _snap(th)})
            state.appr_ep = {}
        _cand_resolve(state, now, "arrived", arrived_src)
        return out

    if st == "idle":
        if th and new_read:
            ok, why = _appr_qualifies(th, supp)
            if ok and receding:
                ok, why = False, "receding"
            if ok and state.appr_present_reads < APPR_PERSIST_READS:
                ok, why = False, "persisting (%d/%d readings)" % (state.appr_present_reads, APPR_PERSIST_READS)
            decision = "alert" if ok else "withheld: " + why
            if ok:
                state.appr_seq += 1
                phrase = _appr_alert_phrase(th)
                state.appr_ep = {"state": "alerted", "id": state.appr_seq, "cls": th["cls"], "dir": th["dir"],
                                 "snow": th["snow"], "alert_ts": now, "alert_edge": th["edge_km"],
                                 "last_edge": th["edge_km"], "eta_hi": th["eta_hi"], "reads": 0}
                state.appr_supp = {}
                if state.appr_cand:
                    state.appr_cand["alerted_ep"] = state.appr_seq
                out.append({"tier": "notice", "key": "appr_alert", "phrase": phrase})
                _episode_log({"ts": now, "event": "alert", "ep": state.appr_seq, "phrase": phrase,
                              "reason": why, "threat": _snap(th, cells=True)})
    else:
        # ---- live episode (alerted / confirmed) ----
        age = now - ep.get("alert_ts", now)
        if th and new_read:
            ep["reads"] = ep.get("reads", 0) + 1
            ep["last_edge"] = th["edge_km"]
        eta_ceiling = (ep.get("eta_hi") or APPR_OVERDUE_DEFAULT_MIN) * 60
        overdue = age > eta_ceiling + APPR_OVERDUE_GRACE_S
        closer = bool(th) and th["edge_km"] <= ep.get("alert_edge", 99) * (1 - APPR_CONFIRM_FRAC)
        if cleared or (overdue and not closer):
            phrase = _appr_fizzle_phrase(ep)
            out.append({"tier": "notice", "key": "appr_fizzle", "phrase": phrase})
            cause = ("cone empty %d readings" % state.appr_empty_reads) if cleared else "overdue, not closer"
            _episode_log({"ts": now, "event": "fizzle", "ep": ep.get("id"), "after_s": int(age),
                          "cause": cause, "confirmed": st == "confirmed", "phrase": phrase,
                          "threat": _snap(th)})
            decision = "fizzle"
            state.appr_ep = {}
            # the fizzled cells stay 'recently wet' to the area voice for SIT_RECENCY_S; keep it
            # quiet meanwhile so "showers to the south-west" can't follow "fizzled out".
            state.appr_quiet_until = now + SIT_RECENCY_S
            if not cleared:     # threat still sitting there: suppress it until it clears / upgrades / closes
                state.appr_supp = {"cls": ep.get("cls"), "edge_km": ep.get("last_edge"), "ts": now}
        elif age > APPR_EPISODE_MAX_S:
            _episode_log({"ts": now, "event": "expired", "ep": ep.get("id"), "after_s": int(age), "threat": _snap(th)})
            state.appr_ep = {}
        elif st == "alerted" and th and new_read and ep.get("reads", 0) >= 1 and not th["weakening"]:
            closed_km = ep.get("alert_edge", 0) - th["edge_km"]
            if closed_km >= max(APPR_CONFIRM_MIN_KM, APPR_CONFIRM_FRAC * ep.get("alert_edge", 0)):
                phrase = _appr_confirm_phrase(ep, th)
                ep["state"] = "confirmed"
                if th["eta_hi"] is not None:
                    ep["eta_hi"] = th["eta_hi"]
                    ep["alert_ts"] = now            # overdue clock restarts from the confirmed ETA
                out.append({"tier": "notice", "key": "appr_confirm", "phrase": phrase})
                _episode_log({"ts": now, "event": "confirm", "ep": ep.get("id"), "after_s": int(age),
                              "phrase": phrase, "threat": _snap(th, cells=True)})
                decision = "confirm"
        if state.appr_ep:
            state.appr_ep = ep

    # ---- shadow candidate bookkeeping ----
    if cur_round is not None:
        cur_round["decision"] = decision or ("episode " + (state.appr_ep or {}).get("state", "idle"))
    if state.appr_cand:
        c = state.appr_cand
        if cleared:
            _cand_resolve(state, now, "no_arrival")
        elif now - c["first_ts"] > APPR_CAND_MAX_S:
            _cand_resolve(state, now, "expired")
    return out


def run_probe(state: ProbeState, *, home, rain_mm_h, pressure_hpa, visibility_m,
              wind_from, wind_kmh, gauges, flood_active=False, feed_stale=False,
              now=None, sample_fn=None, net_sample_fn=fetch_om_precip,
              track_sample_fn=None, landsea_fn=fetch_landsea, forward_precip=None,
              cadence_mult=1.0, sampling_mult=1.0, pressure_change_3h=None,
              steer_fn=fetch_om_steering):
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
    # ONE steering flow for the whole cycle (850 hPa, else surface x 1.6 = old behaviour)
    flow = steering_update(state, home, now, wind_from, wind_kmh, fetch_fn=steer_fn)
    _wf = flow["from_deg"] if flow else wind_from
    # stationary / suspect gauges: kept on screen and in the area state, but left out of
    # everything that judges motion or approach (land front, tracks, approach threat)
    gauge_flags = update_gauge_flags(state, gauges, now,
                                     sampler=(fetch_om_precip if net_sample_fn is fetch_net_precip else net_sample_fn))
    _flagged = set(state.gauge_flags)
    _gauges_mv = [g for g in (gauges or []) if _gkey(g) not in _flagged]
    appr = gauge_approach(gauges, home, wind_from, wind_kmh, flow=flow)
    land = land_front(state, _gauges_mv, home, wind_from, wind_kmh, now, flow=flow)
    # Two independent coverages:
    #  * OFFSHORE sea net (Open-Meteo). When OM is down it keeps its own OC4 sentinel
    #    backup (net_oc4_fallback) so the sea watch degrades rather than going blind.
    #  * LOCAL land coverage. When the EA gauges are present they provide it; when they are
    #    DOWN the HOME RING stands in — sampled from the FREE Open-Meteo net if OM is up
    #    (one batched call, no budget cost), else the OC4 backup. So EA-down triggers the
    #    backup regardless of OM's state.
    _g_ages = [now - t for t in (_iso_ts(g.get("dt")) for g in (gauges or [])) if t is not None]
    _g_newest = min(_g_ages) if _g_ages else None
    if not gauges:
        state.ea_all_late = False
    elif state.ea_all_late:
        if _g_newest is not None and _g_newest < GAUGE_FRESH_S:
            state.ea_all_late = False
    elif _g_newest is not None and _g_newest > GAUGE_LATE_S:
        state.ea_all_late = True

    def _g_late(g):
        if state.ea_all_late:
            return True
        t = _iso_ts(g.get("dt"))
        return t is not None and now - t > GAUGE_LATE_S
    _n_late = sum(1 for g in (gauges or []) if _g_late(g))
    _ea_gauges = bool(gauges) and not state.ea_all_late        # present AND current
    _om_ok = not om_in_backoff(now)
    # a FREE sampler is available if Open-Meteo is up, or radar is serving (radar doesn't
    # depend on Open-Meteo's backoff)
    _free_ok = _om_ok or (net_sample_fn is fetch_net_precip and RADAR_ENABLED and not _rv.get("last_err"))
    sea, vgauges = arc_update(state, home, now, net_sample_fn=net_sample_fn,
                              track_sample_fn=track_sample_fn, landsea_fn=landsea_fn,
                              wind_kmh=wind_kmh, sample_fn=sample_fn, net_ttl_mult=sampling_mult,
                              net_oc4_fallback=True, flow=flow)
    _net_down = sea.get("net_feed") in ("exhausted", "down", "throttled")
    _ring_active = not _ea_gauges                       # ring replaces the land gauges whenever they're gone
    _track_for_ring = track_sample_fn if track_sample_fn is not None else net_sample_fn
    ring_vgauges = home_ring_backup(state, home, now, net_sample_fn, _track_for_ring,
                                    active=_ring_active, om_ok=_free_ok, ttl_mult=sampling_mult)
    if _ring_active:
        # Keep the sea net cards that carry data (OM live, or OC4-backed) and any live
        # mobile, then add the home ring on top.
        vgauges = [v for v in vgauges if (v.get("mm") is not None) or v.get("kind") == "mobile"] + ring_vgauges
    elif _net_down:
        vgauges = [v for v in vgauges if (v.get("mm") is not None) or v.get("kind") == "mobile"]
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
                         "snow": False, "kind": "phys", "key": _gkey(_g),
                         "flag": (state.gauge_flags.get(_gkey(_g)) or {}).get("flag"),
                         "name": _gname(_g), "ref": _g.get("ref"), "grid": _g.get("grid"),
                         "place": _g.get("place"),
                         "mm": _mm, "last_ts": _iso_ts(_g.get("dt")), "late": _g_late(_g)})
    for _vg in vgauges:
        if _vg.get("kind") not in ("sentinel", "picket", "ring"):
            continue
        _k = f"n:{_vg['bearing']:.0f}:{_vg['dist_km']}"
        # key the history by the READING's own time, so a cached/kept reading isn't
        # re-counted every poll (the ring points carry their sample time as model_ts)
        _rts = _vg.get("model_ts") or now
        _push_ghist(state.gauge_hist, _k, _rts, (None if _vg.get("stale") else _vg.get("mm")))
        _sit_pts.append({"bearing": _vg["bearing"], "dist_km": _vg["dist_km"],
                         "char": gauge_character(state.gauge_hist.get(_k), now),
                         "snow": bool(_vg.get("snow")), "kind": "net", "key": _k,
                         "name": _vg.get("name"), "mm": _vg.get("mm"),
                         "last_ts": _vg.get("model_ts"), "src": _vg.get("source"),
                         "stale": bool(_vg.get("stale"))})
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
                              "age_s": (int(now - p["last_ts"]) if p.get("last_ts") else None),
                              "flag": p.get("flag"), "src": p.get("src"), "stale": bool(p.get("stale")),
                              "late": bool(p.get("late")),
                              "id": p.get("key"), "ref": p.get("ref"), "grid": p.get("grid"),
                              "place": p.get("place")}
                             for p in _sit_pts]
    situational["steering"] = _steer_public(flow)
    situational["gauge_flags"] = gauge_flags
    # at-home reading that drives the nowcast "at your location" voice (model/nearest
    # sensor rain rate) — surfaced so the view can SHOW what caused an at-home alert.
    situational["home"] = {"mm": (round(rain_mm_h, 2) if rain_mm_h is not None else None),
                           "band": (_sit_band(rain_mm_h) if rain_mm_h is not None else 0),
                           "name": (classify_intensity(rain_mm_h) or "dry")}
    situational["ts"] = now
    # OFFSHORE sea-net status (its own true state — the home ring no longer masks it).
    situational["net_feed"] = sea.get("net_feed")
    situational["net_feed_reason"] = sea.get("net_feed_reason")
    situational["net_sampled_age_s"] = sea.get("net_sampled_age_s")
    situational["ring_feed"] = state.ring_feed
    situational["ring_feed_reason"] = state.ring_feed_reason
    situational["ring_gauges"] = [{"b": v["bearing"], "d": v["dist_km"], "mm": v.get("mm"),
                                   "snow": bool(v.get("snow")), "src": v.get("source")}
                                  for v in ring_vgauges]
    # LOCAL (land) coverage status: real gauges, or which backup is standing in for them.
    situational["ea_gauges"] = ("present" if _ea_gauges else
                                ("late" if gauges else "down"))       # late = present but all behind
    situational["ea_late"] = {"n_late": _n_late, "n": len(gauges or []),
                              "newest_age_s": (int(_g_newest) if _g_newest is not None else None),
                              "late_after_s": GAUGE_LATE_S}
    if _ea_gauges:
        situational["backup"] = None                              # real gauges; no backup needed
    elif state.ring_feed == "active":
        situational["backup"] = {"RV": "radar home-ring", "OM": "Open-Meteo home-ring"}.get(state.ring_src, "OpenWeather home-ring")
    elif state.ring_feed == "exhausted" and not _free_ok:
        situational["backup"] = "unavailable (OWM budget spent)"
    else:
        situational["backup"] = "unavailable" + (f" ({state.ring_feed_reason})" if state.ring_feed_reason else "")
    # active offshore mobile trackers (spawn only on sea detections) — so they appear
    # on the plan view the moment they are deployed.
    situational["mobiles"] = [{"b": round(m["bearing"]), "d": m["dist_km"], "mm": m.get("mm"),
                               "snow": bool(m.get("snow")), "confirmed": bool(m.get("confirmed")),
                               "src": m.get("source"),
                               "speed_mph": m.get("speed_mph"), "state": m.get("state")}
                              for m in vgauges if m.get("kind") == "mobile"]
    # phase (d): cluster continuity — track systems across sectors; if home is dry but
    # a tracked cluster is closing, surface it as ONE approaching system (dir + ETA).
    _tracks = update_tracks([p for p in _sit_pts if not p.get("flag") and not p.get("stale")],
                            state, now, wind_from, flow=flow)
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
    _probe = run_land_probes(state, situational, home, _wf, now, net_sample_fn,
                             cadence_mult=sampling_mult, seaset=_sea_set(state))
    situational["probe"] = {"active": _probe["active"], "fresh_clear": _probe["fresh_clear"],
                            "n": len(_probe["probes"]), "limits": _probe.get("limits")}
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
    situational_speak = schedule_situational(
        situational, state, now, mult=cadence_mult,
        # the approach engine owns anything upwind: while it is weighing a threat (even one it
        # is withholding), running an episode, suppressing a fizzled one, or just after a
        # fizzle, non-heavy area shower notes stay quiet.
        appr_quiet=(bool(state.appr_ep) or bool(state.appr_supp) or state.appr_present_reads > 0
                    or now < (state.appr_quiet_until or 0)))

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
        if key in APPR_RETIRED_KEYS:
            continue            # approach speech is owned by the approach episode (below)
        if state.announced.get(key) != phrase:
            would.append({"tier": tier, "key": key, "phrase": phrase})
    state.announced = {k: v[1] for k, v in now_keys.items() if k not in APPR_RETIRED_KEYS}

    # Land-front fade: a ONE-SHOT when a front that was approaching weakens or
    # veers away before arriving (and hasn't simply arrived — the band/confirm
    # messages cover a real arrival). Announced with its last tracked direction.
    # (The old land-front fade one-shot is retired: the approach episode's single
    # fizzle message replaces it, judged over every cell in the cone, not one front.)
    land_active_now = bool(land and land.get("active"))
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
    # ---- approach episode: the ONE voice for "rain approaching" -----------------
    # Arrival truth: a measured gauge at home (nearest wet gauge <= GAUGE_CONFIRM_KM, or
    # the situational 'here' < 5 km) is strong; the home weather model alone is weaker and
    # labelled as such in the shadow log so a learner can weight it.
    _arrived_src = None
    if situational.get("here") or confirmed:
        _arrived_src = "gauge"
    elif rain_mm_h is not None and rain_mm_h >= APPR_WET_MMH:
        _arrived_src = "home_model"
    # Newest field READING (EA 15-min buckets; offshore net / home ring sample times): the
    # episode counts readings, not polls, so the same bucket seen twice isn't persistence.
    _obs = [t for t in (_iso_ts(g.get("dt")) for g in (gauges or [])) if t]
    if any(v.get("kind") in ("sentinel", "picket", "mobile") for v in vgauges) and state.net_cache_ts:
        _obs.append(state.net_cache_ts)
    if ring_vgauges and state.ring_cache_ts:
        _obs.append(state.ring_cache_ts)
    _obs_ts = max(_obs) if _obs else None
    _threat = approach_threat(home=home, gauges=_gauges_mv, wind_from=wind_from, wind_kmh=wind_kmh,
                              tracks=_tracks, vgauges=vgauges, land=land, sea=sea,
                              press_cls=pcls, now=now, flow=flow)
    appr_speak = run_approach_episode(state, _threat, arrived=bool(_arrived_src), arrived_src=_arrived_src,
                                      obs_ts=_obs_ts, now=now, home=home, gauges=gauges,
                                      wind_from=wind_from, wind_kmh=wind_kmh, cone_from=_wf,
                                      steering=situational["steering"], n_flagged=len(_flagged))
    would.extend(appr_speak)            # the nowcast channel hears it too (client de-dupes vs RAIN)
    _ep = state.appr_ep or {}
    situational["approach_episode"] = {
        "state": _ep.get("state", "idle"), "id": _ep.get("id"), "cls": _ep.get("cls"),
        "dir": _ep.get("dir"), "alert_age_s": (int(now - _ep["alert_ts"]) if _ep.get("alert_ts") else None),
        "suppressed": bool(state.appr_supp),
        "threat": (None if not _threat else {k: _threat[k] for k in (
            "cls", "why", "edge_km", "dir", "peak_mm", "n_cells", "width_km", "sources", "eta_text", "weakening",
            "speed_src", "data_age_s")}),
        "present_reads": state.appr_present_reads, "empty_reads": state.appr_empty_reads,
        "qualifies": (None if not _threat else _appr_qualifies(_threat, state.appr_supp)[1]),
        "shadow_candidate": (state.appr_cand or {}).get("cand"),
    }

    # Approach keys no longer count as "rain active" (they are silent now); a live
    # approach EPISODE does, so the all-clear can't fire mid-episode and a fizzle this
    # cycle (already in `would`) suppresses a duplicate "settled".
    rain_active = (any(k in RAIN_ACTIVE_KEYS and k not in APPR_RETIRED_KEYS for k in now_keys)
                   or bool(state.appr_ep))
    if rain_active:
        state.was_active = True
    elif state.was_active:
        if not would:
            would.append({"tier": "notice", "key": "settled",
                          "phrase": TEMPLATES["settled"][1]})
        state.was_active = False

    # ---- approach -> the single RAIN voice ---------------------------------------
    # The approach episode is the only source of approach speech; it goes out on the
    # RAIN channel (situational_speak). The old bridge of edge-triggered approach/sea
    # phrases is retired — it re-spoke on every ETA change.
    for _w in appr_speak:
        situational_speak.append({"tier": _w["tier"], "key": "sit_" + _w["key"], "phrase": _w["phrase"]})

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
           + (f" | APPR {situational['approach_episode']['state']}"
              + (f" {_threat['cls']} {_threat['dir']} edge={_threat['edge_km']}km n={_threat['n_cells']}"
                 f" peak={_threat['peak_mm']} [{situational['approach_episode']['qualifies']}]" if _threat else ""))
           + (" | WOULD SPEAK: " + " || ".join(w["phrase"] for w in would) if would else "")
           + (f" | STEER {flow['src']}{'(old)' if flow.get('stale') else ''} from {flow['from_deg']:.0f}"
              f" {(flow.get('kmh') or 0):.0f}km/h" if flow else " | STEER none")
           + (f" flagged={len(_flagged)}" if _flagged else "")
           + f" | SIT {situational['state']} cov={situational['coverage']} n_wet={situational['n_wet']}"
             f" shc={situational['shower_confidence']} peak={situational['intensity']['name']}")

    return {"ts": now, "situation": situation, "situational": situational,
            "situational_speak": situational_speak, "would_speak": would,
            "virtual_gauges": vgauges, "log": log,
            "signals": {"band": band, "trend": trend, "trend_delta": round(trend_d, 2),
                        "press_rate": round(prate, 2), "press_cls": pcls,
                        "vis": vis, "arc": sea, "confirmed": confirmed,
                        "model_only": model_only, "warming_up": warm}}
