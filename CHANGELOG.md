# Changelog

All notable changes to GB Energy Monitor are recorded here, newest first.

Build tags use the format `YYMMDD.N` (UTC date + increment). The server
(`grid_server.py`) and dashboard (`grid_dashboard.html`) are versioned
independently; where a change touches only one, the file and its build number are
noted on the entry.

Entries from 260817 onward carry precise build numbers. Older entries were
reconstructed from development history and are dated by build range rather than
exact tag.

---

## 260826

- Rainfall nowcast: spoken alerts made edge-triggered — a message speaks only when its condition first appears or its wording changes, so a persistent state is announced once rather than repeating on a timer. The all-clear ("conditions have settled") is now a one-shot on the rain→calm transition and never fires on a calm-from-start day. (`rain_probe.py`)

## 260825

- Weather nowcast can now speak. A new "Weather nowcast (One Call 4.0)" alarm category voices the rain-alert phrases (onset, intensity, approach, clearing) through the existing audible/spoken alarm engine — off by default, speaks only while sound is armed, and adopts the current state silently when first enabled. Enabling it triggers the background full-EA pull so it works with the panel closed. (dashboard .10)
- OpenWeather budget: the local-weather + nowcast daily call ceiling raised from 100 to 300 (a ~100 base plus the ~200 One Call 4.0 headroom), still well under the 900/day plan cap. The first-run key modal now explains the optional One Call 4.0 subscription, the automatic free-tier fallback, and the per-day throttle. (server .4, dashboard .9)
- Rainfall-alert diagnostic probe added (`rain_probe.py`, new; wired read-only into the EA overview). Each cycle it logs the phrase the alert would speak, from: at-home model signals (intensity band, rate trend, 3-hour pressure tendency, visibility); a physical gauge-sector approach with a measured-wind ETA; a sea-masked movable offshore Open-Meteo arc (scan→track, inward-edge advection speed, marked virtual gauges); and the OWM one-minute forward nowcast (onset / intensification). No tone is played — diagnostic only. (server .1)
- Probe tracker guards: trend, pressure and visibility signals require a minimum span of history before they classify, and a series whose newest sample is stale is treated as unknown; the pressure rate is capped at a physical limit — removes a spurious "pressure rising" alert on server restart. The offshore arc holds state through a data dropout instead of falsely retracting. (`rain_probe.py`)
- One Call API 4.0 support with auto-detect (`owm_onecall.py`, new). The EA weather panel uses OC4 when the key is subscribed — current conditions plus the one-minute precipitation nowcast — and falls back to the free 2.5 Current Weather call on 401/403, so the panel works without a subscription. Active tier is labelled on `wind.source` / `wind.api`. (server .2)
- EA rainfall map: modelled offshore points from the probe's arc are shown as dashed MODEL cards, counted separately, and excluded from the physical gauge count/cap, the "nearest" outline, and alert confirmation. (dashboard .3)
- EA rainfall map: directional grid widened to 5 columns so a same-distance offshore arc fans out by bearing instead of stacking. Collision spill keeps a gauge in its true direction (nudging apparent distance, not bearing); fully-empty rows and columns are packed out; cards keep a minimum width with horizontal scroll; virtual cards lead with their azimuth. (dashboard .5–.8)
- EA rainfall map: gauges no longer placed in the wrong hemisphere — the collision-spill steps outward only in a gauge's true N/S direction. (dashboard .1)
- EA river panel: on a total river-stations failure the section collapses to a single full-width column, so the error notice centres on the page instead of sitting in the empty left column. (dashboard .2)
- Solar resource verdict now gated by sun elevation. `_rate_solar` uses computed solar elevation instead of a binary day/night flag: night (≤0°) reads "none", below 5° "minimal output", below 15° capped at "reduced" — so the Resource Conditions rollup can no longer read "solar strong" near dawn/dusk when a clear but low sun yields ~1% output. Each solar card exposes `sun_elev_deg`. Resolves the open Known Issue. (server .3)

## 260824

- Local wind: calm and variable states now shown honestly. Direction below 0.5 m/s reads "CALM"; a missing bearing (or a wide 3h direction swing at low speed) reads "VAR". The dial replaces its arrow with a cyan CALM/VAR label and drops the degree readout when direction is undefined. (server .1, dashboard .1)
- Local wind: a missing bearing alone no longer triggers the "unavailable this cycle" notice — a calm wind with no direction is a valid reading, not a fetch failure. (dashboard .1)
- EA errors: upstream CDN error pages (e.g. a 503 HTML body) are reduced to a plain textual reason instead of leaking raw markup into `eaData.error`. (server .1)
- EA errors: river-panel error and soft-error notices now HTML-escaped before injection, so an upstream response body can't render into the DOM. (dashboard .1)

