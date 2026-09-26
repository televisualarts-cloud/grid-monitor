# Weather alert design — unified spatial rain/snow engine

Reference for the **weather** alerting engine (rain and snow). Distinct from the
energy/grid alarms (frequency, margin, gas). Covers what the engine does, how it is
built, its output shape, its tunables, and where it still needs work.

**Status: built (phases a–d), pending live tuning.** Everything below is implemented in
`rain_probe.py` (engine), `grid_server.py` (surfaces it, threads the cadence mode, runs
the probe on the fast poll) and `grid_dashboard.html` (voices it, Normal/Low toggle). It
has been exercised only against offline simulations — the thresholds are first-pass and
want watching against real weather before they are trusted. Nothing earns a tone until it
has been seen to be right (honesty over plausibility).

---

## 1. Principles

1. **One engine, one picture.** Physical (EA) and virtual (model sentinel/picket) gauges
   feed one spatial read of what the weather is *doing* around home. Cadence is driven by
   the **situation state**, never by an individual gauge tipping on/off. This retires the
   old per-gauge `evaluateRainfall` escalation cadence (the source of the nagging).
2. **Low cadence by default.** Silence unless something is near, approaching, or changing.
   Distant rain is *detection*, not *announcement*.
3. **Certainty before speech.** A state must persist (dwell) before it is announced; a
   single spiky reading never speaks.
4. **Proximity sets priority.** Anything within 5 km is always announced; further out is
   progressively lower priority — except a system *approaching on a bearing toward home*,
   which gets attention at any distance. The approach bearing is read from the **gauges
   themselves** (which gauges light up, in what order), not assumed from the wind.
5. **Character sets behaviour.** Isolated showers, widespread showers, continuous rain and
   an approaching system are distinct states with their own wording and cadence, each with
   a snow variant.

---

## 2. How it is built (as implemented)

Everything is server-side in `rain_probe.py`, run each cycle from `run_probe()`. The
`ProbeState` carries the persistent memory: `gauge_hist`, `sit_state/sit_state_since`,
`sit_group/sit_group_since`, `sit_announced`, `sit_last_snow`, `sit_dir_sect`,
`sit_tracks`, `sit_track_seq`.

### 2.1 Per-gauge history
`gauge_hist` is a dict of `id → [[ts, mm_h], …]`, trimmed to `SIT_HIST_WINDOW_S` (90 min),
deduped by timestamp. Physical gauges are keyed `g:<ref>`; virtual net points `n:<brg>:<range>`.
(This is the engine's own history — separate from `_ea_rain_peak`, the 2 h card-border
peak-hold.)

### 2.2 Per-gauge character — `gauge_character()`
Over the window: `wet_fraction`, `n_transitions` (dry↔wet flips — the stop/start
signature), `spikiness` (peak÷mean), `cur_band`/`peak_band` (0–4: dry/light/moderate/
heavy/very-heavy at 0.3 / 2 / 5 / 10 mm/h), `wet` (raining now), `recent_wet` (wet within
`SIT_RECENCY_S`, 40 min), and a `label`: **dry / steady / showery / wet**.

`recent_wet` (40 min) is what decides "still active", decoupled from the 90 min character
window — so an intermittent shower persists through its dry gaps but the situation clears
~40 min after the last wet reading rather than lingering the whole 90 min.

