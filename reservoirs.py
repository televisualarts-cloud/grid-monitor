"""Reservoir storage for the EA page: regional and national readouts plus history.

Build 261016.3  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Standard library only, except that the monthly regional table is
read from the EA's national PDF report, which needs the optional `pypdf` package
(`pip install pypdf`). Without it the regional figures are skipped and the
payload says so; everything else still works.

Sources (all Open Government Licence v3.0 unless noted):
  * EA national monthly water situation report (GOV.UK, PDF): "% full" and
    "average comparison" per geographic region and for England, end of month.
    Its section 8.3 defines which EA areas make up each region.
  * EA weekly "Dry weather and drought in England" summary (GOV.UK, HTML):
    England storage %, gap to the long-term average, drought-status sentences.
    Only published during dry-weather periods.
  * EA monthly area water-situation summaries (GOV.UK, HTML): the "Reservoir
    stocks" paragraph for each EA area, shown as text.
  * EA flood-monitoring API: live levels at reservoirs that have a gauge
    (metres, not % full), with a sanity check that rejects steps and dead
    sensors.
  * London Datastore (Thames Water, OGL v2): daily % of usable capacity for
    the Lower Lee and Lower Thames groups since 1989.
  * Water-company pages, where one can be read reliably: South West Water's
    reservoir-levels page (total and eight reservoirs, server-rendered text,
    updated a few times a week). Each published value is logged once, keyed by
    the date the company gives, so its history builds up from first run.

Not available as data: Wales (Dwr Cymru and NRW publish no % full figures),
Scotland (Scottish Water's page refuses automated requests), Northern Ireland
(NI Water publishes nothing), Republic of Ireland (Uisce Eireann publishes
no reservoir storage series).

Public entry point: get_reservoirs(lat, lon, country) -> dict. Cheap: it returns
the last built snapshot and starts a background rebuild when that is older
than REFRESH_S. The first call after start-up returns {"status": "building"}.
"""

import html as _html
import io
import json
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta, date
from pathlib import Path

try:
    import pypdf as _pypdf          # optional: regional monthly table
except Exception:                   # pragma: no cover - depends on the PC
    _pypdf = None

HERE = Path(__file__).resolve().parent
CACHE_FILE = HERE / "reservoir_cache.json"          # parsed history; survives restarts
LOG_PREFIX = "reservoir_levels-"                     # daily gauge log, one file per month
UA = {"User-Agent": "uk-grid-monitor/1.0 (reservoir readout)"}
GOVUK = "https://www.gov.uk/api/content/government/publications/"
EA_FM = "https://environment.data.gov.uk/flood-monitoring"
LONDON_CSV = ("https://data.london.gov.uk/download/24ry5/"
              "778eefb5-8cef-4d16-a4c8-77dee7ce7e81/london_reservoir_levels.csv")

REFRESH_S = 6 * 3600          # rebuild cadence; every source changes daily at most
GAUGE_REFRESH_S = 3600        # live gauges are cheap, refresh hourly
MONTHLY_YEARS_BACK = 2        # national monthly PDFs: this year and the two before

# EA geographic regions as used by the national report (section 8.3).
REGIONS = ["North-east", "North-west", "Central", "East", "South-east", "South-west"]
REGION_AREAS = {
    "East": ["Cambridgeshire and Bedfordshire", "Lincolnshire and Northamptonshire",
             "Essex, Norfolk and Suffolk", "East Anglia"],
    "South-east": ["Solent and South Downs", "Hertfordshire and North London", "Thames",
                   "Kent and South London", "Kent, South London and East Sussex",
                   "South-east England"],
    "South-west": ["Devon and Cornwall", "Wessex"],
    "Central": ["Shropshire, Herefordshire, Worcestershire and Gloucestershire",
                "Staffordshire, Warwickshire and West Midlands",
                "Derbyshire, Nottinghamshire and Leicestershire", "Midlands"],
    "North-west": ["Cumbria and Lancashire", "Greater Manchester, Merseyside and Cheshire",
                   "North-west England", "North West England"],
    "North-east": ["Yorkshire", "Northumberland, Durham and Tees", "North East"],
}
# Rough region centres, used only when postcodes.io has no answer for a point.
REGION_CENTRES = {
    "North-east": (54.35, -1.45), "North-west": (54.05, -2.75), "Central": (52.65, -2.0),
    "East": (52.55, 0.45), "South-east": (51.35, -0.5), "South-west": (50.75, -3.6),
}

NATIONS = {
    "Wales": "Reservoir data not available in Wales: Dŵr Cymru and Natural Resources "
             "Wales don't publish % full figures.",
    "Scotland": "Scottish Water publishes a national average on its website, but the "
                "page refuses automated requests, so it can't be read here.",
    "Northern Ireland": "NI Water does not publish reservoir levels.",
    "Ireland": "Uisce Éireann does not publish reservoir storage figures.",
}

_lock = threading.Lock()
_state = {"snapshot": None, "built": 0.0, "building": False, "gauges_built": 0.0,
          "error": None}


# ---------------------------------------------------------------- helpers ---
def _get(url, timeout=30, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
    return data if binary else data.decode("utf-8", "replace")


def _get_json(url, timeout=30):
    return json.loads(_get(url, timeout))


def _text(html_body):
    t = re.sub(r"<[^>]+>", " ", html_body or "")
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()


def _load_cache():
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(c):
    try:
        tmp = CACHE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(c, separators=(",", ":")), encoding="utf-8")
        tmp.replace(CACHE_FILE)
    except Exception:
        pass


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _month_from_title(title):
    m = re.search(r"(January|February|March|April|May|June|July|August|September|"
                  r"October|November|December)\s+(\d{4})", title or "", re.I)
    if not m:
        return None
    return f"{int(m.group(2)):04d}-{_MONTHS[m.group(1).lower()]:02d}"


def _attachments(slug):
    try:
        j = _get_json(GOVUK + slug)
        return j.get("details", {}).get("attachments", []) or []
    except Exception:
        return []


# ------------------------------------------------- national monthly (PDF) ---
_TABLE_RE = re.compile(
    r"^\s*(East|Central|North[- ]east|North[- ]west|South[- ]east|South[- ]west|England)"
    r"(?: England)?\s+(\d{1,3})\s*%?\s+([A-Za-z][A-Za-z ]*?)\s*$", re.M | re.I)


def _parse_region_table(text):
    """Section 9.4 'Reservoir table': region, % full, average comparison."""
    i = text.find("Reservoir table")
    if i < 0:
        return None
    rows = {}
    for m in _TABLE_RE.finditer(text[i:i + 1500]):
        name = m.group(1).replace(" ", "-").capitalize() if m.group(1).lower() != "england" else "England"
        rows[name] = {"pct": int(m.group(2)), "vs": m.group(3).strip().capitalize()}
    return rows or None


def _monthly_regions(cache):
    """{'YYYY-MM': {region: {pct, vs}}}, adding any months not yet cached."""
    have = cache.setdefault("monthly", {})
    tried = cache.setdefault("monthly_tried", {})
    if _pypdf is None:
        return have
    year = datetime.now(timezone.utc).year
    for y in range(year - MONTHLY_YEARS_BACK, year + 1):
        for a in _attachments(f"water-situation-national-monthly-reports-for-england-{y}"):
            url = a.get("url") or ""
            if not url.lower().endswith(".pdf"):
                continue
            ym = _month_from_title(a.get("title"))
            if not ym or ym in have:
                continue
            if tried.get(url, 0) > time.time() - 7 * 86400:
                continue                     # failed recently; don't re-download 5 MB every cycle
            try:
                rdr = _pypdf.PdfReader(io.BytesIO(_get(url, 90, binary=True)))
                text = "\n".join((p.extract_text() or "") for p in rdr.pages[-12:])
                rows = _parse_region_table(text)
                if rows:
                    have[ym] = rows
                    tried.pop(url, None)
                else:
                    tried[url] = time.time()
            except Exception:
                tried[url] = time.time()
    return have