## 260823

- Weather block 4: cloud cover and sky description now sourced from Open-Meteo (primary), with OpenWeather as fallback when Open-Meteo is unavailable. Wind, temperature and pressure remain OpenWeather. (server .7)
- Weather block 4: cloud source shown as an "OM"/"OWM" tag by the "Cloud %" row. (dashboard .7/.8)
- Weather block 4: cached readings tagged stale with age and reason; a "cached" marker is shown when a fresh fetch is unavailable. (server .6, dashboard .6)
- Pressure tendency wording: "Falling gently" changed to "Falling slowly" (parallel with "Rising slowly"). (server .5)
- Footer server build now read from the snapshot instead of a hard-coded string. (server .4, dashboard .4)
- Gas config re-read on file change, fixing a stale in-memory config that survived until restart. (server .2)
- Gas unit resolution centralised; auto-detect demoted to a warning that never overrides the setting; added unit-confirmed/warning state. (server .2)
- Gas unit-check flag made clickable (opens settings) and added to the estimated-cost card as well as the gas cost cards. (dashboard .2/.3)

## 260820

- Frequency: dedicated `/api/frequency` endpoint serving a full-resolution 15s recent tail plus a 2h trace. (server)
- Frequency display reworked to show the newest sample (live edge) with a natural age tick, replacing earlier replay/queue approaches. BMRS publishes in ~2-minute bursts of 8 samples. (dashboard)

## 260817 (.1 – .15)

- Octopus matched-rate costing: account resolution with separate electricity/gas account numbers, dated rate-history fetch with DD/non-DD handling, and time-matched costing helpers. (server)
- Billing-period end-day setting added; consumption fetch window widened to cover a full billing month. (server)
- "Tariff and Estimated Costs" panel added; matched-rate costing wired into all cost badge cards, each reading priced at the rate in force at its own timestamp. (dashboard)
- Rainfall alarm feature designed but shelved, to avoid breaking the working rain/river display.

## 260814 – 260815 (builds 260814q – 260815k)

- Gas page: linepack history with forecast-minimum floor and a local-log tail to bridge publication lag; balance line from measured instantaneous supply−demand; wholesale gas price series. (server)
- Re-added missing `get_gas_linepack_history`. (server)
- "GB" not "UK" corrected throughout (BMRS and National Gas data cover Great Britain only). (both)
- Alarm system: Web Audio + Web Speech, selectable categories, tiered CRITICAL/WARNING/NOTICE tones, latching/hysteresis, stale-data suppression, first-poll baseline suppression, CRITICAL 20s repeat, per-category test buttons. (dashboard)
- Gas page balance/price plots: dual axes, UTC x-axis, split imbalance label, rounded bar ends. (dashboard)
- Responsiveness: overlay content capped and centred, graduated zoom for HD screens, EA flood-list alignment fix. (dashboard)
- Colourgramme: LOG made the default scale. (dashboard)

## 260811 and around

- Colourgramme backend: companion plots (periodicity spectrum, average-day, load-duration, day-of-week); rolling 24-month grid split into aligned year-rows. (server)
- BST timezone fix: parse Octopus `interval_start` via `fromisoformat` rather than reconstructing UTC keys, which had missed all summer months. (server)
- History fetch extended to ~25 months; distinguishes fetch failure from genuine data absence. (server)
- Grid-scale battery/pumped storage identification via the Terravolt BMU classification file, cached weekly. (server)
- Weather retrieval resilience: batched requests, independent 15-min cache, 429 backoff, last-good fallback. (server)
- Octopus "My Home" pop-out: cost cards, 48h/2-week bar charts, consumption-patterns insight cards, honest freshness reporting, auto-refresh. (dashboard)
- Colourgramme pop-out: spectrogram palette, SINGLE/STACKED toggle, month multi-select, clip sliders, lin/log toggle, gap handling. (dashboard)
- Terminology standardised from "carpet" to "Colourgramme". (both)

## ~260804 and earlier

- Solar: Sheffield PVLive added (Elexon FUELINST does not meter embedded solar); solar tagged "est". (both)
- Spinning reserve: MELS lookback widened to 30 min with an 80% coverage guard, fixing false near-zero reserve from sparse MEL publication; serves a stale-but-labelled reading rather than a misleading figure. (server)
- Frequency: switched from the lagging archive endpoint to `system/frequency` (~1-min latency). (server)
- Core dashboard panels: frequency heartbeat trace, capacity margin radial gauge, −24h/+24h margin graph, operating reserve panel, supply stack bar, generation-by-source table with trend arrows and "est" tag, weather panel, per-source status strip. (dashboard)
