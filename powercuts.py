# powercuts.py — GB DNO live power-cuts aggregator (data layer) for GB Energy Monitor.
#
# Fetches live power-cut / incident feeds from the GB Distribution Network Operators,
# normalises each to one common incident model, aggregates to per-operator and national
# totals, records a rolling history (monthly JSONL) with exact new/cleared counts, and
# exposes a snapshot for the dashboard's /api/powercuts endpoint.
#
# Feeds (verified 2026-09-09):
#   NGED  — CKAN CSV, open, no auth            (per-incident: lat/lon, customers, ETR, PSR)
#   UKPN  — OpenDataSoft, open, no auth         (per-incident: geopoint, customers, ETR)
#   NPg   — OpenDataSoft, open, no auth         (per-incident: lat/lng, customers, ETR)
#   ENWL  — OpenDataSoft, needs free API key    (gated; adapter ready, dormant without key)
#   SPEN  — OpenDataSoft, needs free API key    (gated; adapter ready, dormant without key)
#   SSEN  — Power Track "Real Time Outage" API   (open, no key, CC-BY 4.0; per-incident:
#          location, customerCount, ETR, affected postcodes)
#
# Off-GB operators (added 2026-09-16):
#   NIE   — NIE Networks OpenDataSoft, needs free API key (Northern Ireland; CC-BY 4.0).
#          Feed is postcode-only (NO coordinates), so incidents are geocoded to BT
#          postcode-district centroids for plotting. Gated; dormant without a key.
#   ESB   — ESB Networks PowerCheck API (Republic of Ireland; shared subscription key).
#          A lightweight list (id/type/coords) plus one per-outage detail call each
#          (customers off, ETR, planner group). Detail responses are cached to keep the
#          per-poll call count down. Restored outages are dropped (cleared, not off).
#
# Honesty over plausibility: operators differ in fields and definitions. Missing data is
# null (never invented); a national total names how many operators are reporting; a feed
# that is down / stale / key-less is flagged, never silently treated as zero cuts.
#
# Python 3, stdlib only. No import of the live server.
#
# Build 261014.5  (version = YYMMDD.N in UT; bump on every change to this file)
# Change history: CHANGELOG.md

from __future__ import annotations
import json, csv, io, os, re, time, threading, datetime, gzip
import concurrent.futures
import urllib.request, urllib.parse, urllib.error

_DIR = os.path.dirname(os.path.abspath(__file__))
KEYS_FILE = os.path.join(_DIR, "powercut_keys.json")           # server-side key store
LAST_IDS_FILE = os.path.join(_DIR, "powercut_last_ids.json")   # for new/cleared diffing across restarts
WATCH_FILE = os.path.join(_DIR, "powercut_watch.json")         # monitored postcode areas (user list)
HIST_PREFIX = os.path.join(_DIR, "powercuts-")                 # powercuts-YYYY-MM.jsonl
UA = {"User-Agent": "uk-grid-monitor/1.0 (personal dashboard)"}

# Rough customers-served per operator, for a "% of customers off" context figure only
# (denominators, not live data). Approximate, label as context.
CUSTOMERS_TRACKED = {
    "NGED": 7_000_000, "UKPN": 8_300_000, "NPg": 3_900_000,
    "ENWL": 2_400_000, "SPEN": 3_500_000, "SSEN": 3_800_000,
    "NIE": 900_000, "ESB": 2_400_000,          # Northern Ireland / Republic of Ireland
}

# NGED live "detailed" power-cuts CSV (all four NGED licence areas in one file)
NGED_CSV = ("https://connecteddata.nationalgrid.co.uk/dataset/"
            "d6672e1e-c684-4cea-bb78-c7e5248b62a2/resource/"
            "a1365982-4e05-463c-8304-8323a2ba0ccd/download/live_detailed_power_cuts.csv")

# SSEN live faults — Power Track "Real Time Outage" API (open, no key, CC-BY 4.0).
# NB: the SSEN data *portal* (data-api.ssen.co.uk) is Cloudflare-gated against non-browser
# user-agents, but THIS data endpoint is open to any client.
SSEN_URL = "https://external.distribution.prd.ssen.co.uk/opendataportal-prd/v4/api/getallfaults"

# OpenDataSoft Explore v2.1 hosts + dataset ids
ODS = {
    "UKPN": ("ukpowernetworks.opendatasoft.com", "ukpn-live-faults", False),
    "NPg":  ("northernpowergrid.opendatasoft.com", "live-power-cuts-data", False),
    "ENWL": ("electricitynorthwest.opendatasoft.com", "live_incidents", True),   # needs key
    "SPEN": ("spenergynetworks.opendatasoft.com", "distribution-network-live-outages", True),  # needs key
    "NIE":  ("nienetworks.opendatasoft.com", "nie-networks-network-faults", True),   # needs key (NI)
}

FETCH_TIMEOUT = 20
CACHE_TTL = 120           # per-source cache; feeds move every ~5-15 min, poll politely
STALE_GRACE = 7200        # keep serving a feed's last good reading (incl. an empty 'no
                          # outages' one) for up to 2h after it stops responding, THEN 'down'
# Frozen-feed detection. A feed can keep answering while its content stops moving (NPg,
# 3–5 Oct 2026: 54 h of identical incidents, all reported 'ok'). When an operator's
# incident set — ids, customers off, status text — has not changed for FROZEN_DAY_S of
# DAYTIME (FROZEN_DAY_START–FROZEN_DAY_END UTC; quiet nights don't count), it is flagged
# 'stale' with a reason. It stays in the totals (last reading, as for any stale feed) and
# clears on the first change. Backtest over Sep–Oct 2026 (aggregate figures, a coarser
# test than this): no flags on UKPN, SSEN, SPEN, ESB; flags on NPg 3–5 Oct, NIE 25–27 Sep,
# NGED 19 Sep and several ENWL spells that look like real freezes.
FROZEN_DAY_S = 3 * 3600
FROZEN_DAY_START, FROZEN_DAY_END = 6, 22   # UTC hours counted as daytime
FROZEN_MAX_STEP = 1800    # a gap longer than this between checks isn't counted as unchanged
ODS_PAGE = 100            # OpenDataSoft records page size (max 100)
ODS_MAX_PAGES = 60        # safety cap (6000 incidents) — GB rarely exceeds a few hundred

# ESB Networks PowerCheck (Republic of Ireland). A public subscription key is embedded in
# ESB's own web map; it is used here as the default and can be overridden from the settings
# UI (stored under key "ESB"). Auth is the 'API-Subscription-Key' request header.
ESB_BASE = "https://api.esb.ie/esbn/powercheck/v1.0/outages"
ESB_DEFAULT_KEY = "f713e48af3a746bbb1b110ab69113960"
ESB_MAX_DETAIL = 300      # safety cap on per-outage detail calls per poll
ESB_DETAIL_TTL = 300      # per-outage detail cache (s) — ids are stable, so re-poll is cheap
ESB_WORKERS = 8           # small thread pool for the detail fan-out
# The ESB list endpoint (a heavier Azure APIM call than the GB feeds) is the one request
# that trips the 'stale' badge when it fails, so give it a longer timeout and one
# short-backoff retry on a transient error before giving up.
ESB_LIST_TIMEOUT = 25     # seconds — longer than the default 20 for the slower ESB list
ESB_LIST_ATTEMPTS = 2     # total attempts (one retry) before the feed is flagged stale
ESB_RETRY_BACKOFF = 2.0   # seconds to wait between list attempts
_ESB_TRANSIENT = (408, 425, 429, 500, 502, 503, 504)   # retry these; fail fast otherwise (e.g. 401 key)


# ───────────────────────── small helpers ────────────────────────────────────
def _num(v):
    """Best-effort number -> int, else None."""
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _s(v):
    return None if v is None else str(v).strip() or None