# ------------------------------------------------------- weekly (HTML) ------
_SENT = re.compile(r"(?<=[.!?])\s+")
_PCT = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?%")
_PRE_SKIP = re.compile(r"(?:\bby|(?:decrease|increase|drop|fall|rise) of(?: over)?|over|decrease|increase|"
                       r"fell|rose|falling|rising|dropped|declined|lta is|"
                       r"under|than|between|to 20|0% to|ranged from)\s*$", re.I)
_POST_SKIP = re.compile(r"\s*(?:below|lower|above|higher|than|of (?:the )?(?:lta|long)|"
                        r"capacity remaining|to \d)", re.I)
_LTA_GAP = re.compile(r"(\d{1,3}(?:\.\d+)?)\s?% (below|lower than|above|higher than) (?:the |where )?"
                      r"(?:average|long[- ]term average|lta|we would expect|normal)", re.I)
_LTA_ABS = re.compile(r"lta is (\d{1,3}(?:\.\d+)?)\s?%", re.I)
_U50 = re.compile(r"\b(\w+) reservoirs?(?: or reservoir)?(?: groups?)? (?:are |were )?(?:now )?"
                  r"(?:less than|under|below) (?:50%|half) full", re.I)


def _parse_weekly(t):
    """England % full, gap to long-term average, count under 50% from weekly prose."""
    pct = gap = n50 = None
    for s in _SENT.split(t):
        if not re.search(r"reservoir|storage|stocks", s, re.I):
            continue
        if not re.search(r"England|national", s, re.I):
            continue
        if pct is None:
            for m in _PCT.finditer(s):
                pre, post = s[max(0, m.start() - 22):m.start()], s[m.end():m.end() + 30]
                if _PRE_SKIP.search(pre) or _POST_SKIP.match(post):
                    continue
                pct = float(m.group(1))
                break
        if gap is None:
            g = _LTA_GAP.search(s)
            if g:
                gap = float(g.group(1)) * (-1 if g.group(2).lower().startswith(("above", "higher")) else 1)
            elif pct is not None:
                a = _LTA_ABS.search(s)
                if a:
                    gap = round(float(a.group(1)) - pct, 1)
    m = _U50.search(t)
    if m:
        w = m.group(1).lower()
        n50 = int(w) if w.isdigit() else _NUMWORDS.get(w)
    return pct, gap, n50


_NUMWORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty twenty-one twenty-two twenty-three "
    "twenty-four twenty-five".split())}


def _weekly(cache):
    """England storage from each weekly report: [{date, pct, below_lta, u50, title}]."""
    have = {w["url"]: w for w in cache.get("weekly", [])}
    slugs = ["dry-weather-and-drought-in-england-july-2025-to-march-2026-summary-reports"]
    year = datetime.now(timezone.utc).year
    slugs += [f"dry-weather-and-drought-in-england-{y}-summary-reports" for y in (year - 1, year)]
    latest_body = cache.get("weekly_latest_body")
    latest_key = cache.get("weekly_latest_key", "")
    for slug in dict.fromkeys(slugs):
        for a in _attachments(slug):
            url = a.get("url") or ""
            if not url.startswith("/government/") or (url in have and "notes" in have[url]):
                continue
            try:
                j = _get_json("https://www.gov.uk/api/content" + url)
            except Exception:
                continue
            t = _text(j.get("details", {}).get("body", ""))
            pct, gap, n50 = _parse_weekly(t)
            when = _week_end(a.get("title")) or (j.get("first_published_at") or "")[:10]
            rec = {"url": url, "title": a.get("title"), "date": when,
                   "pct": pct, "below_lta": gap, "u50": n50, "notes": _drought_notes(t)}
            have[url] = rec
            if when and when >= latest_key:
                latest_key, latest_body = when, t
    cache["weekly"] = sorted(have.values(), key=lambda w: w["date"] or "")
    if latest_body:
        cache["weekly_latest_body"], cache["weekly_latest_key"] = latest_body, latest_key
    return cache["weekly"]


def _week_end(title):
    """'Dry weather and drought in England: 9 to 15 October 2026' -> '2026-10-15'."""
    m = re.search(r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|"
                  r"September|October|November|December)\s+(\d{4})\s*$", title or "", re.I)
    if not m:
        return None
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"


_STATUS_RE = re.compile(
    r"(?:On (\d{1,2} \w+),? )?(?:the )?([A-Z][A-Za-z ,'&-]{2,80}?) (?:areas? )?(?:has |have )?"
    r"moved (?:in)?to (prolonged dry weather|drought|severe drought|recovering from drought|normal)",
    re.I)
_SEVERE_RE = re.compile(r"(?:On (\d{1,2} \w+),? )?the government declared severe drought in "
                        r"(parts of [^.]{3,200})", re.I)


_COUNTY_REGION = {
    "cornwall": "South-west", "devon": "South-west", "dorset": "South-west", "somerset": "South-west",
    "wiltshire": "South-west", "bristol": "South-west", "sussex": "South-east", "kent": "South-east",
    "surrey": "South-east", "hampshire": "South-east", "london": "South-east", "berkshire": "South-east",
    "oxfordshire": "South-east", "hertfordshire": "South-east", "essex": "East", "suffolk": "East",
    "norfolk": "East", "cambridgeshire": "East", "lincolnshire": "East", "bedfordshire": "East",
    "northamptonshire": "East", "yorkshire": "North-east", "durham": "North-east",
    "northumberland": "North-east", "tees": "North-east", "cumbria": "North-west",
    "lancashire": "North-west", "manchester": "North-west", "merseyside": "North-west",
    "cheshire": "North-west", "midlands": "Central", "derbyshire": "Central",
    "nottinghamshire": "Central", "leicestershire": "Central", "staffordshire": "Central",
    "shropshire": "Central", "herefordshire": "Central", "worcestershire": "Central",
    "gloucestershire": "Central", "warwickshire": "Central",
}


def _who_regions(who):
    w = who.lower()
    regs = {r for k, r in _COUNTY_REGION.items() if k in w}
    a = _area_region(who)
    if a:
        regs.add(a)
    if "england" in w and not regs:
        regs = set(REGIONS)
    return sorted(regs)


# EA area names as the weekly report uses them for drought status, and their region.
_STATUS_AREAS = {
    "Yorkshire": "North-east", "North East": "North-east", "Northumberland": "North-east",
    "Cumbria and Lancashire": "North-west", "Greater Manchester": "North-west",
    "West Midlands": "Central", "East Midlands": "Central",
    "Lincolnshire and Northamptonshire": "East", "East Anglia": "East",
    "Cambridgeshire and Bedfordshire": "East", "Essex, Norfolk and Suffolk": "East",
    "Thames": "South-east", "Hertfordshire and North London": "South-east",
    "Kent and South London": "South-east", "Solent and South Downs": "South-east",
    "Devon, Cornwall and Isles of Scilly": "South-west", "Devon and Cornwall": "South-west",
    "Wessex": "South-west",
}


