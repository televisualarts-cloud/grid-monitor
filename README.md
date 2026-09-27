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
  `forecast_view.html` is the Forecast view page (the gauge radar and "under the
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

  `gas_probe.py` is an optional companion too: it watches National Gas's published
  notices and a derived gas-margin signal and logs what a gas alert would say (a
  diagnostic — it doesn't sound an alarm of its own). Put it in the same folder to
  enable it.

  `powercuts.py` aggregates the live outage feeds and `powercuts_page.html` is its
  map page; `metar.py` and `windcuts.py` add the optional wind vs power-cuts
  correlation and need `region_polys.json` beside them. As with the rain modules,
  the server runs fine without any of these — the power-cuts chip and page simply
  don't appear — so they're optional, but the full install is all of them together
  in the one folder.

### Running it
1. Run `grid_server.py`.
2. Open a browser and go to **http://localhost:8412**.

The page refreshes itself roughly every 60 seconds, so you can leave it open.

### Updating an existing install
When you replace any of the files with a newer version — `grid_server.py`,
`rain_probe.py`, `owm_onecall.py`, `gas_probe.py`, `powercuts.py`, `metar.py`, `windcuts.py`, or
`grid_dashboard.html` / `powercuts_page.html` / `forecast_view.html` — **stop and restart `grid_server.py`**
afterwards. The server loads the Python modules once at startup, so changes to any
of them (including the companion modules) only take effect on a restart; a browser
refresh alone is not enough. After restarting, reload the page.

### Files created automatically
Once running, the server writes these into the same folder as needed:
- `bmu_locations.json`, `bmu_registry.json` — power station / unit reference data.
- `margin_history.json` — a rolling log of the capacity margin so it can be graphed over time.
- `alert_history.json` — a log of alerts, kept for up to 30 days.
- A `logs/` folder — the interleaved 15-second data log (`grid_log-YYYY-MM-DD.jsonl`, frequency points and generation-mix rows, pruned after 30 days) and, under `logs/archive/`, the permanent weekly generation files that never expire.
- `forecast_window.json` — your active-forecast-window setting (see *Alert system*).
- `api_usage_daily.json` and `api_calls.jsonl` — the per-UTC-day tally of upstream weather calls by source and purpose, plus a rolling event log of each real call.
- `om_debug.jsonl` — one line per Open-Meteo HTTP attempt (outcome, HTTP status, exact rejection text, and any rate-limit / Retry-After headers). This is the evidence trail for diagnosing an "Open-Meteo down" report: it shows which limit (per minute, hour or day) Open-Meteo said was hit — or whether a "daily / try again tomorrow" reply came despite a low real call count, which points to a shared/CGNAT IP rather than this app's usage.
- `rain_episodes-YYYY-MM.jsonl` and `rain_threats-YYYY-MM.jsonl` — the rain engine's record of each approaching-rain episode (alert, confirm, arrival or fizzle) and of every approach it weighed, alerted or not, used to tune the rain alerts.
- `gas_probe-YYYY-MM.jsonl` (when `gas_probe.py` is installed) — the gas probe's readings and the phrase it would have spoken.
- `ring_sample_ts.json` — the last time the OpenWeather backup gauges (see *weather API limits*) were sampled, so their sampling interval survives a server restart and doesn't re-spend the quota on every restart.
- A `captures/` folder — PNG images you save from the frequency-history viewer's **⭳ save** button.
- Power-cuts data (when the power-cuts files are installed): `powercut_keys.json` — the operator API keys you enter on the page (git-ignored; sent only to the operator they belong to); `powercuts-YYYY-MM.jsonl` — the outage history; `powercut_last_ids.json` — the incident IDs seen last poll, for counting new and cleared cuts; and, for the wind-correlation model, `windcuts_binstats.json` and `windcuts_state.json` plus a `windcuts-YYYY-MM.jsonl` audit log. These are written by the server's background sampler whether or not the power-cuts page is open.

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
- **Summary chips (top centre):** quick-glance pills for electricity, gas, floods and UK & Ireland power cuts. The electricity chip is always shown; the gas and flood chips only appear when there's something worth flagging; the power-cuts chip shows the live UK & Ireland customers-off total (bold, coloured by severity, with a planned/unplanned split in its tooltip) whenever the power-cuts feed is up. Click a chip to jump to the relevant page. (The gas chip is a *derived* signal, not an official National Grid Margins Notice — the tooltip says so.)
- **Buttons (top right), left to right:** **live powercuts map** (opens the UK & Ireland power-cuts map — the same as clicking the power-cuts chip), **alarms**, **gas**, **ea** (Environment Agency), **my home**, and **history**, which opens the alert history and statistics view (see *Alert system* below).

Press **Esc** to close any of the full-screen pages (generators, gas, power cuts, EA, My Home, colourgramme, alert history).

---

## National grid data panels

- **System Status** — "System Nominal", or alerts/alarms when relevant.
- **System Frequency** — the current grid frequency, usually 1–2 minutes old, with a recent trace. The trace is drawn with min/max-envelope downsampling, so a brief dip below 49.8 Hz (or a spike) still shows on the graph rather than being stepped over. If the reading goes stale, the trace dims and a note explains why, rather than showing a misleading value. The panel is **tinted by the frequency's own state** — green in the normal band (49.8–50.2 Hz), amber outside it, red beyond the statutory limits (49.5/50.5 Hz), for both under- and over-frequency. The background follows the live reading; the border holds the highest level reached for 10 minutes after it clears, so a brief excursion stays visible.
  - **⤢ history button** — opens a full-window, scrollable **frequency history** built from the logged 15-second data (up to the ~30-day log retention). Drag to scroll, or use the week / zoom buttons and arrow keys; a **● live** toggle keeps the latest data in view at the 15-second rate without changing the zoom. Hover to read the value at any point, and **click to lock** the readout in place (it's drawn on the plot, so a saved image includes it). **⭳ save** writes a PNG of the current view to a `captures/` folder next to the server. The line is recoloured amber/red exactly where it crosses the operational/statutory limits, and each day is marked at 00:00 on the axis, in UT.
  - **System Risk** (sub-panel, top-right of the frequency panel) — a composite read of how resilient the grid is *right now*: a **system inertia** in GVA·s (stored kinetic energy from synchronous plant only — wind, solar and interconnectors contribute none), a notional **RoCoF** ("if largest trips") estimating how fast frequency would fall if the biggest infeed tripped, and a **CGRI** (Composite Grid Risk Index) rolling frequency deviation, inertia and RoCoF into one figure. A level badge (green/amber/red) uses fast-attack / slow-release hysteresis so it doesn't flap, and the sub-panel is tinted by that level (with the same 10-minute border hold) — separately from the frequency panel, because the risk index is a *notification* about resilience, not a frequency alarm. Each figure has a tooltip. The inertia estimate is **calibrated against NESO's published GB Outturn Inertia** (least-squares fit over April–July 2026, R² 0.90, mean error ~8 GVA·s), so a typical mix reads green and amber appears only in genuinely low-inertia conditions matching NESO's real ~110 GVA·s summer floor. This is still a derived, educational indicator — not an official system-operator signal.
- **National Demand** — current demand, plus an *estimated* national consumption figure (labelled "est").
- **Carbon Intensity** — grams of CO₂ per kWh, with the current index band (e.g. low/moderate/high) and the trend versus an hour ago.
- **Generation and Imports by Source** — a bar chart sorted from biggest contributor down (very small ones are omitted). Each source shows a trend arrow versus an hour ago, and estimated figures carry an "est" tag. A legend shows the renewable and low-carbon percentages. The interconnectors are drawn in a graded blue family so each one is distinguishable when stacked.
  - **generators button** — opens a full-screen "tree-map" style view of all the major generating units currently feeding the grid.
  - **7-day button** — opens a generation-history pop-up. Two live plots cover the last seven days — output (MW) and share (%), stacked by fuel — above an **archive panel** that shows any one week, Sunday→Saturday, all in UTC. The archive steps by whole weeks (◀ ▶ or the year/month/day pickers, back to 2017), toggles between MW and %-share, and always shows the full seven days even where data is missing (gaps read as zero; the current week fills left to right). Each week's data is rolled into a permanent weekly file automatically. Any week with missing periods — including the **current, in-progress week** — can have just those gaps filled on demand with **fill from BMRS** / **fill gaps** (Elexon FUELINST for the metered fuels plus Sheffield PVLive for embedded solar, both modelled/external and labelled as such). Only the already-elapsed gaps are added and self-logged readings are always kept, so a week that mixes both is shown as "gaps filled (BMRS)"; the not-yet-elapsed part of the current week is left untouched. A week that is entirely a prior BMRS pull instead offers **re-pull** to refresh it. Self-logged data is never overwritten by a fill.
- **Resource Conditions** — weather at 12 renewable-energy sites (needs an OpenWeather key — see below).
- **Insight** — a short plain-language read on current conditions, including a cross-check of forecast wind against metered wind where available.
- **Capacity Margin** — how much generation could be called up at medium notice, shown as a radial gauge plus a −24h/+24h trend graph.
- **Immediate Operating Reserve** — how much reserve is instantly available if a generating unit trips off. Shows spinning reserve, the largest single unit (infeed), and whether reserve covers it — with a warning if it doesn't.
- **How Demand Is Being Met Now** — the split between domestic generation and imports, plus a 12-hour wholesale electricity price graph.
- **Interconnector Flows** — which interconnectors are importing and which are exporting; direction is detected live and recolours if a link reverses.
- **System Warnings** — official System Operator messages.

A row of small status indicators along the bottom shows the health of each data source.

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
docker run --rm -v open-meteo-data:/app/data ghcr.io/open-meteo/open-meteo sync dwd_icon_eu temperature_2m,cloud_cover,weather_code,precipitation,rain,showers,wind_u_component_850hPa,wind_v_component_850hPa --past-days 1
docker run -d --name om-sync --restart unless-stopped -v open-meteo-data:/app/data ghcr.io/open-meteo/open-meteo sync dwd_icon_eu temperature_2m,cloud_cover,weather_code,precipitation,rain,showers,wind_u_component_850hPa,wind_v_component_850hPa --past-days 1 --repeat-interval 5
docker run -d --name open-meteo --restart unless-stopped -v open-meteo-data:/app/data -p 8765:8080 ghcr.io/open-meteo/open-meteo
```

The terrain data (`copernicus_dem90`) is about 10 GB and is a one-off; the ICON-EU model (covers the UK and Ireland) is under 1 GB. `om-sync` keeps the model up to date every 5 minutes. Both containers restart with Docker Desktop.

Check it in a browser — the values should be numbers, not `null`:
`http://localhost:8765/v1/forecast?latitude=50.37&longitude=-4.14&current=cloud_cover,precipitation&models=icon_eu`

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

**If the port is blocked — changing it.** Signs: `docker run` fails with "port is already allocated" or "ports are not available", or the footer shows **Open-Meteo: online — local unavailable: unreachable**. To move to another port (8766 here):

1. Remove the container: `docker rm -f open-meteo` (the downloaded weather data is kept — it lives in the `open-meteo-data` volume).
2. Start it on the new port: `docker run -d --name open-meteo --restart unless-stopped -v open-meteo-data:/app/data -p 8766:8080 ghcr.io/open-meteo/open-meteo`
3. Check it: `http://localhost:8766/v1/forecast?latitude=50.37&longitude=-4.14&current=cloud_cover&models=icon_eu` should show a number, not `null`.
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
- A **48-hour linepack trend** chart (how much gas is "in the pipes").

The **balance** is the live supply−demand flow imbalance (total supply minus total demand, in mcm/d) taken directly from the National Gas feed — it is a flow measurement, not a derived rate of change of linepack. The linepack trend (a stock, in mcm) and the balance (a flow) are independent measurements and are not an integral/derivative pair, so short-term movements in one need not match the other. There's no official published "tight" threshold, so any such note is explanatory only.

---

## Power cuts (UK & Ireland)

Open it from the **UK&IRL power cuts** chip or the **live powercuts map** button in the header, or go straight to
**http://localhost:8412/powercuts**. The chip shows the live total number of
customers currently off supply across the UK & Ireland — in bold, and coloured by
severity using the same bands as the page itself (it climbs through yellow, red and
hot-pink as the total rises) — and its tooltip breaks that into planned and
unplanned (customers off and incident counts). Clicking the chip opens the full map
page over the dashboard; the ✕ in the corner or **Esc** closes it. The page also
works on its own at the `/powercuts` URL, independently of the dashboard.

The map draws every distribution network operator's live outages in one place: the
14 GB DNO licence areas (NGED, UKPN, Northern Powergrid, ENWL, SPEN, SSEN) plus
**NIE Networks** (Northern Ireland) and **ESB Networks PowerCheck** (Republic of
Ireland), all in one style and folded into a single national total. Each incident
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

**API keys.** ESB works out of the box (a shared default key is built in); NIE
Networks needs your own key, entered via the ⚙ on the page. Keys are stored
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

---

## Environment Agency page (England)

Open with the **ea** button.
- Enter an England postcode **or place name** (e.g. `SW1A 1AA` or `Sheffield`) to monitor river levels, rainfall and flood status nearby.
- Choose a radius: **20, 40 or 80 km**.
- **Flood alerts** appear in an accordion list (one open at a time; the first is expanded by default) and are also flagged at the very top of the main page. Any warning or alert whose flood area falls within your chosen radius is highlighted as "near you" and floated to the top of the list; the spoken flood alarm also names the nearest local one and its distance.
- Gauges are ordered nearest-first, grouped into distance bands.
- **River level as % of its range.** Each station shows its level as a percentage of its own EA typical range — 0% at the typical low, 100% at the typical high — next to the name in the list (blue below range, green to 80%, amber to 100%, red above) and on its plot, where the range max (100%) and min (0%) are marked as labelled lines.
- **Click a river-level or rainfall gauge** to plot its history. Rainfall is shown as a **mm/h rate** — the raw 15-minute bucket total is converted and kept in the card's hover tooltip — and colour-coded by intensity band (dry / light / moderate / heavy / extremely heavy). **Snow** is drawn in bright pink rather than on the rain scale. If a gauge stops reporting, its card **times out to 0 mm/h** and greys rather than presenting an old value as current, and its history plot runs through to the current time (a gap shows as empty) instead of freezing on the last reading. A gauge card's **border** additionally holds the highest intensity of the last two hours, so recent rain stays visible after it stops, while the number and fill reflect the current reading.
- **Reading age.** River-level readings carry a coloured "…ago" — green up to an hour, amber to four hours, red beyond — so a stale gauge is obvious at a glance.
- **Gauge radar.** The **◎ gauge radar** button at the top right of the plot area swaps the plot for a live radar-style plan of your area from the rain engine: EA gauges coloured by what they are doing (dry, steady, showery, wet), modelled sea points, tracked rain cells with their direction, speed and expected arrival, and your home at the centre. Hover over any marker for its details — a gauge shows its place, grid reference and EA reference, and how far away it is and in which direction from home. Click the button again (or pick a gauge) to go back to the plot.
- **Under the hood.** The **⚙ under the hood** button in the top bar opens the full **Forecast view** in a new browser tab for your location and radius (see *Forecast view* below).
- **Local wind & weather** (below the gauges) shows wind direction and speed, temperature, pressure and sky conditions for your location. Wind, temperature and pressure come from OpenWeather; cloud cover and the sky description come from Open-Meteo (more reliable for this than OpenWeather's cloud field), with OpenWeather as a fallback if Open-Meteo is unavailable. A small "OM"/"OWM" tag by the Cloud % row shows which source supplied it. If a fresh reading isn't available, the panel shows a "cached" marker with the reading's age rather than presenting old data as current.

### Rainfall nowcast (optional — One Call 4.0)

The offshore rainfall watch fills the biggest gap in gauge coverage: the sea.
Real Environment Agency gauges only exist on land, so for a coastal location the
direction weather usually arrives from can be a blind spot. A permanent **net**
of *modelled* sea points watches that arc — sentinels roughly 40 km out plus
inner pickets around 20 km — sampled from the free, keyless Open-Meteo model, so
the wide watch costs nothing against your OpenWeather budget. They appear on the
rainfall map as dashed *MODEL* cards at their true bearing and distance.

When rain is detected offshore, **mobile tracker cards** (marked *TRACK*) spawn
and follow the cell inward through the 5–35 km band, jumping back now and then to
sense whether heavier, lighter, or no rain is following, and estimating the
front's speed and arrival window from how fast it crosses successive ranges. With
an OpenWeather **One Call 4.0** subscription these trackers take radar-fed quality
reads (marked *RADAR* when confirmed), so paid calls are spent only on real
detections; without it they fall back to the free model. When the rain clears the
trackers retreat back offshore and fade, leaving the sentinels watching. Snow is
shown in bright pink throughout, never on the rain scale.

If the free Open-Meteo feed becomes unreachable (see *weather API limits* above) at
the same time as your EA rain gauges, there is no local rain coverage — so **eight
OpenWeather/OC4 probes are placed around your home location** (at 20 km and 10 km on
offset compass points) to stand in as virtual rain gauges, so you still get a working
local picture. They show on the rain-gauge page with their readings, work even for an
inland location with no sea nearby, and are metered against your OpenWeather budget
like the trackers. They retract automatically as soon as **either** your EA gauges
reappear **or** Open-Meteo recovers; while the offshore net is down but your EA gauges
are still reporting, the backup isn't deployed. Like everything modelled, their cards
are labelled as such (and never as "sea", since they can sit over land).

Alongside this the server runs a background rain-alert assessment that combines
your real gauges, OpenWeather's minute-by-minute precipitation forecast for the
next hour, and local pressure and visibility trends — building a picture of what
is happening at your location, what is approaching and from which direction, and
whether it is intensifying or easing. When the measured approach speed looks
reliable the spoken alert can include it ("moving in from the south at around
thirty miles per hour, reaching the coast in thirty to forty-five minutes"); when
it looks wrong — for instance two showers mistaken for one giving an absurd speed
— the figure is withheld rather than risk a misleading number. To hear these as
spoken alerts, enable **Weather nowcast** in the alarms panel and arm sound; until it is switched on the
assessment simply logs what an alert would say (a diagnostic). Like the other
alarm categories it is off by default and speaks only while sound is armed.

Throughout, modelled data is always labelled modelled and is never counted as a
confirmed gauge reading (honesty over plausibility). The whole feature is
throttled to your daily OpenWeather call budget, and if the key isn't subscribed
to One Call 4.0 it simply doesn't appear — the standard rainfall panel keeps
working on the free tier.

### Forecast view

**http://localhost:8412/forecast** (or **⚙ under the hood** on the Environment Agency
page) shows the rain engine's full picture: the same radar plan as the gauge radar,
plus the current situation (for example *approaching · moderate · west*), coverage and
wet-gauge counts, the steering wind it is using, any gauges it has flagged as
stationary or suspect, each tracked rain cell with its speed and arrival estimate, and
what the rain alerts would say. It uses the location saved on the Environment Agency
page (or the location in the link), refreshes on its own, and **demo** shows an example
scene. Nothing here is a forecast service of its own — it shows exactly what drives the
rain alerts.

Gauges wet for hours in one place are ringed and left out of approach tracking:
**amber** = stationary (the weather model agrees it's drizzling there), **red** = suspect
(the model is dry — possibly a faulty gauge). A tracked cell's speed is only shown once
rain has actually moved from gauge to gauge and the speed fits the wind that steers
showers (the ~1.5 km-high "850 hPa" wind); otherwise it reads *speed unverified*.

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

Audible and spoken alarms can be armed from the alarm panel, with per-category toggles. Two of the categories cover the Environment Agency data (and only work while the EA panel has loaded nearby data):

- **River level high** — nearby gauges above their own normal range (each station's published typical-range high), not the rare "record" level. Fires as one aggregated alarm for the whole set rather than one per gauge.
- **Rainfall nearby** — nearby gauges reporting rain, from light to extremely heavy, with extra hysteresis so stop/start rain doesn't spam.

To avoid a flood of alerts when a whole region is affected, these name up to three locations by distance ("less than 5 miles", or rounded miles); if more than three are active, the nearest three are named followed by "and N other locations near you". Nearest (within 5 miles) is treated as more urgent. The repeat tone sounds at most once per hour, with a spoken situation summary every three hours. A **Clear river / rain alarms** button resets these, and they also clear automatically when you change location.

### Frequency and system-risk alarms (kept separate)
These are two distinct alarm categories, because a frequency limit breach is a real alarm while the risk index is a notification:

- **Frequency limits** — the escalating **tones** fire only when the frequency *itself* leaves the band, in either direction: tier 1 at 49.8 / 50.2 Hz, tier 2 at 49.7 / 50.3 Hz, tier 3 at 49.5 / 50.5 Hz (each with a small deadband so it doesn't chatter). Each burst also speaks the reading ("grid frequency high/low …"). To avoid alarm fatigue on a persistent excursion the sound is **capped and spaced**, not continuous: tier 1 up to 10 s every 2 min, tier 2 up to 15 s every 2 min, tier 3 up to 15 s every minute, and after 4 minutes it winds down to up to 5 s every 15 min. A worsening excursion restarts the cadence; if it stays out and isn't recovering, a one-shot spoken warning is given (possible power cuts when low, generation tripping when high). The alarm reads the full-resolution 15-second data, so it catches brief excursions the on-screen trace might smooth over.
- **System risk index** — when the composite risk level rises (low inertia / high CGRI) or a **sudden infeed loss** is detected, the app plays a single short **pip** and shows the on-screen banner (the alert card and the panel tint) — deliberately **not** the escalating tones. It's an "elevated resilience risk" notification, not a frequency alarm.
- **Sustained statutory breach** — separate from the instantaneous crossing above, a distinct critical alert is raised once frequency has stayed beyond a statutory limit (49.5 / 50.5 Hz) continuously for more than a minute (e.g. "Grid frequency has stayed below 49.5 Hz for 1m 35s …"). It updates as the breach persists and is recorded as a single episode, with its total duration, in the alert history.

In the alert list the two are tagged separately (**FREQ** for frequency-limit breaches, **RISK** for the composite risk and infeed-loss items), and each has its own on/off toggle in the alarm panel.

### Active forecast window
In the alarm panel you can set a start and end time (local) to **concentrate the paid API budget** in the hours that matter to you. Inside the window the offshore rain model, land probes and wind reading sample at full cadence; outside it, that sampling is stretched by a "quiet" factor (you choose it) so the daily weather-API quota isn't spent overnight — the alarms themselves still run. The setting is remembered across restarts and may wrap past midnight. Separately, the offshore watch is metered honestly against the free weather tiers and, if a provider reports its daily limit reached, it backs off and leans on the fallback rather than hammering an empty quota.

Click the **history** button (top right) to open the alert history and statistics view. Choose a window — **24 h, 7 d, 30 d, or all** — to see how often each type of alert has fired and how long they typically lasted. History is kept for up to 30 days.

---

## Troubleshooting / first run

**The page says "No data — backend unreachable" (red banner, red pulsing dot).**
This is the most common first-run issue and almost always means one of two things:

1. **`grid_server.py` isn't running.** The dashboard is only a display; all live data comes from the Python backend. Start it by running `grid_server.py` (a Python 3 console window should stay open while you use the dashboard). If that window has closed or shows an error, the page has nothing to talk to.
2. **You opened the HTML file directly.** Double-clicking `grid_dashboard.html` opens it as a `file://…` page, and the data requests won't reach the server. Always open the dashboard at **http://localhost:8412** in your browser instead.

The page retries every 60 seconds, so once the server is running and you're on the right address, it recovers on its own — no need to reload.

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

## Other files in this repository

- `CHANGELOG.md` — what changed and when, with each file's own build history at the end.
- `WEATHER_ALERT_DESIGN.md` — how the rain engine works: gauges, sea points, tracking, the approach alert and its tuning values.
- `KNOWN_ISSUES.md` — known issues and open questions.
- `LICENSE` — MIT licence.

---

*README build 260926.1*