def _truthy(v):
    """Parse a planned flag that may be bool, 'true'/'false', 1/0, or None (unknown)."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "1", "yes", "planned")


def _incident(dno, region, iid, *, lat=None, lon=None, postcodes=None,
              off=None, predicted=None, planned=None, category=None, voltage=None,
              status=None, etr=None, start_ts=None, last_updated=None, psr=None):
    """One normalised incident. Missing fields stay None — never invented."""
    return {
        "dno": dno, "region": region, "id": iid,
        "lat": lat, "lon": lon, "postcodes": postcodes,
        "customers_off": off, "customers_predicted": predicted,
        "planned": planned, "category": category, "voltage": voltage,
        "status": status, "etr": etr, "start_ts": start_ts,
        "last_updated": last_updated, "psr_customers": psr,
    }


def _get(url, headers=None, timeout=FETCH_TIMEOUT):
    h = dict(UA)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        # Some feeds (ESB list, since late Sep 2026) gzip the body without being asked;
        # unpack on the header or the gzip magic bytes so callers always get plain bytes.
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip" or body[:2] == b"\x1f\x8b":
            body = gzip.decompress(body)
        return body


def _ods_records(host, dataset, apikey=None, timeout=FETCH_TIMEOUT):
    """Fetch ALL records of an OpenDataSoft Explore v2.1 dataset (paginated). The API key,
    when present, goes in the Authorization header (never the URL, so it can't leak to
    logs). Returns a list of record dicts; raises on HTTP/network error."""
    headers = {"Authorization": "Apikey " + apikey} if apikey else None
    base = f"https://{host}/api/explore/v2.1/catalog/datasets/{dataset}/records"
    out = []
    offset = 0
    for _ in range(ODS_MAX_PAGES):
        url = base + "?" + urllib.parse.urlencode({"limit": ODS_PAGE, "offset": offset})
        blob = json.loads(_get(url, headers=headers, timeout=timeout))
        results = blob.get("results") or []
        out.extend(results)
        total = blob.get("total_count")
        offset += ODS_PAGE
        if not results or (total is not None and offset >= total):
            break
    return out


# ───────────────────────── per-operator adapters ────────────────────────────
# Each returns a list of normalised incidents, or raises. Never invents fields.

def adapter_nged():
    raw = _get(NGED_CSV).decode("utf-8-sig", "replace")
    rows = list(csv.DictReader(io.StringIO(raw)))
    out = []
    for r in rows:
        out.append(_incident(
            "NGED", _s(r.get("licence_area")) or "NGED", _s(r.get("fault_id")),
            lat=_flt(r.get("location_latitude")), lon=_flt(r.get("location_longitude")),
            postcodes=_s(r.get("postcode")),
            off=_num(r.get("confirmed_off")), predicted=_num(r.get("predicted_off")),
            planned=_truthy(r.get("planned")),
            category=_s(r.get("category")), voltage=_s(r.get("voltage")),
            status=_s(r.get("status")),
            # planned works carry their window in planned_outage_*; 'etr' is blank for them
            etr=_s(r.get("etr")) or (_s(r.get("planned_outage_end_date")) if _truthy(r.get("planned")) else None),
            start_ts=(_s(r.get("planned_outage_start_date")) if _truthy(r.get("planned")) else None)
                     or _s(r.get("date_of_reported_fault")),
            last_updated=_s(r.get("last_updated")),
            psr=_num(r.get("number_of_psr_customers")),
        ))
    return out


def adapter_ukpn(apikey=None):
    """UK Power Networks — ODS 'ukpn-live-faults'. The dataset also carries restored
    incidents (most of its rows) and 'MULT-…' rows that group incidents already listed on
    their own; both are dropped, so only live incidents are counted and plotted.
    Customer counts: 'nocustomeraffected' stays 0 until UKPN confirms a figure (its website's
    'Est. N' comes from NoCustomersPredicted, which is not in this dataset), so a live 0 is
    'not yet confirmed' — customers_off None + count_pending, never a real zero. Planned
    works give their figure in 'noplannedcustomers' (-> customers_predicted).
    'postcodesaffected' lists sectors; 'fullpostcodedata' the affected full postcodes
    (compact, ';'-separated), kept as full_postcodes for monitored-area matching."""
    recs = _ods_records(*ODS["UKPN"][:2], apikey=apikey)
    out = []
    for r in recs:
        ref = _s(r.get("incidentreference")) or ""
        typ = (_s(r.get("powercuttype")) or "").lower()
        if r.get("restoreddatetime") or typ in ("restored", "multiple") or ref.upper().startswith("MULT-"):
            continue
        gp = r.get("geopoint") or {}
        planned = _truthy(r.get("powercuttype"))
        off = _num(r.get("nocustomeraffected"))
        pending = not off                                  # 0 / missing on a live incident
        pl = _num(r.get("noplannedcustomers"))
        full = [t.strip() for t in str(r.get("fullpostcodedata") or "").split(";") if t.strip()]
        inc = _incident(
            "UKPN", "UKPN", ref or None,
            lat=gp.get("lat"), lon=gp.get("lon"),
            postcodes=_s(r.get("postcodesaffected")) or _s(r.get("fullpostcodedata")),
            off=(None if pending else off),
            predicted=(pl if (planned and pl) else None),
            planned=planned,
            category=_s(r.get("incidenttypename")),
            voltage=None,
            status="active",
            etr=_s(r.get("estimatedrestorationdate")),
            start_ts=_s(r.get("creationdatetime")) or _s(r.get("receiveddate")),
            last_updated=_s(r.get("creationdatetime")),
            psr=None,
        )
        if pending:
            inc["count_pending"] = True
        if full:
            inc["full_postcodes"] = full
        out.append(inc)
    return out


def adapter_npg(apikey=None):
    """Northern Powergrid — ODS 'live-power-cuts-data', one row per postcode/stage (collapsed
    by _dedup). The dataset keeps FINISHED incidents (incidentstatus 6, isaffected 0, "The
    scheduled work has now been completed") — 31 of 38 on 14 Oct, 1,366 customers that were
    not off — so those are dropped. Planned/unplanned comes from 'natureofoutage' ("Planned
    Work on System" / "Localised Fault" / "Large Area Fault"); 'powercutcategory' is an
    internal numeric code, so the nature is used as the category. A live 0 in
    'totalconfirmedpowercut' means not yet confirmed: customers_off None + count_pending, the
    predicted figure kept. Times carry a UTC offset."""
    recs = _ods_records(*ODS["NPg"][:2], apikey=apikey)
    out = []
    for r in recs:
        if r.get("incidentstatus") == 6 or r.get("isaffected") == 0:
            continue
        pcs = r.get("postcode")
        if isinstance(pcs, list):
            pcs = ", ".join(str(p) for p in pcs)
        nature = _s(r.get("natureofoutage"))
        nl = (nature or "").lower()
        planned = True if "planned" in nl else (False if ("fault" in nl or "unplanned" in nl) else None)
        off = _num(r.get("totalconfirmedpowercut"))
        inc = _incident(
            "NPg", _s(r.get("area")) or "NPg", _s(r.get("reference")) or _s(r.get("incidentid")),
            lat=r.get("lat"), lon=r.get("lng"),
            postcodes=_s(pcs),
            off=(off or None), predicted=(_num(r.get("totalpredictedpowercut")) or None),
            planned=planned,
            category=nature or _s(r.get("powercutcategory")), voltage=_s(r.get("type")),
            status=_s(r.get("customerstagesequencemessage")) or _s(r.get("incidentstatus")),
            etr=_s(r.get("estimatedtimetillresolution")),
            start_ts=_s(r.get("loggedtime")),
            last_updated=_s(r.get("updatedate")),
            psr=None,
        )
        if not off:
            inc["count_pending"] = True
        out.append(inc)
    return out


def adapter_ssen(apikey=None):
    d = json.loads(_get(SSEN_URL))
    out = []
    for r in (d.get("faults") or []):
        loc = r.get("location") or {}
        aa = r.get("affectedAreas")
        if isinstance(aa, list):
            aa = ", ".join(str(a) for a in aa)
        typ = (_s(r.get("type")) or "").upper()
        # SSEN 'type': PSI = Planned Supply Interruption; LV/HV/EHV = unplanned faults by
        # voltage (observed values: LV, PSI). Unknown codes -> planned unknown (None).
        planned = True if typ == "PSI" else (False if typ in ("LV", "HV", "EHV") else None)
        volt = typ if typ in ("LV", "HV", "EHV") else None
        cat = "Planned supply interruption" if typ == "PSI" else (typ or None)
        out.append(_incident(
            "SSEN", "SSEN", _s(r.get("reference")),
            lat=loc.get("latitude"), lon=loc.get("longitude"),
            postcodes=_s(aa),
            off=_num(r.get("customerCount")), predicted=None,
            planned=planned, category=cat, voltage=volt,
            status=_s(r.get("jobStatus")),
            etr=_s(r.get("estimatedRestorationTimeUtc")),
            start_ts=_s(r.get("loggedAtUtc")),
            last_updated=_s(r.get("estimatedFaultUpdateTimeUtc")) or _s(r.get("loggedAtUtc")),
            psr=None,
        ))
    return out


def _ods_generic(dno):
    """ENWL / SPEN adapter factory — same OpenDataSoft shape; field names TBC in Phase 0
    against a real key, so map defensively across likely names."""
    host, dataset, _needs = ODS[dno]

    def _adapter(apikey=None):
        recs = _ods_records(host, dataset, apikey=apikey)
        out = []
        for r in recs:
            gp = r.get("geopoint") or {}
            lat = gp.get("lat") if gp else (r.get("lat") or r.get("latitude"))
            lon = gp.get("lon") if gp else (r.get("lng") or r.get("lon") or r.get("longitude"))
            off = _num(r.get("nocustomeraffected")) or _num(r.get("totalconfirmedpowercut")) \
                or _num(r.get("customersaffected")) or _num(r.get("customeraffected"))
            out.append(_incident(
                dno, _s(r.get("region")) or _s(r.get("area")) or dno,
                _s(r.get("incidentreference")) or _s(r.get("reference")) or _s(r.get("incidentid")) or _s(r.get("id")),
                lat=lat, lon=lon,
                postcodes=_s(r.get("postcode")) or _s(r.get("postcodesaffected")),
                off=off, predicted=_num(r.get("totalpredictedpowercut")),
                planned=_truthy(r.get("powercuttype") or r.get("planned")),
                category=_s(r.get("powercutcategory")) or _s(r.get("category")) or _s(r.get("incidenttypename")),
                voltage=_s(r.get("voltage")) or _s(r.get("type")),
                status=_s(r.get("status")) or _s(r.get("incidentstatus")),
                etr=_s(r.get("estimatedrestorationdate")) or _s(r.get("estimatedtimetillresolution")) or _s(r.get("etr")),
                start_ts=_s(r.get("creationdatetime")) or _s(r.get("loggedtime")) or _s(r.get("starttime")),
                last_updated=_s(r.get("updatedate")) or _s(r.get("lastupdated")),
                psr=None,
            ))
        return out
    return _adapter


def adapter_enwl(apikey=None):
    """Electricity North West — ODS dataset 'live_incidents'. Field names verified against
    the live feed (the old shared _ods_generic guesses did not match, so every ENWL record
    came back with null lat/lon/customers/id and never plotted):
        geo_point_2d{lat,lon} · incident_num · incident_type · incident_status ·
        outage_time · estimated_restoration_time · customers_off_supply · customers_affected
    Sized/counted on customers_off_supply (customers currently OFF supply — the same basis as
    the national 'customers off' total), falling back to customers_affected. Voltage is taken
    ONLY from an explicit 'High/Low Voltage' incident_type, never guessed otherwise."""
    host, dataset, _ = ODS["ENWL"]
    out = []
    for r in _ods_records(host, dataset, apikey=apikey):
        gp = r.get("geo_point_2d") or {}
        off = _num(r.get("customers_off_supply"))
        if off is None:
            off = _num(r.get("customers_affected"))
        typ = _s(r.get("incident_type"))
        tl = (typ or "").lower()
        volt = "HV" if "high voltage" in tl else ("LV" if "low voltage" in tl else None)
        out.append(_incident(
            "ENWL", "ENWL", _s(r.get("incident_num")),
            lat=gp.get("lat"), lon=gp.get("lon"),
            off=off, predicted=None,
            planned=False,                     # live_incidents are unplanned faults
            category=typ, voltage=volt,
            status=_s(r.get("incident_status")),
            etr=_s(r.get("estimated_restoration_time")),
            start_ts=_s(r.get("outage_time")),
            last_updated=None, psr=None,
        ))
    return out


def _flt(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def adapter_spen(apikey=None):
    """SP Energy Networks — via the ODS 'national-energy-outage-data' (NEO) dataset. SPEN's
    'distribution-network-live-outages' feed carries neither coordinates nor customer counts;
    NEO carries coordinates, voltage and a planned flag (but still NO customer count — SPEN
    publishes none anywhere on ODS). So customers_off stays None and magnitude is instead the
    number of POSTCODE SECTORS affected ('post_code' is a ';'-separated string), exposed as
    incident['sectors']. Filtered to LIVE incidents (not yet restored). Fields verified live:
      fault_id · dno · planned(0/1) · status · voltage · location_latitude/longitude ·
      post_code(';'-sep sectors) · date_of_reported_fault · etr · date_of_restoration · region
    """
    out = []
    for r in _ods_records("spenergynetworks.opendatasoft.com",
                          "national-energy-outage-data", apikey=apikey):
        # live only — drop anything already restored/closed
        if r.get("date_of_restoration"):
            continue
        if (_s(r.get("status")) or "").lower() in ("completed", "restored", "closed", "cancelled"):
            continue
        # planned works whose end time is well past are finished, however they are still
        # labelled (one from 24 Sep was still "Awaiting" on 14 Oct)
        if _truthy(r.get("planned")) and _ended(_s(r.get("etr"))):
            continue
        sectors = [s.strip() for s in str(r.get("post_code") or "").split(";") if s.strip()]
        inc = _incident(
            "SPEN", _s(r.get("region")) or "SPEN", _s(r.get("fault_id")),
            lat=_flt(r.get("location_latitude")), lon=_flt(r.get("location_longitude")),
            postcodes=("; ".join(sectors) or None),
            off=None,                                    # SPEN publishes no customer-off count
            planned=_truthy(r.get("planned")),
            category=None, voltage=_s(r.get("voltage")),
            status=_s(r.get("status")),
            etr=_s(r.get("etr")),
            start_ts=(_s(r.get("planned_outage_start_date")) if _truthy(r.get("planned")) else None)
                     or _s(r.get("date_of_reported_fault")),
            last_updated=_s(r.get("upload_date")), psr=None,
        )
        inc["sectors"] = len(sectors)                    # magnitude proxy: postcode sectors affected
        out.append(inc)
    return out


# ───────────────────────── Northern Ireland (NIE Networks) ──────────────────
# The NIE Networks ODS 'nie-networks-network-faults' feed is postcode-only — it carries no
# coordinates — so incidents are geocoded to the centroid of their BT postcode DISTRICT
# (the outward code, e.g. BT47), the same "postcode centroid, not premises" basis as the GB
# dots but coarser. Districts with no centroid on file stay unplaced (lat/lon None): they
# still count toward totals, they just don't plot. Source: postcodes.io / OS OpenData.
BT_CENTROIDS = {   # BT postcode-district centroids [lon,lat] (postcodes.io, OS OpenData)
    "BT1":[-5.9285,54.5998], "BT2":[-5.9305,54.5938], "BT3":[-5.9046,54.6167],
    "BT4":[-5.87,54.6026], "BT5":[-5.871,54.5891], "BT6":[-5.898,54.58],
    "BT7":[-5.9246,54.5821], "BT8":[-5.9058,54.5399], "BT9":[-5.95,54.5721],
    "BT10":[-5.9855,54.5595], "BT11":[-5.9981,54.5764], "BT12":[-5.9586,54.5929],
    "BT13":[-5.9594,54.6058], "BT14":[-5.9626,54.6214], "BT15":[-5.932,54.6234],
    "BT16":[-5.7971,54.5905], "BT17":[-6.0202,54.5535], "BT18":[-5.8219,54.6413],
    "BT19":[-5.6674,54.6513], "BT20":[-5.6657,54.6585], "BT21":[-5.5478,54.6404],
    "BT22":[-5.5149,54.5023], "BT23":[-5.7153,54.5643], "BT24":[-5.8716,54.4172],
    "BT25":[-6.1126,54.396], "BT26":[-6.0651,54.4543], "BT27":[-6.0239,54.5091],
    "BT28":[-6.0867,54.523], "BT29":[-6.197,54.6205], "BT30":[-5.7022,54.3351],
    "BT31":[-5.9685,54.2768], "BT32":[-6.2455,54.3355], "BT33":[-5.8911,54.2233],
    "BT34":[-6.1758,54.1527], "BT35":[-6.4413,54.1621], "BT36":[-5.9565,54.6766],
    "BT37":[-5.9082,54.6775], "BT38":[-5.8018,54.7287], "BT39":[-6.0182,54.7452],
    "BT40":[-5.8238,54.8458], "BT41":[-6.2561,54.7321], "BT42":[-6.2759,54.8547],
    "BT43":[-6.2658,54.8912], "BT44":[-6.2887,54.9721], "BT45":[-6.639,54.7599],
    "BT46":[-6.6637,54.8652], "BT47":[-7.2139,54.9741], "BT48":[-7.3279,55.0104],
    "BT49":[-6.951,55.0469], "BT51":[-6.6823,55.0602], "BT52":[-6.6553,55.1345],
    "BT53":[-6.4753,55.0854], "BT54":[-6.2568,55.2044], "BT55":[-6.7102,55.1791],
    "BT56":[-6.6503,55.1976], "BT57":[-6.5182,55.2036], "BT60":[-6.649,54.2927],
    "BT61":[-6.6226,54.3688], "BT62":[-6.4663,54.412], "BT63":[-6.391,54.4079],
    "BT64":[-6.3902,54.449], "BT65":[-6.366,54.4449], "BT66":[-6.3329,54.455],
    "BT67":[-6.2673,54.4867], "BT68":[-6.8485,54.3651], "BT69":[-6.9509,54.42],
    "BT70":[-6.8865,54.5142], "BT71":[-6.7045,54.513], "BT74":[-7.6651,54.3449],
    "BT75":[-7.317,54.373], "BT76":[-7.1923,54.4199], "BT77":[-7.1182,54.4291],
    "BT78":[-7.376,54.5704], "BT79":[-7.2009,54.6232], "BT80":[-6.7452,54.6464],
    "BT81":[-7.6108,54.696], "BT82":[-7.4303,54.8264], "BT92":[-7.4678,54.2245],
    "BT93":[-7.8255,54.4683], "BT94":[-7.5337,54.3762],
}
_BT_RE = re.compile(r"(BT\d{1,2})", re.I)


def _ni_geocode(postcode):
    """Map a NI postcode (full unit or district) to its BT-district centroid (lat, lon).
    Returns (None, None) when no BT district can be read or it isn't in the table."""
    if not postcode:
        return None, None
    m = _BT_RE.search(str(postcode).upper())
    if not m:
        return None, None
    c = BT_CENTROIDS.get(m.group(1))
    if not c:
        return None, None
    return c[1], c[0]                    # (lat, lon)


def adapter_nie(apikey=None):
    """NIE Networks (Northern Ireland) — ODS 'nie-networks-network-faults'. Fields (verified
    against the dataset schema): outageid · outagetype · starttime · estrestorefulldatetime ·
    postcode · numcustaffected · statusmessage · updatedtimestamp. No coordinates, so the
    postcode is geocoded to its BT-district centroid. Rows whose type reads as already
    restored are dropped (cleared, not off)."""
    host, dataset, _ = ODS["NIE"]
    out = []
    for r in _ods_records(host, dataset, apikey=apikey):
        typ = _s(r.get("outagetype"))
        tl = (typ or "").lower()
        if "restore" in tl or "cancel" in tl:
            continue                                     # already cleared
        pc = _s(r.get("postcode"))
        lat, lon = _ni_geocode(pc)
        planned = True if "plan" in tl else (False if ("fault" in tl or "unplan" in tl) else None)
        out.append(_incident(
            "NIE", "NIE", _s(r.get("outageid")),
            lat=lat, lon=lon, postcodes=pc,
            off=_num(r.get("numcustaffected")), predicted=None,
            planned=planned, category=typ, voltage=None,
            status=_s(r.get("statusmessage")),
            etr=_nie_time(r.get("estrestorefulldatetime")) or _s(r.get("estrestorefulldatetime")),
            start_ts=_nie_time(r.get("starttime")) or _s(r.get("starttime")),
            last_updated=_nie_time(r.get("updatedtimestamp")) or _s(r.get("updatedtimestamp")), psr=None,
        ))
    return out


# ───────────────────────── Republic of Ireland (ESB Networks) ────────────────
_esb_detail_cache = {}          # id -> {"d": detail_dict, "ts": ...}
_esb_lock = threading.Lock()


def _esb_get(url, key, timeout=FETCH_TIMEOUT):
    return _get(url, headers={"API-Subscription-Key": key,
                              "Referer": "https://powercheck.esbnetworks.ie/"}, timeout=timeout)


def _esb_list(key):
    """Fetch the ESB outage LIST with a longer timeout and one short-backoff retry, so a
    single slow response or transient upstream error (timeout, 429, 5xx) doesn't trip the
    'stale' badge. Retries transient failures; fails fast on a non-transient one (e.g. a
    rejected key), which should surface. Returns the outageMessage list; raises only after
    the attempts are exhausted."""
    last = None
    for attempt in range(ESB_LIST_ATTEMPTS):
        retryable = False
        blob = None
        try:
            blob = json.loads(_esb_get(ESB_BASE, key, timeout=ESB_LIST_TIMEOUT))
        except urllib.error.HTTPError as e:          # HTTP status error from the transport
            last = e
            if e.code not in _ESB_TRANSIENT:
                raise                                # e.g. 401/404 — won't fix on retry
            retryable = True
        except Exception as e:                       # URLError / socket timeout / JSON error
            last = e
            retryable = True
        else:                                        # fetch + parse OK — inspect the body
            if isinstance(blob, dict) and blob.get("statusCode") and not blob.get("outageMessage"):
                code = blob.get("statusCode")
                last = RuntimeError(f"ESB {code}: {str(blob.get('message'))[:80]}")
                if code not in _ESB_TRANSIENT:
                    raise last                       # non-transient body error — surface it
                retryable = True
            else:
                return (blob or {}).get("outageMessage") or []
        if retryable and attempt + 1 < ESB_LIST_ATTEMPTS:
            time.sleep(ESB_RETRY_BACKOFF)
            continue
        raise last
    raise last if last else RuntimeError("ESB list failed")


def _esb_detail(iid, key, now):
    """Fetch (and cache) one outage's detail. Returns the detail dict, or None on error."""
    with _esb_lock:
        c = _esb_detail_cache.get(iid)
        if c and now - c["ts"] < ESB_DETAIL_TTL:
            return c["d"]
    try:
        d = json.loads(_esb_get(f"{ESB_BASE}/{iid}/", key))
    except Exception:
        return None
    with _esb_lock:
        _esb_detail_cache[iid] = {"d": d, "ts": now}
    return d


ESB_PLANNED_GRACE_S = 3600   # a planned ESB outage this long past its end time is treated as finished
PLANNED_GRACE_S = ESB_PLANNED_GRACE_S   # same rule for SPEN planned works
OVERDUE_S = 7 * 86400        # an incident this far past its ETR is 'overdue' (probably left open)


def _ended(iso, grace_s=PLANNED_GRACE_S):
    """True when an ISO end time (naive = local, or with Z/offset) is more than grace_s past."""
    if not iso:
        return False
    try:
        t = datetime.datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    now = datetime.datetime.now(t.tzinfo) if t.tzinfo else datetime.datetime.now()
    return (now - t).total_seconds() > grace_s


def _overdue_days(iso):
    try:
        t = datetime.datetime.fromisoformat(str(iso).strip().replace("Z", "+00:00"))
        now = datetime.datetime.now(t.tzinfo) if t.tzinfo else datetime.datetime.now()
        return int((now - t).total_seconds() // 86400)
    except (ValueError, TypeError):
        return None


_NIE_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AP]M),\s*(\d{1,2})\s+([A-Za-z]{3})[a-z]*(?:\s+(\d{4}))?\s*$", re.I)
_MON = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}


