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
import json, math, time, random, urllib.request, urllib.parse
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

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


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

def pressure_tendency(hist, now):
    """hPa/hr over the available history (up to ~3h), plus a class label."""
    h = _trim(hist, now, 3 * 3600)
    if len(h) < 2 or _span(h) < MIN_PRESS_SPAN_S or not _fresh(h, now):
        return 0.0, "steady"           # warm-up / stale: no bogus slope
    (t0, p0), (t1, p1) = h[0], h[-1]
    dt_hr = (t1 - t0) / 3600.0
    if dt_hr <= 0: return 0.0, "steady"
    rate = (p1 - p0) / dt_hr
    if abs(rate) > PRESS_RATE_SANE:
        return rate, "steady"          # unphysical rate = bad data; report, do not alarm
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

def fetch_om_precip(points, timeout=8):
    """Batched Open-Meteo current precipitation (mm) for many coords in ONE call.
    Modelled. Never raises — returns rates aligned to points (None on failure)."""
    if not points:
        return []
    try:
        q = {"latitude": ",".join(f"{p['lat']:.4f}" for p in points),
             "longitude": ",".join(f"{p['lon']:.4f}" for p in points),
             "current": "precipitation,rain,showers"}
        url = OPEN_METEO_URL + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"User-Agent": "uk-grid-monitor/1.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        blocks = d if isinstance(d, list) else [d]
        out = []
        for b in blocks:
            cur = b.get("current") or {}
            tot = cur.get("precipitation")           # total water-equiv, incl. snow
            if tot is None:
                out.append({"mm": None, "snow": False, "src": "OM"}); continue
            rain = (cur.get("rain") or 0.0) + (cur.get("showers") or 0.0)
            snow_we = tot - rain                     # snow water-equivalent
            out.append({"mm": tot, "snow": bool(snow_we > 0.05 and snow_we >= rain), "src": "OM"})
        return out
    except Exception:
        return [{"mm": None, "snow": False, "src": "OM"}] * len(points)


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
        ja = (a + random.uniform(-NET_DITHER_DEG, NET_DITHER_DEG)) % 360
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


def arc_update(state, home, now, net_sample_fn=fetch_om_precip, track_sample_fn=None,
               landsea_fn=fetch_landsea, wind_kmh=None, sample_fn=None):
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

    # ---- NET: sample sentinels + pickets (shown) and dither fills (hidden) -----
    disp, fills = _net_points(state, home, seaset, sentinel_az)
    net_pts = disp + fills
    for pt, rt in zip(net_pts, net_sample_fn(net_pts) if net_pts else []):
        _apply_sample(pt, rt)
    net_valid = [p for p in net_pts if p.get("mm") is not None]
    info["dropout"] = bool(net_pts) and not net_valid
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
    elif band == "light" or band == "drizzle": cands.append(("band", *TEMPLATES["int_light"]))

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
def run_probe(state: ProbeState, *, home, rain_mm_h, pressure_hpa, visibility_m,
              wind_from, wind_kmh, gauges, flood_active=False, feed_stale=False,
              now=None, sample_fn=None, net_sample_fn=fetch_om_precip,
              track_sample_fn=None, landsea_fn=fetch_landsea, forward_precip=None):
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
    if pressure_hpa is not None: state.press_hist.append((now, pressure_hpa))
    if visibility_m is not None: state.vis_hist.append((now, visibility_m))
    state.rain_hist = _trim(state.rain_hist, now, TREND_WINDOW_S)
    state.press_hist = _trim(state.press_hist, now, 3 * 3600)
    state.vis_hist = _trim(state.vis_hist, now, VIS_WINDOW_S)

    band = classify_intensity(rain_mm_h)
    trend, trend_d = compute_trend(state.rain_hist, now)
    prate, pcls = pressure_tendency(state.press_hist, now)
    warm = _span(state.press_hist) < MIN_PRESS_SPAN_S
    vis = visibility_state(state.vis_hist, now, visibility_m)
    appr = gauge_approach(gauges, home, wind_from, wind_kmh)
    land = land_front(state, gauges, home, wind_from, wind_kmh, now)
    sea, vgauges = arc_update(state, home, now, net_sample_fn=net_sample_fn,
                              track_sample_fn=track_sample_fn, landsea_fn=landsea_fn,
                              wind_kmh=wind_kmh, sample_fn=sample_fn)
    forward = compute_forward(forward_precip, now, band)

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
    # situation to calm — never on a calm-from-start day, never repeated. A
    # land-fade this cycle already IS the all-clear for that front, so settled
    # only speaks when nothing else did.
    if now_keys:
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
           + (" | WOULD SPEAK: " + " || ".join(w["phrase"] for w in would) if would else ""))

    return {"ts": now, "situation": situation, "would_speak": would,
            "virtual_gauges": vgauges, "log": log,
            "signals": {"band": band, "trend": trend, "trend_delta": round(trend_d, 2),
                        "press_rate": round(prate, 2), "press_cls": pcls,
                        "vis": vis, "arc": sea, "confirmed": confirmed,
                        "model_only": model_only, "warming_up": warm}}
