# Known Issues

Issues currently under review in GB Energy Monitor. These are open, unresolved,
or under investigation — resolved items move to `CHANGELOG.md`.

All items here relate to the project's governing principle: **honesty over
plausibility**. Estimated, derived, stale, or basis-mismatched data must be
labelled as such; a display that is individually accurate but invites a
misleading reading is treated as a bug.

Last reviewed: 260930.

---

## Open

- Weather and EA rain gauge API feeds are flakey. EA endpoints regularly error 503;
  OM and OWM APIs appear to time out. Using a fixed IP and avoiding a VPN doesn't
  seem to help. Investigations ongoing (see the candidate issues below).

---

## Rain / forecast engine — how the sources are merged (OPEN; rebuild planned)

Reviewed 260930 from the code (`rain_probe.py` 260929.4) and the September logs
(`rain_threats-2026-09.jsonl`, `api_calls.jsonl`, `om_debug.jsonl`). The Forecast view,
gauge radar and "Rainfall nearby" alerts all come from this engine. Live behaviour is
unchanged until the rebuild below, apart from three stopgaps in `rain_probe.py` 260930.1 /
`grid_server.py` 260930.2 (noted under R1, R3 and R6).

**Summary.** Five inputs were added one at a time — EA gauges, Open-Meteo (public, then
self-hosted), OpenWeather OC4 and RainViewer radar — each with its own timing, freshness
window and fallback, and each read directly by the parts that use it. They are merged as
if every reading described "now", which it doesn't.

### R1. No common valid time
Readings of very different ages go into one field with equal weight. On 29–30 Sep the
850 hPa steering flow was 56–108 km/h: EA gauges (typically ~60 min late) were then ~90 km
behind the rain, radar (10–20 min late) 15–30 km. Clusters, tracks, the approach cone and
ETAs therefore mix positions from different times. That night every approach candidate was
resolved "arrived" in the cycle it opened (edges at 10–20 km, ETAs 5–15 min) and no
approach alert fired; model rain at home was flagged at 17:50, EA gauges confirmed at 18:48.
*Stopgap (260930.1):* the approach arrival window is shifted earlier by the age of the
leading reading, and says "may arrive any time now" when that age uses it up. Positions are
still not moved to "now", so clusters, tracks and the cone are unchanged.

### R2. Readings dated by when they were fetched, not when they were valid
- Home ring: cards are rebuilt with `model_ts = now` every cycle, including cached ones, so a
  30-min-old reading looks fresh and is added to the history again every ~5 min.
- Land probes, mobile trackers and OC4 fallback points are dated by sample time, not radar
  frame time (the sea net already uses frame time).
- The ring samples every 30 min even on radar (free); the sea net beside it every 10 min.

### R3. Cadence set by time of day, not by the source
`forecast_window.json` `quiet_mult: 4.0` (outside 10:00–18:00) stretches the radar net from
10 to 40 min. 29 Sep: no radar read 18:06–18:48 during onset, while OC4 trackers polled 4
paid points every ~5 min. 6,173 OC4 sea calls in September.
*Stopgap (260930.1/.2):* the radar net ignores the quiet factor while radar serves, and mobile
trackers read radar first (OC4 only when radar is unavailable). The home ring and land probes
are still stretched by the quiet factor.

### R4. Inconsistent freshness windows
Recency 40 min, approach gauge age 45, probe freshness 30, late 75 (EA-back 60), history 90.
Normal EA lag reaches ~60 min, so a gauge counts as current for the area picture but drops
out of the approach check for part of every EA batch cycle.

### R5. Spatial coverage
- When EA is down the backup is 8 ring points within 20 km (some near or over the sea),
  while the sea net keeps a point every 20° at 40 km plus pickets and hidden fills. The field
  is sea-weighted: coverage, the rain centroid and the organised/isolated test lean seaward,
  and inland 20–40 km is empty.
- Showery/steady character comes only from physical gauges, so it disappears when EA is down.
- Range rings are 10 km apart — less than one radar frame of motion at 60–100 km/h.
- The "wide band" width reads ~60 km almost every time: the width of the 40 km sentinels
  across the ±60° cone, i.e. the point layout, not the rain.

### R6. Misleading source labels
The **RADAR** chip on a mobile tracker means an OpenWeather OC4 reading (OpenWeather's own
blended estimate), not radar. RADAR on sentinel/picket/ring cards is genuine (RainViewer).
Proposed: chips by source — PROBE RV / PROBE OM / PROBE OC4, TRACK RV / TRACK OC4 / TRACK OM.
*Since 260930.2* trackers normally read radar, so their RADAR chip is usually accurate; it
still shows RADAR for an OC4 read when radar is down. The Forecast view tooltip names the source.

### R7. Same threshold, different quantities
Radar is instantaneous reflectivity converted by Marshall–Palmer (a stratiform relation that
under-reads convective rain; no snow flag); EA gauges are 15-min totals (0.01 mm steps);
Open-Meteo is a 15-min model mean for a grid cell. All share the 0.3 mm/h "wet" threshold.
Radar tiles are sampled at ~0.4 km/px but the underlying cells are ~2.5–3 km blocks.

### R8. Public Open-Meteo largely unavailable 10–27 Sep
`om_debug.jsonl` shows ~45 HTTP errors/day against ~10 successes, so the model net was mostly
blind or on the OC4 fallback until the self-hosted instance took over on 27 Sep (all local
calls OK since).

### R9. Stale design note
`WEATHER_ALERT_DESIGN.md` still says "Land probes are model, not radar"; since 260929.1 they
read radar first.