def _nie_time(v, ref=None):
    """NIE '7:26 PM, 14 Oct' / '10:30 PM, 14 Oct 2026' (NI local = UK local) ->
    'yyyy-mm-ddTHH:MM:00'. A missing year is the one that puts the time nearest `ref`
    (now), so a late-December start read in January lands in the right year."""
    m = _NIE_RE.match(str(v or ""))
    if not m:
        return None
    h, mi, ap, d, mon, y = m.groups()
    h = int(h) % 12 + (12 if ap.upper() == "PM" else 0)
    mo = _MON.get(mon.lower()[:3])
    if not mo:
        return None
    ref = ref or datetime.datetime.now()
    years = [int(y)] if y else [ref.year - 1, ref.year, ref.year + 1]
    best = None
    for yy in years:
        try:
            t = datetime.datetime(yy, mo, int(d), h, int(mi))
        except ValueError:
            continue
        if best is None or abs((t - ref).total_seconds()) < abs((best - ref).total_seconds()):
            best = t
    return best.strftime("%Y-%m-%dT%H:%M:00") if best else None


def _esb_time(v):
    """ESB 'dd/mm/yyyy HH:MM' (Irish local time, = UK local) -> 'yyyy-mm-ddTHH:MM:00',
    the operator-local ISO form the pages and alarms read. None if absent/unparseable."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s+(\d{1,2}):(\d{2})", str(v or ""))
    if not m:
        return None
    d, mo, y, h, mi = (int(x) for x in m.groups())
    return f"{y:04d}-{mo:02d}-{d:02d}T{h:02d}:{mi:02d}:00"


def adapter_esb(apikey=None):
    """ESB Networks PowerCheck (Republic of Ireland). Two-step: a light list endpoint
    (id 'i', type 't' in Planned/Fault/Restored, coord 'p.c' as "lat,lon") then one detail
    call per non-restored outage for the customer count, planner group and times. Detail
    calls are pooled and cached. Restored outages are dropped (cleared, not off)."""
    key = apikey or (load_keys().get("ESB")) or ESB_DEFAULT_KEY
    now = time.time()
    with _esb_lock:                       # evict stale detail entries (bounds memory)
        for _k in [k for k, v in _esb_detail_cache.items() if now - v["ts"] > ESB_DETAIL_TTL]:
            _esb_detail_cache.pop(_k, None)
    msgs = _esb_list(key)                    # longer timeout + one retry before flagging stale
    active = []
    for m in msgs:
        if (m.get("t") or "").lower() == "restored":
            continue
        active.append(m)
    active = active[:ESB_MAX_DETAIL]

    def _coord(m):
        c = ((m.get("p") or {}).get("c")) or ""
        try:
            la, lo = c.split(",")
            return float(la), float(lo)
        except Exception:
            return None, None

    out = []
    ids = [m.get("i") for m in active]
    details = {}
    if ids:
        with concurrent.futures.ThreadPoolExecutor(max_workers=ESB_WORKERS) as ex:
            fut = {ex.submit(_esb_detail, i, key, now): i for i in ids if i}
            for f in concurrent.futures.as_completed(fut):
                details[fut[f]] = f.result()
    for m in active:
        iid = m.get("i")
        typ = _s(m.get("t"))
        planned = True if (typ or "").lower() == "planned" else False
        lat, lon = _coord(m)
        d = details.get(iid) or {}
        if d.get("point") and (lat is None or lon is None):
            try:
                la, lo = str(d["point"].get("c")).split(",")
                lat, lon = float(la), float(lo)
            except Exception:
                pass
        reason = _s(d.get("plannedOutageReason"))
        st_iso, et_iso = _esb_time(d.get("startTime")), _esb_time(d.get("estRestoreTime"))
        # ESB leaves finished PLANNED outages listed as 'Planned' after their window (Cong,
        # 895 customers, still listed 5 h after its 17:00 end on 14 Oct): drop one whose end
        # is more than ESB_PLANNED_GRACE_S past. One with no end time is kept, flagged.
        if planned and _ended(et_iso, ESB_PLANNED_GRACE_S):
            continue
        inc = _incident(
            "ESB", _s(d.get("plannerGroup")) or _s(d.get("location")) or "ESB", _s(iid),
            lat=lat, lon=lon, postcodes=_s(d.get("location")),
            off=_num(d.get("numCustAffected")), predicted=None,
            planned=planned, category=(reason or typ), voltage=None,
            status=_s(d.get("statusMessage")) or typ,
            etr=et_iso or _s(d.get("estRestoreTime")),
            start_ts=st_iso or _s(d.get("startTime")),
            last_updated=_esb_time(d.get("restoreTime")) or st_iso, psr=None,
        )
        if planned and not et_iso:
            inc["end_unknown"] = True
        out.append(inc)
    return out


# Operator registry: dno -> (adapter, needs_key). SSEN has no proven feed (None adapter).
# ESB registers needs_key=False: it always runs on the shared default key and picks up an
# optional override from the key store itself, so it is never gated to "no-key".
ADAPTERS = {
    "NGED": (adapter_nged, False),
    "UKPN": (adapter_ukpn, False),
    "NPg":  (adapter_npg,  False),
    "ENWL": (adapter_enwl, True),
    "SPEN": (adapter_spen, True),
    "SSEN": (adapter_ssen, False),
    "NIE":  (adapter_nie,  True),
    "ESB":  (adapter_esb,  False),
}
OPERATOR_ORDER = ["NGED", "UKPN", "NPg", "ENWL", "SPEN", "SSEN", "NIE", "ESB"]


# ───────────────────────── keys (server-side, header auth) ───────────────────
def load_keys():
    try:
        return dict(json.loads(open(KEYS_FILE, encoding="utf-8").read()))
    except Exception:
        return {}


def save_keys(d):
    """Persist per-operator keys server-side. Never returned to the browser by callers."""
    try:
        with open(KEYS_FILE, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in (d or {}).items() if v}, fh)
        return True
    except Exception:
        return False



# ───────────────────────── monitored postcode areas ─────────────────────────
# Up to WATCH_MAX postcode SECTORS ("PL3 5") or full postcodes ("PL3 5AA") the user wants
# alerts for, beyond the near-you radius. Stored server-side (survives restarts, shared by
# every browser). Each entry is checked against postcodes.io when saved, which also gives
# the place name used in the spoken alert.
#
# Matching, per incident postcode token:
#   monitored SECTOR  <- incident full postcode in that sector, or the same sector  = "affected"
#   monitored FULL    <- the same full postcode                                     = "affected"
#                     <- the incident only names the SECTOR (SPEN publishes no full
#                        postcodes; UKPN's come from 'fullpostcodedata')                                            = "may" (may be affected)
#   district-only tokens (outward code alone) never match: too coarse to say anything.
# ENWL publishes no postcodes and ESB uses Eircodes, so neither can match.
WATCH_MAX = 10
_PC_FULL_RE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?)(\d[A-Z]{2})$")
_PC_SECTOR_RE = re.compile(r"^([A-Z]{1,2}\d[A-Z\d]?) (\d)$")
_PC_UNIT_LETTERS = "ABDEFGHJLNPQRSTUWXYZ"     # letters that can start an inward unit
_watch_lock = threading.Lock()


def pc_parse(s):
    """Normalise a postcode-ish string. Returns (kind, canonical) with kind 'full'
    ('PL3 5AA') or 'sector' ('PL3 5'), or (None, None) if neither. A full postcode is
    recognised with or without its space; a sector needs the space ('PL35' is a district)."""
    t = " ".join(str(s or "").upper().split())
    m = _PC_FULL_RE.match(t.replace(" ", ""))
    if m:
        return "full", m.group(1) + " " + m.group(2)
    m = _PC_SECTOR_RE.match(t)
    if m:
        return "sector", m.group(1) + " " + m.group(2)
    return None, None


def _sector_of(full):
    return full[:-2]                          # 'PL3 5AA' -> 'PL3 5'


def load_watch():
    try:
        d = json.loads(open(WATCH_FILE, encoding="utf-8").read())
        areas = [a for a in (d.get("areas") or []) if isinstance(a, dict) and a.get("pc")]
        return {"areas": areas[:WATCH_MAX]}
    except Exception:
        return {"areas": []}


def _pcio(path):
    req = urllib.request.Request("https://api.postcodes.io" + path,
                                 headers={"User-Agent": UA["User-Agent"]})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None                       # postcode not found
        raise


def _place_name(r):
    """Spoken place for a postcodes.io record: a real civil parish (e.g. 'Bude-Stratton'),
    else for Scotland / Wales / NI the ward (their councils cover large areas), else the
    district ('Plymouth')."""
    par = r.get("parish") or ""
    if par and "unparished" not in par.lower():
        return par
    if (r.get("country") or "") in ("Scotland", "Wales", "Northern Ireland") and r.get("admin_ward"):
        return r["admin_ward"]
    return r.get("admin_district") or r.get("admin_ward") or None


def pc_lookup(kind, pc):
    """Confirm a postcode / sector exists and name its place. Returns
    {'ok':True,'place':..} or {'ok':False,'error':..}; never raises."""
    try:
        if kind == "full":
            d = _pcio("/postcodes/" + urllib.parse.quote(pc))
            r = (d or {}).get("result")
            if not r:
                return {"ok": False, "error": "postcode not found"}
            return {"ok": True, "place": _place_name(r)}
        # sector: postcodes.io search ignores the space ('PL3 5' would also find PL35),
        # so search on sector + first unit letter, which only a real sector can start with
        for L in _PC_UNIT_LETTERS:
            d = _pcio("/postcodes?" + urllib.parse.urlencode({"q": pc + L, "limit": 10}))
            for r in (d or {}).get("result") or []:
                if _sector_of(str(r.get("postcode") or "")) == pc:
                    return {"ok": True, "place": _place_name(r)}
        return {"ok": False, "error": "no postcodes found in this sector"}
    except Exception as e:
        return {"ok": False, "error": "lookup failed (" + type(e).__name__ + ")", "retry": True}


def save_watch(entries):
    """Validate + store the user's list. `entries` is a list of strings (blanks ignored,
    duplicates dropped, max WATCH_MAX). Entries already stored keep their looked-up place
    without a new lookup. Returns {'ok', 'areas', 'rejected':[{input,error}]}. An entry
    whose lookup FAILED for network reasons is kept (marked unverified) rather than lost."""
    old = {a["pc"]: a for a in load_watch()["areas"]}
    areas, rejected, seen = [], [], set()
    for raw in (entries or []):
        s = str(raw or "").strip()
        if not s:
            continue
        kind, pc = pc_parse(s)
        if not kind:
            rejected.append({"input": s, "error": "not a postcode sector (e.g. EX23 4) or full postcode (e.g. EX23 4AA)"})
            continue
        if pc in seen:
            continue
        if len(areas) >= WATCH_MAX:
            rejected.append({"input": s, "error": f"limit of {WATCH_MAX} areas"})
            continue
        seen.add(pc)
        o = old.get(pc)
        if o and o.get("place") and not o.get("unverified"):
            areas.append(o); continue
        lk = pc_lookup(kind, pc)
        if lk.get("ok"):
            areas.append({"pc": pc, "kind": kind, "place": lk.get("place")})
        elif lk.get("retry"):
            areas.append({"pc": pc, "kind": kind, "place": (o or {}).get("place"), "unverified": True})
        else:
            rejected.append({"input": s, "error": lk.get("error")})
    with _watch_lock:
        try:
            tmp = WATCH_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"areas": areas}, fh, indent=1)
            os.replace(tmp, WATCH_FILE)
        except Exception as e:
            return {"ok": False, "error": f"could not save ({type(e).__name__})",
                    "areas": areas, "rejected": rejected}
    return {"ok": True, "areas": areas, "rejected": rejected}


def watch_matches(incidents, areas=None):
    """Incidents touching a monitored area. One row per (area, incident):
    {pc, place, kind, dno, id, planned, off, predicted, sectors, status, etr, start,
     level:'affected'|'may', hit:[matching incident postcodes]}."""
    if areas is None:
        areas = load_watch()["areas"]
    if not areas:
        return []
    out = []
    for inc in incidents or []:
        toks = []
        for t in re.split(r"[;,]", str(inc.get("postcodes") or "")) + list(inc.get("full_postcodes") or []):
            k, pc = pc_parse(t)
            if k:
                toks.append((k, pc))
        if not toks:
            continue
        for a in areas:
            apc, akind = a["pc"], a.get("kind") or pc_parse(a["pc"])[0]
            level, hit = None, []
            for k, pc in toks:
                if akind == "sector":
                    ok = (pc == apc) if k == "sector" else (_sector_of(pc) == apc)
                    if ok:
                        level = "affected"; hit.append(pc)
                else:                                   # monitored full postcode
                    if k == "full" and pc == apc:
                        level = "affected"; hit.append(pc)
                    elif k == "sector" and pc == _sector_of(apc):
                        level = level or "may"; hit.append(pc)
            if level:
                out.append({"pc": apc, "place": a.get("place"), "kind": akind,
                            "dno": inc.get("dno"), "id": inc.get("id"),
                            "planned": inc.get("planned"), "off": inc.get("customers_off"),
                            "predicted": inc.get("customers_predicted"),
                            "sectors": inc.get("sectors"), "status": inc.get("status"),
                            "etr": inc.get("etr"), "start": inc.get("start_ts"),
                            "level": level, "hit": hit[:5]})
    return out

# ───────────────────────── collect + aggregate ──────────────────────────────
_cache = {}         # dno -> {"incidents":[...], "ts":..., "error":None}
_cache_lock = threading.Lock()


def _dedup(incs):
    """Collapse rows that share an incident id. Some feeds (e.g. Northern Powergrid) emit
    one row per customer-restoration-STAGE — the same incident repeated as it is worked —
    so a raw row count over-states incidents and, summed, DOUBLE-COUNTS customers off. Keep
    one row per (dno,id), the one with the largest customers_off as representative. Feeds
    that are already one-row-per-incident (NGED, UKPN) are unaffected. Rows with no id are
    kept as-is (can't dedup safely)."""
    best, noid, pcs = {}, [], {}
    for i in incs:
        if not i.get("id"):
            noid.append(i); continue
        k = (i["dno"], i["id"])
        # every row's postcodes are kept (NPg puts one postcode per row), so postcode
        # matching for monitored areas sees the whole incident, not just one row of it
        for t in re.split(r"[;,]", str(i.get("postcodes") or "")):
            t = " ".join(t.split())
            if t and t not in pcs.setdefault(k, []):
                pcs[k].append(t)
        c = best.get(k)
        if c is None or (i.get("customers_off") or 0) > (c.get("customers_off") or 0):
            best[k] = i
    for k, i in best.items():
        if len(pcs.get(k) or []) > 1:
            i = best[k] = dict(i)
            i["postcodes"] = ", ".join(pcs[k])
    return list(best.values()) + noid


def _fetch_operator(dno, keys, now):
    """Return (status, incidents, error). status in
    ok | stale | no-key | no-feed | error. Cached for CACHE_TTL. On failure, keep serving
    the last good reading — including an empty 'no outages' one, which is a valid state, not
    absent data — for up to STALE_GRACE (2h) as 'stale'; only past that does it become 'error'
    (down) and drop out of the totals. ts always marks the last SUCCESSFUL fetch, so the grace
    window measures true feed silence."""
    adapter, needs_key = ADAPTERS[dno]
    if adapter is None:
        return "no-feed", [], None
    key = (keys or {}).get(dno)
    if needs_key and not key:
        return "no-key", [], None
    with _cache_lock:
        c = _cache.get(dno)
        if c and not c.get("error") and now - c["ts"] < CACHE_TTL:
            return "ok", c["incidents"], None
    try:
        incs = adapter(key) if needs_key else adapter()
        with _cache_lock:
            _cache[dno] = {"incidents": incs, "ts": now, "error": None}
        return "ok", incs, None
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:120]}"
        with _cache_lock:
            c = _cache.get(dno)
            if c and c.get("ts") and now - c["ts"] <= STALE_GRACE:
                c["error"] = err          # keep last good (even an empty reading), flag stale
                return "stale", c.get("incidents") or [], err
        return "error", [], err


