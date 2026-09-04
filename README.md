# grid-monitor

A dashboard for monitoring the GB electricity grid and GB gas supply in real time. It also shows rain, river levels and flood warnings for England (from the Environment Agency), and — if you're an Octopus Energy customer — your own household electricity and gas usage and cost.

**Full disclosure:** This project was vibe-coded with Claude Opus 4.8. Errors are probable, but when found they get corrected. A guiding principle throughout is *honesty over plausibility* — anything estimated, derived or out of date is labelled as such rather than presented as hard fact.

**For educational and hobby purposes only. Not to be used for operational or safety of life decisions.**

---

## Getting started

### What you need
- A machine running Python 3 (the server uses only Python's built-in libraries — nothing to `pip install`).
- Four files placed together in one folder:
  - `grid_dashboard.html`
  - `grid_server.py`
  - `rain_probe.py`
  - `owm_onecall.py`

  `rain_probe.py` and `owm_onecall.py` are companion modules that enable the
  rainfall-alert diagnostics and the OpenWeather One Call 4.0 nowcast. The server
  still runs without them — it just drops those features and uses the free weather
  tier — but the standard install is all four together, in the same folder.

### Running it
1. Run `grid_server.py`.
2. Open a browser and go to **http://localhost:8412**.

The page refreshes itself roughly every 60 seconds, so you can leave it open.

### Updating an existing install
When you replace any of the files with a newer version — `grid_server.py`,
`rain_probe.py`, `owm_onecall.py`, or `grid_dashboard.html` — **stop and restart
`grid_server.py`** afterwards. The server loads the Python modules once at startup,
so changes to any of them (including the companion modules) only take effect on a
restart; a browser refresh alone is not enough. After restarting, reload the page.

### Files created automatically
Once running, the server writes these into the same folder as needed:
- `bmu_locations.json`, `bmu_registry.json` — power station / unit reference data.
- `margin_history.json` — a rolling log of the capacity margin so it can be graphed over time.
- `alert_history.json` — a log of alerts, kept for up to 30 days.
- A `logs/` folder — the interleaved 15-second data log (`grid_log-YYYY-MM-DD.jsonl`, frequency points and generation-mix rows, pruned after 30 days) and, under `logs/archive/`, the permanent weekly generation files that never expire.
- `forecast_window.json` — your active-forecast-window setting (see *Alert system*).
- `api_usage.json` and an API-call log — the per-UTC-day tally of upstream weather calls by source and purpose.
- A `captures/` folder — PNG images you save from the frequency-history viewer's **⭳ save** button.

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
- **Summary chips (top centre):** quick-glance pills for electricity, gas and floods. The electricity chip is always shown; the gas and flood chips only appear when there's something worth flagging. Click a chip to jump to the relevant page. (The gas chip is a *derived* signal, not an official National Grid Margins Notice — the tooltip says so.)
- **history button (top right):** opens the alert history and statistics view (see *Alert system* below).
- Alongside it are buttons to open the **gas**, **ea** (Environment Agency) and **my home** pages.

Press **Esc** to close any of the full-screen pages (generators, gas, EA, My Home, colourgramme, alert history).

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

**This is not a fault of the application.** The app reports Open-Meteo's own response faithfully, falls back to the OpenWeather nowcast (OC4) so the offshore watch keeps working, and re-checks Open-Meteo every 15 minutes — so it recovers on its own once the shared pool frees up, at the 00:00 UTC reset, or if your connection moves to a fresh IP (no restart needed). To confirm it's the shared-IP limit rather than the app, open this in a browser: `https://api.open-meteo.com/v1/forecast?latitude=50.37&longitude=-4.14&current=precipitation` — if you get an `error … Daily API request limit exceeded` response, the limit is being enforced on your IP by Open-Meteo, not by grid-monitor.

To sidestep it entirely, run your own Open-Meteo (it is free and open-source; a Docker image is provided by the project) and point the app at it by setting the `OPEN_METEO_BASE` environment variable before starting `grid_server.py`, e.g. `OPEN_METEO_BASE=http://localhost:8080/v1`. Unset, the app uses the public Open-Meteo host as normal.

---

## GB gas supply page

Open with the **gas** button. It shows the current supply and demand across the GB gas system:
- An animated flow diagram (sources → the NTS "spine" → demand and exports), with brightness waves along the connectors.
- Interconnectors that can flow both ways are detected and recoloured according to direction.
- A supply/demand **balance** percentage on the header badge and on the spine label.
- A **48-hour linepack trend** chart (how much gas is "in the pipes").

The **balance** is the live supply−demand flow imbalance (total supply minus total demand, in mcm/d) taken directly from the National Gas feed — it is a flow measurement, not a derived rate of change of linepack. The linepack trend (a stock, in mcm) and the balance (a flow) are independent measurements and are not an integral/derivative pair, so short-term movements in one need not match the other. There's no official published "tight" threshold, so any such note is explanatory only.

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

**The weather panel asks for a key / My Home asks for credentials.**
Those features need their own credentials (OpenWeather for weather; Octopus Energy for My Home). See the relevant sections above. Both are optional — the core grid, gas and environment pages work without them.

---

*README build 260904.3*