def _drought_status(weekly):
    """Current EA drought status per area, replayed from the weekly reports in date order.

    Only sentences naming EA areas count (water companies' own drought-plan stages
    use the same wording and are ignored). Severe-drought declarations name
    counties, so they are kept per region."""
    latest = {}
    for w in weekly:
        for n in w.get("notes") or []:
            who = n["who"]
            if "water" in who.lower() or len(who) > 140:
                continue
            if n["status"] == "severe drought":
                for reg in _who_regions(who):
                    latest["severe|" + reg] = {"area": who, "region": reg, "status": n["status"],
                                               "when": n["when"], "report": w.get("date")}
                continue
            for name, reg in _STATUS_AREAS.items():
                if re.search(r"\b" + re.escape(name) + r"\b", who):
                    latest[name] = {"area": name, "region": reg, "status": n["status"],
                                    "when": n["when"], "report": w.get("date")}
    order = {"severe drought": 0, "drought": 1, "prolonged dry weather": 2,
             "recovering from drought": 3, "normal": 4}
    return sorted((v for v in latest.values() if v["status"] != "normal"),
                  key=lambda n: (order.get(n["status"], 9), n["area"]))


def _drought_notes(body):
    """Status sentences from the latest weekly report, as short strings."""
    if not body:
        return []
    notes = []
    sents = [x for x in _SENT.split(body) if "drought" in x.lower() or "dry weather" in x.lower()]
    body = " ".join(x[:400] for x in sents)
    for m in _STATUS_RE.finditer(body):
        who = m.group(2).strip().rstrip(",")
        notes.append({"when": m.group(1), "who": who, "status": m.group(3).lower(),
                      "text": m.group(0)})
    for m in _SEVERE_RE.finditer(body):
        notes.append({"when": m.group(1), "who": m.group(2), "status": "severe drought",
                      "text": m.group(0)})
    seen, out = set(), []
    for n in notes:
        k = (n["who"].lower(), n["status"])
        if k not in seen:
            seen.add(k)
            out.append(n)
    return out


# ------------------------------------------------ area summaries (HTML) -----
def _area_notes(cache):
    """Latest 'Reservoir stocks' paragraph per EA area summary."""
    out = []
    for a in _attachments("water-situation-local-area-reports"):
        url = a.get("url") or ""
        if not url.startswith("/government/"):
            continue
        title = a.get("title") or ""
        area = re.split(r" water situation", title, 1, flags=re.I)[0].strip()
        try:
            j = _get_json("https://www.gov.uk/api/content" + url)
        except Exception:
            continue
        t = _text(j.get("details", {}).get("body", ""))
        k = t.lower().find("reservoir stocks")
        if k < 0:
            k = t.lower().find("reservoir stor")
        if k < 0:
            continue
        seg = t[k:k + 1400]
        # stop at the next numbered heading, author line or disclaimer
        cut = re.search(r"\s(?:\d{1,2}\.\s+[A-Z][a-z]|Author:|All data are provisional)", seg[20:])
        if cut:
            seg = seg[:20 + cut.start()]
        seg = re.sub(r"^Reservoir stocks\s+(?=[A-Z])", "", seg).strip()
        tbl = re.search(r"\s(?:Reservoir or reservoir group|Reservoir Percentage|Reservoir group)\s+Percentage", seg)
        if tbl:                                       # an HTML table flattened into the text: drop it
            seg = seg[:tbl.start()].strip()
        out.append({"area": area, "region": _area_region(area), "month": _month_from_title(title),
                    "text": seg[:900], "url": "https://www.gov.uk" + url,
                    "date": (j.get("public_updated_at") or "")[:10], "notes": _drought_notes(t)})
    return out


def _area_region(area):
    a = area.lower()
    for reg, names in REGION_AREAS.items():
        for n in names:
            if n.lower() in a or a in n.lower():
                return reg
    return None


# --------------------------------------------------------- live gauges ------
_SKIP_LABEL = re.compile(r"flood storage|\bfsr\b|sluice|dam\b|wits|spillway channel|"
                         r"thacka beck|skipton|hancocks water|foston|cribbs causeway", re.I)
# The names after "spillway channel" are flood-storage reservoirs (normally empty) whose
# labels don't say so; they would read as alarmingly low supply reservoirs.


def _gauges():
    """Supply-reservoir level gauges with 30 days of daily values and a sanity check."""
    st = _get_json(EA_FM + "/id/stations?_limit=10000", 90).get("items", [])
    cands = [s for s in st if re.search(r"reservoir|\bresr\b", str(s.get("label", "")), re.I)
             and not _SKIP_LABEL.search(str(s.get("label", "")))
             and isinstance(s.get("lat"), (int, float))]
    since = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00Z")
    def one(s):
        try:
            return _gauge_one(s, since)
        except Exception:
            return None
    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(8) as ex:
        out = [g for g in ex.map(one, cands) if g]
    _region_for_points(out)
    return out


def _median(xs):
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def _despike(rows, window=9, limit=1.0):
    """Drop readings more than `limit` m from the running median of their neighbours."""
    if len(rows) < window:
        return rows
    vals = [r["value"] for r in rows]
    h = window // 2
    keep = []
    for i, r in enumerate(rows):
        med = _median(vals[max(0, i - h):i + h + 1])
        if abs(r["value"] - med) <= limit:
            keep.append(r)
    return keep


def _gauge_one(s, since):
    ref = s.get("stationReference")
    try:
        d = _get_json(f"{EA_FM}/id/stations/{urllib.parse.quote(ref)}/readings"
                      f"?_sorted&parameter=level&since={since}&_limit=4000", 30)
    except Exception:
        return None
    rows = [r for r in d.get("items", []) if isinstance(r.get("value"), (int, float))
            and re.search(r"-(m|mAOD|mASD|mBDAT)$", r.get("measure", ""))]
    if len(rows) < 20:
        return None
    # one measure only (some stations carry two level series)
    mid = max({r["measure"] for r in rows}, key=lambda m: sum(r["measure"] == m for r in rows))
    rows = sorted((r for r in rows if r["measure"] == mid), key=lambda r: r["dateTime"])
    unit = mid.rsplit("-", 1)[-1]
    # Reservoir gauges are noisy (wind set-up, sensor sentinels such as Haweswater's
    # 34.87 m), so work from daily medians and judge the sensor on those.
    rows = _despike(rows)
    byday = {}
    for r in rows:
        byday.setdefault(r["dateTime"][:10], []).append(r["value"])
    daily = {k: _median(v) for k, v in byday.items()}
    days = sorted(daily)
    jumps = [abs(daily[b] - daily[a]) for a, b in zip(days, days[1:])]
    vals = [r["value"] for r in rows]
    recent = vals[-96 * 7:]                      # last ~7 days of 15-min readings
    problem = None
    if jumps and max(jumps) > 1.0:
        problem = f"daily level jumped {max(jumps):.1f} m"
    elif len(recent) > 50 and max(recent) - min(recent) < 0.002:
        problem = "no movement for 7 days"
    current = _median(vals[-8:])
    last_t = rows[-1]["dateTime"]
    age_h = (datetime.now(timezone.utc) -
             datetime.fromisoformat(last_t.replace("Z", "+00:00"))).total_seconds() / 3600
    if age_h > 48:
        problem = problem or f"no reading for {age_h / 24:.0f} days"
    scale = None
    try:
        sc = _get_json(f"{EA_FM}/id/stations/{urllib.parse.quote(ref)}/stageScale", 20).get("items", {})
        scale = {"typ_low": sc.get("typicalRangeLow"), "typ_high": sc.get("typicalRangeHigh"),
                 "rec_low": (sc.get("minOnRecord") or {}).get("value"),
                 "rec_high": (sc.get("maxOnRecord") or {}).get("value")}
    except Exception:
        pass
    spill = re.search(r"spillway level\s*=\s*([\d.]+)", s.get("label", ""), re.I)
    label = re.sub(r"\s*\(.*?\)|\s+(LVL|Level|Reservoir Level)$", "", s["label"]).strip()
    return ({
        "ref": ref, "name": label.title() if label.isupper() else label,
        "lat": s["lat"], "lon": s["long"], "unit": unit,
        "value": round(current, 3), "time": last_t,
        "change_7d": round(daily[days[-1]] - daily[days[-8]], 3) if len(days) >= 8 else None,
        "daily": [[k, round(v, 3)] for k, v in sorted(daily.items())],
        "scale": scale, "spillway": float(spill.group(1)) if spill else None,
        "ok": problem is None, "problem": problem,
    })