_frozen = {}        # dno -> {"fp": fingerprint, "since": ts, "last": ts, "day_s": seconds}
_frozen_lock = threading.Lock()


def _daytime_seconds(a, b):
    """Seconds of [a, b) that fall inside the FROZEN_DAY_START–END UTC window."""
    total = 0.0
    t = a
    while t < b:
        d = datetime.datetime.fromtimestamp(t, tz=datetime.timezone.utc)
        nxt = min(b, (d.replace(minute=0, second=0, microsecond=0)
                      + datetime.timedelta(hours=1)).timestamp())
        if FROZEN_DAY_START <= d.hour < FROZEN_DAY_END:
            total += nxt - t
        t = nxt
    return total


def _fingerprint(incs):
    return tuple(sorted((str(i.get("id")), i.get("customers_off"), i.get("status") or "")
                        for i in incs))


def _frozen_check(dno, incs, now):
    """Track how long an operator's content has been unchanged. Returns the time it last
    changed if it now counts as frozen, else None."""
    fp = _fingerprint(incs)
    with _frozen_lock:
        f = _frozen.get(dno)
        if f is None or f["fp"] != fp:
            _frozen[dno] = {"fp": fp, "since": now, "last": now, "day_s": 0.0}
            return None
        step = now - f["last"]
        if 0 < step <= FROZEN_MAX_STEP:
            f["day_s"] += _daytime_seconds(f["last"], now)
        if step > 0:
            f["last"] = now
        return f["since"] if f["day_s"] >= FROZEN_DAY_S else None


