# grid-monitor

A dashboard for monitoring the GB electricity grid and GB gas supply in real time. It also shows rain, river levels and flood warnings for England (from the Environment Agency), and — if you're an Octopus Energy customer — your own household electricity and gas usage and cost.

**Full disclosure:** This project was vibe-coded with Claude Opus 4.8. Errors are probable, but when found they get corrected. A guiding principle throughout is *honesty over plausibility* — anything estimated, derived or out of date is labelled as such rather than presented as hard fact.

---

## Getting started

### What you need
- A machine running Python 3 (the server uses only Python's built-in libraries — nothing to `pip install`).
- Two files placed together in one folder:
  - `grid_dashboard.html`
  - `grid_server.py`

### Running it
1. Run `grid_server.py`.
2. Open a browser and go to **http://localhost:8412**.

The page refreshes itself roughly every 60 seconds, so you can leave it open.

### Files created automatically
Once running, the server writes these into the same folder as needed:
- `bmu_locations.json`, `bmu_registry.json` — power station / unit reference data.
- `margin_history.json` — a rolling log of the capacity margin so it can be graphed over time.
- `alert_history` — a log of alerts, kept for up to 30 days.

After you enter an OpenWeather API key:
- `openweather_key.json`, `openweather_budget.json`, `weather_last_good.json`.

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
- **System Frequency** — the current grid frequency, usually 1–2 minutes old. If the reading goes stale, the trace dims and a note explains why, rather than showing a misleading value.
- **National Demand** — current demand, plus an *estimated* national consumption figure (labelled "est").
- **Carbon Intensity** — grams of CO₂ per kWh, with the current index band (e.g. low/moderate/high) and the trend versus an hour ago.
- **Generation and Imports by Source** — a bar chart sorted from biggest contributor down (very small ones are omitted). Each source shows a trend arrow versus an hour ago, and estimated figures carry an "est" tag. A legend shows the renewable and low-carbon percentages.
  - **generators button** — opens a full-screen "tree-map" style view of all the major generating units currently feeding the grid.
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

The key lives in your browser's local storage and is never sent anywhere except OpenWeather. Calls are spread through the day and capped at 200, keeping you well under the free 1,000/day limit. A counter shows how many calls you've used (it resets at 00:00 UTC).

---

## GB gas supply page

Open with the **gas** button. It shows the current supply and demand across the GB gas system:
- An animated flow diagram (sources → the NTS "spine" → demand and exports), with brightness waves along the connectors.
- Interconnectors that can flow both ways are detected and recoloured according to direction.
- A supply/demand **balance** percentage on the header badge and on the spine label.
- A **48-hour linepack trend** chart (how much gas is "in the pipes").

Because National Grid doesn't publish a historical total-supply series, the balance line is *derived* from the rate of change of linepack — this is stated on the page rather than hidden. There's no official published "tight" threshold, so any such note is explanatory only.

---

## Environment Agency page (England)

Open with the **ea** button.
- Enter an England postcode **or place name** (e.g. `SW1A 1AA` or `Sheffield`) to monitor river levels, rainfall and flood status nearby.
- Choose a radius: **20, 40 or 80 km**.
- **Flood alerts** appear in an accordion list (one open at a time; the first is expanded by default) and are also flagged at the very top of the main page.
- Gauges are ordered nearest-first, grouped into distance bands.
- **Click a river-level or rainfall gauge** to plot its history. Rainfall is colour-coded by intensity band (dry / light / moderate / heavy / violent) based on 15-minute accumulation.

---

## My Home (Octopus Energy)

Open with the **my home** button.

**You need:** to be a UK Octopus Energy customer with your API credentials to hand (find them in your online Octopus account). If you haven't entered them yet, a pop-up prompts you. To change them later, click the settings **cog** (⚙). Credentials are stored in your browser and never sent to the web.

What you can enter: your API key, electricity MPAN/serial, gas MPRN/serial, unit rates and standing charges (p/kWh and p/day), and your preferred units.

What it shows:
- **Cost cards** for electricity and gas — standing charge, today, last 7 days and this month (the month card is highlighted; electricity in cyan, gas in gold, combined total in green). Costs include the standing charge.
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