def _region_for_points(items):
    """Attach 'region' to each {lat, lon} item via postcodes.io, else nearest centre."""
    pts = [{"longitude": i["lon"], "latitude": i["lat"], "limit": 1, "radius": 2000} for i in items]
    res = [None] * len(items)
    for k in range(0, len(pts), 100):
        try:
            req = urllib.request.Request("https://api.postcodes.io/postcodes",
                                         data=json.dumps({"geolocations": pts[k:k + 100]}).encode(),
                                         headers=dict(UA, **{"Content-Type": "application/json"}))
            d = json.loads(urllib.request.urlopen(req, timeout=15).read())
            for n, it in enumerate(d.get("result") or []):
                r = (it.get("result") or [None])[0]
                if r:
                    res[k + n] = r
        except Exception:
            pass
    for i, r in zip(items, res):
        i["region"] = (_ons_to_ea(r) if r else None) or _nearest_region(i["lat"], i["lon"])
        i["country"] = (r or {}).get("country") or "England"


def _ons_to_ea(r):
    """postcodes.io result -> EA geographic region (None outside England)."""
    if (r.get("country") or "") != "England":
        return None
    reg = r.get("region") or ""
    county = " ".join(filter(None, [r.get("admin_county"), r.get("admin_district")]))
    if reg in ("North East", "Yorkshire and The Humber"):
        return "North-east"
    if reg == "North West":
        return "North-west"
    if reg in ("West Midlands", "East Midlands"):
        return "East" if re.search(r"Lincoln|Northampton|Rutland|Kesteven|Lindsey|Boston|"
                                   r"South Holland|Kettering|Corby|Wellingborough", county) else "Central"
    if reg == "East of England":
        return "South-east" if re.search(r"Hertford|Watford|St Albans|Dacorum|Three Rivers|"
                                         r"Hertsmere|Welwyn|Stevenage|Broxbourne", county) else "East"
    if reg in ("London", "South East"):
        return "South-east"
    if reg == "South West":
        return "Central" if re.search(r"(?<!South )Gloucester|Cheltenham|Cotswold|Stroud|Tewkesbury|"
                                      r"Forest of Dean", county) else "South-west"
    return None


def _nearest_region(lat, lon):
    def d(c):
        return math.hypot(lat - c[0], (lon - c[1]) * math.cos(math.radians(lat)))
    return min(REGION_CENTRES, key=lambda k: d(REGION_CENTRES[k]))