def collect(keys=None):
    """Fetch every operator, normalise, aggregate. Returns a snapshot dict. National totals
    sum ONLY operators currently reporting (ok/stale); the rest are named, not zeroed."""
    keys = keys if keys is not None else load_keys()
    now = time.time()
    operators, all_incidents, all_overdue = [], [], []
    nat_off = nat_pred = nat_inc = nat_psr = 0
    reporting = 0
    for dno in OPERATOR_ORDER:
        status, incs, err = _fetch_operator(dno, keys, now)
        incs = _dedup(incs)          # collapse multi-stage rows -> one per incident
        # Overdue: estimated restoration more than OVERDUE_S past (a SPEN fault due back on
        # 24 Sep still listed on 14 Oct). Probably closed by the operator but never removed —
        # not confirmed either way, so it is neither counted as off NOR treated as restored:
        # kept out of 'incidents' and every total, listed under 'overdue', and its id stays
        # in the new/cleared diffing (log_sample) so going overdue never reads as a clearance.
        overdue = []
        if status in ("ok", "stale"):
            live = []
            for i in incs:
                if _ended(i.get("etr"), OVERDUE_S):
                    i = dict(i, overdue=True, overdue_days=_overdue_days(i.get("etr")))
                    overdue.append(i)
                else:
                    live.append(i)
            incs = live
        frozen_since = _frozen_check(dno, incs, now) if status == "ok" else None
        if frozen_since is not None:
            status = "stale"
            err = (f"feed content unchanged since {_iso(frozen_since)[11:16]} UTC "
                   f"({_iso(frozen_since)[:10]}) — may be frozen")
        off = sum((i["customers_off"] or 0) for i in incs)
        pred = sum((i["customers_predicted"] or 0) for i in incs)
        psr = sum((i["psr_customers"] or 0) for i in incs)
        op = {
            "dno": dno, "status": status, "error": err,
            "frozen_since": frozen_since,
            "incidents": len(incs), "customers_off": off,
            "customers_predicted": pred, "psr_customers": psr,
            "customers_tracked": CUSTOMERS_TRACKED.get(dno),
            "has_psr": any(i["psr_customers"] is not None for i in incs),
            "overdue": len(overdue),
        }
        operators.append(op)
        if status in ("ok", "stale"):
            reporting += 1
            nat_off += off; nat_pred += pred; nat_inc += len(incs); nat_psr += psr
            all_incidents.extend(incs)
            all_overdue.extend(overdue)
    national = {
        "customers_off": nat_off, "customers_predicted": nat_pred,
        "incidents": nat_inc, "psr_customers": nat_psr,
        "operators_reporting": reporting, "operators_total": len(OPERATOR_ORDER),
    }
    return {
        "generated": now,
        "generated_iso": _iso(now),
        "national": national,
        "operators": operators,
        "incidents": all_incidents,
        "overdue": all_overdue,          # past ETR by > OVERDUE_S: shown greyed, never counted
        "attribution": "Live power-cut data from the UK & Ireland distribution network operators: "
                       "NGED, UKPN, Northern Powergrid, ENWL, SPEN, SSEN, NIE Networks "
                       "(Northern Ireland) and ESB Networks PowerCheck (Republic of Ireland).",
    }


