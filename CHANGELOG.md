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
