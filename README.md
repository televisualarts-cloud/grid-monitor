# grid-monitor

A dashboard for monitoring the GB electricity grid and GB gas supply in real time. It also shows live power cuts across the UK & Ireland, rain, river levels and flood warnings for England (from the Environment Agency), and — if you're an Octopus Energy customer — your own household electricity and gas usage and cost.

**Full disclosure:** This project was vibe-coded with Claude Opus 4.8 and Opus 5.5. Errors are probable, but when found they get corrected. A guiding principle throughout is *honesty over plausibility* — anything estimated, derived or out of date is labelled as such rather than presented as hard fact.

**For educational and hobby purposes only. Not to be used for operational or safety of life decisions.**

---

## Getting started

### What you need
- A machine running Python 3 (the server uses only Python's built-in libraries — nothing to `pip install`).
- Five files placed together in one folder:
  - `grid_dashboard.html`
  - `grid_server.py`
  - `rain_probe.py`
  - `owm_onecall.py`
  - `forecast_view.html`

  `rain_probe.py` and `owm_onecall.py` are companion modules that enable the
  rainfall-alert diagnostics and the OpenWeather One Call 4.0 nowcast, and
  `forecast_view.html` is the Forecast view page (the rain & gauge radar and "under the
  hood" on the Environment Agency page). The server still runs without them — it
  just drops those features and uses the free weather tier — but the standard
  install is all five together, in the same folder.

  For the **UK & Ireland power-cuts** view (see *Power cuts* below), add these to
  the same folder:
  - `powercuts.py`
  - `powercuts_page.html`
  - `metar.py`
  - `windcuts.py`
  - `region_polys.json`

  `balancing.py` adds the **balancing costs** panel (see *Balancing costs* below):
  what NESO spends each day to keep the system balanced. It is optional; without
  it the panel says the module isn't installed.

  `gas_probe.py` is an optional companion too: it runs the gas alarm ladder
  (National Gas's published notices plus derived supply-margin and linepack signals),
  logs every reading, and feeds the dashboard's **Gas supply & margin** alarm category.
  Put it in the same folder to enable it.

  `powercuts.py` aggregates the live outage feeds and `powercuts_page.html` is its
  map page; `metar.py` and `windcuts.py` add the optional wind vs power-cuts
  correlation and need `region_polys.json` beside them. As with the rain modules,
  the server runs fine without any of these — the power-cuts chip and page simply
  don't appear — so they're optional, but the full install is all of them together
  in the one folder.

### Running it
1. Run `grid_server.py`.
2. Open a browser and go to **http://localhost:8412**.

If another program is already using port 8412, the server starts on the next free port
(8413–8421) and says so in its console window, e.g. *"GB Energy Monitor is on port 8413 —
open http://localhost:8413"*. It remembers that port in `server_port.json` and tries it first
next time, so your bookmark only needs changing once; delete the file to go back to trying
8412 first. The port in use is shown in the dashboard footer. If GB Energy Monitor is already
running, a second launch just tells you where and exits, so you never get two copies. To pick
a port yourself run `grid_server.py --port N`; if N is taken you are shown the free ports to
choose from.

The page refreshes itself roughly every 60 seconds, so you can leave it open.

### Updating an existing install
When you replace any of the files with a newer version — `grid_server.py`,
`rain_probe.py`, `owm_onecall.py`, `gas_probe.py`, `powercuts.py`, `metar.py`, `windcuts.py`, `balancing.py`, or
`grid_dashboard.html` / `powercuts_page.html` / `forecast_view.html` — **stop and restart `grid_server.py`**
afterwards. The server loads the Python modules once at startup, so changes to any
of them (including the companion modules) only take effect on a restart; a browser
refresh alone is not enough. After restarting, reload the page.

### Files created automatically
Once running, the server writes these into the same folder as needed:
- `bmu_locations.json`, `bmu_registry.json` — power station / unit reference data.
- `osm_sites.json` (when `osm_sites.py` is installed) — OpenStreetMap generator sites and BM unit positions from UK Grid Atlas, refreshed weekly, for the transmission wall.
- `dno_ecr.json` (when `dno_data.py` is installed) — the distribution network operators' Embedded Capacity Registers (connected sites of 1 MW or more), refreshed weekly, for the wall's substation panel and site cards.
- `dno_lines.json` (when `dno_data.py` is installed) — the operators' own 132 / 66 / 33 kV line maps, refreshed monthly. A `dno_lines` folder, if you make one, is where to put operator shapefile .zip downloads (such as NGED's) for the wall to read.
- `etys_ratings.json`, `etys_boundaries.json` (when `etys.py` is installed) — NESO's ETYS Appendix B transmission circuits with their seasonal ratings and its substation code index, and the ETYS study boundaries; checked monthly for newer ones.
- `b6_log.json`, `gsp_log.json` — the wall's modelled Scotland → England transfer and its estimates at NGED's measured grid supply points, per half-hour, kept a few days for the accuracy checks.
- `repd_sites.json` (when `osm_sites.py` is installed) — the operational and under-construction GB projects from DESNZ's quarterly Renewable Energy Planning Database, checked weekly for a new quarter.
- `margin_history.json` — a rolling log of the capacity margin so it can be graphed over time.
- `alert_history.json` — a log of alerts, kept for up to 30 days.
- `alert_dismissed.json` — alerts dismissed for 7 days from the alert bar.
- A `logs/` folder — the interleaved 15-second data log (`grid_log-YYYY-MM-DD.jsonl`, frequency points and generation-mix rows, pruned after 30 days) and, under `logs/archive/`, the permanent weekly generation files that never expire.
- `balancing_store.json` (with a daily backup, `balancing_store.bak.json`) — the balancing-costs history: NESO's confirmed daily costs, the Elexon figures behind the estimates, and the daily wind and frequency summaries, kept for about 400 days (see *Balancing costs*).
- `forecast_window.json` — your active-forecast-window setting (see *Alert system*).
- `gauge_mutes.json` — rain gauges you've muted from the rain alerts (see *Click a river-level or rainfall gauge*).
- `api_usage_daily.json` and `api_calls.jsonl` — the per-UTC-day tally of upstream weather calls by source and purpose, plus a rolling event log of each real call.
- `om_debug.jsonl` — one line per Open-Meteo HTTP attempt (outcome, HTTP status, exact rejection text, and any rate-limit / Retry-After headers). This is the evidence trail for diagnosing an "Open-Meteo down" report: it shows which limit (per minute, hour or day) Open-Meteo said was hit — or whether a "daily / try again tomorrow" reply came despite a low real call count, which points to a shared/CGNAT IP rather than this app's usage.
- `rain_episodes-YYYY-MM.jsonl` and `rain_threats-YYYY-MM.jsonl` — the rain engine's record of each approaching-rain episode (alert, confirm, arrival or fizzle) and of every approach it weighed, alerted or not, used to tune the rain alerts.
- `gas_probe-YYYY-MM.jsonl` (when `gas_probe.py` is installed) — the gas alarm readings each hour and anything spoken.
- `gas_storage_store.json` (when `gas_storage.py` is installed) — about two years of National Gas storage and LNG daily figures and the GIE Europe history behind the gas storage page. The first start fetches the history slowly in the background (about 5 minutes) so it never crowds out the live gas feed.
- `ring_sample_ts.json` — the last time the OpenWeather backup gauges (see *weather API limits*) were sampled, so their sampling interval survives a server restart and doesn't re-spend the quota on every restart.
- A `captures/` folder — PNG images you save from the frequency-history viewer's **⭳ save** button.
- Power-cuts data (when the power-cuts files are installed): `powercut_keys.json` — the operator API keys you enter on the page (git-ignored; sent only to the operator they belong to); `powercuts-YYYY-MM.jsonl` — the outage history; `powercut_last_ids.json` — the incident IDs seen last poll, for counting new and cleared cuts; `powercut_watch.json` — your monitored postcode areas (git-ignored); and, for the wind-correlation model, `windcuts_binstats.json` and `windcuts_state.json` plus a `windcuts-YYYY-MM.jsonl` audit log. These are written by the server's background sampler whether or not the power-cuts page is open.

After you enter an OpenWeather API key:
- `openweather_key.json`, `openweather_budget.json`, `wind_budget.json`, `weather_last_good.json`.

The weather panel works on OpenWeather's free tier. An **optional One Call 4.0**
subscription additionally unlocks the rainfall nowcast — the offshore rain watch
and the minute-ahead forecast. The server detects the tier automatically and
falls back to the free Current Weather Data API if the key isn't subscribed to
One Call 4.0. OpenWeather calls are capped per UTC day (300/day for the
local-weather + nowcast path) to stay within your plan.

After you enter Octopus Energy credentials:
- `octopus_config.json`, plus an `/octopus_history` folder holding up to two years of your half-hourly usage.

---

## Reading the page

### Status and header
- **Status badge (top area):** a green flashing icon and "nominal" means the server is pulling data correctly. It turns amber or red for warnings and alarms.
- **Summary chips (top centre):** quick-glance pills for electricity, gas, floods and UK & Ireland power cuts. The electricity chip is always shown; the gas and flood chips only appear when there's something worth flagging; the power-cuts chip shows the live UK & Ireland customers-off total (bold, coloured by severity, with a planned/unplanned split in its tooltip) whenever the power-cuts feed is up; the small tag at its right end switches the total between unplanned cuts only (amber **unplanned**) and planned works included (blue **incl. planned**). An orange **Constraint** chip (for example "Constraint B6 3.6 GW") appears while NESO is turning down 500 MW or more of generation for system reasons. That figure is measured from Elexon's SO-flagged instructions and kept fresh by the server whether or not a wall page is open; it names the constrained boundary when a wall page has picked one out, and the tooltip says where and which units. Clicking it opens the network map. It is normal operation, not an alarm. Click a chip to jump to the relevant page. (The gas chip is a *derived* signal, not an official National Grid Margins Notice — the tooltip says so.)
- **Buttons (top right), left to right:** **live powercuts map** (opens the UK & Ireland power-cuts map — the same as clicking the power-cuts chip), **alarms**, **gas**, **ea** (Environment Agency), **my home**, **history**, which opens the alert history and statistics view (see *Alert system* below), **🗺 network**, which opens the transmission network map as a full-page pop-up, and **▦ wall**, which opens the transmission wall in its own window (see *Transmission wall* below). On narrow screens the buttons wrap onto more lines.

Press **Esc** to close any of the full-screen pages (generators, gas, power cuts, EA, My Home, colourgramme, alert history, balancing costs, network map).

### Light and dark theme
The **☀ light** / **☾ dark** button (top right of the dashboard, the wall's header, the power-cuts page and the forecast view when opened on their own) switches every page between the dark design and a light version. The choice is remembered in the browser and shared by all the pages and pop-ups on this server: switching on one page reloads the others that are open. Dark is the default. The light version is made from the dark one: each colour keeps its hue, with dark backgrounds made light, light text made dark and data colours a deeper shade, so the colour keys still match what is drawn.

---

## National grid data panels

- **System Status** — "System Nominal", or alerts/alarms when relevant.
- **System Frequency** — the current grid frequency, usually 1–2 minutes old, with a recent trace. The trace is drawn with min/max-envelope downsampling, so a brief dip below 49.8 Hz (or a spike) still shows on the graph rather than being stepped over. If the reading goes stale, the trace dims and a note explains why, rather than showing a misleading value. The panel is **tinted by the frequency's own state** — green in the normal band (49.8–50.2 Hz), amber to 49.65 / 50.35 Hz, red to the statutory limits (49.5 / 50.5 Hz) and purple beyond, for both under- and over-frequency. The deviation dial runs to ±0.7 Hz in the same colours. Readings outside the normal band are marked along the trace with ▲ (above 50.2 Hz) or ▼ (below 49.8 Hz), labelled with their deviation from 50 Hz. Beside the dial, a **histogram** shows how long the frequency spent at each value over the last **1h / 2h / 6h / 12h / 24h** (bin width **.01 / .02 / .05 / .1 Hz**, log time scale), so short excursions show beside the hours spent near 50 Hz. While a frequency alarm is sounding, **🔇 mute alarm** in the panel's footer silences it until its level changes. The background follows the live reading; the border holds the highest level reached for 10 minutes after it clears, so a brief excursion stays visible.
  - **⤢ history button** — opens a full-window, scrollable **frequency history** built from the logged 15-second data (up to the ~30-day log retention). It opens on the last 48 hours; zoomed in to 6 hours or less it plots every 15-second sample, with hour and 10-minute gridlines. Drag to scroll, or use the ◀ step / step ▶ (one view width) and zoom buttons and arrow keys; a **● live** toggle keeps the latest data in view at the 15-second rate without changing the zoom. Hover to read the value at any point, and **click to lock** the readout in place (it's drawn on the plot, so a saved image includes it). **⭳ save** writes a PNG of the current view to a `captures/` folder next to the server. The line is recoloured amber/red exactly where it crosses the operational/statutory limits, and each day is marked at 00:00 on the axis, in UT.
  - **System Risk** (sub-panel, top-right of the frequency panel) — a composite read of how resilient the grid is *right now*: a **system inertia** in GVA·s (stored kinetic energy from synchronous plant only — wind, solar and interconnectors contribute none), a notional **RoCoF** ("if largest trips") estimating how fast frequency would fall if the biggest infeed tripped, and a **CGRI** (Composite Grid Risk Index) rolling frequency deviation, inertia and RoCoF into one figure. Inertia amplifies the CGRI only when it is genuinely reduced (notional RoCoF 0.25 Hz/s or more); on a normal grid the CGRI equals the frequency deviation, so its amber level lines up with the ±0.2 Hz operational band and it adds warnings only on a weak grid. Risk alerts state what raised them — frequency outside the operational band, reduced inertia, frequency moving quickly, or a sudden infeed loss — and the spoken frequency alarm mentions inertia only when the inertia estimate itself is reduced. A level badge (green/amber/red) uses fast-attack / slow-release hysteresis so it doesn't flap, and the sub-panel is tinted by that level (with the same 10-minute border hold) — separately from the frequency panel, because the risk index is a *notification* about resilience, not a frequency alarm. Each figure has a tooltip. The inertia estimate is **calibrated against NESO's published GB Outturn Inertia** (least-squares fit over April–July 2026, R² 0.90, mean error ~8 GVA·s), so a typical mix reads green and amber appears only in genuinely low-inertia conditions matching NESO's real ~110 GVA·s summer floor. This is still a derived, educational indicator — not an official system-operator signal.
- **Periodicity spectrum / 1-minute cycle** (the **⤢ spectrum** button beside **⤢ history**) — an amplitude spectrum of grid frequency for periods from 30 s to 30 min, drawn like the home page's periodicity spectrum (log period axis), in mHz on a 0–15 mHz scale (5 and 2.5 mHz zooms in the expanded view), and the longest period shown can be set to **5m, 15m or 30m** — 5m spreads out the region around the 1-minute line. Both choices are remembered. The marked spike at **1 minute** is a small wobble that repeats exactly once a minute. It has been found in GB and other grids and is attributed mainly to battery systems in uninterruptible power supplies (UPSs) running self-tests on internet-synchronised clocks (Hartmann et al., arXiv 2510.09862, 2025). Choose how much recent data is averaged — **1h, 12h, 24h, 7d or 30d** (remembered per browser). Longer windows use longer segments (64 min for 1h up to 17 h for 7d/30d), so the background is lower and steady cycles stand out more; 1 hour is usually too short to separate the 1-minute line from the background. Method: Welch — Hann-windowed, detrended segments overlapping by half; segments with a gap over 2 minutes or more than 10% missing are skipped, shorter gaps are interpolated. **⤢ spectrum** swaps the frequency trace for the spectrum plot in the same panel, with a hover readout, the 1-minute amplitude against neighbouring periods, the average minute (the four measured phases, :00 :15 :30 :45, with the fitted cycle) and the second of the minute where it is lowest. **✕** returns to the trace. Data comes from `GET /api/freq-raw?days=N` (the raw 15-second log as mHz offsets from 50 Hz, about 23 KB a day; loaded once per window length), kept live every 2 minutes from `/api/freq-tail`. Typical GB values in autumn 2026: about 1.7 mHz amplitude (3.4 mHz peak-to-peak), about 4 times the neighbouring periods over 7–30 days, lowest around 15–20 s past the minute.
- **National Demand** — current demand, plus an *estimated* national consumption figure (labelled "est").
- **Carbon Intensity** — grams of CO₂ per kWh, with the current index band (e.g. low/moderate/high) and the trend versus an hour ago.
- **Generation and Imports by Source** — a bar chart sorted from biggest contributor down (very small ones are omitted). Each source shows a trend arrow versus an hour ago, and estimated figures carry an "est" tag. A legend shows the renewable and low-carbon percentages. The interconnectors are drawn in a graded blue family so each one is distinguishable when stacked.
  - **generators button** — opens a full-screen "tree-map" style view of all the major generating units currently feeding the grid, one tile per station under its site name. Each unit's level is NESO's latest accepted (instructed) level where one applies, else its Physical Notification, so plant running on NESO instructions is counted even when it notified 0 MW. Small supplier/aggregator units are one "Aggregated small sites" tile per fuel.
  - **7-day button** — opens a generation-history pop-up. Two live plots cover the last seven days — output (MW) and share (%), stacked by fuel — above an **archive panel** that shows any one week, Sunday→Saturday, all in UTC. The archive steps by whole weeks (◀ ▶ or the year/month/day pickers, back to 2017), toggles between MW and %-share, and always shows the full seven days even where data is missing (gaps read as zero; the current week fills left to right). Each week's data is rolled into a permanent weekly file automatically. Any week with missing periods — including the **current, in-progress week** — can have just those gaps filled on demand with **fill from BMRS** / **fill gaps** (Elexon FUELINST for the metered fuels plus Sheffield PVLive for embedded solar, both modelled/external and labelled as such). Only the already-elapsed gaps are added and self-logged readings are always kept, so a week that mixes both is shown as "gaps filled (BMRS)"; the not-yet-elapsed part of the current week is left untouched. A week that is entirely a prior BMRS pull instead offers **re-pull** to refresh it. Self-logged data is never overwritten by a fill.
- **Balancing Costs** — what NESO spends each day to keep the system balanced: six summary figures, with an optional daily cost chart in the panel and a **details** pop-out. See *Balancing costs* below.
- **Resource Conditions** — weather at 12 renewable-energy sites (needs an OpenWeather key — see below).
- **Insight** — a short plain-language read on current conditions, including a cross-check of forecast wind against metered wind where available.
- **Capacity Margin** — how much generation could be called up at medium notice, shown as a radial gauge plus a −24h/+24h trend graph.
- **Immediate Operating Reserve** — how much reserve is instantly available if a generating unit trips off. Shows spinning reserve, the largest single unit (infeed), and whether reserve covers it — with a warning if it doesn't. Units are counted at their NESO-instructed level where one applies (the same rule as the generators view and battery figure).
- **How Demand Is Being Met Now** — the split between domestic generation and imports, plus a 12-hour wholesale electricity price graph.
- **Interconnector Flows** — which interconnectors are importing and which are exporting; direction is detected live and recolours if a link reverses.
- **System Warnings** — official NESO notices from the last 48 hours. Notices in force are listed first with their severity; recent ones that were cancelled, replaced by a newer notice, have ended, or are operational (IT, trades, tests) follow, dimmed. Severity follows the notice type: demand control is critical; high risk of demand reduction, a Capacity Market Notice or risk of system disturbance is a warning; an Electricity Margin Notice and other advisory notices are a notice. An Electricity Margin Notice is a market signal, not a sign that power cuts are expected.

A row of small status indicators along the bottom shows the health of each data source.

### Balancing costs
The **Balancing costs · NESO** panel, below *Generation and imports*, shows what NESO pays generators and others each day to keep the system balanced: mostly to turn wind down where the network can't carry it and turn gas up elsewhere. Needs `balancing.py` beside the server; no keys.

- **Confirmed and provisional.** NESO publishes its confirmed daily cost about five days after the event. For the days since, the panel shows an **estimate** from Elexon's indicative Balancing Mechanism settlement (about a day behind), scaled up by the ratio seen on recent confirmed days, because the Balancing Mechanism leaves out NESO's own trades and contracts. Estimates are always marked **est**, drawn hatched and given a likely range; each one is replaced by NESO's figure when that is published. Today is part of a day and marked "so far".
- **Summary figures:** the highest estimate, yesterday, the latest confirmed day, last month's total, the constraints share of the cost over the last 30 confirmed days, and the highest confirmed day. The **highest estimate** is coloured against the average of the last 30 confirmed days: green more than 10% below, white within ±10%, yellow up to 25% above, orange up to 50%, red up to 75%, hot pink beyond.
- **▾ chart** opens the daily cost chart inside the panel (confirmed bars, provisional hatched with a whisker for the likely range, the highest confirmed day as a dashed line, event markers). The panel remembers whether it's open. Hover any day for its breakdown.
- **⤢ details** opens the full view: the summary figures; the daily cost chart with period totals by NESO category; a strip for the smaller services (energy imbalance, reserve, frequency response, other) on their own scale, where below zero is money NESO earned; strips for planned flow over the Scottish boundary limits (NESO day-ahead), wind turned down (Elexon) and wind output (local logs), all lined up by day; a table of how well each tracks the cost; **who was paid** (gas and others paid to increase output against wind paid to reduce); and a **half-hour heatmap** of when in the day the money was spent.
- **Event markers:** ▲ a notable high or record, ▼ a notable low; red is bad news for cost or the system, green is good news. Hover a marker for the detail. Records are the highest since the stored history began. From the local logs: a **major infeed loss** marker when frequency falls 0.2 Hz or more within 30 seconds to 49.85 Hz or below, or goes below 49.6 Hz; otherwise a **frequency low** marker on a day whose lowest reading is below 49.76 Hz.
- **Updates:** NESO every 6 hours. Elexon: today's settlement periods are fetched one by one as each settles (checked every 30 minutes); a finished day is fetched whole once, and again two days later, when it is kept as final. The first run fetches about 50 days of history, which takes a few minutes and downloads roughly 700 MB from Elexon (mostly the accepted-volume files used for wind turned down); after that it's about 45 MB a day.

### Weather data (optional)
The Resource Conditions panel needs a **free** API key from **openweathermap.org**:
1. Sign up, create a new key, copy it.
2. Click the **key** (⚙) button on the weather panel and paste it in.

The key is stored by the local server in `openweather_key.json` (in the project folder) and is used only to fetch weather — it's never shown in the page again and never sent anywhere except OpenWeather. Calls are spread through the day and capped at 200, keeping you well under the free 1,000/day limit. A counter shows how many calls you've used (it resets at 00:00 UTC).

### A note on weather API limits (shared IPs / VPNs)
The two weather services this app uses have free-tier limits that reset daily at 00:00 UTC: OpenWeather is tied to your API **key**, while Open-Meteo (the free, keyless model used for the offshore rain watch, embedded solar and cloud cover) is limited **per IP address**.

Because Open-Meteo's limit is per IP and has no key, that daily allowance is **shared by everyone using the same public IP**. If you are behind a **VPN**, a corporate/university network, mobile data, or an ISP that uses carrier-grade NAT (CGNAT), you may share one public IP with many other people — and their Open-Meteo usage counts against the same pool. In that case you can see Open-Meteo return "Daily API request limit exceeded — try again tomorrow" **much sooner than your own usage would suggest**, or even continuously, regardless of how few calls this app has made.

**This is not a fault of the application.** The app reports Open-Meteo's own response faithfully and re-checks Open-Meteo every 15 minutes — so it recovers on its own once the shared pool frees up, at the 00:00 UTC reset, or if your connection moves to a fresh IP (no restart needed). While Open-Meteo is unavailable the offshore rain watch keeps working on the OpenWeather nowcast (OC4). If your EA rain gauges are *also* unavailable at the same time — so there is no local rain coverage at all — **eight OpenWeather/OC4 probes are placed around your home location** (at 20 km and 10 km on offset compass points) to stand in as virtual rain gauges, and they retract as soon as either your EA gauges reappear or Open-Meteo recovers. To confirm it's the shared-IP limit rather than the app, open this in a browser: `https://api.open-meteo.com/v1/forecast?latitude=50.37&longitude=-4.14&current=precipitation` — if you get an `error … Daily API request limit exceeded` response, the limit is being enforced on your IP by Open-Meteo, not by grid-monitor.

**If you hit the Open-Meteo limit regularly, don't run the server behind a VPN or a shared/CGNAT connection.** Because the cap is enforced per public IP, the single most effective fix is to give the server its own lightly-used IP: run it on a **direct home broadband connection** rather than a VPN, mobile data, or a corporate/university network, and exclude the Python process from any VPN via split-tunnelling if you use one. Note that some consumer ISPs (mobile broadband especially, but also some fixed-line providers) use **CGNAT**, which shares one public IP across many customers — if yours does, ask the ISP for a non-CGNAT or static IP, as no setting in this app can change the shared pool you land in. Failing that, use the self-host option below for a private quota that no IP limit can touch.

To sidestep it entirely, run your own Open-Meteo (see below). This is optional: without it the app uses the public Open-Meteo host as normal.

### Optional: self-hosted Open-Meteo

Open-Meteo is free and open-source and can run on your own PC in Docker, giving a private instance that no shared-IP limit can touch. The app uses it first and falls back to the public host automatically, so a stopped or broken local instance never blanks the dashboard.

**Set up (Windows, Docker Desktop with the WSL 2 backend).** In Docker Desktop's settings, tick *Start Docker Desktop when you sign in*, and under *Resources → WSL integration* turn on your Ubuntu. Then in an Ubuntu window:

```
docker volume create open-meteo-data
docker run --rm -v open-meteo-data:/app/data ghcr.io/open-meteo/open-meteo sync copernicus_dem90 static
docker run --rm -v open-meteo-data:/app/data ghcr.io/open-meteo/open-meteo sync dwd_icon_eu temperature_2m,relative_humidity_2m,cloud_cover,weather_code,precipitation,rain,showers,snowfall_water_equivalent,wind_u_component_850hPa,wind_v_component_850hPa --past-days 1
docker run -d --name om-sync --restart unless-stopped -v open-meteo-data:/app/data ghcr.io/open-meteo/open-meteo sync dwd_icon_eu temperature_2m,relative_humidity_2m,cloud_cover,weather_code,precipitation,rain,showers,snowfall_water_equivalent,wind_u_component_850hPa,wind_v_component_850hPa --past-days 1 --repeat-interval 5
docker run -d --name open-meteo --restart unless-stopped -v open-meteo-data:/app/data -p 8765:8080 ghcr.io/open-meteo/open-meteo
```

The terrain data (`copernicus_dem90`) is about 10 GB and is a one-off; the ICON-EU model (covers the UK and Ireland) is under 1 GB. `om-sync` keeps the model up to date every 5 minutes. Both containers restart with Docker Desktop.

The local instance serves only what it has synced. `relative_humidity_2m` gives the dew point (`dew_point_2m`) and `snowfall_water_equivalent` gives `snowfall`, both used by the at-home forecast plots. If a variable the app asks for isn't synced, the server fetches just that variable from public Open-Meteo instead (at most every 30 minutes), so nothing breaks, but it uses the public allowance.

**Adding variables to an existing set-up** (e.g. from before 4 Nov 2026, without humidity and snowfall). In an Ubuntu window: remove the sync container, download the new variables once, then start the sync container again with the full list:

```
docker rm -f om-sync
docker run --rm -v open-meteo-data:/app/data ghcr.io/open-meteo/open-meteo sync dwd_icon_eu relative_humidity_2m,snowfall_water_equivalent --past-days 1
docker run -d --name om-sync --restart unless-stopped -v open-meteo-data:/app/data ghcr.io/open-meteo/open-meteo sync dwd_icon_eu temperature_2m,relative_humidity_2m,cloud_cover,weather_code,precipitation,rain,showers,snowfall_water_equivalent,wind_u_component_850hPa,wind_v_component_850hPa --past-days 1 --repeat-interval 5
```

The `open-meteo` container (the one with the port) doesn't need changing. Check (use your own port, the one in `OPEN_METEO_BASE`, if not 8765; a plain 404 means something else is on that port): `http://127.0.0.1:8765/v1/forecast?latitude=50.37&longitude=-4.14&hourly=dew_point_2m,snowfall&models=icon_eu&forecast_days=1` should show numbers for `dew_point_2m`, not `null`.

Check it in a browser — the values should be numbers, not `null`:
`http://127.0.0.1:8765/v1/forecast?latitude=50.37&longitude=-4.14&current=cloud_cover,precipitation&models=icon_eu`

**Point the app at it.** In PowerShell, then close all terminal windows and restart `grid_server.py`:

```
setx OPEN_METEO_BASE "http://localhost:8765/v1"
setx OPEN_METEO_MODELS "icon_eu"
```

`OPEN_METEO_MODELS` is required for a self-hosted instance: it holds only the models you synced, and without a `models=` parameter it picks others and returns all-null values. To go back to public only, remove both settings (`[Environment]::SetEnvironmentVariable("OPEN_METEO_BASE",$null,"User")`, likewise for `OPEN_METEO_MODELS`).

**Choosing a port.** 8765 is only an example; any free port works, as long as the number before the colon in `-p` and the one in `OPEN_METEO_BASE` match (the `:8080` after it is inside the container — leave that alone). Before starting, check the port in PowerShell:

```
netstat -ano | findstr :8765
netsh interface ipv4 show excludedportrange protocol=tcp
```

The first should print nothing (nothing is listening on it). The second lists port ranges Windows has reserved for Hyper-V/WSL — these can't be used even though `netstat` shows them free — so make sure your port isn't inside any range listed. (A browser connecting *to* a `:8080` address doesn't occupy that port on your PC; only a program *listening* on it does.)

**Another program on the same port.** Windows lets two programs listen on the same
port at different local addresses — one on `127.0.0.1`, another on the IPv6 `::1` or
on all addresses. If something else on your PC already uses your chosen port, the app
(which asks for `localhost`) may still reach Open-Meteo by one address while
`127.0.0.1` reaches the other program: it appears to work, but only because of how
`localhost` happens to resolve. That is why the check above uses `127.0.0.1`. If the
check returns a plain *"Error response … 404 … Nothing matches the given URI"* page
instead of Open-Meteo's JSON (numbers, not an error), another program is answering
on that port. `netstat -ano | findstr :8765` lists every listener with its process
ID; move Open-Meteo to a free port as below.

**If the port is blocked — changing it.** Signs: `docker run` fails with "port is already allocated" or "ports are not available", or the footer shows **Open-Meteo: online — local unavailable: unreachable**. To move to another port (8766 here):

1. Remove the container: `docker rm -f open-meteo` (the downloaded weather data is kept — it lives in the `open-meteo-data` volume).
2. Start it on the new port: `docker run -d --name open-meteo --restart unless-stopped -v open-meteo-data:/app/data -p 8766:8080 ghcr.io/open-meteo/open-meteo`
3. Check it: `http://127.0.0.1:8766/v1/forecast?latitude=50.37&longitude=-4.14&current=cloud_cover&models=icon_eu` should show a number, not `null`.
4. Update the app's setting in PowerShell: `setx OPEN_METEO_BASE "http://localhost:8766/v1"`
5. Close all terminal windows, restart `grid_server.py`, and confirm the footer shows **Open-Meteo: local**.

The `om-sync` container uses no port and doesn't need changing. Until the app points at a working port it falls back to the public host, so nothing stops working in the meantime.

**"ports are not available … /forwards/expose returned unexpected status: 500".** Docker Desktop can give this on Windows even when the port is free. What cleared it here: publish the port as `-p 8765:8080` (not `-p 127.0.0.1:8765:8080`) and click OK on the Windows prompt that Docker Desktop then shows. Remove the failed container (`docker rm open-meteo`) before each retry. If Windows Firewall asks whether to allow Docker on your network, you can decline — the app reaches it from the same PC regardless.

**How the fallback works.** With `OPEN_METEO_BASE` set, each Open-Meteo request tries the local instance first (3 s timeout). If it is unreachable, returns an error, or has no data (all values null — e.g. `om-sync` stopped), the same request goes to the public host, and local is skipped for 5 minutes and then retried; it recovers by itself. Public rate limits never block local requests. Local calls are counted as `OM-local` in `api_usage_daily.json`, public ones as `OM`, and each switch is logged in `om_debug.jsonl` (`local_down` / `local_up`).

**Which one is running** is shown in the source status strip at the bottom of the main page: **Open-Meteo: local** or **Open-Meteo: online** (green); amber **Open-Meteo: online — local unavailable: *reason* · retry in N min** when your local instance is down; amber *rate-limited* / *daily limit reached* when the public host is refusing requests.

---

## GB gas supply page

Open with the **gas** button. It shows the current supply and demand across the GB gas system:
- An animated flow diagram (sources → the NTS "spine" → demand and exports), with brightness waves along the connectors.
- Interconnectors that can flow both ways are detected and recoloured according to direction.
- A supply/demand **balance** percentage on the header badge and on the spine label.
- A **48-hour linepack trend** chart (how much gas is "in the pipes"), with National Gas's latest forecast of today's lowest linepack and when it falls.
- An **Alarm levels** strip: each measure the gas alarms watch, outlined blue, amber or red at notice, warning or critical (hover for the levels).

**Gas alarms** (alarm category **Gas supply & margin**, off by default). Gas has its own tones — a softer, lower triangle wave (notice one note, warning two rising, critical three rising) — and every spoken line starts "Gas:".
- **Official notices**, spoken as published: Gas Deficit Warning (warning) and Gas Balancing Notification (critical), and when either is withdrawn.
- **Forecast demand** (National Gas's NTS forecast, tomorrow's once published at about 13:10): 300 mcm notice, 350 warning, 400 critical — announced once per day and level, and critical only if supply is also tight. A peak winter day can exceed 400 mcm (417 in 2017).
- **Demand against expected available supply** — the test a real Margins Notice uses. Available supply is estimated as non-storage supply flowing now (UK shelf, Norway, interconnector imports) plus the published daily deliverability of the LNG terminals and storage sites (about 410 mcm/d in October 2026): 90 % (or under 50 mcm/d spare) notice, 95 % warning, 100 % critical.
- **Linepack**: predicted closing linepack under 5 mcm above National Gas's forecast minimum (warning) or at or below it (critical); live linepack more than 5 mcm below the forecast minimum (warning); predicted closing linepack revised down more than 10 mcm in 3 hours (notice); linepack falling faster than normal for the hour (warning, only alongside another signal).
- **Storage** stock lasting under 5 days at full withdrawal (notice), and the **system average price** at twice its 30-day median or more (notice).

The derived levels are first-pass and are being calibrated over the winter; each must last 2 hours before it speaks, then repeats every 3 hours while it lasts, with a one-off "back to normal" when it clears. The server evaluates them hourly (`/api/gas-probe`) and logs each reading to `gas_probe-YYYY-MM.jsonl`. The header's "gas margin tight" chip (demand over supply by 10 % or more right now) is visual only.

### Gas storage

The switch above the gas page's system flow diagram, **System flow | 🛢 Gas storage**, puts the gas storage view in the diagram's place (`/gas-storage` opens the dashboard there):
- **UK**: the GB storage stock in TWh (with % of capacity), days of gas, LNG tanks and how much storage can deliver. On the system flow diagram, sources with more detail have a small **◂** beside their name; click the name to slide out its column on the left, and again (or **Hide … ▸**) to close. One opens at a time:
  - **Storage ◂** slides out each storage site: its estimated fill, yesterday's injection or withdrawal, live withdrawal now, and how many days it would last at full withdrawal; plus the days of gas.
  - **Isle of Grain, South Hook or Dragon LNG ◂** slides out the LNG tanks (stock for all terminals together, days at today's send-out, the last tanker unloading) and a row per LNG terminal with its live send-out, with Grain split into its two connection points.
  - **St Fergus, Teesside or Bacton ◂** slides out that terminal's sub-terminals as National Gas reports them (St Fergus Shell / NSMP / Mobil, Teesside CATS / PX, Bacton Perenco / Shell / SEAL): each one's live flow and share of the terminal, its line stacked by flow into the terminal's label like the storage sites; a sub-terminal at zero is greyed.
- **Estimated, not measured.** National Gas publishes the GB stock but not each site's. Each site's stock moves by its own published daily flows and must stay between empty and its working capacity (GIE's figure where the site reports to GIE, otherwise Ofgem's GB gas storage table, January 2025), and the sites must add up to the total; a year of flows pins each site to a range, shown "88–92 %" (or "≈21 %" when the range is narrow). Hover a site for its capacity source and GIE's own figure. Hole House Farm has no published capacity, so it shows no %.
- **Rough** is paused as storage (Centrica, 2025) and is not in National Gas's stock, so it is left out of storage; its live flow is shown as "Rough (production)".
- **Days of gas at today's demand** = storage plus LNG tank stock ÷ today's demand, as if every other supply stopped. Storage alone can deliver only part of the demand (shown). Three other "days" figures appear, each named for what it divides by: **days at average use** in the Europe table (stock ÷ average daily use; GB uses its 12-month average NTS demand, which includes exports, so it compares like-for-like with the other countries), and the gas alarm badge **storage at max withdrawal** (stock ÷ the sites' declared maximum withdrawal: how long storage could run flat out, not days of demand).
- **Europe** (needs a free GIE AGSI+ key, entered with **⚙ GIE key** on the page; saved as "GIE" in `powercut_keys.json` with the electricity operator keys and sent only to GIE): **Trend** — the EU % full over 12 months, each of the previous five years as a faded line, their average and range, with the EU's 90 % target window (met any time between 1 October and 1 December, with some flexibility), days of storage and neighbours; **By country** — every country with last year's mark, days of storage, stock and net flow, Europe as a thick bar. Days = stock ÷ the country's average daily use. GIE's UK figures cover only three of the sites, so GB comes from National Gas.

The **balance** is the live supply−demand flow imbalance (total supply minus total demand, in mcm/d) taken directly from the National Gas feed — it is a flow measurement, not a derived rate of change of linepack. The linepack trend (a stock, in mcm) and the balance (a flow) are independent measurements and are not an integral/derivative pair, so short-term movements in one need not match the other. There's no official published "tight" threshold, so any such note is explanatory only.

---

## Power cuts (UK & Ireland)

Open it from the **UK&IRL power cuts** chip or the **live powercuts map** button in the header, or go straight to
**http://localhost:8412/powercuts**. The chip shows the live total number of
customers currently off supply across the UK & Ireland — in bold, and coloured by
severity using the same bands as the page itself (it climbs through yellow, red and
hot-pink as the total rises) — and its tooltip breaks that into planned and
unplanned (customers off and incident counts). Clicking the chip opens the full map
page over the dashboard; the ✕ at the right end of the page's header or **Esc** closes it. The page also
works on its own at the `/powercuts` URL, independently of the dashboard.

The **By operator** panel (and its **countries** view) gives each operator's or country's
customers off with a severity bar. The bars are linear: out of 10,000 customers off while
no one has more, then out of the largest. Each bar is coloured by the same alert bands
as the chip, applied to that row's own total: green below 25,000, yellow from 25,000,
red from 100,000, hot pink from 500,000. The wall's power-cuts panel draws its bars the
same way. Both views use the same columns, so switching keeps the figures and bars in place.

The map draws every distribution network operator's live outages in one place: the
14 GB DNO licence areas (NGED, UKPN, Northern Powergrid, ENWL, SPEN, SSEN) plus
**NIE Networks** (Northern Ireland) and **ESB Networks PowerCheck** (Ireland), all in one style and folded into a single national total. Each incident
is a dot sized and coloured by how many customers it affects; planned and unplanned
cuts are distinguished; and per-operator and per-country breakdowns, history plots
over 6h/24h/7d/30d, and a statistics panel sit alongside. Where an operator
publishes no coordinates its incidents are placed on the postcode-district centroid;
where one publishes no customer counts (SPEN) it is counted by incident rather than
customers-off, and the page says so.

**Feed freshness.** Each operator's feed is polled independently and its state is
shown honestly. A feed's last good reading — including a valid "no outages" reading —
is kept visible for up to **2 hours** after it stops responding, shown as *stale*,
before it is marked *down* and dropped from the national totals. Each region's map
label is coloured to match: **green** live, **amber** stale, **red** down.

**Only live cuts are counted.** Several operators' feeds also carry incidents that are
already over — restored faults (UKPN), completed work (Northern Powergrid), and planned
works left listed after their finish time (ESB, SPEN) — and these are dropped, so they
never reach the map or the totals. A planned outage is treated as finished an hour after
its published end time. Where an operator hasn't confirmed a customer figure yet (UKPN and
Northern Powergrid publish 0 until they do), the cut is shown as *customer count not yet
confirmed* rather than as 0 off, with the predicted figure where one is given. UKPN's own
website shows an "Est." figure that isn't in its open data, so it can't be shown here.

**Overdue cuts.** A cut whose estimated restoration time is more than **7 days** past is
probably closed by the operator but never removed from its feed — but that isn't
confirmed either way. It is drawn as a small grey dot whose tooltip says so, and is left
out of every total and every alert. It is never counted or announced as a restoration;
if the operator updates it, it comes back as a normal cut.

**Map.** Scroll to zoom and drag to pan; double-click resets. Borders, dots and
region labels stay the same size on screen as you zoom, so zooming in separates
nearby incidents rather than enlarging them. Hover over an incident for its details:
operator and region, planned or unplanned, cause, customers off (or postcode sectors
for SPEN), start and estimated restore time (or finish time for planned work), the
operator's status text, postcodes, distance and direction from your location, and
whether it is new, overdue, or its operator is muted. Overlapping incidents are counted
("+2 more here — zoom in to separate"). Times are shown as the operator publishes them.
Clicking a region still opens that operator's own map.

**History plots.** The customers-off and rate plots cover the chosen window (6h, 24h,
7d, 30d). Hover over either for a crosshair and the exact sample at that time — with
**smoothed** on, the customers-off readout is the value in force at that moment and the
rate is the 1-hour rolling figure. **⤢ by operator** opens a pop-up with customers off
stacked by operator (the top edge is the national total); a band stops where that
operator's feed was down, and SPEN has no band because it publishes no customer counts.
A thin strip under each plot marks **grid events** from the dashboard's alert log —
red for critical or severe, amber for warnings (frequency, system risk, margin, N-1,
NESO System Warnings and so on; feed-data notices are left out) — so you can see
whether a rise in cuts lines up with something on the grid. Hover over a mark for its
title. If a window holds more than 40 events only the critical ones are shown, and the
plot says so. Flood warnings are not included, as they are not kept historically.

**API keys.** NGED, UKPN, Northern Powergrid and SSEN need no key, and ESB works out of
the box (a shared default key is built in). Electricity North West, SP Energy Networks and
NIE Networks each need a free OpenDataSoft key, entered via **⚙ feeds / keys** on the page. Northern
Powergrid and UKPN also accept an optional key there: without one they share OpenDataSoft's anonymous
allowance of 5,000 calls a day per internet address, and a free account key gets its own allowance. Keys are stored
server-side in `powercut_keys.json` and are sent only to the operator they belong to.

**Wind vs power cuts (optional).** With `metar.py` and `windcuts.py` installed, the
statistics panel gains a stats/wind switch. It learns, per licence region, how the
rate of *new* unplanned cuts responds to wind — binning cuts by the trailing-max
gust of that region's METAR stations (keyless NOAA Aviation Weather data, so no key
and no daily cap) and accumulating a mean-response curve over time, so an isolated
storm only drives its own region's curve and calm periods set an honest baseline. It
shows a mini response curve with an enlarge-to-full-page view, a region selector, a
faults/customers-off toggle, and a live "given the current gust, expect roughly N…"
prediction. It needs a few weeks of data to mature and says so until then. Like the
power-cuts history, this model accumulates in the background whenever the server is
running — it does not depend on the page being open.

**Power cuts near you.** Using the location you set on the Environment Agency page, the
dashboard watches for power cuts within a chosen radius of it — **5, 10 or 20 km**
(10 km by default), set in the alarm panel. The power-cuts chip tooltip shows how many
are near you and the nearest; on the map your location is marked with the radius drawn
around it, and the By-Operator panel lists the nearby incidents first, nearest first,
with distance, direction and the estimated restoration time. Arm the **Power cut near
you** alarm category to hear them: a new unplanned fault is spoken as a warning ("Power
cut near you: unplanned fault 2.3 kilometres north-east of you, about 450 customers off,
estimated back on at 6:30 pm"), planned works as a quieter notice with their start and
finish times, and each is announced again when it's over. Each incident is announced
only once, even across page reloads. An incident with no coordinates counts if one of
its postcodes is in your postcode district. Muted providers are skipped, and a cut is
only called restored once it has actually left the operator's live feed — never because
a feed went quiet, a provider was muted or the radius changed.

**Monitored postcode areas.** Besides your own area, you can watch up to **10** other
postcode areas — a sector such as `EX23 4` or a full postcode such as `EX23 4AA`. Click
**📍 Monitored areas** at the top of the power-cuts page, enter them in the slots (✕
clears a slot) and **Save**. Each slot can also take an optional **name**, such as "Les":
alerts then speak the name and place ("Les in Plymouth") instead of the postcode. Each entry is checked against postcodes.io when you save, and
its place name is shown beside it (an entry that isn't a real postcode is shown in red
with the reason, and not saved). The list is kept by the server in `powercut_watch.json`,
so it survives restarts and is the same in every browser. While a monitored area has a
cut, the button turns red and shows how many areas are affected.

A sector matches any cut in that sector; a full postcode matches only that postcode — or,
where the operator publishes sectors only (SP Energy Networks), its sector, announced as
*may be affected*. Electricity North West publishes no postcodes and ESB uses Eircodes, so
areas they supply can't be matched.

With the **Monitored postcode areas** alarm category on (it is by default) and the alarms
armed, a new cut or planned works in a monitored area sounds a two-tone siren followed by
speech: "A monitored postcode area has suffered a power cut. The area is PL3 5, Plymouth.
53 customers are without power." — or, with a name, "… This affects Les in Plymouth." The customer figure is left out for SP Energy Networks
(it publishes none) and when the operator hasn't confirmed one yet. Several areas at once
become a single message, and each area is announced again, with a soft tone, when its
power is restored. Muted providers are skipped, and each cut is announced only once, even
across page reloads — unless the area's **Repeat** box is ticked: then the alarm sounds
again (siren, then a spoken reminder with customers off and estimated restoration) every
30 minutes while a cut there continues. Planned works repeat only once they have started.

---

## Environment Agency page (England)

### Reservoirs

A **Reservoirs** section sits at the top of the Environment Agency page, in two rows of badges. Click **▾ detail**, or any local badge, to open the drop-down with charts.

- **Local row**, for the location set on the page:
  - **Drought status** for the EA region: severe drought, drought or prolonged dry weather, as stated in the EA reports. Severe drought is declared for named counties, so it shows only when your location is in one of them.
  - **Region % full** at the end of the last month, with the EA's comparison to normal, for example "37% full · below average". The % is the storage level; "below average" says how that compares with normal for the time of year.
  - On the UK row, England's badge gives "54% full · normal ≈75%": the normal figure is the long-term average implied by the EA's "21% below average".
  - **Water company:** each company's total % full for the region, the reservoir nearest home (within 40 km) and the lowest of the large reservoirs (over 2,000 Ml).
  - **Lower Thames group % full**, when you are within 60 km of central London.
  - The **nearest live reservoir gauge** within 60 km. This is a water level in metres, not % full; the badge shows how far it sits below the typical range or the spillway when the EA gives one.
  - For a location in **Wales** the row shows only "Reservoir data not available in Wales" (and the same for Scotland and Northern Ireland). None of these publish usable % full figures.
- **UK row:** England's storage from the latest weekly EA drought summary, with the gap to the long-term average, then Scotland, Wales and Northern Ireland marked as not available. Hover over any badge for the details and the date the figure applies to.
- **Drop-down:** pick any EA region (North-east, North-west, Central, East, South-east, South-west) or England. Your own region is marked ⌂. Each region shows:
  - monthly % full since January 2024, with England for comparison;
  - drought status for each area;
  - the reservoir paragraph from each EA area's monthly report;
  - cards for the live gauges, with up to 30 days of levels (longer once the server's own daily log builds up).

  Each region also has a card per water company:
  - **Total chart:** the company total, one line per year from January to December (this year bold, last year blue, earlier years faint).
  - **Bars:** every reservoir's % full. A white tick marks the same week last year, and ⌂ marks the one nearest home. Companies with many reservoirs show the 16 largest, with a button for all.
  - **History:** click a reservoir for its full history, year by year.
  - **Stale figures:** values older than 45 days are dimmed and dated, for example Northumbrian's last open-data upload.
  - **Groups:** where a company publishes groups rather than reservoirs (Northumbrian, Essex & Suffolk, United Utilities), these are shown first.

  The England tab adds a bar per company total and a **Data sources** table. It gives the newest value held from each feed and website, how many days old it is, and whether the last check worked.

  England adds the weekly series since July 2025, with the long-term average where the report gives it. South-east adds the Lower Thames and Lower Lee groups for this year, against last year and the 1989–last year range.
- **Live gauges are checked first.** A gauge whose daily level jumps by more than 1 m, or which has not moved for 7 days, is shown dimmed as a suspect sensor and never used in a badge. For example, Meldon jumped 8.6 m on 24 Sep 2026. Single-reading spikes, such as Haweswater's 34.87 m sentinel values, are filtered out first. Flood-storage reservoirs are left out.
- **Sources** (all Open Government Licence):
  - the EA national monthly water situation report (regional table, from the PDF);
  - the EA weekly *Dry weather and drought in England* summaries;
  - the EA area monthly summaries;
  - the EA flood-monitoring API;
  - Thames Water figures via the London Datastore;
  - **Water companies, two kinds of source merged into one store:**
    - **Stream** (streamwaterdata.co.uk, the industry's open data portal, CC BY 4.0) supplies the history in a common format. Coverage: Yorkshire Water (75 reservoirs, weekly since 2022), Southern Water (daily since 2002), Wessex Water (weekly since 2021), Severn Trent (2023–25), Northumbrian incl. Essex & Suffolk (2022–25), South West Water and SES Water (2023 to May 2026), and South Staffs. Some companies keep their Stream data current; others upload in batches months later. A daily catalogue search picks up new layers, such as a new year's file, without a code change.
    - **Company websites** fill the weeks Stream hasn't caught up with: Severn Trent (weekly totals since 2022 and every reservoir), South West Water, Northumbrian, Essex & Suffolk. They also cover companies not on Stream: United Utilities (groups, with average and last-year figures) and Bristol Water.
    - **South West Lakes Trust** (swlakestrust.org.uk) publishes a weekly table of South West Water reservoirs; it is used only for the ones South West Water's own page doesn't list (Argal, College, Crowdy, Siblyback, Upper Tamar, Wistlandpound, Meldon, Fernworthy, Avon), each dated to the table's week-ending date.
    - A reservoir with no reading for over 90 days is left out of its company's list (the card says how many), unless the company has no current readings at all. Blackpool Pit, for example, appears only in South West Water's Stream feed, which stopped at 31 May 2026.
    - **Reconciliation:** for the same reservoir and date, a Stream value replaces a website value.
    - **Company totals:** the company's own published total where it gives one. Otherwise, a capacity-weighted figure from the reservoirs that report capacity, used only for weeks when they cover at least 80% of the capacity.
    - **Storage:** everything read is kept in `reservoir_store.json`, with a daily copy in `reservoir_store.bak.json`. The history therefore survives a feed being withdrawn or a website changing.
    - **Not available:** Anglian, Thames (apart from the London groups), Affinity, South East Water, Portsmouth and Cambridge publish nothing usable.
- **Needs `reservoirs.py`** in the same folder. The monthly regional figures also need the `pypdf` package (`pip install pypdf`). Without it, those badges say so and everything else still works.
- **Refresh:** the server fetches everything in the background, every 6 hours for the reports and hourly for the gauges. The first run after a new install downloads about 30 monthly PDFs (around 5 MB each) once. Results are kept in `reservoir_cache.json`, and one gauge reading per day goes to `reservoir_levels-YYYY-MM.jsonl`. Both are git-ignored. `GET /api/reservoirs?lat=&lon=&country=` returns the whole set.


Open with the **ea** button.
- Enter an England postcode **or place name** (e.g. `SW1A 1AA` or `Sheffield`) to monitor river levels, rainfall and flood status nearby.
- Choose a radius: **20, 40 or 80 km**.
- **Flood alerts** appear in an accordion list (one open at a time; the first is expanded by default) and are also flagged at the very top of the main page. Any warning or alert whose flood area falls within your chosen radius is highlighted as "near you" and floated to the top of the list; the spoken flood alarm also names the nearest local one and its distance.
- Gauges are ordered nearest-first, grouped into distance bands.
- **River level as % of its range.** Each station shows its level as a percentage of its own EA typical range — 0% at the typical low, 100% at the typical high — next to the name in the list (blue below range, green to 80%, amber to 100%, red above) and on its plot, where the range max (100%) and min (0%) are marked as labelled lines.
- **Click a river-level or rainfall gauge** to plot its history. Rainfall is shown as a **mm/h rate** — the raw 15-minute bucket total is converted and kept in the card's hover tooltip — and colour-coded by intensity band (dry / light / moderate / heavy / extremely heavy). **Snow** is drawn in bright pink rather than on the rain scale. EA readings normally reach the dashboard 30–60 minutes after their time (the EA publishes in delayed batches), so a card keeps showing the last reported rate: it **dims** once that reading is over 30 minutes old (the tooltip gives its time and age), and after **75 minutes** shows **—** (not known) rather than 0, which would wrongly read as dry. A gauge's history plot runs through to the current time (a gap shows as empty) instead of freezing on the last reading. A gauge card's **border** additionally holds the highest intensity of the last two hours, so recent rain stays visible after it stops, while the number and fill reflect the current reading.
- **Mute a ratty rain gauge from the alerts.** With a rain gauge selected, the plot header has an **alerts normal · <0.3 · mute** switch. **<0.3** makes the rain alerts treat that gauge's readings under 0.3 mm/h as dry; **mute** leaves it out of the rain alerts altogether. The card and plot keep showing its raw readings. The dot before each gauge's name shows the setting: **green** normal, **amber** <0.3, **red** muted. The setting is kept by the server, so it survives a page refresh and a restart. A gauge that hasn't reported for over 3 hours stays on screen with its name dimmed.
- **Reading age.** River-level readings carry a coloured "…ago" — green up to an hour, amber to four hours, red beyond — so a stale gauge is obvious at a glance.
- **Rain & gauge radar.** The **◎ rain & gauge radar** button at the top right of the plot area swaps the plot for a live radar-style plan of your area from the rain engine: EA gauges coloured by what they are doing (dry, steady, showery, wet; gauges whose data is running late are dashed rings), sea points (from radar, or modelled when radar is unavailable), tracked rain cells with their direction, speed and expected arrival, and your home at the centre, over the **real radar picture** (RainViewer's newest frame, moved on to the current time by the motion the radar itself measures, in weather-app colours; snow coloured by wet-bulb temperature). Cells of 55 dBZ or more are drawn **white as possible hail** — a hint, not a detection, as the free radar has no dual-polarisation data. A line at the top left says in a few words what is happening at home or when rain is due ("Rain ~10:30 · in 14 min", "Dry next 3 h"), counting down to a due time, and mentions hail nearby. A movable bar plot (lower right; ✕ hides it) shows the highest rain rate on the plot over the last **1h / 6h / 24h** from radar and / or gauges. Hover over any marker for its details — a gauge shows its place, grid reference and EA reference, and how far away it is and in which direction from home. Click the button again (or pick a gauge) to go back to the plot.
- **Under the hood.** The **⚙ under the hood** button in the top bar opens the full **Forecast view** in a new browser tab for your location and radius (see *Forecast view* below).
- **Local wind & weather** (below the gauges) shows wind direction and speed, temperature, pressure and sky conditions for your location. Wind, temperature and pressure come from OpenWeather; today's low and high are Open-Meteo's forecast for the whole day (tagged "OM" — a model forecast, not a measurement); cloud cover and the sky description come from Open-Meteo (more reliable for this than OpenWeather's cloud field), with OpenWeather as a fallback if Open-Meteo is unavailable. A small "OM"/"OWM" tag by the Cloud % row shows which source supplied it. If a fresh reading isn't available, the panel shows a "cached" marker with the reading's age rather than presenting old data as current. The **850 hPa wind** (about 1.5 km up — the flow showers actually move with, from the rain engine) is shown alongside: a green dotted arrow on the compass and a green dash on the speed dial, with its direction and speed in green below. Hover for how far it has veered or backed from the surface wind and how old the reading is. It appears only when a real 850 hPa reading is available, faded and marked *(old)* when it is stale.
  Below the dials, two **at-home plots** run from 12 hours ago to 12 hours ahead: **rain / snow** (measured at home by radar and the nearest gauge, then OpenWeather's minute nowcast and the Open-Meteo forecast, drawn lighter) and **temperature and dew point** (logged readings, then the forecast). The same block appears under the wall's Rain radar view.

### Rainfall nowcast (optional — One Call 4.0)

The offshore rainfall watch fills the biggest gap in gauge coverage: the sea.
Real Environment Agency gauges only exist on land, so for a coastal location the
direction weather usually arrives from can be a blind spot. A permanent **net**
of sea points watches that arc — sentinels roughly 40 km out plus inner pickets
around 20 km. They are read from **measured rain radar** (RainViewer's free
past-radar service: 10-minute frames, usually 10–20 minutes behind real time, for
personal and educational use), falling back to the free, keyless Open-Meteo model
when radar is unavailable — neither costs anything against your OpenWeather
budget. They appear on the rainfall map as dashed cards at their true bearing and
distance, marked *RADAR* or *MODEL* by where the reading came from. A model can
miss or misplace a narrow rain band entirely; radar shows where it actually is.

When rain is detected offshore, **mobile tracker cards** (marked *TRACK*) spawn
and follow the cell inward through the 5–35 km band, jumping back now and then to
sense whether heavier, lighter, or no rain is following, and estimating the
front's speed and arrival window from how fast it crosses successive ranges. With
the trackers read **measured radar** too (RainViewer, free); only when radar is
unavailable do they take OpenWeather **One Call 4.0** reads (paid, spent only on real
detections), and without that the free model. When the rain clears the
trackers retreat back offshore and fade, leaving the sentinels watching. Snow is
shown in bright pink throughout, never on the rain scale.

**When your EA rain gauges can't describe the present** — the EA feed is down, or
every gauge's newest reading is more than 75 minutes old — **eight virtual gauges are
placed around your home location** (at 20 km and 10 km on offset compass points) to
stand in, so you still get a working local picture. The EA publishes readings in
batches, normally up to about an hour behind, so only a longer delay counts; once the
gauges fall behind, they count as current again only when the newest reading is under
an hour old, so the backup doesn't flip on and off around each EA batch. The virtual
gauges are read from radar, or the Open-Meteo model if radar is unavailable, or
OpenWeather/OC4 (metered against your OpenWeather budget) only if neither free source
is available. They show on the rain-gauge page with their readings, work even for an
inland location with no sea nearby, and retract when your EA gauges are current again.
Their cards are labelled by source (and never as "sea", since they can sit over land).
An individual gauge more than 75 minutes behind is shown as *late* and is not counted
as dry — a late gauge says nothing about what is happening now.

Alongside this the server runs a background rain-alert assessment — every 5 minutes,
at the location last used on the Environment Agency page or Forecast view, whether or
not a page is open — that combines
your real gauges, OpenWeather's minute-by-minute precipitation forecast for the
next hour, and local pressure and visibility trends — building a picture of what
is happening at your location, what is approaching and from which direction, and
whether it is intensifying or easing. When the measured approach speed looks
reliable the spoken alert can include it ("moving in from the south at around
thirty miles per hour, reaching the coast in thirty to forty-five minutes"); when
it looks wrong — for instance two showers mistaken for one giving an absurd speed
— the figure is withheld rather than risk a misleading number. With a measured speed the
arrival is spoken as a clock time ("around 10:35", "between 10:30 and 10:45"), or as
"in about 15 minutes" / "soon" when it is within half an hour; possible hail in the
approaching rain is mentioned. One rain area gets one approach alert, however many
cells it holds. To hear these as
spoken alerts, enable **Weather nowcast** in the alarms panel and arm sound; until it is switched on the
assessment simply logs what an alert would say (a diagnostic). Like the other
alarm categories it is off by default and speaks only while sound is armed.

Throughout, modelled data is always labelled modelled and radar readings are
labelled radar; neither is counted as a physical gauge reading (honesty over
plausibility). The whole feature is
throttled to your daily OpenWeather call budget, and if the key isn't subscribed
to One Call 4.0 it simply doesn't appear — the standard rainfall panel keeps
working on the free tier.

### Rainfall prediction fine tuning

In October 2026 the approaching-rain alerts were checked against two weeks of recorded
data — radar, EA gauges and the engine's own decisions, logged side by side — using
radar at your location as the referee for when rain actually arrived. The alerts
themselves held up well, but the follow-up messages didn't: with only one EA gauge close
enough to count as "at home", rain that arrived briefly or lightly often went unseen, and
the engine said it had *fizzled out* while it was raining. Two changes followed:

- **Radar counts as arrival.** Rain on a fresh radar frame at your location now ends an
  approach as *arrived*, alongside the nearby gauge.
- **"Fizzled out" only when radar agrees.** The engine only says rain has fizzled out
  when radar shows the direction it was coming from is clear. If rain is still there,
  it says nothing and waits — the approach ends on arrival, or quietly after three hours.

Replayed over the same two weeks:

| | Before | After |
|---|---|---|
| Spoken approach messages that were right | 67% | 89% |
| "Fizzled out" messages that were wrong | 7 of 9 | none |
| Alerts followed by rain at your location | 89% | 89% (unchanged) |
| Rain starts warned beforehand | 43% | 43% (unchanged) |

The remaining errors are alerts for rain that didn't arrive. The rain starts that went
unwarned were mostly light, isolated showers; widening the alert distances to catch them
would have raised more false alarms than it caught. These figures come from two weeks of
autumn weather, so treat them as a guide (each message is worth about 5 points), not a
guarantee.

### Forecast view

**http://localhost:8412/forecast** (or **⚙ under the hood** on the Environment Agency
page) shows the rain engine's full picture: the same radar plan as the rain & gauge radar,
plus the current situation (for example *approaching · moderate · west*), coverage and
wet-gauge counts, how many EA gauges are running late, the steering wind it is using, any gauges it has flagged as
stationary or suspect, each tracked rain cell with its speed and arrival estimate, and
what the rain alerts would say. It uses the location saved on the Environment Agency
page (or the location in the link), refreshes on its own, and **demo** shows an example
scene. Nothing here is a forecast service of its own — it shows exactly what drives the
rain alerts.

Gauges wet for hours in one place are ringed and left out of approach tracking:
**amber** = stationary (the weather model agrees it's drizzling there), **red** = suspect
(the model is dry — possibly a faulty gauge). A tracked cell's speed and heading are measured on the radar
(the cell found again in the next frame), then confirmed by a second source: rain
handing over from gauge to gauge, or the mobile probes. Only when the radar can't
measure it is the wind that steers showers (the ~1.5 km-high "850 hPa" wind) used,
and the cell is then marked as a wind estimate.

---

## My Home (Octopus Energy)

Open with the **my home** button.

**You need:** to be a UK Octopus Energy customer with your API credentials to hand (find them in your online Octopus account). If you haven't entered them yet, a pop-up prompts you. To change them later, click the settings **cog** (⚙). Credentials are stored by the local server in `octopus_config.json` (in the project folder); your API key is sent only to Octopus Energy to fetch your usage, and nowhere else.

What you can enter: your API key, your account number (and a separate gas account number if your gas is on a different account), electricity MPAN/serial, gas MPRN/serial, unit rates and standing charges (p/kWh and p/day), your payment method (Direct Debit or non-Direct Debit), the day your billing period ends, and your gas **units**.

**Gas units (important for correct costs).** Octopus's data doesn't say what unit your gas readings are in — it depends on your meter. SMETS1 meters report gas already converted to kWh; SMETS2 meters report raw volume in cubic metres (m³). The two differ by roughly 11× (each m³ is about 11.22 kWh), so getting this wrong makes your gas cost come out about 11× too high or too low. The **Units** setting in ⚙ lets you tell the app which you have — **kWh**, **m³**, or **Auto-detect**. If you pick m³, the app applies the standard industry conversion (volume × 1.02264 × calorific value ÷ 3.6) and labels the value as converted. Auto-detect is offered but is *not* reliable in every case — a genuinely low-usage month in kWh can look like m³ — so an explicit choice is recommended. The quickest way to confirm: check your Octopus bill; if it quotes gas in "m³ (Units)" and then converts to kWh, choose m³.

**Unit-check flag.** If the app spots that your gas readings look inconsistent with the unit you've set (for example, configured as kWh but the numbers look like m³), a small amber "check unit?" pill appears on the gas cost cards and on the estimated-cost card. Clicking it opens the settings so you can fix the unit. The pill also appears as "confirm unit" when gas is running on a default rather than an explicit choice. It never changes any figure — it only prompts you to verify the setting.

**Live tariff rates (recommended).** If you enter your account number, the app looks up your actual tariff and its real rates directly from Octopus — including how the rate has changed over time — and costs your usage against the rate that applied on each date. This stays accurate through price changes and tariff switches, which matters especially on variable tariffs. The unit rates and standing charges you type are used only as a fallback when a live tariff can't be resolved, and are labelled as such. All rates used are VAT-inclusive.

What it shows:
- **Cost cards** for electricity and gas — standing charge, today, last 7 days and this month (the month card is highlighted; electricity in cyan, gas in gold, combined total in green). Costs include the standing charge and, where live rates are available, are matched to the rate in force on each day.
- **Tariff and Estimated Costs panel** (below the usage patterns) — shows each fuel's tariff name and current unit rate and standing charge (VAT-inclusive), plus an estimated cost for your last complete billing period (set your billing end-day in settings). This is an estimate of energy usage cost, not a bill: actual bills may differ depending on how your payments are spread out, small differences in the exact billing dates, and the natural lag in Octopus delivering the most recent half-hourly readings. Where gas is read in m³, the estimate uses the converted kWh (see *Gas units* above).
- **Usage charts** — switch between **30-min · 48h** and **daily · 2 weeks** views.
- **Consumption patterns** — peak time, always-on baseline, overnight share, weekday vs weekend split, standing-charge share, overall trend, your dearest and cheapest days, and an annual projection.
- Data freshness is shown, and the page refreshes itself while open.

### Energy usage colourgrammes
Open with the **colourgramme** (▦) button inside My Home. This plots up to two years of your usage at 30-minute resolution as a heat-map "carpet".
Controls include:
- **Fuel:** Electricity or Gas.
- **View:** Single month, or Stacked (averaged across several selected months).
- **Months:** multi-select.
- **Colour scale:** Linear or Log₁₀, with Min/Max clip sliders.
- **Gap handling:** fill small gaps, or skip any incomplete day.

Companion charts alongside the carpet:
- **Periodicity spectrum** — which usage cycles dominate.
- **Average day** — your typical daily shape with a 10–90% band.
- **Load-duration curve** — the share of time spent above each usage level.
- **Day of week** — mean daily kWh per weekday.
- **Rolling 24-month view** — two years split into aligned year-rows for easy year-on-year comparison.

---

## Alert system

Built-in monitoring raises alert messages on the main page when data crosses thresholds. Alerts are classified by type, and routine operational/IT notices are shown as neutral blue "NOTICE" items rather than alarms.

- **The alert bar** shows the worst alert, with **show N more ▾** for the rest; with all shown, one card is open at a time (click its title line). Each card's colour follows its severity: red critical, amber warning, blue notice.
- **Gas alerts** sit in the same list with their own tags: **NG** for official National Gas notices (Gas Deficit Warning, Gas Balancing Notification), **GAS** for the gas page's alarm levels (supply margin, closing linepack, linepack below the forecast minimum, closing linepack revised down, high demand forecast, price stress, linepack falling fast), **STORE** for storage (storage at max withdrawal under 5 days; days of gas under 5 / 3 days; EU storage below its 5-year range) and **DATA** for stale storage figures or a rejected GIE key. They use the same first-pass thresholds as the gas alarm levels and never change the electricity status in the header, and they make no sound.
- **Dismiss for 7 days.** An alert open for a day or more (not critical) has **dismiss 7 d**. It then sits behind **N dismissed · show** (with **restore**) and comes back after 7 days, or sooner if it clears and returns or changes level. Dismissed alerts don't count towards the header status. Dismissals are kept by the server in `alert_dismissed.json`.

Audible and spoken alarms can be armed from the alarm panel, with per-category toggles. Two of the categories cover the Environment Agency data (and only work while the EA panel has loaded nearby data):

- **River level high** — nearby gauges above their own normal range (each station's published typical-range high), not the rare "record" level. Fires as one aggregated alarm for the whole set rather than one per gauge.
- **Rainfall nearby** — nearby gauges reporting rain, from light to extremely heavy, with extra hysteresis so stop/start rain doesn't spam.

To avoid a flood of alerts when a whole region is affected, these name up to three locations by distance ("less than 5 miles", or rounded miles); if more than three are active, the nearest three are named followed by "and N other locations near you". Nearest (within 5 miles) is treated as more urgent. The repeat tone sounds at most once per hour, with a spoken situation summary every three hours. A **Clear river / rain alarms** button resets these, and they also clear automatically when you change location.

### Frequency and system-risk alarms (kept separate)
These are two distinct alarm categories, because a frequency limit breach is a real alarm while the risk index is a notification:

- **Frequency limits** — the escalating **tones** fire only when the frequency *itself* leaves the band, in either direction: tier 1 at 49.8 / 50.2 Hz, tier 2 at 49.7 / 50.3 Hz, tier 3 at 49.5 / 50.5 Hz, and on the low side only tier 4 at 49.1 Hz and tier 5 at 48.8 Hz, where automatic demand disconnection begins (each with a small deadband so it doesn't chatter). The deepest under-frequency tier always takes priority. Tiers 3–5 use a siren that gets faster with each tier. Each burst also speaks the reading ("grid frequency high/low …", "very low" at tiers 4–5, led by Warning / Critical / Severe / Emergency). The spoken value is the worst point of the whole excursion, held from when it began until it has cleared, with the current reading after it ("dipped to 49.70 hertz, now 49.78"). At tier 5 it adds "Automatic demand disconnection may begin soon." To avoid alarm fatigue on a persistent excursion the sound is **capped and spaced**, not continuous: tier 1 up to 10 s every 2 min, tier 2 up to 15 s every 2 min, tier 3 up to 15 s every minute, tier 4 up to 15 s every 45 s, tier 5 up to 20 s every 30 s. After 4 minutes it winds down to up to 5 s every 15 min (every 2 min at tiers 4–5, so it never goes quiet). A worsening excursion restarts the cadence; if it stays out and isn't recovering, a one-shot spoken warning is given (possible power cuts when low, generation tripping when high). The alarm reads the full-resolution 15-second data, so it catches brief excursions the on-screen trace might smooth over.
- **System risk index** — when the composite risk level rises (low inertia / high CGRI) or a **sudden infeed loss** is detected, the app plays a single short **pip** and shows the on-screen banner (the alert card and the panel tint) — deliberately **not** the escalating tones. It's an "elevated resilience risk" notification, not a frequency alarm.
- **Sustained statutory breach** — separate from the instantaneous crossing above, a distinct critical alert is raised once frequency has stayed beyond a statutory limit (49.5 / 50.5 Hz) continuously for more than a minute (e.g. "Grid frequency has stayed below 49.5 Hz for 1m 35s …"). It updates as the breach persists and is recorded as a single episode, with its total duration, in the alert history.

In the alert list the two are tagged separately (**FREQ** for frequency-limit breaches, **RISK** for the composite risk and infeed-loss items), and each has its own on/off toggle in the alarm panel.

### Active forecast window
In the alarm panel you can set a start and end time (local) to **concentrate the paid API budget** in the hours that matter to you. Inside the window the offshore rain model, land probes and wind reading sample at full cadence; outside it, that sampling is stretched by a "quiet" factor (the radar net, which is free, is never stretched — it reads every 10-minute frame around the clock) (you choose it) so the daily weather-API quota isn't spent overnight — the alarms themselves still run. The setting is remembered across restarts and may wrap past midnight. Separately, the offshore watch is metered honestly against the free weather tiers and, if a provider reports its daily limit reached, it backs off and leans on the fallback rather than hammering an empty quota.

Click the **history** button (top right) to open the alert history and statistics view. Choose a window — **24 h, 7 d, 30 d, or all** — to see how often each type of alert has fired and how long they typically lasted. History is kept for up to 30 days.

---

## Troubleshooting / first run

**The page says "No data — backend unreachable" (red banner, red pulsing dot).**
This is the most common first-run issue and almost always means one of two things:

1. **`grid_server.py` isn't running.** The dashboard is only a display; all live data comes from the Python backend. Start it by running `grid_server.py` (a Python 3 console window should stay open while you use the dashboard). If that window has closed or shows an error, the page has nothing to talk to.
2. **You opened the HTML file directly.** Double-clicking `grid_dashboard.html` opens it as a `file://…` page, and the data requests won't reach the server. Always open the dashboard at **http://localhost:8412** in your browser instead.

The page retries every 60 seconds, so once the server is running and you're on the right address, it recovers on its own — no need to reload.

If the console says **GB Energy Monitor is on port 84xx**, another program had port 8412: use the address it shows (see *Running it*). On Windows a port can also be *blocked* rather than in use — Hyper-V, WSL and Docker reserve ranges of ports — and the console says which.

**Nothing happens when I run `grid_server.py`, or I get an error in the console.**
- Make sure **Python 3** is installed and on your system path. Test with `python --version` (or `python3 --version`) in a terminal — it should report Python 3.10 or newer (the project is developed against 3.13).
- If Windows opens the file in an editor instead of running it, run it from a terminal: `python grid_server.py` from inside the project folder.
- Errors like `SyntaxError` usually mean an older Python version is being used — check the version as above.

**The page loads but a panel is blank or a source shows "failed".**
Individual data feeds (grid, gas, flood, weather) come from separate public services and can occasionally be slow or unavailable. The dashboard flags a failed feed in the status strip at the bottom rather than blanking the whole page, and retries automatically. A single failing feed doesn't mean the app is broken.

**The Environment Agency (rivers / rainfall) calls keep timing out.**
The EA real-time flood-monitoring API is a public Beta service that is occasionally slow or unresponsive for everyone — its calls can time out server-side regardless of your connection. The dashboard keeps the last good data, flags "retrying automatically", and recovers on its own when the API responds again; a single bad cycle is not a fault in the app. While the flood feed isn't responding, the flood chip keeps its last count with a **\*** in its alert colour (the EA page says "Backend unreadable for N minutes"), and the flood alarm doesn't treat the outage as the flooding having ended — so a standing warning isn't announced again when EA comes back. A flood episode only counts as over after 30 minutes with none in force. To check whether it's the EA API rather than your machine, open `https://environment.data.gov.uk/flood-monitoring/id/floods` in a browser — if that hangs or errors, the API itself is down.

**A power-cuts operator shows "stale" or "down".**
Each operator feed is independent and public, and some go quiet from time to time (ESB in particular). A feed's last good data stays on the map as *stale* for up to two hours before it's marked *down* and dropped from the totals, and the region's map label is coloured green/amber/red to match — so a brief outage of one feed doesn't blank the map or the total. NIE Networks needs your own API key (entered via the ⚙ on the page); without it that region simply reads "key needed".

**The offshore rain watch shows "Open-Meteo daily limit exceeded".**
See *A note on weather API limits (shared IPs / VPNs)* above — this is almost always a shared/VPN/CGNAT public IP hitting Open-Meteo's per-IP cap, not your own usage or a fault in the app. The offshore watch keeps working on the OpenWeather (One Call 4.0) fallback meanwhile, reading a few sea points per sample — those nearest the wind's direction first.

**The weather panel asks for a key / My Home asks for credentials.**
Those features need their own credentials (OpenWeather for weather; Octopus Energy for My Home). See the relevant sections above. Both are optional — the core grid, gas and environment pages work without them.

---

## Transmission wall (`/wall`)

A control-room style page: the GB transmission network at true scale with modelled live flows. Open it from the dashboard's **▦ wall** button or at `http://localhost:8412/wall`. The dashboard's **🗺 network** button opens the same zoomable map as a full-page pop-up (✕ or Esc to close). The pop-up is capped at 1,320 px wide like the other pop-ups; on a phone or narrow window the substation panel moves under the map and the page scrolls.

- **Layouts.** Each window picks its own (press **M**, or add `?screen=` to the address): `hd` 1920×1080, `uw` 5120×1440, `d1` + `d2` for an ultrawide plus an HD screen, `h1` + `h2` for two HD screens. **F** toggles full screen. On screen 1 of a pair, **▶ start screen 2** opens the other layout full screen on your second display (Chrome/Edge ask permission once). `start_wall.bat` opens both as app windows; set each monitor's position and size at the top of the file.
- **Title-bar readouts (5120 × 1440 single screen).** Demand, price, carbon intensity, low-carbon share, gas linepack and gas demand, in bold figures that count to each new value, each with its change over the last hour (▲ / ▼, green or amber where the direction is good or bad).
- **What is measured and what is modelled.** Unit outputs are published by Elexon: each unit's Physical Notification, or its NESO-accepted level where it has been instructed (shown "NESO"). Interconnector flows are metered (FUELINST). **Line flows are a model** (DC load flow in the browser), because measured per-circuit flows are not public; demand is spread by region, with Scotland's share of it estimated each half-hour from NESO's metered Scotland / England & Wales split (its Demand Data Update; the split is published up to a day late, so the share now is fitted from the last weeks of actuals by time of day, weekday or weekend and forecast embedded wind and solar); internal HVDC flows are estimates. The Scotland → England transfer panel shows the demand estimate, its error on the latest day of actuals, and how the modelled transfer has compared with NESO's metered transfer (the page reports each run's figure to the server, which checks it once NESO publishes). NESO's boundary constraint figures are day-ahead forecasts, not the flow now. Embedded (distribution-connected) wind and solar is placed where it is: NESO's national estimate for the half-hour is shared over the nodes by the wind and solar capacity under each one in the operators' Embedded Capacity Registers (only the registered part of NESO's capacity; the rest, mostly rooftop solar, stays spread with demand), injected there and added to its region's demand. Where the units' notified levels don't add up to Elexon's metered output for a fuel (FUELINST; mostly grid batteries, which don't follow their notifications in frequency services), the difference is spread over that fuel's units by capacity (batteries, pumped storage) or by output (the rest), within each unit's capacity, and added at their nodes; the units' published levels shown in the panels are unchanged. At NGED's grid supply points (South West, South Wales, Midlands) the model uses the measured import from NGED's live 5-minute data instead of a share of demand, where the reading is current and the node serves only NGED's area; each measured node's substation panel shows it, with what the model would have put there, and the server scores those estimates against the measurements over the last day. Circuit ratings come from NESO's Electricity Ten Year Statement (Appendix B, the newest one, found and downloaded by `etys.py`): each circuit's seasonal rating where it can be matched to the map (by name, by connections, through tees; about 56 % of the 400 and 275 kV circuits), otherwise the standard rating for its voltage, marked * in the substation panel. **Radar picture.** The rain radar view draws RainViewer's actual radar (about 10–20 minutes old) under its gauges at up to 75 % opacity, coloured by rain rate with a key (rain green to red, sleet pink, snow blue), and moves it on to the current time using the rain's own movement between the last two radar frames (or the 850 hPa wind when there isn't enough rain to measure it); click the key to see the frame as measured. Snow and sleet get their own colours when the wet-bulb temperature at home is at or below 1 °C (snow) or 2 °C (sleet). **Rain radar and local weather.** The regional panel's **Rain radar** view shows the rain radar (round, whatever the panel's shape) with the Rivers & floods page's local weather block below it: wind dials, gusts, conditions and pressure, for the location set on that page. **Counting numbers.** When a refresh changes a number in the wall's panels (MW, GW, % of supply, price), it counts from the old figure to the new one over about a minute, as the circuit values on the maps do, with a tinted background while it counts (blue to green rising, yellow to orange to red falling, stronger for a bigger change). Power cuts figures don't count. **Power cuts panel.** The 5120 single-screen layout has a **Power cuts · UK & Ireland** panel: customers off now (all cuts, and unplanned and planned), new and cleared cuts in the last hour, each operator with all its customers off and incidents as the power-cuts page counts them, how many are planned, and its feed's state, and the biggest unplanned cuts. The full map is the dashboard's power-cuts page. **No line-overload alerts.** The wall doesn't raise alerts from modelled line loading. NESO keeps lines well below their ratings so they can survive a fault, so a line the model shows near its rating is more likely a model or rating error than a real overload (tried and removed on 30 October). **ETYS boundaries and constraints.** NESO's ETYS study boundaries (B0–B17 and local ones, from its shapefile) are drawn as faint dashed lines on the ETYS boundaries layer. Hover over one for the modelled transfer across it; its limit, which is NESO's day-ahead limit where a NESO constraint group covers about the same boundary (SCOTEX for B6, SSE_SP2 for B4) and otherwise the present capability in ETYS 2025, a planning figure; and how much generation NESO is turning down on its exporting side for system reasons. That last figure is measured: the units' notified levels less their accepted levels, for instructions Elexon flags SO (system reasons, mostly constraints). A boundary turns **orange** when at least 200 MW is turned down behind it and the modelled transfer is at 90 % of its limit or more. The limit check picks out which of several nested boundaries the turn-down is for. Being constrained is normal operation (NESO holds the boundary at its limit and pays for it), so it isn't an alarm. The same turn-down shows as an orange **Constraint** badge in the wall's header bar (and the network map's), beside the **updated at** time and the dashboard's other header chips (Grid status, UK & IRL power cuts, gas margin, flood warnings; shorter labels on 1920-wide screens), which is when the wall's unit outputs and interconnector flows were last updated (about once a minute; marked STALE after 4 minutes). Which side of a boundary each substation is on comes from its position: the boundary line is carried out to sea from both ends and closed far offshore, and a boundary that nearly closes on itself (London, the Midlands, the local pockets such as SW1 or NW2) is closed straight across its mouth and read as inward or outward. For a pocket, turn-down only counts when the pocket is exporting. Everything modelled is labelled.
- **Getting back.** Esc clears the selected substation (so do "✕ clear selection" in the substation panel and a click on empty map).
- **Regional map.** Pick a preset, or scroll to zoom, drag to move and double-click to zoom in (+/− buttons too). Selecting a substation anywhere (another map, the substation list or its search, the other screen) zooms the regional map in to it, or centres it if the map is already closer in; a click on the regional map itself leaves it where it is unless "Follow selection" is on, which keeps the selected substation centred. On the ultrawide, dual screen 1 and dual-HD screen 1 layouts its **⇤ two panels** button spreads it over the map panel beside it (**⇥ one panel** brings it back; remembered per layout).
- **Gas sources view.** The regional panel's **Gas sources** source shows the gas system flow diagram. On the ultrawide, dual screen 1 and dual-HD screen 1 layouts it spreads over the map panel beside it; **⇥ one panel** keeps it in its own slot (remembered per layout). Spread over two panels it has one **◂ Expand all / Collapse ▸** button instead of clicking each source: expanded, every source column is open at once (St Fergus, Teesside, storage, Bacton and LNG), stacked down the left level with its source, and the drawing grows taller to fit the panel; the choice is remembered. In one panel it opens with the storage sites out and each source opens on its own, as on the dashboard. The single-HD layout has no regional panel, so its map panel has the same source buttons.
- **Gas panels either side.** While Gas sources spans two panels, the columns either side show gas panels instead of their usual ones (they come back when the view changes): on the left **Gas system now** (supply, demand, supply − demand, linepack, the closing-linepack forecast against National Gas's minimum and tomorrow's demand forecast), **Gas alarm levels** (the gas page's badges, coloured by level), **Linepack · last 48 h** (with the forecast minimum) and **Gas price · SAP**; on the right **UK gas storage** (stock, days of gas, filling or withdrawing now, each site's estimated fill), **Europe gas storage** (EU % full against its 5-year range, with the Netherlands, Belgium, France and Germany), **LNG** (tank stock, send-out per terminal, last cargo) and **Gas for power** (power-station gas against CCGT + OCGT output, with the implied efficiency as a cross-check between the two feeds). Where a column can't fit all its panels at a useful size, or there is only one column (dual-HD screen 1), buttons at the top choose which are shown (remembered per layout). Gas figures refresh every 2 minutes.
- **Regional panel source.** The buttons in its title switch it between the network detail map and the power-cuts map, gas page, rivers & floods page, rain radar, generators treemap, 7-day generation view or My home energy page. Each page keeps running once opened, and each layout remembers its choice.
- **Reading it.** Each map has a key in a strip below it (the key button hides it and the map grows back). Line colour = voltage; line width grows with modelled MW (2 px per GW); moving dashes = modelled flow direction on every circuit carrying 5 MW or more (faint below 40 MW; faster and brighter = more MW). Zoomed in, each circuit carrying 5 MW or more shows its modelled MW. A circuit value with a tinted background changed at the last model run (about once a minute) by at least 10 MW and 10 %, and shows the direction and amount (▲ / ▼, ⇄ reversed): blue to green rising, yellow to orange to red falling, stronger for a bigger change; it stays until the next run. When a run lands, the maps don't jump: every circuit's width, dashes and value, the offshore lines, the HVDC estimates, the interconnectors and the Scotland → England transfer move smoothly from what was shown to the new figures over most of the time until the next run (80 % of the gap since the last change, 20–90 s, slowing as it arrives), and while a changed value is counting its ▲ / ▼ / ⇄ mark flashes once a second. Hover cards and panels show the new figures at once. With reduced motion set, values change in one step. Values never overlap (some are left out where it's busy) and fade as the map zooms out. Circles = generation at a substation now (area ∝ MW, colour = main fuel). An arc travelling round a circle shows the last 15 minutes: green and clockwise while rising, orange and anticlockwise while falling; a longer arc is a bigger change, a faster one a quicker change (shown from 50 MW and 10 %). Interconnectors are drawn along their cable routes, width proportional to MW (2 px per GW), blue importing and orange exporting, with dashes moving in or out and a label at the far end; the same arc sits at the landing point; ⇄ = direction change scheduled within the hour. The Western Link and Caithness–Moray HVDC lines (dotted amber, estimates) also widen with their estimated MW. The Scotland → England transfer is labelled once, near the middle of the border. Corner brackets mark the selected substation.
- **Local networks (DNO registers).** `dno_data.py` reads the six distribution network operators' Embedded Capacity Registers weekly: each connected generation or storage site of 1 MW or more, with its technology, capacity, connection voltage and the grid supply point and bulk supply point it is under. UK Power Networks, SP Energy Networks, Northern Powergrid and Electricity North West need the account API keys you enter on the power-cuts page (the same keys); NGED and SSEN need none. Each register's grid supply points are matched to network nodes by name and position. Selecting a substation then shows the embedded generation under it (by technology, its bulk supply points and the largest sites), with the larger generator sites within 8 km; generator-site hover cards say where the site connects. The Data sources panel shows each register's status.
- **Local network lines.** The operators' own maps of their 132, 66 and 33 kV lines are drawn where available: UK Power Networks (overhead and underground), Northern Powergrid (overhead and underground), Electricity North West (overhead) and SSEN (overhead), refreshed monthly. National Grid Electricity Distribution's maps can't be fetched automatically: download its 132 / 66 / 33 kV shapefiles from its data portal (dataportal2.nationalgrid.co.uk) and put the .zip files in a `dno_lines` folder next to `grid_server.py` (file names with "ug" or "underground" are taken as cables). SP Energy Networks publishes no line maps. The 132 kV lines share the **132 kV dist.** layer (where an operator's map covers OpenStreetMap's line, the operator's is used); **66 kV** and **33 kV** are their own layers, fading in further in. Dashed = underground. Hover over a line for its voltage, operator and circuit. Display only: none of these are in the flow model. UK Power Networks' and Northern Powergrid's underground cable maps come under their shared-data licences, so they stay in your local cache only.
- **Live local circuits.** In Northern Powergrid's area, the substation panel lists the live readings of the site's 132 kV circuits (measured, refreshed every 5 minutes).
- **Substation panel.** The search box at its top finds substations by name as you type (generating ones first); click a match or press Enter to select it, Esc to clear. With nothing selected it lists every named substation generating now, biggest first; the panel scrolls when there is more than fits (and keeps its place as it refreshes). Besides the units (MW now / registered capacity) and outages, each circuit shows its modelled flow and its loading as a percentage of its rating (amber from 70 %, red from 90 %).
- **Offshore wind connections.** Most offshore farms' export cables aren't in the network file, so each farm without one is drawn with a straight line (offshore purple; not the cable's route) to its connection point: from NESO's TEC register, the farm's or its offshore transmission owner's published information, or the platform at the end of its mapped export cable. Smaller farms that land on the local 132/33 kV network are drawn to that landing and on to the nearest network node (never another wind farm's own substation); a farm with no published connection goes to the nearest onshore substation. The connections were audited on 2026-10-29 against NESO's TEC register, Ofgem and the projects' own information (see the CHANGELOG). Where a mapped 132 kV export cable starts at the farm, it is followed to where it ends; the straight line then starts there (and isn't drawn at all if the cable already reaches the connection). Where a mapped cable leaves the connection and ends at sea short of the farm (mapped only part way), the straight line runs from the farm just to that end (Greater Gabbard and Galloper at Sizewell, for example). A farm that lands on the local network beside where another farm's line already starts stops at its landing (Gunfleet Sands 3 at Clacton, beside Gunfleet Sands). The line shows the farm's own published output (its units' notified or NESO-instructed levels) as dashes and an MW value, and the farm's units are counted at that connection in the flow model. Hover over a line for its connection and source. The connection points are listed in `OFFSHORE_LINKS` in `wall.py`; a newly built farm needs adding there.
- **Map layers.** Detail fades in as a map zooms in. With all of GB in view only the 400 kV network is drawn, plus any circuit or offshore connection carrying 500 MW or more (so a big offshore farm's link to shore always shows); zooming in brings in the 275 kV and offshore circuits, then Scotland's 132 kV transmission, then the 132 kV distribution lines in England, Wales and Northern Ireland (thin olive lines, detail only, not in the flow model). The regional presets are close enough to show them all. Substations and their names appear with their highest voltage's layer. Generation circles, interconnectors and the HVDC links are always shown. The **generators** layer adds about 3,600 generator sites from OpenStreetMap (via UK Grid Atlas, refreshed weekly) as faint dots under the network, area by capacity and colour by fuel; zoomed right out only sites of 500 MW or more are drawn, and smaller ones fade in as you zoom in (a quarter of the size at twice the zoom). Zoomed in (from about a regional preset), sites are named in the space the substation names and circuit values leave, biggest first, up to 120 in view; smaller sites are named as you zoom in further. Hover over a dot for its name, capacity, operator and start date, and its record in DESNZ's Renewable Energy Planning Database (REPD, quarterly, Open Government Licence): whether it is operational or still under construction, and REPD's capacity where OpenStreetMap has none or the two differ. The **REPD** layer adds, as rings, the operational REPD projects that match no OpenStreetMap site. The **layers** buttons at the start of each map's key strip (400 kV, 275 kV · offshore, 132 kV, 132 kV dist., generators, REPD) switch a layer off at every zoom, or back on to fade in as before; all start on, and each layout and map remembers its own.
- **System frequency panel.** The wall shows the dashboard's own System frequency panel (value, deviation dial, time-at-frequency histogram, system risk and 2-hour trace) through its embedded view, `/grid_dashboard.html#embed=freq`, scaled to fit. It is on screen 2 of both dual layouts and at the top left of the ultrawide single layout.
- **Outages (reported).** Operators' REMIT unavailability reports, fetched from Elexon every 5 minutes, for units and interconnectors reduced now or within 24 hours. A dashed red ring marks a substation with a reduced unit (details in its tooltip and substation panel); interconnector labels show reduced capacity ("cap 1,000/2,000"); the **Outages** panel (screen 2, and the ultrawide single layout) lists them biggest first. These are the operators' own reports, not estimates. Closures and reports running over 6 months are left out.
- **Substation names.** Names come from OpenStreetMap. About 160 nodes have no name there, so the server looks around each one in OpenStreetMap in the background (a few minutes after start-up, then monthly) and borrows one: a named substation of 132 kV or more within 0.5 km, or a power station within 1 km, becomes its name; a substation or station up to 2–2.5 km away is shown as "near …"; otherwise the name of the largest generator connected there is used, and failing that the node gets a location ("Unnamed 275 kV node near Dunbar", "offshore, 30 km E of Peterhead"). The hover card and substation panel say where each name came from. A few grid substations that OpenStreetMap's network record names after a wind farm's substation beside them are given their own names (Sizewell, Heysham, New Deer, Crystal Rig; `NODE_RENAME` in `wall.py`), with the OSM name in the hover card. Results are kept in `node_names.json`; `node_names_seed.json` gives a new install names straight away. If OpenStreetMap is busy it simply tries again later.
- **Unit positions.** Each unit is counted at the substation nearest to it. Positions come from Terravolt's unit list, corrected and filled from OpenStreetMap where UK Grid Atlas (github.com/jacobwright32/uk-grid-atlas) maps the unit to an OSM site (`osm_sites.py`, refreshed weekly): a unit with no Terravolt position gets the OSM one, and one more than 1 km out is moved to it. Because the atlas matches by name, a move of over 20 km is made only when the fuel agrees and the names share a word; otherwise Terravolt's position is kept. Hover over a unit's name in the substation panel to see which position is used. Without `osm_sites.py` the wall uses Terravolt's positions alone.
- **Data files.** `grid_network.json` (built by `build_network.py`, rarely re-run) and `node_names_seed.json` must sit next to `grid_server.py`.

## Other files in this repository

- `wall.py`, `osm_sites.py`, `dno_data.py`, `etys.py`, `grid_wall.html`, `build_network.py`, `grid_network.json`, `node_names_seed.json`, `start_wall.bat` — the transmission wall; see *Transmission wall*.
- `balancing.py` — the balancing-costs module (NESO and Elexon fetchers, estimates, events); see *Balancing costs*.
- `gas_storage.py` — the gas storage module; see *Gas storage*. (`gas_storage.html`, the old pop-up page, is no longer used.)
- `CHANGELOG.md` — what changed and when, with each file's own build history at the end.
- `WEATHER_ALERT_DESIGN.md` — how the rain engine works: gauges, sea points, tracking, the approach alert and its tuning values.
- `KNOWN_ISSUES.md` — known issues and open questions.
- `LICENSE` — MIT licence.

---

*README build 261105.1*