# ───────────────────────── history (monthly JSONL + new/cleared) ─────────────
def _iso(ts):
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hist_path(ts):
    m = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).strftime("%Y-%m")
    return HIST_PREFIX + m + ".jsonl"


def _load_last_ids():
    try:
        return json.loads(open(LAST_IDS_FILE, encoding="utf-8").read())
    except Exception:
        return {}


def _save_last_ids(d):
    try:
        with open(LAST_IDS_FILE, "w", encoding="utf-8") as fh:
            json.dump(d, fh)
    except Exception:
        pass


def _volt_counts(incidents):
    """Count ACTIVE incidents by voltage class (HV / LV / EHV). Feeds that don't report
    a voltage (e.g. UKPN) fall in 'unknown' — never guessed. Planned interruptions carry
    no voltage and so land in 'unknown' too."""
    c = {"HV": 0, "LV": 0, "EHV": 0, "unknown": 0}
    for i in incidents or []:
        v = (i.get("voltage") or "").upper()
        if v in ("HV", "LV", "EHV"):
            c[v] += 1
        else:
            c["unknown"] += 1
    return c


def _worst_incident(incidents):
    """The single active incident with the most customers off (across all reporting
    operators), as a compact record. None if there are no incidents."""
    best = None
    for i in incidents or []:
        off = i.get("customers_off") or 0
        if best is None or off > (best.get("customers_off") or 0):
            best = i
    if best is None:
        return None
    return {"off": best.get("customers_off") or 0, "dno": best.get("dno"),
            "id": best.get("id"), "voltage": best.get("voltage"),
            "category": best.get("category"), "region": best.get("region")}