### 2.3 Aggregation & direction — `build_situational()`
Over the active gauges (wet now, or `recent_wet`) within 40 km:
- **coverage** = active *physical* gauges ÷ physical gauges in the field (virtual sea
  sentinels are excluded so they don't dilute a land coverage figure).
- **shower_confidence** = proximity-weighted count of `showery` gauges (closer = more
  weight) — two nearby gauges independently stop/starting is strong evidence; one lone
  spiky gauge is weak.
- **intensity centroid** = the intensity-weighted circular-mean **bearing** and mean
  **distance** of the active cluster. This is the stable direction: it does not flip when
  the single heaviest gauge jumps across a sector boundary.
- **peak_band / snow / nearest_wet_km / here** (nearest wet < 5 km).

### 2.4 States
`base_state ∈ { clear, isolated_showers, widespread_showers, continuous, approaching, wet }`
plus a `_snow` suffix when the active cluster is snow-dominant.

| State | Chosen when |
|---|---|
| **clear** | no active gauges and no approaching track |
| **continuous** | coverage ≥ 0.5 and steady-dominated (steadiness, not breadth) |
| **widespread_showers** | ≥2 showery gauges, coverage ≥ 0.35, ≥4 active (needs breadth) |
| **isolated_showers** | any showery gauge, not widespread |
| **wet** | active but not yet characterised (onset / short history) — **silent** |
| **approaching** | promoted in `run_probe` when a tracked cluster is closing, its centre is still beyond NEAR (20 km), and nothing is at home (see 2.6) |

### 2.5 Cluster continuity — `update_tracks()` (the cross-sector fix)
Active gauges are single-linkage clustered (`SIT_CLUSTER_LINK_KM`, 18 km) and each cluster
is matched to a persistent **track** by nearest centre (`SIT_TRACK_MATCH_KM`, 22 km).
Matched tracks update; unmatched clusters spawn a new track (with an incrementing id);
unmatched tracks survive `SIT_TRACK_MAX_MISS` (2) cycles then drop. Each track keeps a
centroid history; `_track_motion()` measures the closing speed over `SIT_TRACK_WINDOW_S`
(45 min) and, through the shared `speed_trust` gate + `eta_from_speed`, marks it
`approaching` with an ETA.

The effect: a system drifting NW→N→NNE as it closes stays **one track with one id and a
smoothly moving bearing** — it is never re-detected as a new system when it crosses a
sector boundary, which was the misinterpretation risk this fixes.

### 2.6 Approach promotion
If a track is `approaching`, its centre is still beyond `SIT_NEAR_KM` (20 km), and nothing
is wet within 5 km, `run_probe` promotes the situation to **approaching**, taking the
track's (hysteresis-stabilised) bearing and ETA as the locus. Once the track's centre
crosses inside NEAR, the area/here states take over — giving the narrative *approach →
arrival* from one continuous system.

### 2.7 Direction hysteresis — `_sector_hyst()`
The spoken direction is the centroid bearing binned to an 8th, but with a deadband: the
sector only changes once the bearing is clearly (half a sector + ~12°) past the previous
sector's centre. `sit_dir_sect` remembers it across cycles. No boundary flip-flop.

### 2.8 Cadence & wording — `schedule_situational()`
Produces `situational_speak = [{tier, key, phrase}]`:
- **Dwell:** an area/approaching state must persist `SIT_DWELL_S` (9 min, tracked per
  *group* so isolated↔widespread doesn't reset it) before its first announcement.
- **Here (< 5 km):** announced immediately; heartbeat `CAD_HERE_HEAVY` (10 min) for heavy+
  else `CAD_HERE_LIGHT` (20 min).
- **Showers:** heartbeat `CAD_SHOWERS` (40 min).
- **Continuous:** heartbeat `CAD_CONTINUOUS` (60 min).
- **Approaching:** no longer spoken here — owned by the approach episode (§2.10).
- **Clearing:** a one-shot on active→clear.
- **Re-announce logic:** keyed on a stable **signature** (state, peak band, snow, direction
  sector) — *not* the phrase string — and an intensity **downgrade never re-announces** (so
  the peak decaying as history ages doesn't announce "easing" then clear). Intensification
  and a genuine direction move do re-announce.
- **Normal / Low:** a `cadence_mult` (1.0 / 2.0) stretches every heartbeat. Arrivals within
  5 km and one-shots are unaffected.

Wording examples the engine emits:
- "Very heavy rain is approaching from the north-west, in 55 to 80 minutes."
- "Isolated very heavy showers to the north-west, which may be with you soon."
- "Continuous rain has set in." · "Snow is falling at your location." · "The rain is clearing."

### 2.9 Land model-probes — frugal airmass confirmation
Scattered land showers often sit *between* the EA gauges, so gauge-only detection can read
"clear" while showers continue (the modelled net is sea-only). While a **showery episode**
is live (`SIT_PROBE_EPISODE_S`, 90 min after the last showery read), `run_land_probes()`
deploys a **few** (`SIT_PROBE_MAX`, 3) free Open-Meteo points around the last cluster
centroid — one on it, one **downwind** (where the next cell would be), one **upwind** — and
reads the model there. It is deliberately sparing: a point set is sampled at most every
`SIT_PROBE_INTERVAL_S` (20 min), plus one **confirmation** deployment once the gauges have
gone dry, before any all-clear. Each deployment is a single batched keyless call.

The result gates the situation: while the probes show the airmass is live, the state is
**held at isolated showers** even with every gauge dry; the **all-clear is withheld** until
a *fresh* probe confirms the model has also gone quiet. So an intermittent shower can no
longer flash a false "rain stopped". (Free Open-Meteo is a model — it can lag a sharp
convective cell; OC4 radar would confirm more tightly, a future upgrade.)

Two supporting fixes went in with this: the steady/showery **character is taken from
physical gauges only** (the continuous model net has no real intermittency), and a
transient uncharacterised **`wet`** state is treated as active-but-silent, so it neither
announces nor triggers a false clearing — clearing now fires only when the episode actually
spoke.

### 2.10 Approach episode — the one approach voice *(260923.1)*
Replaced four overlapping approach voices (land-front phrase, sea-arc phrase, cluster-track
`approaching` state, and the would→situational bridge). Those re-spoke on every ETA change
and never reported whether the rain arrived.

- **Threat.** Each cycle, every wet cell (≥ 0.3 mm/h, not merely > 0) in the upwind cone
  (wind-from ± 60°, 8–40 km) is gathered from all sources: EA gauges (reading ≤ 45 min old),
  sea sentinels/pickets/mobiles, home-ring points, and tracks the tracker judges approaching.
- **Class.** *Organised* = ≥ 3 wet cells spread ≥ 15 km **across** the wind, or a cell
  ≥ 2 mm/h, or ≥ 2 cells with pressure falling. Otherwise *isolated*. Track member counts
  are not used, because a cluster keeps recently-wet gauges, so a single shower's trail would
  look like a band.
- **Alert gates.** Organised: edge ≤ 35 km, ETA within the 90-min window. Isolated: edge
  ≤ 12 km, ETA midpoint ≤ 40 min, not weakening.
- **Timing counts readings, not minutes.** EA buckets are 15-min and the net is sampled every
  15 min, while the probe polls every ~5 min. A new reading is a newer field-observation
  timestamp. Alert after 2 readings with a threat; confirm needs a later reading; fizzle
  needs 2 empty readings; one missed reading is tolerated.
- **Sequence.** alert → optional confirm (once: a later reading shows it closer by
  ≥ max(3 km, 25 %) and not weakening) → arrived (silent; the at-home voice speaks) or
  fizzle (one message: cone empty 2 readings, or ETA window + 20 min passed without getting
  materially closer).
- **After a fizzle.** A threat still in the cone is suppressed until the cone clears. It may
  re-alert only if it upgrades isolated → organised, or comes clearly closer. Non-heavy
  area shower notes stay quiet while the engine is weighing or running an approach, and for
  40 min after a fizzle. Isolated *light* showers beyond 20 km are never announced by the
  area voice.
- **Logs.** `rain_episodes-YYYY-MM.jsonl` holds alert/confirm/arrived/fizzle/expired events
  with the threat and its cells. `rain_threats-YYYY-MM.jsonl` is the shadow log: every threat
  run, alerted or not, with location-agnostic features per reading (class, edge, intensity,
  cells, cross-wind width, speed source, trend, pressure, sources, decision) plus install
  geometry (gauges in the cone, typical spacing, home rounded to 0.01°, month, hour). Its
  outcome is `arrived` (truth `gauge` or weaker `home_model`), `no_arrival` or `expired`.
  This is the groundwork for a per-install learner (Stage 2 shadow mode, Stage 3 drive).
- **Output.** `situational.approach_episode` = {state, id, cls, dir, alert_age_s,
  suppressed, threat, present_reads, empty_reads, qualifies, shadow_candidate}.

### 2.11 Steering wind, honest track motion, stationary gauges *(260926.1; the 260923.2 build was never deployed)*
Prompted by a lone Dartmoor gauge (SX651824) that drizzled lightly all day. It produced a
leftover track "approaching at 64 mph", which is physically impossible.

- **Steering wind.** Showers move with the ~850 hPa flow, not the 10 m wind. One keyless
  Open-Meteo pressure-level read at home (`wind_speed/direction_850hPa`), cached 1 h, retried
  15 min after a failure (~24 calls/day), covers the whole field. When Open-Meteo is down, a
  850 hPa read up to 3 h old is kept (marked old); after that, surface wind × 1.6 (`src:
  surface`) is used — exactly the pre-steering behaviour. It is the **one flow** wherever the
  probe asks where rain comes from and how fast: the approach cone, the land front's upwind
  gauges and fallback speed, the gauge approach, the track upwind/heading checks, land-probe
  placement, the sea trackers' chase speed and the sea-speed check. The 1.6 factor applies
  only to the surface fallback, never to a real 850 hPa speed. It appears as
  `situational.steering`.
- **Plausibility gate.** `speed_plausible()`: a measured speed must be 0.5–1.5× the steering
  speed, and its heading within ±40° of the steering flow. A speed that fails is withheld with
  a reason (`speed_note`, e.g. "64 mph vs steering 17 mph"). Land, sea and track speeds all
  go through it before feeding an ETA. Otherwise the ETA comes from the steering wind. On the
  surface fallback the original looser test applies (0.4–3.0× of 1.6 × surface wind, heading
  within 75°), so behaviour without Open-Meteo is unchanged.
- **Track motion.** Motion is only measured when a cluster has both **gained and lost**
  member gauges across ≥ 10 min, i.e. a hand-over. A fixed gauge can't move, and a cluster
  that only grows shifts its centroid without real motion. Hand-over and motion are judged on
  the gauges **wet now** (a dried gauge counts as lost at once), and speed and ETA are measured
  from their centre, because the full cluster keeps recently-wet gauges and lags a moving band.
  An unverified track is never marked approaching. A track surviving a missed cycle is `stale`
  and never approaching.
- **Stationary / suspect gauges.** A gauge wet (> 0) in ≥ 75 % of ≥ 10 readings over 3 h is
  flagged. The flag clears when that falls below 50 %. It is excluded from tracks and
  approach threats, but still shown and still counted by the area state. One hourly model
  spot-check at the gauge (free Open-Meteo) labels it `stationary` (model wet: static
  drizzle) or `suspect` (model dry: possible fault). Flags appear as
  `situational.gauge_flags` and on each point.
- **Forecast view** (formerly the engine view). Adds a steering-wind row and a flagged-gauge count. Flagged gauges get a
  dashed ring (amber stationary, red suspect). Tracks read "speed unverified" or "stale"
  with the reason in the tooltip.
- **Shadow log.** Candidates also record the steering wind and the flagged-gauge count, and
  each reading records `speed_src`.

### 2.12 Sea net on the OpenWeather fallback *(260926.1)*
Open-Meteo is daily-capped on a shared IP for most of most days, so the sea net usually runs on
the OC4 fallback. It previously read the same 4 sentinels every sample (chosen by list
position), leaving the upwind 240°/260° sentinels, all pickets and the hidden fills unread, and
labelled every point `OM`.

- **Upwind first, then rotate.** Of the `OC4_FALLBACK_MAX` (4) points read per sample,
  `NET_FB_UPWIND` (2) are the sentinels/pickets nearest the flow's source; the rest rotate round
  the others, so every point is read regularly. Same call count as before.
- **Kept readings.** A point not read this sample keeps its last OC4 reading with its time.
  After `NET_OC4_KEEP_S` (60 min) it is shown as `stale` and can't detect or be a threat; after
  `NET_OC4_DROP_S` (3 h) it is dropped.
- **Honest labels.** Each point's `source` is what actually read it (`OC4`/`OM`),
  `confirmed` is true for an OC4 reading, and `model_ts`/`age_s` give its real reading time, so
  the Forecast view shows true ages. Point history is keyed by reading time, so a kept reading
  isn't re-counted every poll.

---

## 3. Output shape

On `/api/ea`, under `rain_probe`:

- **`situational`** — `{ state, base_state, snow, n_field, n_phys, n_active, n_wet,
  nearest_wet_km, coverage, shower_confidence, n_showery, n_steady,
  intensity:{peak_band, name, bearing, dist_km}, locus, here, approaching, stable_s,
  tracks:[…], approach_track }`.
- **`tracks`** — each `{ id, bearing, dist_km, peak_band, snow, n, approaching, speed_kmh,
  eta_text, age_s }`.
- **`situational_speak`** — the cadence-scheduled `[{tier, key, phrase}]` to voice this cycle.

The rain-probe diag log also carries a one-line `SIT <state> cov=… n_wet=… shc=… peak=…`
summary.

---

## 4. Frontend

- **`evaluateSituational(rp)`** voices `situational_speak` under the existing **"Rainfall
  nearby"** category, edge-triggered (each new phrase once, primed silently on enable/arm).
- The old `evaluateRainfall` escalation cadence is **retired** — it keeps only the visual
  header popup.
- **Normal / Low** button in the alarms panel (persisted `alarmCadenceLow`); it appends
  `&cadence=low` to the EA fetch, which `grid_server` reads and passes as `cadence_mult`.
- The probe (and so the situational voice) runs on the **fast rain poll** (~5 min): wind is
  TTL-/budget-throttled, so `grid_server` now fetches it and runs the probe in both the
  full and rain-only paths.

---

## 5. Tunables (first-pass — to be tuned live)

All in `rain_probe.py`:

| Constant | Value | Meaning |
|---|---|---|
| `SIT_HIST_WINDOW_S` | 90 min | per-gauge character window |
| `SIT_RECENCY_S` | 40 min | "still active" window (clearing) |
| `SIT_WET_MMH` | 0.3 mm/h | wet threshold (= `ARC_DETECT_MMH`) |
| band cuts | 2 / 5 / 10 mm/h | light / moderate / heavy / very heavy |
| `SIT_HERE/NEAR/MID_KM` | 5 / 20 / 40 | proximity bands |
| `SIT_WIDESPREAD_COVER` / `SIT_CONTINUOUS_COVER` | 0.35 / 0.5 | coverage cuts |
| `SIT_SHOWERY_MIN_TRANS` | 2 | flips in window to look showery |
| `SIT_DWELL_S` | 9 min | certainty dwell before an area/approach announcement |
| `CAD_HERE_HEAVY/LIGHT` | 10 / 20 min | here heartbeat |
| `CAD_SHOWERS` / `CAD_CONTINUOUS` | 40 / 60 min | area heartbeats |
| Low multiplier | ×2 | halves periodic cadence |
| `SIT_CLUSTER_LINK_KM` | 18 | cluster single-linkage distance |
| `SIT_TRACK_MATCH_KM` | 22 | track nearest-match distance |
| `SIT_TRACK_MAX_MISS` | 2 | cycles a track survives unmatched |
| `SIT_TRACK_WINDOW_S` | 45 min | track motion / speed window |
| `SIT_PROBE_MAX` | 3 | land probe points per deployment (frugal) |
| `SIT_PROBE_INTERVAL_S` | 20 min | re-probe cadence during a showery episode |
| `SIT_PROBE_EPISODE_S` | 90 min | how long a showery episode stays live |
| `SIT_PROBE_FRESH_S` | 30 min | a probe reading older than this can't confirm |
| `SIT_PROBE_DOWNWIND_KM` | 12 | probe offset up/downwind of the cluster |
| `APPR_ORG_ALERT_KM` / `APPR_ISO_ALERT_KM` | 35 / 12 | alert range, organised / isolated |
| `APPR_ORG_MIN_CELLS` / `APPR_ORG_WIDTH_KM` / `APPR_ORG_PEAK_MMH` | 3 / 15 / 2.0 | organised criteria |
| `APPR_ISO_MAX_ETA_MIN` | 40 | isolated ETA midpoint ceiling |
| `APPR_PERSIST_READS` / `APPR_FIZZLE_READS` | 2 / 2 | readings to alert / to fizzle |
| `APPR_CONFIRM_FRAC` / `APPR_CONFIRM_MIN_KM` | 0.25 / 3 | closing needed to confirm or re-alert |
| `APPR_OVERDUE_GRACE_S` | 20 min | past-ETA grace before an overdue fizzle |
| `STEER_TTL_S` / `STEER_RETRY_S` | 60 / 15 min | steering-wind cache / retry after failure |
| `STEER_RATIO_LO` / `_HI` / `STEER_HEADING_TOL` | 0.5 / 1.5 / 40° | measured-speed plausibility |
| `TRACK_MIN_SPAN_S` | 10 min | minimum history for track motion |
| `GAUGE_STAT_*` | 3 h, ≥ 10 reads, 75 % / 50 % | stationary flag set / clear |
| `GAUGE_CHECK_S` / `GAUGE_CHECK_WET_MMH` | 60 min / 0.05 mm/h | flagged-gauge model spot-check |
| `STEER_KEEP_S` / `SURF_HEADING_TOL` | 3 h / 75° | keep an old 850 hPa read / surface-fallback heading check |
| `OC4_FALLBACK_MAX` / `NET_FB_UPWIND` | 4 / 2 | sea points read per fallback sample / of which upwind |
| `NET_OC4_KEEP_S` / `NET_OC4_DROP_S` | 60 min / 3 h | kept OC4 sea reading: stale after / dropped after |

---

## 6. How to inspect

- `GET /api/ea?lat=…&lon=…&dist=…` → `rain_probe.situational` (state, tracks, metrics) and
  `rain_probe.situational_speak`.
- The `SIT …` line in the rain-probe diag log.
- **Forecast view** at `/forecast` (or **⚙ under the hood** on the EA page): the live plan and
  HUD; `/forecast?view=radar` is the radar alone (the EA page's **◎ gauge radar**).
- Arm the **"Rainfall nearby"** alarm category to hear it; toggle **Normal / Low** in the
  alarms panel. The **test** button speaks a sample situational phrase.

Watch the `situational` state and `tracks` against real conditions (cross-check Gridwatch /
radar) before trusting the voice, and adjust the tunables above.

---

## 7. Known rough edges & remaining work

- **Not yet tuned live.** All thresholds are first-pass from offline simulation.
- **Transit flap.** While a front transits the 5–20 km band the *area* state can wobble
  between showers/continuous. It doesn't misannounce (dwell + signature gating suppress
  speech), but the diag log looks busy there. Candidate: let an approaching track's identity
  carry through the transit rather than handing straight to the area classifier.
- **Land probes are model, not radar.** Between-gauge land showers are now confirmed by
  free Open-Meteo probes (§2.9), which can lag or miss a sharp convective cell; OC4 radar
  reads for the land probes would tighten this.
- **OC4 nowcast still separate.** The One Call 4.0 "Weather nowcast" category remains its own
  opt-in offshore-detail layer; if both it and "Rainfall nearby" are armed there is minor
  at-home overlap. Folding the nowcast fully into this one voice is the last consolidation.
- **Land-snow inference not done.** Physical gauges can't self-distinguish snow, so their
  `snow` flag is currently always false; only the model/virtual points carry snow. Inferring
  land snow from home OWM `snow.1h` + near-freezing temperature is outstanding.
- **Wind field as approach prior.** The approach is currently read purely from gauge motion
  (which is the right primary source); using the *area* wind field as a tie-breaker when
  corridor coverage is sparse is not yet wired.

---

## 8. Build history

- **(a/b)** situational map + per-gauge history + character + state classification, exposed
  silently. *(server 260827.5)*
- **(c)** cadence scheduler + state wording + voice migration (retire old cadence) + Normal/
  Low. *(server 260827.6, dashboard 260827.20)*
- **(d)** intensity-weighted centroid direction + sector hysteresis; cluster-continuity
  tracking with a re-centring approach + ETA. *(server 260827.7)*
- **(d.2)** frugal land model-probes to confirm a showery airmass between gauges (hold
  showers through gaps, gate the all-clear on a fresh model confirmation); character from
  physical gauges only; badge gated on the situational state. *(server 260827.8, dashboard 260827.21)*
- **(d.3)** engine debug view: `situational.points` added to the output; a standalone
  live plan-view page served at `/engine`. *(server 260827.10; renamed Forecast view at `/forecast` in 260926.1)*
- **(e)** approach episode: one approach voice (alert / confirm / fizzle), reading-based
  timing, organised-vs-isolated gating, post-fizzle suppression, area-voice muting, and
  episode + shadow-threat logs. *(rain_probe 260923.1, dashboard 260923.3)*
- **(f)** 850 hPa steering-wind plausibility gate, hand-over-only track motion, stale tracks,
  stationary/suspect gauge flags, engine-view labels; steering flow used throughout.
  *(rain_probe 260926.1 — the 260923.2 build was never deployed)*
- **(g)** sea-net OC4 fallback: upwind-first + rotating reads, kept readings with ages, honest
  source labels. *(rain_probe 260926.1)*

---

## 9. Tuning log — live radar comparisons

A running record of engine-vs-radar comparisons used to steer the first-pass thresholds.
Each entry: the radar picture, what the engine reported, what matched, what didn't, and any
parameter change made (or a candidate not yet applied).

### 2026-08-28 — widespread band over Plymouth
- **Radar:** broad SW→NE band across the whole area; several **heavy** (yellow) cores — N
  (Tavistock/Princetown ~15–20 km), NE (Bovey Tracey ~35 km), E, SE (Salcombe ~35 km);
  Plymouth itself under light-blue rain.
- **Engine:** `continuous · moderate · north`; coverage 0.52, shower-conf 0, nearest wet
  6.4 km, 16 active/wet, track #1 N ~20 km "moderate · drifting", stable 6 min (pre-dwell,
  silent).
- **Matched well:** the *state* (continuous, not showery — coverage 0.52 with shower-conf 0),
  the *main mass* (N ~20 km), *home on the edge* (nearest 6.4 km), and correctly still
  silent under the 9-min dwell.
- **Gap — intensity under-read:** engine `moderate` vs radar `heavy`. `intensity.name` is the
  peak *gauge* band, so no EA gauge under the yellow cores read ≥5 mm/h this cycle
  (tipping-bucket lag/undercatch; some cores fall between gauges). The free Open-Meteo net
  does not close it. Its announcement would therefore understate intensity.
- **Note:** the widespread field collapsed to a *single* track (single-linkage 18 km) —
  correct for continuous rain, but embedded heavy cores aren't called out individually.
- **Candidate actions (not yet applied):** (a) OC4 radar reads for the land probes to lift
  intensity during events — *leaning toward this, event-only*; (b) let the model net/probes
  raise the reported peak when they show a heavier core than the gauges; (c) accept the
  honest gauge-eye intensity. No change made yet — gathering more comparisons first.

_Send comparisons across a range of conditions (showery, continuous, approaching, clearing,
snow) — each adds an entry here and, once a pattern is clear, a threshold tweak._