### Planned fix — rebuild the data layer, keep the voice logic
- **One reading format.** An adapter per source (public and local Open-Meteo = one source,
  two transports) emitting: source, kind (accumulation / instantaneous / forecast), position,
  valid start–end, fetched time, mm/h, snow, quality.
- **One fused field.** A fixed polar grid covering land and sea evenly, rebuilt on each radar
  frame. Radar is the backbone, moved to "now" by the steering flow; EA gauges are ground
  truth for confirmation and radar calibration, allowing for their lag; the model fills where
  radar is missing; OC4 only when nothing else is available. Each cell carries value,
  confidence and age.
- **Consumers read only the field** (situation, tracks, approach, cards, radar plot), so
  freshness and source choice are decided once.
- **Cadence belongs to the source.** Radar every frame, always; quiet hours affect speech only.
- **Approach from motion across frames**, projected by data age plus lead time; when the lag
  exceeds the useful warning time, say "arriving now" rather than a 5–10 min ETA.
- **Showery/steady from the radar time series**, so it survives an EA outage.
- The approach episode, cadence, fizzle/suppression logic and Forecast view are kept.

**Evidence first.** A standalone, read-only recorder (`rain_recorder.py` 260930.1, dev folder,
not part of the app) logs every source at the same 409 points (home, a 5–50 km polar grid,
every EA gauge) with valid and first-seen times, plus raw radar tiles and engine snapshots.
Weights and thresholds for the rebuild are to be set from a week or more of its data. First
readings (30 Sep): RainViewer frames seen ~2.5 min after frame time; EA readings 39–99 min
late (median 54).

---

## Candidate issues — EA feed robustness (UNCONFIRMED; confirm by analysis before any change)

Reported 260913 after an EA flood-monitoring API stall. These are **hypotheses to
investigate, not accepted diagnoses** — each must be confirmed against the code and a
reproduced stall before any change is made. Where evidence already on hand bears on a
claim it is noted; honesty over plausibility applies to this list too.

### C1. EA "latest readings" payload size — `/data/readings?latest&_limit=15000`
- **Hypothesis:** the latest-index fetch (`_ea_latest_index()`, grid_server.py ~L4638,
  `timeout=60`) asks EA to assemble ~15,000 records into one JSON blob; on a degraded EA
  gateway this stalls with the TCP connection held open until the client times out.
- **Proposed fix:** switch to `/id/measures?_limit=1000` (or parameter-filtered queries)
  and read each value from the nested `latestReading` object instead of top-level `items`.
- **Status:** UNCONFIRMED. The `_limit=15000` request is real and worth trimming for
  routine robustness/bandwidth. BUT on 260911–13 even a `_limit=1` EA query timed out
  (tested from two independent IPs including a Linux/OpenSSL host), so payload size did
  **not** cause that outage — it was an EA-side total stall. Treat payload trimming as a
  chronic-slowness improvement, not the cure for a full outage. Before switching, confirm
  the `measures[]`/`latestReading` schema still yields every gauge the dashboard needs.

### C2. Windows Schannel OCSP revocation freeze
- **Hypothesis:** urllib on Windows uses native Schannel, which does a background OCSP
  revocation check on Azure-hosted `.gov.uk` certs; if that hangs, `urlopen` freezes for
  30–60 s, ignoring the socket timeout.
- **Proposed fix:** force OpenSSL, or shell out to `curl --ssl-no-revoke`.
- **Status:** UNCONFIRMED — and likely a misdiagnosis. Standard CPython's `ssl`/`urllib`
  use bundled **OpenSSL, not Windows Schannel**, so the OCSP-via-Schannel mechanism does
  not apply to a normal Python build; and the same stall reproduced on a Linux/OpenSSL
  host today, which points to an EA-server-side hang rather than a local TLS/OCSP issue.
  Confirm the actual backend (`ssl.OPENSSL_VERSION`) before spending effort here. The real
  robustness lever is a hard, socket-honoured per-request timeout (see C4), not the TLS layer.

### C3. No gzip transport negotiation
- **Hypothesis:** `fetch_json` does not send `Accept-Encoding: gzip, deflate`, so EA
  returns uncompressed payloads — slower transfers and more drops on a poor link.
- **Proposed fix:** send `Accept-Encoding: gzip, deflate` and `gzip.decompress()` the body
  when the response is gzip-encoded.
- **Status:** UNCONFIRMED but plausible and low-risk; a genuine efficiency win for the
  large latest-index payload (pairs well with C1). Confirm `fetch_json`'s current headers
  and add transparent gzip handling. Not a cause of a full stall.

### C4. EA fetch blocking / fail-fast caching
- **Hypothesis:** a synchronous EA fetch in the request path freezes the whole app when EA
  hangs, blocking other routines and endpoints.
- **Proposed fix:** fail-fast worker/background pattern; on `URLError`/`TimeoutError`
  return the cached index (`_ea_latest_cache["idx"]`) with `stale=True` instead of blocking.
- **Status:** PARTLY MITIGATED — confirm scope. The server is already `ThreadingHTTPServer`
  (L63 / L7102), so a hung EA fetch blocks only that request's own thread, not the whole
  app or the `/api/grid` path — the "freezes everything" impact is overstated.
  `_ea_latest_cache` already exists with `idx`/`stale` fields (L4537). Real work to confirm:
  (a) does `_ea_latest_index()` on timeout actually return the cached `idx` with
  `stale=True`, or propagate the exception and blank the panel? (b) is the effective wait
  bounded — the 60 s `timeout` is long; a shorter fail-fast timeout + serve-stale is the
  main win; (c) optionally move the index refresh to a background sampler like the
  powercuts one.

---

## Notes

- Resolved issues are not kept here; see `CHANGELOG.md`.