def log_sample(snapshot):
    """Append one history row for this snapshot, computing per-operator NEW and CLEARED
    incident counts by diffing incident-id sets against the previous sample (persisted, so
    it survives restarts). Rates (cuts/hr, restorations/hr) are derived from these at plot
    time. Never raises."""
    try:
        now = snapshot["generated"]
        prev = _load_last_ids()
        prev_ids = prev.get("ids", {})           # dno -> [ids]
        cur_ids, per_op = {}, {}
        for op in snapshot["operators"]:
            dno = op["dno"]
            ids = [i["id"] for i in (snapshot["incidents"] + (snapshot.get("overdue") or []))
                   if i["dno"] == dno and i["id"]]
            cur_ids[dno] = ids
            ps = set(prev_ids.get(dno, []))
            cs = set(ids)
            # only count new/cleared when this operator is actually reporting now
            new = len(cs - ps) if op["status"] in ("ok", "stale") else 0
            cleared = len(ps - cs) if op["status"] in ("ok", "stale") else 0
            per_op[dno] = {
                "status": op["status"],
                **({"frozen": True} if op.get("frozen_since") else {}),
                "active": op["incidents"], "off": op["customers_off"],
                "new": new, "cleared": cleared,
            }
        # UNPLANNED-only national figures, so the dashboard's storm/power-cuts alarm can
        # track non-planned cuts and restorations. un_new: new unplanned incidents this
        # sample (ids not seen last sample); un_off: customers off in unplanned incidents.
        _prev_all = set()
        for _ids in prev_ids.values():
            _prev_all.update(_ids)
        _un_ids = set(i["id"] for i in snapshot["incidents"] if i.get("id") and not i.get("planned"))
        un_new = len(_un_ids - _prev_all)
        un_off = sum((i["customers_off"] or 0) for i in snapshot["incidents"] if not i.get("planned"))
        row = {
            "ts": now, "iso": snapshot["generated_iso"],
            "national": {
                "off": snapshot["national"]["customers_off"],
                "active": snapshot["national"]["incidents"],
                "reporting": snapshot["national"]["operators_reporting"],
                "new": sum(o["new"] for o in per_op.values()),
                "cleared": sum(o["cleared"] for o in per_op.values()),
                "un_new": un_new,          # new UNPLANNED incidents this sample
                "un_off": un_off,          # customers off in UNPLANNED incidents
            },
            "operators": per_op,
            # Per-sample extras for the stats panel: the single worst incident and the
            # active-incident voltage breakdown at this instant. Older rows won't have
            # these (added with this build), so readers must tolerate their absence.
            "worst": _worst_incident(snapshot.get("incidents")),
            "volt": _volt_counts(snapshot.get("incidents")),
        }
        with open(_hist_path(now), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        _save_last_ids({"ts": now, "ids": cur_ids})
        return row
    except Exception:
        return None


def history_tail(hours=24, now=None):
    """Return history rows within the last `hours`, reading the current month's file plus
    the previous month's when the window crosses the boundary. Kept indefinitely."""
    now = now or time.time()
    cutoff = now - hours * 3600
    files = {_hist_path(now), _hist_path(cutoff)}    # current + (maybe) previous month
    rows = []
    for p in files:
        try:
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if r.get("ts", 0) >= cutoff:
                        rows.append(r)
        except FileNotFoundError:
            pass
        except Exception:
            pass
    rows.sort(key=lambda r: r.get("ts", 0))
    return rows


def compute_stats(snapshot, now=None):
    """Small stats block for the dashboard's statistics panel:

      * max_off_24h / max_off_week  — peak NATIONAL customers-off over the last 24h / 7d,
        each with the timestamp it occurred (from the persisted history's national.off).
      * worst_inc_24h / worst_inc_week — the worst SINGLE incident (most customers off)
        over the same windows, with its dno/voltage and time.
      * volt_now — current active-incident counts by voltage (HV/LV/EHV/unknown).
      * history_span_h — how much history actually exists (so the panel can say the 7-day
        figures are still filling in).

    Peaks read straight from history. Worst-single-incident and voltage windows rely on
    the per-sample 'worst'/'volt' fields (added with this build), so before a full window
    of those has accrued the figures fall back to the live snapshot — which is always
    folded in, so 'now' is reflected immediately. Never raises."""
    try:
        now = now or snapshot.get("generated") or time.time()
        incs = snapshot.get("incidents") or []
        rows_wk = history_tail(24 * 7, now=now)
        rows_24 = [r for r in rows_wk if r.get("ts", 0) >= now - 24 * 3600]

        def peak_off(rows):
            best = None
            for r in rows:
                off = (r.get("national") or {}).get("off")
                if off is None:
                    continue
                if best is None or off > best["off"]:
                    best = {"off": off, "ts": r.get("ts")}
            return best

        cur_off = (snapshot.get("national") or {}).get("customers_off")

        def fold_off(stat):
            if cur_off is None:
                return stat
            if stat is None or cur_off > stat["off"]:
                return {"off": cur_off, "ts": now}
            return stat

        def peak_worst(rows):
            best = None
            for r in rows:
                w = r.get("worst")
                if not w or w.get("off") is None:
                    continue
                if best is None or w["off"] > best["off"]:
                    best = dict(w); best["ts"] = r.get("ts")
            return best

        worst_now = _worst_incident(incs)

        def fold_worst(stat):
            if not worst_now:
                return stat
            if stat is None or (worst_now["off"] or 0) > (stat.get("off") or 0):
                s = dict(worst_now); s["ts"] = now; return s
            return stat

        return {
            "now": now,
            "max_off_24h":   fold_off(peak_off(rows_24)),
            "max_off_week":  fold_off(peak_off(rows_wk)),
            "worst_inc_24h":  fold_worst(peak_worst(rows_24)),
            "worst_inc_week": fold_worst(peak_worst(rows_wk)),
            "volt_now": _volt_counts(incs),
            "history_span_h": round((now - rows_wk[0]["ts"]) / 3600.0, 1) if rows_wk else 0,
        }
    except Exception:
        return None


def snapshot(keys=None, log=True, history_hours=24):
    """One call for the /api/powercuts endpoint: collect live, log a history sample, and
    attach the recent history tail."""
    snap = collect(keys)
    if log:
        log_sample(snap)
    snap["history"] = history_tail(history_hours, now=snap["generated"])
    return snap
