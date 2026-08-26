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
import json, math, time, urllib.request, urllib.parse
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
ARC_SENTINEL_KM  = 40                 # resting scan ring
ARC_INNER_KM     = [30, 20, 10]       # drawn inward while tracking
ARC_SCAN_STEP    = 20                 # degrees between candidate azimuths (finer = freer)
ARC_DETECT_MMH   = 0.3                # model rate that counts as "rain on this point"
ARC_LOCK_SAMPLES = 2                  # consecutive detections before scan->track
ARC_TIMEOUT_S    = 90 * 60            # give up a track that never arrives / progresses
SEA_MASK_TTL     = 24 * 3600          # land/sea geography is static; refresh ~daily
GEOCODE_BASE     = "https://api.postcodes.io"

# Approach ETA: system motion tends to run faster than the 10 m wind.
ADVECT_FAST_FACTOR = 1.6
ETA_MIN_MIN      = 15
ETA_MAX_MIN      = 90

GAUGE_CONFIRM_KM = 8.0         # a physical gauge this close counts as "at your location"
UPWIND_HALF_ANGLE = 60        # a gauge within +/- this of wind-from is "upwind"

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
    arc_lock_ts: float | None = None
    arc_track_az: list = field(default_factory=list)   # azimuths currently tracked
    sea_pts: list = field(default_factory=list)         # [[az,range],...] that are sea
    sea_home: list | None = None
    sea_ts: float = 0.0
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


def eta_window(dist_km, wind_kmh):
    """Arrival window from distance and advection speed. Returns
    (text, lo_min, hi_min) or (None, None, None) if it should be dropped."""
    if not dist_km or not wind_kmh or wind_kmh <= 0.5:
        return None, None, None
    slow = dist_km / wind_kmh * 60.0                    # surface wind = slow edge
    fast = dist_km / (wind_kmh * ADVECT_FAST_FACTOR) * 60.0
    lo, hi = _round5(fast), _round5(slow)
    if hi > ETA_MAX_MIN:                                # too far out for a claim
        return None, None, None
    if lo < ETA_MIN_MIN:
        return "within the next fifteen minutes", lo, hi
    return f"in {lo} to {hi} minutes", lo, hi