def _log_gauges(gauges):
    """Append one value per gauge per day, so plots can run past the API's 30 days."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = HERE / f"{LOG_PREFIX}{today[:7]}.jsonl"
    try:
        if path.exists() and f'"d":"{today}"' in path.read_text(encoding="utf-8")[-20000:]:
            return
        with path.open("a", encoding="utf-8") as f:
            for g in gauges:
                if g["ok"]:
                    f.write(json.dumps({"d": today, "ref": g["ref"], "v": g["value"]},
                                       separators=(",", ":")) + "\n")
    except Exception:
        pass


def _logged_history(refs, days=400):
    """{ref: [[date, value], ...]} from our own daily log files."""
    out = {r: {} for r in refs}
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    for p in sorted(HERE.glob(f"{LOG_PREFIX}*.jsonl")):
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                o = json.loads(line)
                if o.get("ref") in out and o.get("d", "") >= start:
                    out[o["ref"]][o["d"]] = o["v"]
        except Exception:
            continue
    return {r: sorted(v.items()) for r, v in out.items()}


# ------------------------------------------------------ water companies -----
# Per-reservoir % full from two kinds of source, merged into one local store:
#   * Stream (streamwaterdata.co.uk), the water industry's open data portal (CC BY 4.0):
#     ArcGIS feature services in a common schema (RESERVOIR_NAME, DATE, LATITUDE,
#     LONGITUDE, CAPACITY, CURRENT_PERCENTAGE). Some companies keep theirs current
#     (Yorkshire, Southern, Wessex); others upload in batches months behind
#     (Severn Trent yearly, South West Water and SES half-yearly, Northumbrian yearly).
#     New layers (e.g. "... 2026") are found by a daily catalogue search.
#   * Company websites, for the weeks Stream hasn't caught up with, and for companies
#     not on Stream at all (United Utilities, Bristol Water).
# The store (reservoir_store.json) keeps every value ever read, keyed by the date
# the source gives, so the history survives a feed being withdrawn or a website
# changing. A dated copy is kept as reservoir_store.bak.json (refreshed daily).
SWW_URL = "https://www.southwestwater.co.uk/environment/water-resources/reservoir-levels"
_SWW_TOTAL = re.compile(r"total reservoir storage for the South West Water area is at\s*"
                        r"([\d.]+)\s*%\s*on\s*(\d{1,2})/(\d{1,2})/(\d{4})", re.I)
_SWW_ONE = re.compile(r"\b([A-Z][a-z]+) reservoir storage levels? is at\s*([\d.]+)\s*%"
                      r"\s*on\s*(\d{1,2})/(\d{1,2})/(\d{4})")


COMPANY_LOG = "reservoir_company_log.jsonl"   # 261016.2 log, migrated into the store

STORE_FILE = HERE / "reservoir_store.json"
STORE_BAK = HERE / "reservoir_store.bak.json"
STREAM_SEARCH = "https://www.streamwaterdata.co.uk/api/search/v1/collections/all/items?q=reservoir&limit=100"
STREAM_REFRESH_S = 24 * 3600     # catalogue search; layers themselves are checked every build
WEB_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) uk-grid-monitor/1.0"}
STALE_DAYS = 45                  # a reservoir's newest value older than this is shown as stale

# key -> company; "title" is matched against Stream layer titles; region is a fallback
# only (each reservoir's own position decides its region: Northumbrian's dataset
# includes Essex & Suffolk's Abberton and Hanningfield, which are in the East).
COMPANIES = {
    "yw":  {"name": "Yorkshire Water", "title": r"^Yorkshire Water Reservoir Levels", "region": "North-east"},
    "nw":  {"name": "Northumbrian Water", "title": r"^Northumbrian Water Reservoir Levels", "region": "North-east",
            "by_position": True},
    "uu":  {"name": "United Utilities", "title": None, "region": "North-west"},
    "st":  {"name": "Severn Trent", "title": r"^Severn Trent Water Raw Water Storage", "region": "Central"},
    "ss":  {"name": "South Staffs Water", "title": r"^South Staffs Water", "region": "Central"},
    "sw":  {"name": "Southern Water", "title": r"^Southern Water Monthly Reservoir Levels 2002 to Present", "region": "South-east"},
    "ses": {"name": "SES Water", "title": r"^SES Water Reservoir Levels", "region": "South-east"},
    "sww": {"name": "South West Water", "title": r"^South West Water", "region": "South-west"},
    "wx":  {"name": "Wessex Water", "title": r"^Wessex Water Reservoir Levels", "region": "South-west"},
    "bw":  {"name": "Bristol Water", "title": None, "region": "South-west"},
}
# Stream layers known today (the catalogue search adds any new ones).
STREAM_LAYERS = {
    "yw": [f"https://services-eu1.arcgis.com/1WqkK5cDKUbF0CkH/arcgis/rest/services/"
           f"Yorkshire%20Water%20Reservoir%20Levels%20{y}/FeatureServer" for y in range(2022, 2027)],
    "nw": ["https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/2022_Northumbrian_Water_Reservoir_Level_Data/FeatureServer"],
    "st": ["https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/ST_reservoir_level_2023/FeatureServer",
           "https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/ST_reservoir_level_2024/FeatureServer",
           "https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/Severn_Trent_Water_Raw_Water_Storage_-_Reservoir_Levels_2025/FeatureServer"],
    "ss": ["https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/Stream_Open_Data___Raw_Water_Storage_Res_Levels/FeatureServer"],
    "sw": ["https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/Southern_Water_Monthly_Reservoir_Levels_2002_to_Present/FeatureServer"],
    "ses": ["https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/SES_Water_Reservoir_Levels/FeatureServer"],
    "sww": ["https://services-eu1.arcgis.com/XxS6FebPX29TRGDJ/arcgis/rest/services/South_West_Water_(SWB)_Reservoir_Levels/FeatureServer"],
    "wx": ["https://services.arcgis.com/3SZ6e0uCvPROr4mS/arcgis/rest/services/Wessex_Water_Raw_Reservoir_Levels_view/FeatureServer"],
}
# website names -> the name Stream uses (after _norm), where they differ
NAME_ALIASES = {"STAUNTON": "STAUNTON HAROLD", "TOTALS": "TOTAL", "WHITACRE COMBINED": "WHITACRE",
                "BARTLEY AND FRANKLEY": "BARTLEY AND FRANKLEY", "WEIR WOOD": "WEIRWOOD",
                "ELAN VALLEY": "ELAN VALLEY"}
# positions for website-only reservoirs (Stream supplies the rest)
SITE_POS = {
    ("sww", "TOTTIFORD"): (50.622, -3.680), ("sww", "DRIFT"): (50.105, -5.588),
    ("sww", "VENFORD"): (50.533, -3.858),
    ("bw", "CHEDDAR"): (51.283, -2.777), ("bw", "CHEW VALLEY LAKE"): (51.338, -2.620),
    ("bw", "BLAGDON LAKE"): (51.330, -2.709),
}
_store_lock = threading.Lock()


def _norm(name):
    n = re.sub(r"\(.*?\)", " ", str(name or "")).upper().replace("&", " AND ").replace("/", " AND ")
    n = re.sub(r"\b(IRE|ESR|RES|RESERVOIRS?|LAKE)\b\s*$", "", n.strip())
    n = re.sub(r"\s+-\s+", " - ", re.sub(r"\s+", " ", n)).strip(" -")
    return NAME_ALIASES.get(n, n)


def _display(name):
    n = re.sub(r"\s+(IRE|ESR|RES|RESERVOIRS?)\s*$", "", str(name or "").strip(), flags=re.I)
    n = re.sub(r"\s+", " ", n)
    return n.title() if n.isupper() else n


def _load_store():
    try:
        return json.loads(STORE_FILE.read_text(encoding="utf-8"))
    except Exception:
        try:                                         # fall back to the backup copy
            return json.loads(STORE_BAK.read_text(encoding="utf-8"))
        except Exception:
            return {"companies": {}, "layers": {}, "status": {}}


def _save_store(st):
    try:
        tmp = STORE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")
        tmp.replace(STORE_FILE)
        if not STORE_BAK.exists() or time.time() - STORE_BAK.stat().st_mtime > 86400:
            STORE_BAK.write_text(STORE_FILE.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception:
        pass


def _site(st, co, name, kind="reservoir"):
    c = st["companies"].setdefault(co, {"sites": {}})
    k = _norm(name)
    s = c["sites"].get(k)
    if s is None:
        s = c["sites"][k] = {"name": _display(name), "kind": kind, "series": {}}
        pos = SITE_POS.get((co, k))
        if pos:
            s["lat"], s["lon"] = pos
    return s


def _put(st, co, name, d, pct, src, kind="reservoir", **meta):
    """Store one value. Stream wins over a website value for the same date."""
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return False
    if not (0 <= pct <= 110) or not d:
        return False
    if d > (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d"):
        return False                                 # future-dated rows (seen in South Staffs)
    s = _site(st, co, name, kind)
    for k, v in meta.items():
        if v is not None and v != "":
            s[k] = v
    old = s["series"].get(d)
    if old is not None and old[1] == "stream" and src != "stream":
        return False
    s["series"][d] = [round(pct, 2), src]
    s.setdefault("src", {})[src] = max(s.get("src", {}).get(src, ""), d)
    return old is None or old[0] != round(pct, 2)


def _parse_day(v):
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, timezone.utc).strftime("%Y-%m-%d")
    v = str(v or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", v)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", v)
    return m.group(0) if m else None


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _stream_discover(st):
    """Daily: find Stream reservoir layers we don't know yet (e.g. a new year's layer)."""
    if time.time() - st.get("discovered", 0) < STREAM_REFRESH_S:
        return
    try:
        items = _get_json(STREAM_SEARCH, 60).get("features", [])
    except Exception as e:
        st.setdefault("status", {})["stream_catalogue"] = {"ok": False, "error": str(e)[:200],
                                                           "at": time.time(), "kind": "catalogue"}
        return
    extra = st.setdefault("extra_layers", {})
    for it in items:
        p = it.get("properties") or {}
        if p.get("type") != "Feature Service" or not p.get("url"):
            continue
        url = p["url"].split("/FeatureServer")[0] + "/FeatureServer"
        for co, c in COMPANIES.items():
            if c["title"] and re.search(c["title"], p.get("title", ""), re.I):
                if url not in STREAM_LAYERS.get(co, []) and url not in extra.get(co, []):
                    extra.setdefault(co, []).append(url)
    st["discovered"] = time.time()
    st.setdefault("status", {})["stream_catalogue"] = {"ok": True, "at": time.time(), "kind": "catalogue",
                                                       "label": "Stream catalogue search"}


def _stream_layer(st, co, url):
    """Read a Stream layer if it has changed since we last read it. Returns rows stored."""
    lay = st.setdefault("layers", {}).setdefault(url, {})
    meta = _get_json(url + "/0?f=json", 40)
    edited = (meta.get("editingInfo") or {}).get("lastEditDate") or meta.get("serviceItemId")
    if lay.get("edited") == edited and lay.get("rows"):
        lay["checked"] = time.time()
        return 0
    names = {f["name"] for f in meta.get("fields", [])}
    dcol = "DATE" if "DATE" in names else "DATE_ISO"
    page = int(meta.get("maxRecordCount") or 1000)
    off, n, newest = 0, 0, lay.get("newest", "")
    while True:
        q = url + "/0/query?" + urllib.parse.urlencode({
            "where": "1=1", "outFields": "RESERVOIR_NAME,%s,LATITUDE,LONGITUDE,CAPACITY,CAPACITY_UNITS,"
                                          "CURRENT_PERCENTAGE" % dcol,
            "returnGeometry": "false", "resultOffset": off, "resultRecordCount": page,
            "orderByFields": "OBJECTID" if "OBJECTID" in names else ("ObjectId" if "ObjectId" in names else "FID"),
            "f": "json"})
        d = _get_json(q, 60)
        if "error" in d:
            raise ValueError(str(d["error"])[:200])
        fs = d.get("features", [])
        for f in fs:
            a = f.get("attributes", {})
            day = _parse_day(a.get(dcol))
            cap = _num(a.get("CAPACITY"))
            unit = str(a.get("CAPACITY_UNITS") or "").strip().upper()
            if cap is not None and unit == "M3":
                cap = cap / 1000.0                    # cubic metres -> Ml
            # Yorkshire's "TCM" is thousand cubic metres, i.e. Ml already
            if _put(st, co, a.get("RESERVOIR_NAME"), day, a.get("CURRENT_PERCENTAGE"), "stream",
                    lat=_num(a.get("LATITUDE")), lon=_num(a.get("LONGITUDE")), capacity=cap):
                n += 1
            if day and day > newest and day <= datetime.now(timezone.utc).strftime("%Y-%m-%d"):
                newest = day
        off += len(fs)
        if not fs or (len(fs) < page and not d.get("exceededTransferLimit")):
            break
        if off > 200000:
            break
    lay.update(edited=edited, rows=off, newest=newest, checked=time.time(), company=co)
    return n


# ---- company websites (current weeks, and companies not on Stream) ----
def _web(url, timeout=30):
    req = urllib.request.Request(url, headers=WEB_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _tables(html_):
    out = []
    for tb in re.findall(r"<table.*?</table>", html_, re.S | re.I):
        rows = []
        for tr in re.findall(r"<tr.*?</tr>", tb, re.S | re.I):
            rows.append([_text(c) for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.S | re.I)])
        out.append(rows)
    return out


def _pct(s):
    m = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%?", s or "")
    return float(m.group(1)) if m else None


_DMY = re.compile(r"(\d{1,2})(?:st|nd|rd|th)?\s+(January|February|March|April|May|June|July|August|"
                  r"September|October|November|December)\s+(\d{4})", re.I)


def _dmy(s):
    m = _DMY.search(s or "")
    return (f"{int(m.group(3)):04d}-{_MONTHS[m.group(2).lower()]:02d}-{int(m.group(1)):02d}"
            if m else None)


def _web_sww(st):
    t = _text(re.sub(r"<script.*?</script>|<style.*?</style>", " ", _web(SWW_URL), flags=re.S))
    iso = lambda d, m, y: f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
    n = 0
    tot = _SWW_TOTAL.search(t)
    if tot:
        n += _put(st, "sww", "Total", iso(*tot.group(2, 3, 4)), tot.group(1), "web", kind="total")
    for m in _SWW_ONE.finditer(t):
        n += _put(st, "sww", m.group(1), iso(*m.group(3, 4, 5)), m.group(2), "web")
    if not tot:
        raise ValueError("page layout changed: total not found")
    return n


ST_BASE = "https://www.stwater.co.uk"
ST_INDEX = ST_BASE + "/about-us/reservoir-levels/"


def _web_st(st):
    """Severn Trent: the index lists every weekly total; weekly pages give each reservoir."""
    h = _web(ST_INDEX)
    t = _text(re.sub(r"<script.*?</script>|<style.*?</style>", " ", h, flags=re.S))
    n = 0
    for m in re.finditer(r"(\d{1,3}\.\d)\s*%\s*(\d{1,2}(?:st|nd|rd|th)?\s+[A-Z][a-z]+\s+\d{4})", t):
        n += _put(st, "st", "Total", _dmy(m.group(2)), m.group(1), "web", kind="total")
    links = sorted(set(re.findall(r'href="(/about-us/reservoir-levels/raw-water-storage-levels-[^"]+)"', h)))
    sites = st["companies"].get("st", {}).get("sites", {})
    newest_stream = max((s.get("src", {}).get("stream", "") for s in sites.values()), default="")
    done = st.setdefault("st_pages", {})
    for link in links:
        m = re.search(r"levels-(\d{1,2})-([a-z]+)-(\d{4})", link)
        if not m or m.group(2) not in _MONTHS:
            continue
        day = f"{int(m.group(3)):04d}-{_MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
        if day <= newest_stream or link in done:
            continue                                  # Stream already has that week
        try:
            rows = [r for tb in _tables(_web(ST_BASE + link)) for r in tb]
        except Exception:
            continue
        for r in rows:
            if len(r) >= 4 and r[-2] and re.match(r"^\d{1,3}(\.\d+)?$", r[-2]):
                name = r[0] or r[1]
                if not name or name.lower().startswith("reservoir"):
                    continue
                kind = ("group" if (r[1] or "").lower() == "including" else
                        "total" if _norm(name) == "TOTAL" else "reservoir")
                n += _put(st, "st", name, day, r[-2], "web", kind=kind, capacity=_num(r[-3]))
        done[link] = day
    return n


def _web_table_groups(st, co, url, region_hint):
    """Northumbrian / Essex & Suffolk / United Utilities group tables."""
    h = _web(url)
    t = _text(h)
    day = _dmy(t)
    n = 0
    for tb in _tables(h):
        head = tb[0] if tb else []
        hd = _dmy(" ".join(head)) or day
        for r in tb[1:]:
            if len(r) < 2 or _pct(r[1]) is None or not r[0]:
                continue
            if "metres" in r[0].lower():
                continue                               # UU's Thirlmere level row
            extra = {}
            if co == "uu" and len(r) >= 5:
                extra = {"avg": _pct(r[3]), "last_year": _pct(r[4]), "week_change": _pct(r[2])}
            if len(r) >= 3 and re.search(r"average|normal", r[2], re.I):
                extra["vs"] = r[2]
            name = r[0]
            kind = "total" if re.search(r"regional total", name, re.I) else "group"
            n += _put(st, co, name, hd, _pct(r[1]), "web", kind=kind, region=region_hint, **extra)
    if not n and not day:
        raise ValueError("no table found")
    return n


def _web_bristol(st):
    t = _text(re.sub(r"<script.*?</script>|<style.*?</style>", " ", _web("https://www.bristolwater.co.uk/en/reservoir-storage"), flags=re.S))
    n = 0
    days = re.findall(r"\d{1,2}/\d{1,2}/\d{4}", t)
    day = _parse_day(days[0]) if days else None
    tot = re.search(r"total reservoir storage for the Bristol Water area is at\s*(\d{1,3}(?:\.\d+)?)%", t, re.I)
    if tot and day:
        ly = re.search(r"is at\s*" + re.escape(tot.group(1)) + r"%.{0,80}?same date in \d{4}, storage was at\s*(\d{1,3}(?:\.\d+)?)%", t, re.I | re.S)
        n += _put(st, "bw", "Total", day, tot.group(1), "web", kind="total",
                  last_year=float(ly.group(1)) if ly else None)
    for m in re.finditer(r"(\d{1,2}/\d{1,2}/\d{4})\s+([A-Z][A-Za-z ]+?) reservoir storage is at\s*(\d{1,3}(?:\.\d+)?)%"
                         r"(?:.{0,80}?same date in \d{4}, storage was at\s*(\d{1,3}(?:\.\d+)?)%)?", t, re.S):
        n += _put(st, "bw", m.group(2), _parse_day(m.group(1)), m.group(3), "web",
                  last_year=float(m.group(4)) if m.group(4) else None)
    if not tot:
        raise ValueError("page layout changed: total not found")
    return n


WEB_SOURCES = {
    "sww": ("South West Water website", SWW_URL, _web_sww),
    "st": ("Severn Trent website", ST_INDEX, _web_st),
    "nw": ("Northumbrian Water website", "https://www.nwl.co.uk/help-and-support/water/water-resources/",
           lambda st: _web_table_groups(st, "nw", "https://www.nwl.co.uk/help-and-support/water/water-resources/", "North-east")),
    "esw": ("Essex & Suffolk Water website", "https://www.eswater.co.uk/help-and-support/water/water-resources/",
            lambda st: _web_table_groups(st, "nw", "https://www.eswater.co.uk/help-and-support/water/water-resources/", "East")),
    "uu": ("United Utilities website", "https://www.unitedutilities.com/help-and-support/your-water-supply/your-reservoirs/reservoir-levels",
           lambda st: _web_table_groups(st, "uu", "https://www.unitedutilities.com/help-and-support/your-water-supply/your-reservoirs/reservoir-levels", "North-west")),
    "bw": ("Bristol Water website", "https://www.bristolwater.co.uk/en/reservoir-storage", _web_bristol),
}


def _migrate_company_log(st):
    """Bring values from the 261016.2 log (South West Water) into the store, once."""
    path = HERE / COMPANY_LOG
    if st.get("migrated_log") or not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            o = json.loads(line)
            if o.get("co") == "sww":
                _put(st, "sww", o["name"], o["d"], o["pct"], "web",
                     kind="total" if o["name"] == "Total" else "reservoir")
    except Exception:
        pass
    st["migrated_log"] = True


def _assign_regions(st):
    todo = [s for c in st["companies"].values() for s in c["sites"].values()
            if "region" not in s and s.get("lat") is not None]
    if todo:
        _region_for_points(todo)
        for s in todo:
            s.pop("country", None)


def _refresh_companies():
    """Read every Stream layer that changed and every company website; update the store."""
    with _store_lock:
        st = _load_store()
        st.setdefault("companies", {})
        status = st.setdefault("status", {})
        _migrate_company_log(st)
        _stream_discover(st)
        for co in COMPANIES:
            for url in STREAM_LAYERS.get(co, []) + st.get("extra_layers", {}).get(co, []):
                key = "stream:" + url.rsplit("/services/", 1)[-1].split("/")[0]
                try:
                    n = _stream_layer(st, co, url)
                    lay = st["layers"][url]
                    status[key] = {"ok": True, "at": time.time(), "newest": lay.get("newest"),
                                   "company": co, "new": n, "kind": "stream"}
                except Exception as e:
                    prev = status.get(key, {})
                    status[key] = dict(prev, ok=False, at=time.time(), error=str(e)[:200],
                                       company=co, kind="stream")
        for key, (label, url, fn) in WEB_SOURCES.items():
            co = "nw" if key == "esw" else key
            try:
                n = fn(st)
                newest = max((s.get("src", {}).get("web", "") for s in
                              st["companies"].get(co, {}).get("sites", {}).values()), default="")
                status["web:" + key] = {"ok": True, "at": time.time(), "newest": newest, "company": co,
                                        "label": label, "url": url, "new": n, "kind": "web"}
            except Exception as e:
                prev = status.get("web:" + key, {})
                status["web:" + key] = dict(prev, ok=False, at=time.time(), error=str(e)[:200],
                                            company=co, label=label, url=url, kind="web")
        try:
            _assign_regions(st)
        except Exception:
            pass
        _save_store(st)
        return st


def _iso_week(d):
    y, w, _ = date.fromisoformat(d).isocalendar()
    return f"{y}-W{w:02d}"


def _weekly_series(series, since=None):
    """{date: [pct, src]} -> [[date, pct]] keeping the last value in each ISO week."""
    wk = {}
    for d in sorted(series):
        if since and d < since:
            continue
        wk[_iso_week(d)] = (d, series[d][0])
    return [list(v) for _, v in sorted(wk.items())]


def _company_payload(st):
    """Latest value, recent series and company totals, by company and region."""
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=400)).isoformat()
    out = []
    for co, cfg in COMPANIES.items():
        c = st["companies"].get(co)
        if not c or not c.get("sites"):
            continue
        sites, totals, published = [], {}, {}
        for k, s in c["sites"].items():
            if not s.get("series"):
                continue
            last = max(s["series"])
            val, src = s["series"][last]
            region = (s.get("region") if cfg.get("by_position") or s.get("kind") == "group" else None) or cfg["region"]
            if s.get("kind") == "total":
                published[region] = (s, last, val, src)
                continue
            # value about a year earlier, for "same time last year"
            ly_target = date.fromisoformat(last) - timedelta(days=365)
            near = [d for d in s["series"] if abs((date.fromisoformat(d) - ly_target).days) <= 6]
            ly_val = (s["series"][min(near, key=lambda d: abs((date.fromisoformat(d) - ly_target).days))][0]
                      if near else s.get("last_year"))
            sites.append({
                "key": k, "name": s["name"], "kind": s.get("kind", "reservoir"), "region": region,
                "lat": s.get("lat"), "lon": s.get("lon"), "capacity": s.get("capacity"),
                "pct": val, "date": last, "src": src, "first": min(s["series"]),
                "stale": (today - date.fromisoformat(last)).days > STALE_DAYS,
                "last_year": ly_val, "avg": s.get("avg"), "vs": s.get("vs"),
                "week_change": s.get("week_change"),
                "recent": _weekly_series(s["series"], since)})
        # capacity-weighted weekly totals per region, from reservoirs that report a capacity
        for region in sorted({x["region"] for x in sites} | set(published)):
            rs = [s for s in c["sites"].values() if s.get("kind", "reservoir") == "reservoir"
                  and ((s.get("region") if cfg.get("by_position") else None) or cfg["region"]) == region
                  and s.get("capacity")]
            ser = []
            if len(rs) >= 2:
                capsum = sum(s["capacity"] for s in rs)
                wk = {}
                for s in rs:
                    for d, (p, _src) in s["series"].items():
                        cur = wk.setdefault(_iso_week(d), {})
                        if s["name"] not in cur or d > cur[s["name"]][0]:
                            cur[s["name"]] = (d, p, s["capacity"])
                for w in sorted(wk):
                    vals = wk[w].values()
                    cap = sum(v[2] for v in vals)
                    if cap >= 0.8 * capsum:           # need most of the storage to report that week
                        ser.append([max(v[0] for v in vals), round(sum(v[1] * v[2] for v in vals) / cap, 1)])
            if region in published:
                ps, last, val, src = published[region]
                pser = _weekly_series(ps["series"])
                first = pser[0][0] if pser else last
                # company's own total where published; our capacity-weighted figure fills in before that
                hist = [p for p in ser if p[0] < first] + pser
                totals[region] = {"pct": val, "date": last, "src": src, "series": hist,
                                  "computed_before": first if ser and ser[0][0] < first else None,
                                  "last_year": ps.get("last_year"),
                                  "stale": (today - date.fromisoformat(last)).days > STALE_DAYS}
            elif ser:
                totals[region] = {"pct": ser[-1][1], "date": ser[-1][0], "src": "computed",
                                  "series": ser, "computed": True,
                                  "stale": (today - date.fromisoformat(ser[-1][0])).days > STALE_DAYS}
        out.append({"key": co, "company": cfg["name"], "region": cfg["region"],
                    "sites": sorted(sites, key=lambda x: -x["pct"]), "totals": totals})
    status = []
    for k, v in sorted(st.get("status", {}).items()):
        if k == "stream_catalogue":
            continue
        status.append(dict(v, source=k))
    return out, status


def _companies(cache):
    st = _refresh_companies()
    comps, status = _company_payload(st)
    cache["companies"], cache["company_status"] = comps, status
    return comps, status


def company_series(co, key):
    """Full history of one reservoir, for the detail chart."""
    st = _load_store()
    s = st.get("companies", {}).get(co, {}).get("sites", {}).get(key)
    if not s:
        return {"error": "not found"}
    return {"company": COMPANIES.get(co, {}).get("name"), "name": s["name"], "capacity": s.get("capacity"),
            "series": [[d, v[0]] for d, v in sorted(s["series"].items())]}


# --------------------------------------------------------------- London -----
def _london():
    """Lower Lee / Lower Thames groups: latest, this year, and day-of-year range since 1989."""
    rows = []
    for line in _get(LONDON_CSV, 60).splitlines()[1:]:
        p = [x.strip() for x in line.split(",")]
        if len(p) < 5:
            continue
        try:
            d = datetime.strptime(p[0], "%d-%b-%y").date()
            rows.append((d, float(p[3]), float(p[4])))
        except ValueError:
            continue
    if not rows:
        return None
    rows.sort()
    last = rows[-1]
    yr = last[0].year
    out = {"latest": {"date": last[0].isoformat(), "lee": last[1], "thames": last[2]},
           "series": {}}
    for idx, key in ((1, "lee"), (2, "thames")):
        by_doy = {}
        for d, *v in rows:
            if d.year < yr:
                by_doy.setdefault(min(d.timetuple().tm_yday, 365), []).append(v[idx - 1])
        rng = []
        for doy in range(1, 366, 3):                 # every 3rd day keeps the payload small
            xs = sorted(by_doy.get(doy, []))
            if xs:
                rng.append([doy, xs[0], xs[len(xs) // 2], xs[-1]])
        this = [[d.timetuple().tm_yday, v[idx - 1]] for d, *v in rows if d.year == yr]
        prev = [[d.timetuple().tm_yday, v[idx - 1]] for d, *v in rows if d.year == yr - 1][::3]
        out["series"][key] = {"range": rng, "this": this[::2] + [this[-1]] if this else [],
                              "prev": prev, "year": yr}
    return out


# ---------------------------------------------------------------- build -----
def _build():
    cache = _load_cache()
    errors = {}
    try:
        monthly = _monthly_regions(cache)
    except Exception as e:
        monthly, errors["monthly"] = cache.get("monthly", {}), str(e)
    try:
        weekly = _weekly(cache)
    except Exception as e:
        weekly, errors["weekly"] = cache.get("weekly", []), str(e)
    try:
        areas = _area_notes(cache)
        cache["areas"] = areas
    except Exception as e:
        areas, errors["areas"] = cache.get("areas", []), str(e)
    try:
        london = _london()
        cache["london"] = london
    except Exception as e:
        london, errors["london"] = cache.get("london"), str(e)
    try:
        companies, company_status = _companies(cache)
    except Exception as e:
        companies, company_status = cache.get("companies", []), cache.get("company_status", [])
        errors["companies"] = str(e)
    _save_cache(cache)
    gauges = _state["snapshot"]["gauges"] if _state["snapshot"] else cache.get("gauges", [])
    slim = lambda xs: [{k: v for k, v in x.items() if k != "notes"} for x in xs]
    return {
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "monthly": monthly, "weekly": slim(w for w in weekly if w.get("pct") is not None),
        "drought": _drought_status(sorted(weekly + areas, key=lambda w: w.get("date") or "")),
        "areas": slim(areas), "london": london, "gauges": gauges, "companies": companies,
        "company_status": company_status,
        "regions": REGIONS, "region_areas": REGION_AREAS,
        "pypdf": _pypdf is not None, "errors": errors,
        "nations": NATIONS,
    }


def _refresh_gauges():
    try:
        g = _gauges()
        _log_gauges(g)
        hist = _logged_history([x["ref"] for x in g])
        for x in g:                                   # merge our log with the API's 30 days
            merged = dict(hist.get(x["ref"], []))
            merged.update(dict(x["daily"]))
            x["daily"] = sorted(merged.items())
        with _lock:
            if _state["snapshot"]:
                _state["snapshot"]["gauges"] = g
            _state["gauges_built"] = time.time()
        c = _load_cache()
        c["gauges"] = g
        _save_cache(c)
    except Exception as e:
        _state["error"] = f"gauges: {e}"


def _worker():
    try:
        snap = _build()
        with _lock:
            _state["snapshot"], _state["built"] = snap, time.time()
        if time.time() - _state["gauges_built"] > GAUGE_REFRESH_S:
            _refresh_gauges()
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {e}"
    finally:
        _state["building"] = False


def _kick(force=False):
    with _lock:
        if _state["building"]:
            return
        stale = time.time() - _state["built"] > REFRESH_S
        gstale = time.time() - _state["gauges_built"] > GAUGE_REFRESH_S
        if not (force or stale or gstale):
            return
        _state["building"] = True
    if not stale and not force:
        threading.Thread(target=lambda: (_refresh_gauges(), _state.update(building=False)),
                         daemon=True).start()
    else:
        threading.Thread(target=_worker, daemon=True).start()


def home_region(lat, lon):
    """(region, country) for a point, via postcodes.io with a wide search radius."""
    try:
        d = _get_json(f"https://api.postcodes.io/postcodes?lon={lon}&lat={lat}"
                      f"&limit=1&radius=10000&widesearch=true", 10)
        r = (d.get("result") or [None])[0]
        if r:
            places = [r.get("admin_county"), r.get("admin_district")]
            places += re.split(r"\s*(?:&|,|\band\b)\s*", r.get("pfa") or "")
            places = [p.strip() for p in places if p and len(p.strip()) > 3 and "Police" not in p]
            return _ons_to_ea(r), r.get("country"), places
    except Exception:
        pass
    return _nearest_region(lat, lon), None, []


_home_cache = {}


def get_reservoirs(lat=None, lon=None, country=None, series=None):
    if series:                                        # "co|key": one reservoir's full history
        co, _, key = series.partition("|")
        return company_series(co, key)
    _kick()
    with _lock:
        snap = _state["snapshot"]
    if snap is None:
        c = _load_cache()                             # serve last run's data while rebuilding
        if c.get("monthly") or c.get("weekly"):
            slim = lambda xs: [{k: v for k, v in x.items() if k != "notes"} for x in xs]
            snap = {"monthly": c.get("monthly", {}),
                    "weekly": slim(w for w in c.get("weekly", []) if w.get("pct") is not None),
                    "drought": _drought_status(sorted(c.get("weekly", []) + c.get("areas", []),
                                                      key=lambda w: w.get("date") or "")),
                    "areas": slim(c.get("areas", [])), "london": c.get("london"),
                    "companies": c.get("companies", []), "company_status": c.get("company_status", []),
                    "gauges": c.get("gauges", []), "regions": REGIONS,
                    "region_areas": REGION_AREAS, "pypdf": _pypdf is not None,
                    "errors": {}, "built": None, "stale": True, "nations": NATIONS}
        else:
            return {"status": "building", "error": _state["error"]}
    out = dict(snap)
    out["status"] = "ok" if not out.get("stale") else "refreshing"
    if lat is not None and lon is not None:
        key = (round(lat, 2), round(lon, 2))
        if key not in _home_cache:
            _home_cache[key] = home_region(lat, lon)
        reg, ctry, places = _home_cache[key]
        ctry = ctry or country
        if ctry and ctry != "England":
            reg = None
        out["home"] = {"lat": lat, "lon": lon, "region": reg, "country": ctry or "England",
                       "places": places}
    return out


if __name__ == "__main__":           # manual check: python reservoirs.py
    snap = _build()
    _refresh_gauges()
    print(json.dumps({k: (v if k not in ("gauges", "london", "areas") else len(v or []))
                      for k, v in snap.items() if k != "weekly"}, indent=1)[:3000])
    print("weekly:", [(w["date"], w["pct"], w["below_lta"]) for w in snap["weekly"]][-6:])