def gauge_approach(gauges, home, wind_from, wind_kmh):
    """Physical-gauge (measured) approach signal. gauges: list of dicts with
    lat/lon/mm/dist_km. Returns dict or None. Also reports nearest wet gauge and
    whether any wet gauge is within confirmation range (measured 'at home')."""
    wet = [g for g in gauges if (g.get("mm") or 0) > 0
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
    ranges = [ARC_SENTINEL_KM] + ARC_INNER_KM
    cand = []
    for a in range(0, 360, ARC_SCAN_STEP):
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
             "current": "precipitation"}
        url = OPEN_METEO_URL + "?" + urllib.parse.urlencode(q)
        req = urllib.request.Request(url, headers={"User-Agent": "uk-grid-monitor/1.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=timeout).read())
        blocks = d if isinstance(d, list) else [d]
        return [(b.get("current") or {}).get("precipitation") for b in blocks]
    except Exception:
        return [None] * len(points)


def arc_update(state, home, now, sample_fn=fetch_om_precip, landsea_fn=fetch_landsea):
    """Scan/track state machine over the SEA-ONLY movable arc. Land points are never
    sampled (saves the call) nor shown. Sentinels sit at whatever azimuths are open
    water; on a hit the track follows those bearings and draws inward. Returns
    (info, virtual_gauges)."""
    ensure_sea_mask(state, home, now, landsea_fn)
    seaset = _sea_set(state)
    sentinel_az = sorted({a for (a, r) in seaset if r == ARC_SENTINEL_KM})
    info = {"mode": state.arc_mode, "edge_km": None, "speed_kmh": None,
            "eta_text": None, "detected": False, "dir_spoken": None}
    if not sentinel_az:                      # no sea around here (or mask unknown)
        state.arc_mode, state.arc_hits = "scan", 0
        return info, []

    if state.arc_mode == "track" and state.arc_track_az:
        azs = [a for a in state.arc_track_az if a in sentinel_az] or sentinel_az
        ranges = [ARC_SENTINEL_KM] + ARC_INNER_KM
    else:
        azs, ranges = sentinel_az, [ARC_SENTINEL_KM]

    pts = []
    for a in azs:
        for r in ranges:
            if (a, r) not in seaset:         # skip a land pocket along this azimuth
                continue
            la, lo = offset_latlon(home[0], home[1], a, r)
            pts.append({"lat": la, "lon": lo, "range_km": r, "bearing": a})
    rates = sample_fn(pts) if pts else []
    for p, rt in zip(pts, rates):
        p["mm"] = rt
    valid = [p for p in pts if p.get("mm") is not None]
    dropout = bool(pts) and not valid          # feed returned no usable readings
    info["dropout"] = dropout

    wet = [p for p in pts if (p.get("mm") or 0) >= ARC_DETECT_MMH]
    edge = min((p["range_km"] for p in wet), default=None)
    info["edge_km"], info["detected"] = edge, bool(wet)
    if wet:
        nearest = min(wet, key=lambda p: p["range_km"])
        info["dir_spoken"] = compass(nearest["bearing"], spoken=True)

    if dropout:
        pass                                   # offshore dropout: hold state, no transition
    elif state.arc_mode == "scan":
        state.arc_hits = state.arc_hits + 1 if wet else 0
        if state.arc_hits >= ARC_LOCK_SAMPLES:
            state.arc_mode = "track"
            state.arc_lock_ts = now
            state.arc_edge_km, state.arc_edge_ts = edge, now
            state.arc_track_az = sorted({p["bearing"] for p in wet}) or sentinel_az
            info["mode"] = "track"
    else:  # track: measure inward motion of the leading edge across ranges
        if edge is not None and state.arc_edge_km is not None and state.arc_edge_ts:
            moved = state.arc_edge_km - edge
            dt_hr = (now - state.arc_edge_ts) / 3600.0
            if moved > 0 and dt_hr > 0:
                info["speed_kmh"] = round(moved / dt_hr, 1)
                txt, _, _ = eta_window(edge, info["speed_kmh"])
                info["eta_text"] = txt
        if edge is not None:
            state.arc_edge_km, state.arc_edge_ts = edge, now
            state.arc_track_az = sorted({p["bearing"] for p in wet}) or state.arc_track_az
        if (not wet) or (state.arc_lock_ts and now - state.arc_lock_ts > ARC_TIMEOUT_S):
            state.arc_mode, state.arc_hits = "scan", 0
            state.arc_edge_km = state.arc_edge_ts = state.arc_lock_ts = None
            state.arc_track_az = []
            info["mode"] = "scan"

    # marked virtual-gauge records: bearing in the name, distance on the card line
    vgauges = []
    for p in pts:
        vgauges.append({
            "modelled": True, "source": "OM",
            "name": f"{compass(p['bearing'])} sea \u00b7 {p['bearing']:.0f}\u00b0",
            "lat": p["lat"], "lon": p["lon"],
            "bearing": p["bearing"], "dist_km": p["range_km"],
            "mm": p.get("mm"), "model_ts": now,
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
    "sea_approach":  ("notice",  "Rain may be moving in from the sea to the {dir}. There are no gauges out there to confirm it."),
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

    # directional approach (physical land gauges)
    if appr and appr.get("approach"):
        a = appr["approach"]
        if a["eta_text"] and a["eta_lo"] is not None and a["eta_lo"] < ETA_MIN_MIN:
            cands.append(("approach", TEMPLATES["approach_imm"][0],
                          TEMPLATES["approach_imm"][1].format(dir=a["dir"])))
        elif a["eta_text"]:
            cands.append(("approach", TEMPLATES["approach_win"][0],
                          TEMPLATES["approach_win"][1].format(dir=a["dir"], eta=a["eta_text"])))
        else:
            cands.append(("approach", TEMPLATES["approach_soft"][0],
                          TEMPLATES["approach_soft"][1].format(dir=a["dir"])))

    # offshore modelled approach
    if sea and sea.get("detected"):
        d = sea.get("dir_spoken") or "the sea"
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
              now=None, sample_fn=fetch_om_precip, landsea_fn=fetch_landsea,
              forward_precip=None):
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
    sea, vgauges = arc_update(state, home, now, sample_fn=sample_fn, landsea_fn=landsea_fn)
    forward = compute_forward(forward_precip, now, band)

    confirmed = bool(appr and appr.get("confirmed_at_home"))
    raining = band not in (None, "dry")
    model_only = raining and not confirmed and not (appr and appr.get("n_wet"))

    situation = {"band": band, "trend": trend, "press_cls": pcls, "vis": vis,
                 "approach": appr, "sea": sea, "confirmed": confirmed,
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

    # All-clear: a ONE-SHOT, spoken only on the transition from an active
    # situation to calm — never on a calm-from-start day, never repeated.
    if now_keys:
        state.was_active = True
    elif state.was_active:
        would.append({"tier": "notice", "key": "settled",
                      "phrase": TEMPLATES["settled"][1]})
        state.was_active = False

    log = (f"band={band} trend={trend}({trend_d:+.1f}) "
           f"press={prate:+.2f}hPa/h[{pcls}{'/warmup' if warm else ''}] vis={'low' if vis['low'] else 'ok'}"
           f"{'/drop' if vis['dropping'] else ''} "
           f"arc={sea['mode']}"
           + (f" edge={sea['edge_km']}km" if sea.get("edge_km") else "")
           + (f" spd={sea['speed_kmh']}km/h" if sea.get("speed_kmh") else "")
           + (f" | approach {appr['approach']['dir']} {appr['approach']['eta_text']}"
              if appr and appr.get("approach") else "")
           + (f" fwd_onset={forward['onset_eta_min']}min" if forward and forward.get("onset_eta_min") is not None else "")
           + (" fwd_heavier" if forward and forward.get("intensify") else "")
           + (" | WOULD SPEAK: " + " || ".join(w["phrase"] for w in would) if would else ""))

    return {"ts": now, "situation": situation, "would_speak": would,
            "virtual_gauges": vgauges, "log": log,
            "signals": {"band": band, "trend": trend, "trend_delta": round(trend_d, 2),
                        "press_rate": round(prate, 2), "press_cls": pcls,
                        "vis": vis, "arc": sea, "confirmed": confirmed,
                        "model_only": model_only, "warming_up": warm}}
