# Changelog

All notable changes to GB Energy Monitor are recorded here, newest first.

Dates are shown as `yyyy-mm-dd` (UTC). Build tags use the format `YYMMDD.N`
(UTC date + increment). The server
(`grid_server.py`) and dashboard (`grid_dashboard.html`) are versioned
independently; where a change touches only one, the file and its build number are
noted on the entry.

Entries from 2026-08-17 onward carry precise build numbers. Older entries were
reconstructed from development history and are dated by build range rather than
exact tag.

**Per-file history.** Each file is versioned independently and has its own change history. The per-file build histories, formerly kept in the file headers, are in [Per-file build history](#per-file-build-history) at the end of this file. The dated entries below summarise changes across files.

---

## 2026-11-05

- **Light theme for every page.** A **☀ light** / **☾ dark** button on the dashboard, the wall, the power-cuts page and the forecast view switches every page between the dark design and a light version made from it (each colour keeps its hue; backgrounds light, text dark, data colours a deeper shade). The choice is shared by all pages and frames on the server, and switching on one page reloads the others. (`grid_dashboard.html`, `grid_wall.html`, `powercuts_page.html`, `forecast_view.html`)
- **Rain cells: movement measured on the radar, confirmed by a second source.** Each tracked cell's speed and heading are now measured on the radar itself (the cell found again in the next frame), then confirmed by gauge hand-over or the mobile probes; the 850 hPa wind is the last fallback, not a gate. Cells no longer show a blanket "speed unverified", and approach alerts are kept to one per episode for the same rain area. (`rain_probe.py`)
- **Possible hail.** Radar cells at 55 dBZ or more are drawn white in the radar view (a hint, not a detection: the free radar has no dual-polarisation data), the key gains a "hail?" swatch, and the nowcast line and spoken approach alerts mention possible hail nearby. (`rain_probe.py`, `forecast_view.html`)
- **Radar view: home nowcast line.** Top left of the radar: a few words on rain at home now or when it is due ("Heavy rain now · stops 10:40", "Rain ~10:30 · in 14 min", "Dry next 3 h"), with its source underneath and a live countdown to a due time. (`forecast_view.html`, `rain_probe.py`)
- **Spoken approach alerts give a clock time.** With a measured speed, arrival is spoken as "around 10:35" or "between 10:30 and 10:45"; within half an hour as "in about 15 minutes" or "soon". (`rain_probe.py`)
- **Wall: big circuits on the GB map at every zoom.** Any circuit or offshore connection carrying 500 MW or more is now drawn on the zoomed-out GB map, with its end substations, whatever its voltage layer (it fades in from 400 MW); before, offshore connections such as Hornsea's (about 2 GW) appeared only once the map was zoomed in. A layer switched off in the key stays off. (`grid_wall.html` 261105.4)
- **Wind dials and weather panels.** The dials are larger, with three lines of text under each (the 850 hPa wind in mph and m/s), and the weather panels open with "Temp | Dew point" and "Rain 1h" on one line in a large font sized to the panel. (`grid_dashboard.html`)
- **Rivers & floods page: the rain & gauge radar fills its column and fits HD screens** (renamed from "gauge radar"); the key and rate plot are kept clear of the radar circle. The power-cuts page opened on its own is centred again. (`grid_dashboard.html`, `forecast_view.html`, `powercuts_page.html`)

## 2026-11-04

- **Frequency panel rebuilt.** Bands are now green 49.8–50.2 Hz, amber to 49.65 / 50.35, red to 49.5 / 50.5 and purple to 49.3 / 50.7 and beyond, on a dial widened to ±0.7 Hz. A histogram beside the dial shows the time spent at each frequency over 1 – 24 h (bin width 0.01 – 0.1 Hz). Labelled ▲ / ▼ markers show excursions on the 2-hour trace and the history plot. The 1-minute-cycle mini plot is gone; the full spectrum is behind a **⤢ spectrum** button. A **🔇 mute alarm** button silences an active frequency alarm until its level changes. (`grid_dashboard.html`)
- **Frequency history.** Opens on the last 48 hours, zooms in to individual 15-second samples, has hour and 10-minute gridlines, steps by one view width, and in live mode runs with the clock. The min / max band and peaks take the alert colours and sit at the right times. (`grid_dashboard.html`)
- **Rain radar: motion measured over three frame pairs, fresher frames, max-rate plot.** The picture is moved on to now only by motion the radar measures reliably (uncertain motion is not used), and the key shows it beside the 850 hPa wind. New frames reach the view within about a minute of publication. A movable bar plot shows the highest rain rate in the plot over the last 1 / 6 / 24 h, from radar and / or gauges. (`rain_probe.py`, `forecast_view.html`, `grid_server.py`)
- **At-home rain / snow and temperature plots, −12 h to +12 h** (wall Rain radar view, Rivers & floods page): measured at home, then OpenWeather's minute nowcast and the Open-Meteo forecast, with dew point. The self-hosted Open-Meteo sync needs `relative_humidity_2m` and `snowfall_water_equivalent` (see README). (`grid_dashboard.html`, `grid_server.py`)
- **Wall 5120 × 1440: live readouts in the title bar.** Demand, price, carbon, low-carbon %, gas linepack and gas demand, each counting to new values with a 1-hour trend arrow. Server: 1-hour deltas for low-carbon, linepack and gas demand. (`grid_wall.html`, `grid_server.py`)
- **Dashboard: prompt to turn on audible alarms** two minutes after load if they are still off. (`grid_dashboard.html`)
- **Gas page and wall: gas panels rearranged; gas storage by country shows % full or days left.** (`grid_dashboard.html`, `grid_wall.html`)

## 2026-10-31

- **Power cuts: severity bars** out of 10,000 customers (or the largest), coloured by the alert bands, on the power-cuts page and the wall; the By country table lines up with By operator. (`powercuts_page.html`, `grid_wall.html`)
- **Wall: every ETYS boundary gets a turn-down figure**, so a constrained boundary such as B4 can show. (`wall.py`)

## 2026-10-30

- **Wall: constrained boundaries and a Constraint chip.** From Elexon's SO-flagged turn-down instructions the wall works out which ETYS boundary NESO is holding back and by how much; the dashboard and wall show an orange **Constraint** chip (e.g. "Constraint B6 3.6 GW") while 500 MW or more is turned down. (`wall.py`, `grid_wall.html`, `grid_dashboard.html`, `grid_server.py`)
- **Wall: pinch-point alerts removed.** Near-rating flows in the model were more likely model or rating errors than real overloads, so the circuit alerts and glows built over the previous days were taken out; ETYS boundaries stay, drawn fainter. (`wall.py`, `grid_wall.html`, `grid_dashboard.html`)
- **Wall: substation and power-cuts panels on the 5120 single screen.** A Power cuts panel counting as the power-cuts page does; the substation panel grows while a substation is selected, has an expand button, output bars and % of GB supply; numbers count to new values with flashing ▲ / ▼ marks; the dashboard's header chips appear in the wall's header. (`grid_wall.html`)
- **Rain radar: the real radar picture under the gauges.** The radar view draws RainViewer's newest frame around home (0.5 km cells, 40 km each way), moved on to the current time, with a weather-app colour key and snow coloured by wet-bulb temperature. On the wall it is round again, with the local weather beneath it. (`rain_probe.py`, `forecast_view.html`, `grid_dashboard.html`)
- **Fix: flows jumped on nearly every circuit just after midnight** (FUELINST was requested without a time window). (`grid_server.py`)

## 2026-10-29

- **Wall: real circuit ratings and ETYS boundaries from NESO** (new module `etys.py`): the newest ETYS Appendix B is read for seasonal circuit ratings, and the ETYS study boundaries are drawn with the modelled transfer across each. (`etys.py`, `wall.py`, `grid_wall.html`)
- **Wall: local networks from the DNOs** (new module `dno_data.py`): the six operators' Embedded Capacity Registers (sites of 1 MW and over), their 132 / 66 / 33 kV line maps, Northern Powergrid's live 132 kV readings and NGED's measured grid supply point imports. (`dno_data.py`, `wall.py`, `grid_wall.html`)
- **Wall: a better flow model.** Units' output reconciled with Elexon's metered output by fuel (closing the battery gap), embedded wind and solar placed by the registers' capacity, Scotland's share of demand from NESO's metered split, and a national supply check. (`wall.py`)
- **Wall: generator sites and positions from OpenStreetMap and DESNZ's REPD** (new module `osm_sites.py`): about 3,600 generator sites drawn and named when zoomed in, BM unit positions corrected, and offshore farms joined to their connection points (audited against NESO's TEC register). (`osm_sites.py`, `wall.py`, `grid_wall.html`)
- **Wall: maps and substation panel.** Detail fades in with zoom, with a button per layer; flows ease to each new model run; circuit values and flow dashes from 5 MW; the regional map zooms to the selected substation; a search box and a scrolling list of every generating substation. (`grid_wall.html`)
- **Wall: "Gas sources" view** spanning two panels, with gas panels either side and one Expand all / Collapse button. (`grid_wall.html`)
- **Reservoirs: South West Lakes Trust's weekly table added, long-silent reservoirs hidden, South West Water's page read again after a wording change.** (`reservoirs.py`)
- **Gas flow diagram: every export route shown once, storage injection on the demand side, terminals slide out like storage.** (`grid_dashboard.html`)

## 2026-10-28

- **Gas storage** (new module `gas_storage.py`): each GB storage site's daily flows and estimated fill, the GB stock, LNG tanks and deliverability from National Gas, and Europe from GIE AGSI+ (free key). Shown as a **System flow | 🛢 Gas storage** switch on the gas page (UK figures, Europe trend against the last five years, or by country), as slide-out storage and LNG columns on the flow diagram, and as a wall view. Rough is no longer counted as storage. New `/api/gas-storage`. (`gas_storage.py`, `grid_server.py`, `grid_dashboard.html`, `grid_wall.html`)
- **Gas alerts in the main alert list; alerts can be dismissed for 7 days.** New tags NG (National Gas notices), GAS (alarm levels) and STORE. Alert panels collapse to the worst alert with "show N more", and alert wording is cut to the figures. (`grid_server.py`, `grid_dashboard.html`)
- **Power cuts: the key window shows which operators have a key and whether the feed is live.** (`powercuts_page.html`)

## 2026-10-25

- **Wall: names for unnamed substations, found automatically.** About 160 of the network's 593 nodes have no name in OpenStreetMap; a background task (a few minutes after start-up, then monthly) borrows one from a nearby named substation or power station, the largest generator connected there, or a location ("Unnamed 275 kV node near Marshfield"). The hover card and substation panel say where each name came from. (`wall.py`, `node_names_seed.json`)
- **Power cuts: optional keys for Northern Powergrid and UKPN.** Their OpenDataSoft feeds are open, so they ran without a key on the anonymous allowance (5,000 calls a day per IP, shared with anything else on the same connection). **⚙ feeds / keys** now takes an optional free OpenDataSoft account key for each; when one is stored it is sent with every request (Authorization header, never the URL), which uses that account's own allowance instead. (`powercuts.py` 261025.5, `powercuts_page.html` 261025.3, `grid_server.py` 261025.1, README)
- **Power cuts: incidents plotted in another operator's area are flagged.** SSEN listed a 3-customer LV fault (UF3074) at "BD17 7AX", which is Baildon in West Yorkshire, Northern Powergrid's area; SSEN's own coordinates match that postcode, so the postcode itself is probably mistyped (RG17 7AX Hungerford and BH17 7AX Poole are both SSEN postcodes), and the true place can't be known. Each incident is now checked against the licence areas in `region_polys.json`: one inside another operator's area and more than 5 km outside its own is kept where published and still counted, but drawn faded with a dashed rim, explained in its hover card ("plotted in NPg's area, 205 km outside SSEN's — location as published, probably a wrong postcode"), and left out of the near-you box and the near-you alarm. Points outside every area (Shetland, Achill and other islands the simplified outlines leave out) are not flagged. On 25 Oct this flagged 1 of 1,156 incidents. (`powercuts.py` 261025.4, `powercuts_page.html` 261025.2, `grid_dashboard.html` 261025.1)
- **Power cuts: Northern Powergrid no longer runs out of its daily API allowance.** NPg showed "down" from 15:38 UTC while its own map was live. OpenDataSoft allows 5,000 anonymous calls a day per IP, and the feeds were read 100 rows per call; with the storm NPg's dataset held about 9,000 rows (one per postcode), so each 5-minute poll cost 93 calls and the allowance ran out at about 13:40 UTC (NPg's reply: 429 "exceeded the requests limit for anonymous users", reset 00:00 UTC). All OpenDataSoft feeds (NPg, UKPN, ENWL, SPEN, NIE) now use the dataset's export endpoint, every row in one call, gzip-compressed, and NPg asks the server for live rows only (6,269 rows, 195 incidents in one call on 25 Oct). Paging remains as a fallback if an export is refused, but not on a quota error. (`powercuts.py` 261025.3)
- **Power cuts: SP Energy Networks incidents placed by their postcodes when SPEN's location is wrong.** Some SPEN incidents, mostly high-voltage ones, carry a location far from the postcodes they list, often about 1.5° of latitude too far south. On 25 Oct a Helensburgh/Greenock fault (G84, PA16) was drawn in the Irish Sea beside the Isle of Man, an Ayrshire 33 kV fault (KA26, DG8) off Blackpool, and a Haddington fault (EH41) in the Solway; 8 of 144 live SPEN incidents were more than 35 km from every postcode district they named. Each SPEN point is now checked against the centres of its postcode districts (postcodes.io, cached in `outcode_centroids.json`, git-ignored); one more than 35 km from all of them is drawn at the centre of the main group of districts, and its hover card says so ("SPEN's location was 194 km from these postcodes; shown at their centre"). Distances from home and the near-you alarm use the corrected position. The published point is kept as `pos_published`. A fresh cache fills on the first poll (up to 250 district lookups; 40 per poll took about 20 minutes to place every incident). (`powercuts.py` 261025.1–.2, `powercuts_page.html` 261025.1, `.gitignore`)

## 2026-10-24

- **Rain voice: "heavier" message says when, and that rain is continuing.** "The rain is expected to become heavier shortly" gave no time and, after a long spell of rain, no reminder that it was still raining. The timing now comes from the one-minute nowcast: "in a few minutes" (within 5 min), "in about 10 minutes" (to the nearest 5) or "soon" when not known; no spoken message says "shortly". After 30 minutes or more of rain measured at home (dry gaps under 20 min ignored) it reads "Rain is continuing at your location and is expected to become heavier in about 10 minutes." Spoken once per episode: the countdown changing does not repeat it. (`rain_probe.py` 261024.1)
- **Wall: circuit flows easier to read.** Each circuit is drawn wider the more modelled MW it carries (2 px per GW on a voltage base, the same scale as the interconnectors), and its moving dashes are brighter and faster with more flow (at half the speed first used). When a circuit's modelled flow changes at a model run (about once a minute) by at least 10 MW and 10 %, or reverses, its value gets a tinted background (up to 40 %) and the direction and amount ("164 ▲18", ⇄ for a reversal): blue to green rising, yellow to orange to red falling, stronger with a bigger change (full at 100 MW or 50 %). The tint stays until the next run. Values no longer overlap each other or substation names (changed ones are placed first, then the heaviest flows; one with no room is left out), and unchanged values fade as the regional map zooms out. The Western Link and Caithness–Moray HVDC lines also widen with their estimated MW, still dotted amber as estimates. Key and help updated. (`grid_wall.html` 261024.4, .5, .10, .11, .12)
- **Wall: reported outages (REMIT).** The server fetches operators' REMIT unavailability reports from Elexon every 5 minutes and adds those reducing a GB unit or interconnector now or within 24 h to `/api/wall` (`outages`); closures and reports running over 6 months are left out. On the map a faint dashed red ring marks a substation with a reduced unit, with each report in its tooltip and the substation panel; interconnector labels and the Interconnectors panel show reduced capacity in the current direction ("cap 1,000/2,000"). A new **Outages** panel lists reductions in force (biggest first) and those starting within 24 h, on screen 2 of both dual layouts and in the ultrawide single layout. Labelled "reported · REMIT" throughout. (`wall.py` 261024.1, `grid_wall.html` 261024.4, .6)
- **Wall: Scotland → England border label.** The border is now the plain land outline (the grey dashed line looked like a circuit) and the transfer is labelled once, near the middle of the border (on the regional map zoomed in, the middle of the part in view), at the position along it that crosses the fewest circuits, routes and names. (`grid_wall.html` 261024.2, .3)
- **Wall: the dashboard's frequency panel.** The wall's frequency panel is now the dashboard's own System frequency card (value, 1-minute cycle, deviation dial, system risk, 2-hour trace), shown through a new embedded view `/grid_dashboard.html#embed=freq`, scaled to fit with its full border. It sits top left of the ultrawide single layout (in place of "System now") and top middle of screen 2 in both dual layouts, where "System now" no longer repeats the frequency. Embedded views no longer show the dashboard's start-up pop-ups. (`grid_wall.html` 261024.7–.9, .14; `grid_dashboard.html` 261024.2, .10)
- **Wall: more regional panel views.** The switchable regional panel adds **Generators**, **7-day generation** and **My home**, the dashboard's own pages through new embedded views (`#embed=gen`, `#embed=gh`, `#embed=octo`, close buttons hidden). The source buttons wrap onto a second line where they don't fit. (`grid_wall.html` 261024.15, `grid_dashboard.html` 261024.11)
- **Network pop-up: blank map on reopening fixed.** Two causes. Chrome gives a hidden frame a 0×0 window and the map rebuilt itself at that size; the map now ignores zero-size resizes and the dashboard asks it to lay out again on each reopen. Firefox throws NS_ERROR_FAILURE when a hidden canvas is drawn, which stopped the map's animation loop for good; the loop now survives a failed draw. As a safety net the dashboard reloads the map if it is not drawn within 0.7 s of reopening. (`grid_wall.html` 261023.14, 261024.1; `grid_dashboard.html` 261023.4, 261024.1)
- **All pages: the dashboard's size rules, and phone layouts.** The power-cuts page and forecast view match the dashboard: content at most 1,320 px wide and centred (was 1,280 and 1,040), the same margins, a single column below 980 px, narrower margins on phones, and the dashboard's gentle zoom on very wide screens when opened on their own (inside a dashboard pop-up they inherit it). The dashboard's header buttons wrap on narrow screens instead of running off the side. The network pop-up is capped at 1,320 px and centred like the other pop-ups; below 900 px the substation panel moves under a near-full-height map and the page scrolls, with the key hidden at first and a compact title bar. The wall itself is unchanged. (`grid_dashboard.html` 261024.9, `powercuts_page.html` 261024.6, `forecast_view.html` 261024.1, `grid_wall.html` 261024.13)
- **Fix: unit output ignored NESO instructions.** The generator treemap, battery charge/discharge and operating reserve read only Physical Notifications. On a constrained day most gas plant notifies 0 MW and runs on NESO's accepted offers: on 24 Oct only Fawley Cogen (80 MW notified) counted as gas, so the whole metered gas total (4.9 GW) was drawn as one Fawley tile, and the battery figure read 40 MW discharging / 846 MW charging instead of 546 / 356 MW. Each unit's level now (and 15 min ago, for ramps) is NESO's latest accepted level (BOALF) where one applies, else its notified level, the same rule as the wall; operating reserve counts what is actually running. `/api/units` units carry `instr`; `battery` gains `units_instructed`. (`grid_server.py` 261024.1, .3; `grid_dashboard.html` 261024.3, .4)
- **Generator treemap: readable station names.** Tiles take the site name shared by a station's units in the Terravolt map ("Grain U1", "Grain U2" → Grain), else the Elexon registry name tidied of unit, module and company suffixes, else the code; curated names still win (Heysham is "Heysham 1 & 2", as both share one code stem). Parts of one site registered separately (Walney 1 and 2, Robin Rigg East and West) are one tile, and supplier/aggregator units (`2__…`, `C__…`) are one "Aggregated small sites" tile per fuel. (`grid_server.py` 261024.2)
- **Weather panel: THI replaced by apparent temperature.** The Thom THI has no wind term, barely responds to humidity near 14.5 °C, runs backwards below it and labelled any cold reading "Comfortable". The row is now **Apparent**: the Bureau of Meteorology apparent temperature, AT = T + 0.33e − 0.70ws − 4.00 (e = water-vapour pressure in hPa; ws = the local 10 m wind in m/s from the wind dial), valid in cold and heat, with bands Very cold < 0, Cold < 8, Cool < 15, Comfortable < 22, Warm < 26, Hot < 30, Oppressive. "Feels like" stays as OpenWeather's own figure. (`grid_dashboard.html` 261024.5)
- **Monitored areas: repeat alarms.** Each monitored area on the power-cuts page has a **Repeat** tick box. For ticked areas the dashboard sounds the alarm again (siren, then a spoken reminder with customers off and estimated restoration) every 30 minutes while a cut there continues; planned works repeat only once started. Stored with the area (`repeat` in `powercut_watch.json` and each match). (`powercuts.py` 261024.1, `powercuts_page.html` 261024.1, `grid_dashboard.html` 261024.6)
- **"Republic of Ireland" renamed "Ireland" throughout.** Dashboard, power-cuts page (key, notes, map area, countries, wind-vs-cuts regions), server texts, README and the region files (`region_polys.json`, `grid_network.json`). The wind-vs-cuts history converts itself: the first time the updated `windcuts.py` runs it renames the region in `windcuts_state.json`, `windcuts_binstats.json` and every `windcuts-YYYY-MM.jsonl` (cells under both names added together; safe to repeat). Ireland and Northern Ireland now have one region button each (their country and licence-area entries were identical). (`grid_dashboard.html` 261024.8, `powercuts_page.html` 261024.2, .3, `powercuts.py` 261024.2, `windcuts.py` 261024.1, `reservoirs.py` 261024.1)
- **Wind vs power cuts chart: readable axes.** The gust axis title sat on the last tick label; it is now "gust (kt)", centred under the ticks. Chart text is 1 pt larger, the y-axis values and title 2 pt larger and brighter. (`powercuts_page.html` 261024.4, .5)
- **Gas alarms: a full alarm ladder, now voiced.** The **Gas supply & margin** alarm category (was "Gas margin (derived)", a silent blip on the 10 % chip) now plays the gas probe's alerts with the gas tones from the design (triangle wave: notice 440 Hz; warning 400→440; critical 400→440→660) and speech starting "Gas:". Levels, anchored on a peak day of over 400 mcm (417 in 2017):
  - forecast demand 300 / 350 / 400 mcm → notice / warning / critical, once per forecast day and level; 400+ is critical only when supply is also tight (else a warning);
  - demand against expected available supply 90 % (or under 50 mcm/d spare) / 95 % / 100 % → notice / warning / critical (100 % was a warning);
  - live linepack more than 5 mcm below today's forecast minimum → warning; predicted closing linepack revised down more than 10 mcm in 3 hours → notice;
  - storage stock under 5 days at full withdrawal → notice (11.4 days on 24 Oct); system average price at least twice its 30-day median → notice (×1.2 on 24 Oct);
  - the linepack-rate warning now speaks only alongside another signal; official notices and the predicted-closing-linepack levels are unchanged.
  
  Derived states still need 2 hours before speaking and repeat every 3 hours. The server keeps the last 20 spoken events in `/api/gas-probe` (`events`); the dashboard voices each once (remembered per browser, never if over 2 h old) and adopts what's there silently the first time. The gas page gains an **Alarm levels** strip showing each measure against its levels. The 10 % "gas margin tight" chip and banner stay, visual only. Off by default; first-pass levels, labelled as calibrating. (`gas_probe.py` 261024.2, `grid_server.py` 261024.5, `grid_dashboard.html` 261024.14)
- **Gas: forecast minimum linepack and the gas alert probe fixed.** Three faults found while reviewing the gas alerts.
  - **Forecast minimum.** National Gas re-issues its forecast of the day's lowest linepack several times a day, each with its own time of minimum. The server took the one with the latest time of minimum, not the latest issued, so the chart drew 326 mcm on 24 Oct while the current forecast was 312 (matching the actual low of 311.8 at 21:00). It now uses the latest issued forecast for the current gas day (06:00–06:00 UK time), and the chart label adds when the minimum falls ("forecast min 312 at 20:00 UT"). `/api/gas` adds `linepack_min_forecast_at`.
  - **Margins Notice signal (probe).** It compared the day's total entry and exit nominations, which always nearly match, so it sat at "notice" or "warn" every day and logged "forecast demand has reached available supply" most nights. It now compares National Gas's NTS demand forecast (the day-ahead one once published, as a real Margins Notice does) with an Expected Available Supply estimate: non-storage supply flowing now (UK shelf, Norway, interconnector imports) plus the published daily deliverability of the LNG terminals and storage sites. Tonight: 163 mcm/d forecast for 25 Oct against about 411 available (ratio 0.40), so quiet. The phrase names the day: "Gas: forecast demand for tomorrow is within five percent of expected available supply."
  - **Gas-day alignment (probe).** Just after midnight the probe read the next gas day's predicted closing linepack against the current day's forecast minimum, which raised false "at or below the forecast minimum" states (22 Oct 00:00–04:00). Day-specific values now come only from the current gas day; the 0 placeholder printed for a day before its first real forecast is ignored. The linepack-rate guard now uses the live NTS linepack (the hourly publication it read is a day behind, so it saw one unchanging value).
  
  The probe still only logs; nothing new is spoken. (`grid_server.py` 261024.4, `gas_probe.py` 261024.1, `grid_dashboard.html` 261024.13)
- **Gas page: 48-hour linepack chart labels on narrow screens.** A time tick could sit on top of the right-hand "22:58 UT" label (ticks only kept a fixed fraction of a step clear); each tick is now measured and left out if it would touch either end label. The two-line legend ran off the right edge on a phone; it now moves to the left edge, then shrinks to 9 px, then uses short wording ("— linepack mcm (solid live)", "— supply−demand mcm/d") until it fits. The footer's "HTML build" now matches the file. (`grid_dashboard.html` 261024.12)
- **Dashboard: "▦ wall" button aligned.** It was a link styled as a button, so its text sat lower than its neighbours; it is now a button. (`grid_dashboard.html` 261024.7)

## 2026-10-23

- **Transmission wall (`/wall`).** A control-room style page: the GB transmission network at true geographic scale with modelled live flows, for one or two screens.
  - **Layouts.** Six, each window choosing its own (`?screen=`, or press M): `hd` 1920×1080; `uw` 5120×1440; `d1`+`d2` ultrawide + HD; `h1`+`h2` two HD screens. Drawn at exact pixel size and scaled to the window; F = full screen. The two windows of a pair share selection and zoom (BroadcastChannel, same browser profile); "start screen 2" opens the other screen on the second display (Window Management API, Chrome/Edge). `start_wall.bat` launches both as Chrome app windows.
  - **Network.** 593 nodes / 815 circuits (400, 275, Scottish 132 kV, offshore 150–220 kV) from the gb-dispatch-model prebuilt network (OpenStreetMap, Zenodo 18161054), names from OpenStreetMap, built once with `build_network.py` into `grid_network.json`, with interconnector cable routes (`icroute`) and the 1,021 DNO-run 132 kV circuits in England, Wales and NI (`dist`, drawn on request as thin olive lines, not in the flow model). The page always asks for a fresh copy of the network file.
  - **Live inputs, every minute.** Each BM unit's level now, 15 min ago and at +30/+60 min, from the latest NESO acceptance (BOALF) where instructed, else its PN; units placed at the nearest node from the Terravolt locations; interconnectors from FUELINST at their landing nodes, with trader schedules shown only where they match the metered flow; demand spread by DNO-area customer share. The server keeps 20 minutes of interconnector flows in memory for their 15-minute change.
  - **Flows.** DC load flow in the browser (Cholesky on the reduced susceptance matrix, ~584 nodes), labelled "modelled" throughout. Internal HVDC flows are not published: the Western Link is estimated as a share of the Scotland → England transfer (its 2,250 MW rating against NESO's SCOTEX limit, capped), Caithness–Moray as a capped parallel path. The transfer is measured across the real border (Scotland outline, offshore nodes by sea side); a first version split at 55.05°N, which counted Northumberland and the North Sea Link landing at Blyth as Scotland.
  - **Reading it.** Circles = generation at a substation (area ∝ MW, colour = main fuel) with an arc travelling round it for a 15-min rise or fall (≥ 50 MW and 10 %); interconnectors along their cable routes, width ∝ MW (2 px per GW), blue importing / orange exporting, labelled at the far end, ⇄ for a reversal scheduled within the hour; corner brackets mark the selected substation; HVDC estimates read "≈". Each map has a key strip below it (hidden or shown together, remembered).
  - **Panels.** Movers (last 15 min, next 30 min scheduled; NESO-instructed units marked), substation detail (units, circuits, 2-hour output chart with the scheduled path, "✕ clear selection"), NESO day-ahead constraint groups (flow vs limit), Elexon derated margin / LoLP, system figures from `/api/grid`. Esc steps back: layout picker, then selection.
  - **Regional map.** Presets, wheel zoom about the cursor, drag, double-click zoom and +/− buttons; names and circuit MW appear once zoomed in (view under 4.5° of latitude). Its title buttons switch the panel to the power-cuts map, gas page, rivers & floods or rain radar instead, remembered per layout; the dashboard gains embedded views (`#embed=gas`, `#embed=ea`) for this.

  (`wall.py` 261023.1–.2, `grid_wall.html` 261023.1–.13, `build_network.py` 261023.1–.3, `start_wall.bat` 261023.1, `grid_server.py` 261023.1–.2, `grid_dashboard.html` 261023.1–.2)

- **Dashboard: "▦ wall" and "🗺 network" header buttons.** ▦ wall opens `/wall` in its own window. 🗺 network opens the wall's zoomable map full page like the power-cuts pop-up (`/wall?screen=map`: map, substation panel, "All GB" starting view, text 1.5 pt larger, no cross-talk with open wall screens; ✕ or Esc to close, Esc first clears a selection). (`grid_dashboard.html` 261023.1, .3; `grid_wall.html` 261023.13)

---

## 2026-10-22

- **Rain alerts: audit of every spoken message against measurements.** All rain-voice and nowcast messages in the rain recorder logs (30 Sep – 22 Oct) were checked against the radar at home, the gauge within 5 km and current area readings. Fixed:
  - **Rain at home** now also counts the radar at home (wet on two frames running), not only the 2.9 km gauge, which missed 10.5 mm/h at home on 22 Oct 18:05.
  - **Showers / continuous** name what is falling now at measured points (gauges, radar net), each the lower of its last two readings; the 90-min peak is no longer spoken (13 of 38 overstated). "Continuous rain has set in" is now "Continuous rain across the area." and stays quiet within 30 min of rain at home (it was mostly spoken 15–20 min after rain ended at home). A changed area message repeats no sooner than 20 min.
  - **Nowcast at-home lines** use the measured home reading (10 of 36 claimed rain at home while it was measured dry); "starting" and "getting heavier soon" removed. "Confirmed by nearby gauges" needs a gauge within 5 km (was 8 km; 10 of 16 dropped). "Picking up" / "easing" only while rain is measured at home.
  - **Visibility drop** says visibility only ("Rain expected soon" was wrong 7 times in 8). Rising pressure when dry no longer says "the rain is easing". The pressure + heavy rain line no longer claims rising wind or persistence (neither is measured).
  - **All-clear** "The rain has cleared from the area." only when the area is clear, nothing approaching, no minute-ahead onset, and not alongside the rain voice's own "clearing". The old "No rain is expected in the near term" was followed by rain at home within an hour 18 times in 32.
  - Same replay through the new code: rain voice 122 messages (was 148); no intensity word above what was measured.
  - Forecast view: the state line shows the current area intensity, with the 90-min peak in brackets only when higher.

  (`rain_probe.py` 261022.2, `forecast_view.html` 261022.2)

- **Home pressure plot.** A small trace of logged home pressure under the Pressure reading in the EA page's weather panel. `pressure_history.json` now keeps 72 h per location (was 4 h); the 3-hour tendency is unchanged. Readings come one per fresh OpenWeather fetch (about every 15 min; longer outside the forecast window when its quiet multiplier is on). The time axis spans whatever history exists, up to 72 h; the last 3 h (tendency window) is shaded; y range at least 10 mB because OpenWeather pressure is whole mB; gaps over 75 min break the line; hover shows time and value. `/api/ea` adds `wind.conditions.pressure_series` ([[epoch_s, hPa], …]) from the same file, no extra API calls. Sunrise and sunset moved to the bottom of the temperature block to keep the blocks the same height.

  (`grid_server.py` 261022.1, `grid_dashboard.html` 261022.1)

- **"At your location" rain voice describes rain measured at home.** The intensity word came from the heaviest reading within 40 km over the last 90 min, so "Very heavy rain at your location" was spoken for cells 20–33 km away. Replay of the rain recorder logs (30 Sep – 22 Oct): 60 of 98 at-home messages named more than was measured at home; "very heavy" was said 25 times, about 3 of them true at home.
  - Intensity now comes from the radar at home (~1 km box, frame ≤ 15 min old) and the nearest physical gauge within 5 km, each the lower of its last two distinct readings; the higher of the two sources is used.
  - Heartbeats 20 min (heavy+) and 60 min (light/moderate), were 10 / 20. A heavier band re-announces only above everything said at home in the last 30 min. Rain at home dropping out and back within 30 min at the same or a lower band stays quiet.
  - At most hourly, heavier rain 5–40 km away is appended ("Heavy rain about 20 km to the south-west.").
  - Same replay with the new code: 43 messages (was 95), busiest day 10 (was 29), "very heavy" once (radar 14 mm/h at home).
  - Forecast view shows the at-home voice reading and its source.

  (`rain_probe.py` 261022.1, `forecast_view.html` 261022.1)

## 2026-10-18

- **Periodicity spectrum (1-minute cycle).** A small amplitude spectrum of grid frequency (periods 30 s – 30 min, 0–15 mHz) under the frequency reading, styled like the home page's periodicity spectrum, with the 1-minute line marked — the once-a-minute wobble attributed to synchronised UPS battery self-tests (Hartmann et al., arXiv 2510.09862, 2025). Averaging windows 1h / 12h / 24h / 7d / 30d (Welch, segments 64 min – 17 h), vertical scale 15 / 5 / 2.5 mHz and longest period shown 5m / 15m / 30m, all remembered. ⤢ (or a click) swaps the frequency trace for a larger plot in the same panel, with hover readout, the 1-minute amplitude against neighbouring periods, the average minute and the second of the minute where it is lowest. New `GET /api/freq-raw?days=N` serves the raw 15-second log compactly; live updates use `/api/freq-tail`.

  (`grid_server.py` 261018.1, `grid_dashboard.html` 261018.4)

- **Frequency alarm: the spoken extreme is now held for the whole excursion.** The "dipped to" / "peaked at" value used to be the worst point of the latest 8-sample burst only, so it crept back towards 50 Hz as frequency recovered. It is now the worst point since the excursion began, extended only outward, and cleared once the excursion has ended (after the usual hysteresis) or switches side.
- **Two new under-frequency tiers: 49.1 Hz (tier 4) and 48.8 Hz (tier 5).** Low side only; the deepest tier always takes priority. Faster sirens (0.3 s and 0.18 s notes against 0.5 s at tier 3) and a shorter cadence (15 s every 45 s; 20 s every 30 s). After 4 minutes they wind down to 5 s every 2 min instead of every 15 min. Spoken lead "Severe" / "Emergency" and "very low"; tier 5 adds "Automatic demand disconnection may begin soon."

  (`grid_dashboard.html` 261018.1)

## 2026-10-17

- **Balancing costs panel.** A new **Balancing costs · NESO** panel below *Generation and imports* shows what NESO spends each day to keep the system balanced, from new module `balancing.py` and `GET /api/balancing` (`?detail=1` adds the half-hours, correlations and period totals). No keys.
  - **Confirmed and provisional.** NESO's daily BSUoS cost data (per half-hour, by category) is the confirmed figure, about 5 days behind. Later days are estimated from Elexon's indicative Balancing Mechanism cashflows (about a day behind), divided by the BM-to-NESO ratio on recent confirmed days above £15m (median, with the 10th–90th percentile as the likely range; currently 0.933, 0.873–0.946 from 21 days). Estimates are always marked, hatched and replaced when NESO publishes.
  - **Summary figures:** highest estimate, yesterday, latest confirmed, last month's total, constraints share (last 30 confirmed days) and highest confirmed. The highest estimate is coloured against the mean of the last 30 confirmed days: green below −10%, white within ±10%, yellow to +25%, orange to +50%, red to +75%, hot pink above.
  - **▾ chart** opens the daily cost chart inside the panel (remembered per browser); **⤢ details** opens a pop-out with the summary figures; the daily cost chart with period totals by category; a signed strip for energy imbalance, reserve, frequency response and other on their own scale; strips for planned flow over the Scottish boundary limits (NESO day-ahead flows and limits), wind turned down (Elexon accepted bids on wind units) and mean wind (local logs), all aligned by day with their own date axes; a correlation table; who was paid (gas and others up, wind down, net BM cost); and a half-hour heatmap. Tooltips give each day's rank, ratio to the 30-day average, NESO breakdown, constraint split, costliest boundary and drivers.
  - **Event markers** (▲ high / ▼ low, red bad / green good, with tooltips): highest cost since the stored history began (confirmed or provisional), lowest cost day, peak reserve and best imbalance day, peak flow over limits, most wind turned down, highest wind, and from the local logs a "major infeed loss" (≥ 0.2 Hz fall within 30 s to ≤ 49.85 Hz, or below 49.6 Hz) or a "frequency low" (day minimum below 49.76 Hz, noting when the fall was gradual).
  - **Store and cadence:** everything is kept in `balancing_store.json` (git-ignored, daily backup `balancing_store.bak.json`, about 400 days). NESO refreshes every 6 h, refetching from 10 days before the newest confirmed day or from any older day with a missing part. Elexon: today period by period as each settles (every 30 min), a finished day whole once and again two days later as final. First run backfills 50 days (~700 MB from Elexon); then ~45 MB a day. UK settlement days throughout, including 46/50-period clock-change days.
  
  (`balancing.py` 261017.1 new, `grid_server.py` 261017.1, `grid_dashboard.html` 261017.1)

## 2026-10-16

- **Reservoirs: water companies across England, with history.**
  - **Coverage:** every English region now has water-company reservoir cards.
  - **Sources:** Stream (the industry's open data portal) gives the history: Yorkshire, Southern, Wessex, Severn Trent, Northumbrian (incl. Essex & Suffolk), South West Water, SES and South Staffs. Company websites give the weeks Stream lags (Severn Trent, South West Water, Northumbrian, Essex & Suffolk) and companies not on Stream (United Utilities, Bristol Water).
  - **Store:** everything read is merged into `reservoir_store.json` (daily backup `reservoir_store.bak.json`). For the same reservoir and date, Stream wins over the website. The South West Water values logged by 261016.2 are migrated in.
  - **New Stream layers** are found by a daily catalogue search.
  - **Cards** show the company total year by year (Jan–Dec, one line per year) and % full bars with a last-year tick. Click a reservoir for its full history (`/api/reservoirs?series=co|key`). Values older than 45 days are dimmed.
  - **Local row:** company totals for the region, the reservoir nearest home and the lowest large reservoir.
  - **England tab:** company totals and a **Data sources** table: newest data per source, its age in days, and the last check.
  - **Wording:** badges and the region figure read "37% full · below average" (not "37% below average", which read as a gap rather than a level); England reads "54% full · normal ≈75%".
  
  (`reservoirs.py` 261016.2–.3, `grid_dashboard.html` 261016.2–.3, `grid_server.py` 261016.2)
- **Reservoirs on the EA page.** A Reservoirs section at the top of the Environment Agency page, with two rows of badges and a drop-down for any region.
  - **Local row:** drought status, region % full against normal, Lower Thames group near London, and the nearest sound live gauge within 60 km. A location in Wales shows "Reservoir data not available in Wales" (the same for Scotland and NI).
  - **UK row:** England weekly % full and its gap to the long-term average; Scotland, Wales and NI not available.
  - **Drop-down,** one view per EA region or England:
    - monthly % full since Jan 2024, with England for comparison;
    - drought status and the EA area reports' reservoir paragraphs;
    - live gauge cards with sparklines;
    - England adds the weekly series since Jul 2025;
    - South-east adds the London groups against their range since 1989.
  - **Sources:** EA national monthly report (regional table read from the PDF with optional `pypdf`), EA weekly drought summaries, EA area summaries, EA flood-monitoring reservoir gauges, London Datastore.
  - **Gauge checks:** day-to-day jumps over 1 m and dead sensors are flagged and kept out of the badges, and spikes are filtered.
  - **Server:** new `GET /api/reservoirs`, built in a background thread (reports every 6 h, gauges hourly). Cache in `reservoir_cache.json`; daily gauge log in `reservoir_levels-YYYY-MM.jsonl`.
  
  (`reservoirs.py` 261016.1 new, `grid_server.py` 261016.1, `grid_dashboard.html` 261016.1)

## 2026-10-15

- **Monitored areas: optional names.** Each slot in the Monitored areas popup has an optional name field (e.g. "Les", up to 30 characters). Alerts for a named area speak the name and place — "This affects Les in Plymouth", "Power has been restored for Les in Plymouth" — and areas without a name keep the postcode and place. Names are stored with the area in `powercut_watch.json` (`alias`), can be changed without a new postcode lookup, and are kept on a rejected entry so it can be corrected; a name without a postcode is ignored. `/api/powercut-watch` accepts `{"pc", "alias"}` objects as well as plain postcodes; `watch.matches` carry `alias`. (`powercuts.py` 261015.1, `grid_server.py` 261015.1, `powercuts_page.html` 261015.1, `grid_dashboard.html` 261015.1)

## 2026-10-14

- **Power cuts: monitored postcode areas.** Up to 10 postcode sectors (`EX23 4`) or full postcodes (`EX23 4AA`) can be monitored in addition to the near-you radius, set from a new **Monitored areas** button on the power-cuts page (10 slots, ✕ to clear a slot, Save). Each entry is checked on postcodes.io when saved, which also gives the place name (civil parish, else ward in Scotland/Wales/NI, else district); unrecognised entries are shown with the reason and not saved; an entry whose lookup fails on the network is kept, marked unverified. The list is stored server-side in `powercut_watch.json` (git-ignored), so it survives restarts and is shared by every browser. New `GET/POST /api/powercut-watch`. (`powercuts.py` 261014.1, `grid_server.py` 261014.1, `powercuts_page.html` 261014.1)
- **Monitored areas: matching.** The server matches each incident's postcodes against the list and returns the hits in `/api/powercuts` as `watch.matches` (area, place, operator, incident, customers off, `level`). A sector matches any full postcode in it or the same sector; a full postcode matches only itself, or — where the operator publishes sectors only (UKPN, SPEN) — its sector, as `level: "may"` ("may be affected"). Outward-code-only tokens never match. ENWL publishes no postcodes and ESB uses Eircodes, so neither can match. The button shows the number of monitored areas with a cut now. (`powercuts.py` 261014.1, `grid_server.py` 261014.1, `powercuts_page.html` 261014.1)
- **Power cuts: NPg incidents keep all their postcodes.** Collapsing NPg's one-row-per-stage records to one incident kept only the representative row's postcode; the postcodes of all collapsed rows are now kept on the incident. Customers-off handling is unchanged. (`powercuts.py` 261014.1)
- **Monitored areas: alarm.** New alarm category "Monitored postcode areas" (on by default): a two-tone siren (~2.7 s), then speech, when an unplanned cut or planned works start in a monitored area — "A monitored postcode area has suffered a power cut. The area is PL3 5, Plymouth. 53 customers are without power." Customer figures are summed over the area's new incidents and omitted for SPEN (no published count). Several areas at once become one message. Planned works name the start/finish time when within a week. A soft tone and speech when an announced incident leaves the feed with its operator's feed live ("Power has been restored in monitored area …"). Each incident is announced once, remembered for 48 h (`pcWatchSeen`); muted providers are skipped. Test button plays the siren and a sample message. (`grid_dashboard.html` 261014.1)
- **Power cuts: UKPN live incidents only, counts honest.** The UKPN dataset also carries restored incidents (73 of 107 rows on 14 Oct) and `MULT-` rows grouping incidents already listed; both were counted in the UKPN and national incident totals and plotted as "0 off". They are now dropped. `nocustomeraffected` is 0 until UKPN confirms a figure (the "Est. N" on UKPN's website comes from `NoCustomersPredicted`, which the dataset lacks; the website's own API refuses scripted access), so a live 0 is now `customers_off: null` with `count_pending: true`, and the map tooltip says "customer count not yet confirmed by UKPN". Planned works take `noplannedcustomers` as the predicted figure. `fullpostcodedata` is kept as `full_postcodes`, so monitored full postcodes match UKPN incidents exactly rather than as "may be affected". (`powercuts.py` 261014.2, `powercuts_page.html` 261014.2)
- **Power cuts: Northern Powergrid finished incidents dropped.** The NPg dataset keeps completed incidents (`incidentstatus` 6, `isaffected` 0, "The scheduled work has now been completed"): 31 of 38 on 14 Oct, carrying 1,366 customers counted as off in the national total, history and alarms. They are now dropped. Planned/unplanned is taken from `natureofoutage` (was always "type not stated"), which also replaces the numeric `powercutcategory` code as the category. A live 0 confirmed is `customers_off: null` + `count_pending`, with the predicted figure kept. (`powercuts.py` 261014.3)
- **Power cuts: ESB finished planned outages dropped.** ESB leaves planned outages listed as "Planned" after their window (four on 14 Oct, ~1,000 customers, one ended 6 Oct). A planned outage more than 1 h past its end time is now dropped; one with no end time is kept with `end_unknown` ("end time not given" in the tooltip). ESB's `dd/mm/yyyy HH:MM` times are converted to ISO local time, so the dashboard can speak them. (`powercuts.py` 261014.3, `powercuts_page.html` 261014.3)
- **Power cuts: NGED planned works window.** Planned works take their start and end from `planned_outage_start_date` / `planned_outage_end_date` (`etr` is blank for them), so tooltips and alerts give "until …". (`powercuts.py` 261014.3)
- **Spoken power-cut times honour UTC offsets.** The near-you and monitored-area alerts read every time as operator local time, so SSEN (`…Z`) and NPg (`+00:00`) restoration times were spoken an hour early during BST. Times with a zone are now converted; bare times are still local. (`grid_dashboard.html` 261014.2)
- **Power cuts: SPEN finished planned works dropped; NIE times readable.** Checked with keys: ENWL (1 live incident) clean; SPEN's restored filter works (58 of 66 rows dropped), but a planned outage due to end at 12:00 on 24 Sep was still listed as "Awaiting" on 14 Oct — SPEN planned works more than 1 h past their end time are now dropped (same rule as ESB, shared `_ended()`), and take their start from `planned_outage_start_date`. NIE is clean, but its times come as `7:26 PM, 14 Oct`; now converted to ISO local time (missing year inferred as the one nearest now), so the dashboard can speak them. (`powercuts.py` 261014.4)
- **Power cuts: overdue incidents.** An incident whose estimated restoration is more than 7 days past (e.g. a SPEN fault due back on 24 Sep, still listed as "Awaiting" on 14 Oct) is probably closed but never removed by the operator — not confirmed either way. It is now moved out of `incidents` into a new `overdue` list (with `overdue_days`): left out of every total, the monitored-area matches and the near-you list, but its id stays in the new/cleared diffing and in the dashboard's active set, so going overdue is never counted or announced as a restoration. Applies to all operators. Operators carry an `overdue` count. Map: drawn as a small grey dot, no pulse; tooltip "overdue — estimated restoration was N days ago; possibly closed by … but still listed, not confirmed. Not counted in any total." (`powercuts.py` 261014.5, `powercuts_page.html` 261014.4, `grid_dashboard.html` 261014.3)
- **Rainfall prediction fine tuning.** Approach alerts were checked against two weeks of recorder data (30 Sep – 13 Oct; radar, EA gauges and engine decisions side by side), with radar at home as the reference for arrival. Alerts were 16 of 18 right, but 7 of 9 "fizzled out" messages were wrong: arrival was judged only from the one EA gauge within 5 km (47155, 2.9 km) or the home model, and the "overdue, not closer" rule timed out while the leading edge sat at the 20 km inner ring. (A) Radar rain at home (mean of a ~1 km box, ≥ 0.3 mm/h, frame ≤ 20 min old) now counts as arrival (`arrived_src: "radar"`). (C) A due fizzle is held — not spoken — while radar shows ≥ 0.3 mm/h anywhere 5–25 km upwind (±45° of the steering flow); the episode then ends on arrival, on a later clear check, or silently at 3 h, and the hold is logged once as `fizzle_held`. Without radar the engine behaves as before. Replayed: spoken approach messages right 67% → 89%; wrong fizzles 7 → 0; alert distances, persistence and fizzle counts unchanged (the data supported them). New `radar_home_view()`, cached per radar frame. (`rain_probe.py` 261014.1)

## 2026-10-06

- **Wind gusts: London has a station; foreign stations out of the rollups.** The AWC bbox query returns a thinned set (~56) without London City, Gatwick, Biggin Hill, Northolt, Luton or Southend, so the London licence area had no gust in any sample since 260917.1. These are now fetched by id in a second call and merged. London rests on EGLC alone (the only METAR inside the area), which is silent while the airport is shut (Sat midday – Sun midday). French, Norwegian and Channel Islands stations (8 today) were filed under England/Scotland by the nearest-country fallback, so a Brittany gust could stand as England's; they are now kept but feed no region, ALL included. METARs older than 3 h are dropped. `status()` counts UK/IE stations and reports `foreign` separately. (`metar.py` 261006.1)
- **Wind gusts: Heathrow stands in for London.** While EGLC has no current report, the London area takes Heathrow's (EGLL) gust, marked `stand_in: "EGLL"` with `n_stations: 0`. Added to the London area key only, so Heathrow still counts once in Southern England, England and ALL. Observed, so it feeds the learned curves as well as the display; no model fallback. (`metar.py` 261006.2)
- **Power cuts: frozen-feed detection.** NPg's feed answered normally but repeated identical incidents from 01:09 on 3 Oct to 07:47 on 5 Oct, reported `ok` throughout. An operator whose incident set (ids, customers off, status text) is unchanged for 3 h of daytime (06–22 UTC) is now flagged `stale` with the reason and the time it last changed (`frozen_since`); it stays in the totals and clears on the first change. History rows carry `"frozen": true`. Backtest on Sep–Oct aggregates: no flags on UKPN, SSEN, SPEN, ESB; flags NPg 3–5 Oct, NIE 25–27 Sep, NGED 19 Sep and several ENWL spells. Tracking is in memory and restarts with the server. (`powercuts.py` 261006.1)

## 2026-10-01

- **Power cuts near you: border, totals and summary.** The Near-you box on the power-cuts page:
  - takes a 2 px border coloured by the nearest listed cut (planned or unplanned) as a share of the set radius: under 100% yellow, under 50% amber, under 20% red, under 10% hot pink (at 10 km: <10 / <5 / <2 / <1 km); no cuts in range, the plain border;
  - with two or more cuts in range, shows the number of cuts and total customers off under its heading, with the unplanned/planned split (a "+" where a cut reports no count);
  - lists the three nearest cuts in order of distance (was up to six plus "and N more"), with any others in the radius totalled on one line: distance band, count, customers off and split.
  
  (`powercuts_page.html` 261001.1–.3)

## 2026-09-30

- **System risk: CGRI no longer alarms inside the operational band on a normal grid.** The inertia multiplier was 1 + notional RoCoF/0.5 at all times (~1.4× on a typical day), so CGRI reached its 0.2 amber cut at ~0.14 Hz off nominal. Replayed over September's logs: 386 amber episodes (amber 11% of the time), only 14 with frequency outside 49.8–50.2 Hz. Inertia now amplifies CGRI only when reduced (notional RoCoF ≥ 0.25 Hz/s, the inertia band's amber cut): same replay gives 24 episodes (0.5% of the time), all 19 band excursions still alerted, 5 more on weak-inertia grids. (`grid_server.py` 260930.1)
- **System risk alert text states the real cause.** Amber alerts said "outside the normal operational band" whenever inertia wasn't reduced, including when frequency was inside the band and only CGRI had crossed. The server now reports what raised the level (`drivers`: statutory / band / cgri / gen_loss) and the alert names it: outside the band, reduced inertia (with GVA·s and notional RoCoF), frequency moving quickly, or a sudden infeed loss. `system_risk` also carries `m_inertia`. (`grid_server.py` 260930.1)
- **Spoken frequency alarm no longer blames inertia for every excursion.** The voice appended "System inertia reduced" / "System inertia is low, limited reserve" from the composite risk level, so any high or low excursion was blamed on inertia. It now uses the inertia estimate's own band and says nothing about inertia when it is normal; "limited reserve" is dropped (reserve is not what the risk level measures). CGRI tooltip updated. (`grid_dashboard.html` 260930.1)
- **Rain engine stopgaps (ahead of the rebuild).** (1) Quiet hours no longer slow the radar sea net: while radar serves it reads every 10-min frame around the clock (29 Sep had a 42-min radar gap during an evening onset); the quiet factor still stretches the model net. (2) Mobile trackers read radar first; OpenWeather OC4 (its own estimate, paid) only when radar is unavailable, then the model — during 29 Sep's rain the trackers made ~48 paid calls an hour. A tracker keeps its reading's source and radar frame time; its card and Forecast view tooltip name the source. (5) Approach arrival window corrected for the age of the leading reading: the window is shifted earlier by that age, and when it is used up the alert says the rain "may arrive any time now" (0–5 min: "within a few minutes"). The threat carries `data_age_s`, also in the shadow log. (`rain_probe.py` 260930.1, `grid_server.py` 260930.2, `forecast_view.html` 260930.1)
- **Port clash handling.** On Windows the server's socket allowed a second program to bind 8412 alongside it with no error, sharing connections unpredictably; it now binds exclusively (`SO_EXCLUSIVEADDRUSE`), so a clash is detected. If 8412 is in use or blocked (Hyper-V/WSL/Docker reserved range) the next free of 8413–8421 is used with a console banner, and remembered in `server_port.json` (tried first next start; git-ignored). A launch that finds GB Energy Monitor already running (new `/api/whoami`, or the dashboard page for older builds) reports the address and exits instead of starting a second copy. An explicit `--port N` is exact; if taken, the free ports are listed to choose from (or listed and the server exits when there is no console). The dashboard footer shows the port. (`grid_server.py` 260930.3, `grid_dashboard.html` 260930.2)
- **850 hPa wind on the wind dials.** The compass shows the 850 hPa wind (the rain engine's steering flow, ~1.5 km up — the wind showers move with) as a second arrow of the same shape, slightly larger and behind the surface arrow, with a green dotted outline and faint fill; the speed dial shows it as a green dash like the gust marker. Green "850 hPa" lines give direction and speed; the hover text adds the veer/back from the surface wind and the reading's age. Drawn only for a real 850 hPa read (not the surface ×1.6 fallback); an old read is faded and marked "(old)". (`grid_dashboard.html` 260930.3)
- **Known issues: rain / forecast engine.** New section documenting how the rain sources are merged (timing, cadence, spatial coverage), what the September logs show, and the planned rebuild.

## 2026-09-29

- **ESB feed fix.** ESB's outage list now arrives gzip-compressed even when not requested, so every parse failed and the feed went stale/down. Responses are decompressed when gzipped (header or magic bytes). (`powercuts.py` 260929.1)
- **Power-cuts plots.** Orange "active incidents" key on the customers-off plot. Hover crosshair and readout on both plots, reporting the sample under the pointer (held value in stepped mode). (`powercuts_page.html` 260929.1–.2)
- **Power-cuts map.** Borders, dot rims, pulse rings and region labels stay one screen size at any zoom; dots grow only by √zoom. Hover card per incident: operator, type, customers off, start and restore/finish time, status, postcodes, distance from home, new/muted flags, overlap count. Status text ends at a sentence; ESB's stock text ends at "as quickly as possible." (`powercuts_page.html` 260929.2–.4)
- **By operator and grid events.** "⤢ by operator" opens a pop-up with customers off stacked by operator (band stops where a feed was down; SPEN has no counts). A strip under both plots shows grid events from the alert journal (warning and above, feed-data excluded), merged per kind, critical-only above 40 marks, with titles on hover. (`powercuts_page.html` 260929.5–.6)
- **README.** Power-cuts section gains *Map* (zoom, hover card) and *History plots* (hover readout, by-operator pop-up, grid-event strip) paragraphs.
- **Rain engine: radar sea net.** The offshore net, home ring and land probes read measured radar (RainViewer, 10-min frames, zoom 7, Universal Blue decoded to dBZ, mm/h by Marshall-Palmer) and fall back to Open-Meteo when radar is unavailable. Radar readings are labelled measured and dated by frame time; net interval 10 min while radar serves. Prompted by a band on 29 Sep that radar showed at 1–9 mm/h on the south/south-west sentinels at 16:50 UT while the model net read dry. (`rain_probe.py` 260929.1, `grid_server.py` 260929.1)
- **Rain engine runs without a page open.** A background sampler runs it every 5 min at the location pages last requested (`rain_home.json`, git-ignored). The wind read no longer sits inside the EA try, so an EA failure can't skip the engine; engine cycles are serialised with a lock. (`grid_server.py` 260929.1)
- **Late EA gauges.** A gauge whose newest reading is over 75 min old (normal EA lag reaches ~60 min just before each batch) is marked late: excluded from the situation field (not read as dry, not diluting coverage); if all are late the home ring stands in, and EA counts again only below 60 min. The home ring keeps its readings when switched off, samples at once when switched on with nothing cached (free sampler only), reports "OWM budget spent" only when true, is labelled by its real source (radar ring was named "OpenWeather backup"), and uses radar even while Open-Meteo is backing off. (`rain_probe.py` 260929.1–.2)
- **Open-Meteo precipitation units.** Open-Meteo's `current` precipitation is the amount over the preceding `interval` seconds (900 = 15 min, confirmed on the self-hosted instance), but the engine used it as mm/h, so every model reading was 4× too low and the 0.3 mm/h wet threshold was effectively 1.2 mm/h. Amounts are now converted to mm/h from each response's interval (900 assumed if missing). (`rain_probe.py` 260929.3)
- **Virtual rain points stay virtual.** 260929.1 flagged radar-fed net and ring points `modelled: false`, which the dashboard reads as "real EA gauge": their cards were drawn as gauge cards and one could anchor the rainfall map's centre. `modelled` again marks every virtual point; a new `measured` flag says whether the reading is radar/OpenWeather. Radar-fed net and ring cards show a RADAR chip. (`rain_probe.py` 260929.4, `grid_dashboard.html` 260929.1)
- **README.** Rain sections describe the radar sea net, late EA gauges and the background engine; self-hosting check uses `127.0.0.1` and explains how to spot another program on the same port.
- **Weather panel: Low / High today.** The Min/Max temp rows were always "—" on One Call 4.0 (its current endpoint has no min/max; 2.5's temp_min/temp_max were only the spread observed across the area at that moment, not the day's range). They are now **Low today** / **High today** from Open-Meteo's daily forecast for the location (local-first, cached 1 h, reset at midnight, separate from the cloud call), tagged OM with a "model, not a measurement" tooltip; "—" if Open-Meteo is unavailable. Server footer build label corrected (was left at 260928.4). (`grid_server.py` 260929.2, `grid_dashboard.html` 260929.2)
- **Build tags for the remaining modules.** `gas_probe.py`, `metar.py` and `owm_onecall.py` now carry the standard header build line, so every code file has one. No code changes. (`gas_probe.py`, `metar.py`, `owm_onecall.py` 260929.1)
- **Rain gauge cards no longer read 0 while EA is late.** A card zeroed its rate once the reading was over 30 min old, but EA readings routinely reach the dashboard 30–60 min late, so in heavy rain the cards showed 0.0 mm/h while the gauge plot and radar showed the rain. Cards now keep the last reported rate, dim it past 30 min (age in the tooltip), and show "—" (not known, never 0) only past 75 min — the rain engine's late limit. The dashboard's rain pop-up uses the same 75 min, so a late batch can't trigger "Rain stopped"; its "… now" wording still needs a reading under 30 min old. (`grid_dashboard.html` 260929.3)
- **Forecast view.** Late gauges drawn as dashed hollow rings with a note; EA gauges line shows the late count; radar net points labelled; API line counts OM-local separately and lists radar calls; ages over 90 min shown to 0.1 h; RainViewer credit. (`forecast_view.html` 260929.1)

## 2026-09-28

- **NESO notices tiered by type.** An Electricity Margin Notice was raised as a critical alert and spoken as critical, because the classifier matched words anywhere in the text and every margin notice's body mentions "Demand Control". Tier now comes from the notice type (Elexon `warningType`, or the notice's heading for type OTHER), never the body: demand control is critical; high risk of demand reduction (critical from 2 h before its window), Capacity Market Notice and risk of system disturbance are warnings; Electricity Margin Notice, NRAPM, Demand Flexibility Service, emergency instruction to a single unit, interconnector emergency assistance, geomagnetic notices and unrecognised types are notices; IT, SO-SO trades, tests (including demand control by voltage reduction tests) and manifest-error claims raise nothing. Checked against every SYSWARN notice since 2023: none reaches critical. (`grid_server.py` 260928.1)
- **Cancellations, updates and expiry.** The server reads the last 48 h of SYSWARN (was only the latest notice). A cancellation or newer notice of the same kind for an overlapping window cancels or replaces the older one; a notice whose window has ended drops out (a Capacity Market Notice, which states no end, 6 h after it commences; a notice with no window, 24 h after publication). Windows are read in UK local time. Each notice carries `kind`, `tier`, `status`, `name` and `when`. (`grid_server.py` 260928.1)
- **Alert text.** NESO alerts are titled by type and window (e.g. "NESO: Electricity Margin Notice · 16:00–19:00 Mon 28 Sep") with a plain-words line on what the notice means. "System nominal" says when an advisory NESO notice is in force. (`grid_server.py` 260928.1)
- **Dashboard.** The system-warnings alarm uses the server's tier and speaks the notice type and window instead of the raw text; notices are a soft blip, cancelled/ended/operational ones are silent. A critical system warning that clears now stops the critical repeat (it previously kept running). The System Warnings panel lists notices in force with a severity chip, then up to three recent ones not in force, dimmed, with their status. The alarm panel description and test phrase updated. (`grid_dashboard.html` 260928.1)
- **README.** System Warnings panel description updated.
- **Frequency feed down shows "—".** When the newest frequency reading is more than 5 min old, or there is none, the server sends no value (`hz` null, `down` true) with the time of the last reading; the dashboard shows "—" for the frequency, deviation, dial and drift, hides the dial marker, and reads "no data since HH:MM UT (N h NN min)". The trace keeps its real history, and with no history in the last 2 h it still draws its grid with "no frequency data in the last 2 h". The fallback to `datasets/FREQ` is removed: it ends at the previous midnight, so during today's Elexon outage the stale alert quoted the 00:00 reading (555 min) rather than the real last one (03:33 UT). The last reading's time is kept in memory and read from the 15 s log after a restart (newest by timestamp, since an older build could append the midnight archive point after the real last reading). The "Frequency feed stale" alert now gives that time and duration, and the footer marks frequency failed while it is down. (`grid_server.py` 260928.2, `grid_dashboard.html` 260928.2)
- **System risk while data is missing.** With no current frequency the risk level is not set (badge "—"; hysteresis resumes on recovery). With a generation mix older than 15 min, inertia, "if largest trips" and CGRI are not set. (`grid_server.py` 260928.2)
- **Generation log: rows at their own time, no replayed backlog.** Each generation-mix row in the data log is now stamped with the FUELINST batch's publish time (`t`), with the write time in `logged`; it was stamped with the time it was written. Only a batch published within 15 min is logged, and never one already logged: after today's Elexon outage the backlog replayed at about one batch a minute and was written at 10:36–10:59 BST with current solar, battery, demand and risk figures beside it, and each server restart re-wrote the last pre-outage batch. Those rows blocked the BMRS gap-fill in their half-hours, giving the spikes in the archive plot. Replayed batches now leave the hole to the gap-fill, and on startup the server reads the last logged batch from the log. The 28 bad rows were moved out of `grid_log-2026-09-28.jsonl` into `removed-grid_log-2026-09-28.jsonl`. (`grid_server.py` 260928.3)
- **Per-gauge rain-alert mute.** Selecting a rain gauge shows an "alerts normal · <0.3 · mute" switch in the plot header. "<0.3" makes the rain alerts read that gauge's readings under 0.3 mm/h as dry; "mute" leaves the gauge out of the rain alerts. Applies to the rain engine's input and the page's rain popup; the cards and plot still show the raw readings. Held on the server in `gauge_mutes.json` (git-ignored), keyed by EA station reference, set via `POST /api/gauge-mute`, read via `GET /api/gauge-mutes` and with `/api/ea`. Takes effect at the next poll. (`grid_server.py` 260928.4, `grid_dashboard.html` 260928.3, `.gitignore`)
- **Rain card dot and no-report dimming.** Each gauge card has a dot before its name: green normal, amber "<0.3", red muted. A gauge with no report for over 3 h keeps its card, with the name dimmed to mid grey. Card size unchanged. (`grid_dashboard.html` 260928.3)
- **README.** Rain gauge mute and dot documented; `gauge_mutes.json` added to the files created automatically.

## 2026-09-27

- **Self-hosted Open-Meteo, with automatic fallback.** When `OPEN_METEO_BASE` points to a self-hosted Open-Meteo, every Open-Meteo request (cloud, offshore/land rain net, 850 hPa steering wind) tries it first with a 3 s timeout. If it is unreachable, returns an error, or answers with only null values, the same request goes to the public host instead, without `models=` (the previous behaviour). Local is then skipped for 5 minutes and retried; it recovers without a restart. Up/down transitions are logged once each to `om_debug.jsonl` (`local_down`, `local_up`, with the reason). `OPEN_METEO_BASE` unset: public host only, unchanged. (`rain_probe.py`, `grid_server.py` 260927.2)
- **`OPEN_METEO_MODELS`.** Optional setting (e.g. `icon_eu`) sent as `models=` to the self-hosted instance. Needed because a self-hosted instance holds only the models you synced, and its default model choice returns all-null values. (`rain_probe.py`, `grid_server.py` 260927.1)
- **Separate public/local rate-limit handling and metering.** The public-host rate-limit backoff now applies only to public requests, so a public daily cap never blocks the local instance. Local calls are metered as `OM-local`, public as `OM`, in `api_usage_daily.json`. The cloud read now honours the public backoff (previously it called regardless) and a public rate-limit error body on it trips the shared backoff. (`rain_probe.py`, `grid_server.py` 260927.2)
- **Open-Meteo source label.** The snapshot carries `open_meteo` (mode `local`, `public_fallback` or `public`, with reason and retry time). The footer status strip shows **Open-Meteo: local** or **Open-Meteo: online** (green), amber with "local unavailable: <reason> · retry in N min" when a configured local instance is down, and amber "rate-limited" / "daily limit reached" when the public host is refusing. The EA page cloud source reads "Open-Meteo (local)" when the local instance supplied it. (`grid_dashboard.html` 260927.1, `grid_server.py` 260927.2)
- **Land probes unthrottled on self-hosted Open-Meteo.** While the local instance is serving, the land probes have no per-episode sample cap (public: 8), sample every 15 min while gauges are wet (public: 30 min, stretched in quiet hours) with all points (public: 1), run the clear-check 15 min apart (public: 30), and use three upwind ranges, 9/16/25 km (public: 9/16). Reading freshness, the wet threshold and episode gating are unchanged. On fallback to the public host the public limits apply again, and the episode's samples so far count towards its cap. The probe summary carries `limits` (`local`/`public`). (`rain_probe.py` 260927.3)
- **README.** Self-hosting Open-Meteo documented: setup, `OPEN_METEO_MODELS`, choosing and changing a port, the Docker Desktop port-forwarding error, fallback behaviour and the footer label.

## 2026-09-26

- **Build history moved out of file headers.** Header changelogs removed; each file now carries only its current build and a pointer to this file. Dashboard footer build label corrected (was 260918.4). (`grid_dashboard.html`, `powercuts_page.html`, `powercuts.py`, `windcuts.py` 260926.1)
- **Forecast view.** The engine view is now the **Forecast view** (`forecast_view.html`, served at `/forecast`); the `/engine` address and `engine_view_live.html` are removed. Gauge tooltips give the place, grid reference and EA reference, plus distance and direction from home; sea points, tracks, probes and trackers also give distance and direction. A radar-only mode (`/forecast?view=radar`) feeds the EA page's gauge radar. The location comes from the link or the EA page's saved location (no built-in default), the page connects by itself, the developer notes at the bottom are replaced by a short description, and the paste-JSON box is removed. (`forecast_view.html` 260926.1, `grid_server.py` 260926.1)
- **EA page: gauge radar and under the hood.** "⇅ swap panels" and its code are removed; local weather always sits above rainfall. A **◎ gauge radar** toggle in the plot area shows the Forecast view's radar there; clicking again, or picking a gauge, returns to the plot. The radar is not reloaded by EA refreshes and is unloaded when closed. **⚙ under the hood** in the top bar opens the full Forecast view in a new tab. (`grid_dashboard.html` 260926.1)
- **Header: live powercuts map.** A **🔦 live powercuts map** button opens the power-cuts map (same as the power-cuts chip) in the place **history** was; **history** now sits to the right of **my home**. (`grid_dashboard.html` 260926.1)
- **Flood alarm no longer repeats on EA feed blips.** A failed EA floods call was reported as zero floods, which ended the episode, so the same standing alert was announced again when EA recovered. The server now keeps serving the last good counts (national and near-you), marked stale with their age, and retries after 1 minute; the dashboard never treats an error or stale reply as "no floods", and a flood episode only ends after 30 minutes of good readings with none in force. New warnings, severe warnings and new floods near you still speak at once. While its count is held the flood chip shows a "*" in its alert colour, and the EA page shows "Backend unreadable for N minutes — showing the last good flood data". (`grid_server.py`, `grid_dashboard.html` 260926.1)
- **Power cut near you.** New alarm category that watches for power cuts within 5, 10 or 20 km (default 10, set in the alarm panel) of the location set on the EA page, planned works included. A new unplanned fault is spoken as a warning with distance, direction, customers off and estimated restoration; planned works as a quieter notice with start and finish; each again when it's over. Announced once per incident (remembered across reloads for 48 h); several at once become one message. Incidents without coordinates match on your postcode district. Muted providers are skipped, and "restored" needs the incident to have left a live feed, so a feed outage, a mute or a radius change can't pass for one. The power-cuts chip tooltip gains a "near you" line. (`grid_dashboard.html` 260926.1)
- **Power-cuts map: near you.** Your location is marked on the map with the near-you radius drawn around it, and the By-Operator panel opens with a "Near you" list (distance, direction, planned/unplanned, customers off, estimated restoration or finish). (`powercuts_page.html` 260926.1)
- **Reverse postcode lookup.** `/api/geocode?lat=…&lon=…` returns the postcode at a point (used for near-you matching of incidents with postcodes but no coordinates). (`grid_server.py` 260926.1)
- **Rain gauges identified by EA reference.** The EA labels every rainfall gauge "Rainfall station", so the probe now keys gauges by station reference and names them by their place (else grid reference). Without this, gauge flags and track hand-over would have treated all gauges as one. (`rain_probe.py` 260926.1)
- **README.** Forecast view, gauge radar, under the hood, the header buttons and power cuts near you documented; `gas_probe.py` added to the file list; the rain and gas logs and `powercut_last_ids.json` added to the files created automatically; an "Other files in this repository" section added; the reference to `om_diag.py` (not part of the repository) removed; the note that `rain_probe.py` patches the dashboard on startup is removed (that code no longer exists).
- **Rain probe Stage 2 rebuilt.** The 850 hPa steering wind (hourly Open-Meteo read, kept up to 3 h, else surface wind × 1.6) is now the one flow for the approach cone, land front, gauge approach, track checks, land-probe placement and sea-tracker speed. Measured land, sea and track speeds must be 0.5–1.5× the steering speed and within 40° of its direction, else withheld with a reason. Tracks need a gauge hand-over, judged on the gauges wet now, before a speed counts; motion and track ETAs use the wet-now centre. Stale tracks are never approaching. Gauges wet in ≥ 75 % of ≥ 10 readings over 3 h are flagged stationary or suspect (hourly model check) and left out of tracking and the approach threat. (`rain_probe.py` 260926.1)
- **Sea net on the OpenWeather fallback.** Of the 4 sea points read per sample while Open-Meteo is capped, 2 are nearest the flow's source and 2 rotate round the rest (previously the same 4 every time; 240°/260° and all pickets were never read). Unread points keep their last reading with its age; a kept reading is stale after 60 min and can't detect. Points are labelled by the source that read them, and the engine view gets real reading ages. (`rain_probe.py` 260926.1)

## 2026-09-23

- **Steering-wind check on rain motion.** Track, land and sea speeds are now checked against the 850 hPa steering wind (one Open-Meteo read per hour, surface wind × 1.6 fallback). A speed outside 0.5–1.5× that wind, or heading more than 40° off it, is withheld as unverified, and the ETA uses the steering wind instead. Track motion now needs member gauges to hand over, not just a centroid shift; leftover (stale) tracks are never marked approaching. (`rain_probe.py` 260926.1)
- **Stationary/suspect gauge flag.** A gauge wet in ≥ 75 % of readings over 3 h is excluded from approach tracking. An hourly model spot-check labels it stationary (static drizzle) or suspect (possible fault). The engine view shows the flag, the steering wind, and "speed unverified" / "stale" on tracks. (`rain_probe.py` 260926.1, `engine_view_live.html`)
- **One approach voice for rain.** The four overlapping "rain approaching" voices (land-front, sea-arc, cluster-track, and the bridge into the Rain channel) are replaced by a single approach episode: alert → optional single confirm → silent arrival or one "fizzled out" message. It considers every wet cell (≥ 0.3 mm/h) in the upwind cone from all sources. Organised bands (≥ 3 cells ≥ 15 km across the wind, ≥ 2 mm/h, or falling pressure) alert from 35 km; isolated showers only within 12 km with an ETA midpoint ≤ 40 min. Timing counts 15-min readings, not poll minutes (2 to alert, 2 empty to fizzle). A fizzled threat can't re-alert unless it upgrades or comes clearly closer. Area shower notes stay quiet while an approach is being weighed or run. (`rain_probe.py` 260923.1)
- **Rain episode and shadow-threat logs.** `rain_episodes-YYYY-MM.jsonl` records each alert/confirm/arrival/fizzle. `rain_threats-YYYY-MM.jsonl` records every threat, alerted or not, with location-agnostic features and its outcome, as groundwork for a per-install learner. Both are git-ignored. (`rain_probe.py` 260923.1, `.gitignore`)
- **Nowcast de-duplication.** The nowcast channel no longer repeats approach-episode lines when Rainfall is armed. (`grid_dashboard.html` 260923.3)
- **Frequency alarm judges the whole publish burst.** BMRS delivers ~8 × 15s frequency points together every ~2 min; the alarm, system-risk level and FREQ/RISK alert cards previously judged only the newest point, so a mid-burst excursion that recovered by the last point never alarmed and the voice quoted the newest value rather than the extreme. The frequency payload now carries `burst_min`/`burst_max` (exactly one burst, 120 s of measurement time); `compute_system_risk` judges dev/CGRI/level on the extreme (new `hz_judge`, `hz_min`, `hz_max`; `hz` stays the newest point); alert text reads "dipped to X Hz in the latest 2-min data, now Y Hz". (`grid_server.py` 260923.1)
- **Alarm voice and hysteresis use the extreme.** Spoken alerts say "dipped to X hertz, now Y" (or "peaked at"); tier hysteresis clears only when the burst's extreme on the alarmed side has recovered; the worsening fallback compares burst-to-burst on the same side; the frequency panel colour uses the extremes. (`grid_dashboard.html` 260923.1)
- **Excursion arrow in the frequency dial.** Animated chevron arrow in the dial centre — down for low, up for high, amber at tier 1 and red at tier 2+ — shown from an excursion's first alert for at least 2 min and for as long as it lasts; the chevrons light in sequence without moving; independent of whether the alarm is armed; static under reduced-motion. (`grid_dashboard.html` 260923.1–.2)

## 2026-09-17

- **Power-cuts pop-out on the main dashboard.** A new **UK&IRL power cuts** chip in the header summary strip shows the live UK & Ireland customers-off total, in bold and coloured by the same severity bands as the power-cuts page itself (yellow 25–50k, red 100–500k, hot-pink 500k+). Its tooltip gives the planned/unplanned split (customers off and incident counts), and clicking it opens the full power-cuts page in a full-window overlay with a corner ✕ (Esc also closes) — the standalone `/powercuts` URL still works independently. The page is hosted in the overlay via an iframe, so it stays a single source of truth. (dashboard 260917.1)
- **Wind vs power-cuts correlation tracker.** Two new stdlib modules learn, over time, how the unplanned power-cut *arrival rate* responds to wind. `metar.py` pulls every UK & Ireland METAR in one keyless call to NOAA's Aviation Weather Center (sustained wind + gusts in knots, no API key, no daily cap) and tags each station with its licence area and country. `windcuts.py` joins that wind to each power-cuts sample and accumulates running per-(region, gust-bin) sufficient statistics — so an isolated storm only drives its own region's curve, calm samples define an honest baseline (no false low-wind peak), and the fault-rate metric includes SPEN even though SPEN publishes no customer counts. The power-cuts Statistics panel gains a stats/wind switch with a mini learned-response curve and an enlarge-to-full-page modal (region + faults/customers-off selectors, a live "given the current gust, expect roughly N…" prediction, and learning-maturity states), fed by a new `/api/windcuts` endpoint. Accumulation runs in the server's background sampler, so it builds whether or not any page is open. (new `metar.py`, `windcuts.py`; `powercuts_page.html`; `grid_server.py`; `region_polys.json`)
- **Power-cuts feed resilience.** A feed's last good reading — including a valid empty "no outages" reading — is now kept visible as **stale** for up to **2 hours** of silence before it is marked **down** and dropped from the national totals (previously an empty-then-failed feed such as ESB fell straight to "down"). Region map labels are coloured by freshness — **green** live, **amber** stale, **red** down — and NIE/ESB incidents are placed by operator identity, so an unresolved Northern Ireland postcode still counts to Northern Ireland rather than an "unknown" bucket. (`powercuts.py` 260917.2, `powercuts_page.html` 260917.5, `windcuts.py` 260917.2)
- **Antivirus false-positive fix.** The power-cuts page embeds its map-polygon data as three large arrays that had each sat on a single ~48 KB line — a shape heuristic scanners read as a packed/obfuscated payload. They are now wrapped to short lines (pure whitespace, no behaviour change), and a low-contrast selected-button style in the wind modal was fixed at the same time. (`powercuts_page.html` 260917.3–.5)

## 2026-09-16

- **Power-cuts map extended to the whole UK & Ireland.** Northern Ireland (**NIE Networks**) and the Republic of Ireland (**ESB Networks PowerCheck**) are added alongside the 14 GB DNO licence areas — drawn in the same style, folded into the national totals, each with its own API-key input (ESB ships with a shared default key; NIE needs your own). The page is rebranded **"UK & Ireland Power Cuts"** (every user-facing "GB" → "UK", filenames unchanged). (`powercuts.py`, `powercuts_page.html`)
- **Power-cuts readout & layout refinements.** The national customers-off figure now scales its font up one step per severity threshold (10k/25k/50k/100k/1M) and takes a warning colour on the bands above; the National-now panel gains planned and unplanned rows; the By-Operator panel gains an operators/countries toggle (per-country totals via point-in-country classification); the 6h/24h/7d/30d window selector moved above the plots and the PSR note moved to the below-map legend; the Isle of Wight was split from the mainland coastline; and ESB's stale badge was made more tolerant (longer timeout + one retry before it flags). API keys are stored server-side in `powercut_keys.json` (git-ignored). (`powercuts_page.html`, `powercuts.py`)

## 2026-09-10

- **Power-cuts map: real DNO licence-area boundaries, pastel-blue regions, offshore acronyms, severity-graded incidents.** The map draws the actual boundaries of all 14 GB DNO licence areas (source: NESO "GIS Boundaries for GB DNO Licence Areas", `gb-dno-license-areas-20240503`, reprojected EPSG:27700→WGS84 and simplified to ~1 km; NESO Open Data Licence — approximate, as NESO notes the boundaries shift with new rural connections; the far-north Shetland rings above the map frame are dropped). Areas are filled in dark blues (graph-coloured so neighbours always differ) so the incident dots stand out against them; each area's operator acronym (NGED/UKPN/NPg/ENWL/SPEN/SSEN) sits in a pill out in the sea — western areas down the left/Atlantic side, eastern down the right/North Sea side — each with a thin leader line back to its area (West Midlands, land-locked, points across to the Irish Sea). Unplanned incidents are coloured on a sliding customers-affected ramp — <10 yellow · 50 orange · 100 dark-orange · 500 red · 750 purple · >1000 hot-pink (piecewise-linear, clamped; matched to the real per-incident range — today's largest single GB cut was 174) — with **dot size also scaling to customers off**; planned cuts are light-blue dots. A fixed key in the top-right gives the operator acronym→full-name list and a combined dot-size/colour scale (10/50/100/500/750/1000+). The map spans the full width with the operator table and plots in a grid beneath; the key floats top-right only when the canvas is wide enough to clear the coastline (≥640 px, verified land-free) and otherwise drops into normal flow below the map, and the canvas height tracks width (clamped 360–820 px) so nothing over-expands on desktop or collapses on a phone. Boundary data is embedded (regenerable from `dno_boundaries.json`; Shetland rings above the 59.2°N frame dropped). Verified headless at 343/700/1170 px: 14 acronym labels placed, right column clears the key, zero land under the floating key, heat ramp exact at every breakpoint. (`powercuts_page.html`)
- **Open-Meteo rejection diagnostics — evidence, not guesswork.** When the dashboard reports "Open-Meteo down", the existing call meter could say *how many* calls were made but not *why* they were refused. `fetch_om_precip` now logs every OM HTTP attempt to `om_debug.jsonl` — outcome (ok / error-body / http-error / exception / backoff-skip), HTTP status, the exact rejection text OM returns, and any rate-limit / Retry-After headers — and distinguishes a real OM rejection from time spent sitting out a self-imposed cooldown. A new standalone `om_diag.py` reads that log plus `api_calls.jsonl` and reconstructs the true per-minute / per-hour / per-day call rate (summing OM's per-location counting) against the free-tier limits of **600 / 5,000 / 10,000**, then reports the peak against each and prints recent rejections verbatim. Its verdict separates the three real causes that had been conflated: an actual per-window limit being hit; a "daily / try again tomorrow" reply arriving despite a real count well under 10,000 (which indicates a shared/CGNAT IP, since OM's count is per-IP not per-app); and a "down" that was really a network timeout or a stale cooldown, not a limit at all. No behaviour change to the probe itself; purely additive logging. (`rain_probe.py`, `om_diag.py`)

## 2026-09-09

- **OpenWeather backup gauges when local rain sensing is lost.** When the EA rain gauges are unavailable *and* the offshore net can't run (Open-Meteo down — a shared/CGNAT public IP can sit "daily limit exceeded" regardless of this app's own usage), **eight OpenWeather/OC4 probes are placed around the home location** — at 20 km and 10 km on offset compass points — standing in as virtual rain gauges so there is still a working local picture. They appear on the rain-gauge page with their readings (labelled as modelled OC4 backup, never "sea", since they can sit over land) and feed the situational engine like any other gauge set. Sea-independent, so an inland location with no offshore arc still gets a full backup; budget-guarded through the same OC4 sampler as the mobile trackers (reports "exhausted" honestly, showing no cards rather than blank ones, once the budget can't cover a cycle). It retracts and the normal system resumes as soon as **either** the EA gauges reappear **or** Open-Meteo recovers; when the offshore net is down but the EA gauges are present the backup isn't deployed and the dead net's empty offshore cards are suppressed rather than shown as blank circles. `rain_probe.py` also self-applies the small dashboard change these cards need — on server startup, per-hunk and idempotently, keeping a `.bak` and reporting on the console any anchor it couldn't match. Still modelled, still marked. (`rain_probe.py`, `grid_dashboard.html`)
- **Offshore OC4 fallback no longer reports "blind" on a dry arc.** `owm_onecall.fetch_sea_precip` collapsed a dry reading and a failed fetch into the same `mm=None`, so when Open-Meteo was daily-capped and the sea was simply rain-free, the OC4 fallback's dry readings were counted as no-coverage and the feed showed "exhausted / sea points blind" despite OC4 answering correctly. Dry OC4 reads now return the real rate (`0.0` when dry) with a `sent` flag — matching the Open-Meteo net's contract — and `arc_update` judges fallback coverage by whether the read reached the source, not by whether it found rain. So a capped-but-covered dry arc now reads "degraded" (OC4 tracking, arc dry), and only a genuine OC4 failure reads "exhausted". (`owm_onecall.py`, `rain_probe.py`)

## 2026-09-04

- **Nowcast all-clear no longer false-fires on dry days.** The spoken "No rain is expected in the near term" all-clear was armed by *any* nowcast candidate, including benign dry-sky signals (pressure rising or falling under a clear sky, or a fog/visibility note). A pressure reading drifting across a threshold on a rain-free day therefore spoke a bogus all-clear every time it fell away — several an hour with no rain in sight. The all-clear is now armed only by genuine rain signals (rain at home, or a land/offshore front or minute-ahead onset arriving), so it follows a real rain episode and speaks once. Verified: a dry-day pressure flicker now produces zero all-clears, while a real rain→clear transition still speaks exactly one. (`rain_probe.py`, server 260904.10)
- **System-inertia estimate calibrated to NESO real data.** The output-weighted inertia proxy that feeds the system-risk index was fitted by least squares against NESO's published GB System Inertia (Outturn Inertia, GVA·s) over 242 half-hourly samples spanning April–July 2026 (real range 108–233 GVA·s): `inertia ≈ 1.69 × Σ(MW·H) + 69`, R² 0.90, mean error 7.8 GVA·s. The previous seed constants (×1.4 + 30) under-read inertia by ~35 % (mean error 53 GVA·s), which over-stated the notional post-fault RoCoF by ~1.5× and pushed the risk index amber in ordinary conditions — a source of false alerting. After recalibration the index reads green in typical mix, amber only in the genuinely low-inertia summer conditions that match NESO's real ~110 GVA·s floor, and red never from inertia alone. The readout unit is relabelled **GVA·s** to reflect that it now tracks NESO's series. Measured-frequency audible alarms are unaffected (they were never driven by this proxy). (server 260904.9)
- **System-risk panel enlarged.** The panel is taller with slightly larger fonts for easier reading. (dashboard 260904.9)
- **Frequency history viewer.** A new **⤢ history** button on the System Frequency panel opens a full-window, scrollable plot built from the logged 15-second data (served compactly as a min/max/mean envelope, ~30-day retention). Drag or use the week / zoom buttons and arrow keys to move; a **● live** toggle keeps the latest data in view at the 15-second rate without changing zoom; hovering reads the value at any point and **clicking locks** the readout (drawn on the plot so it is captured); **⭳ save** writes a PNG to a `captures/` folder beside the server. Excursions recolour the line amber/red exactly where it crosses the operational/statutory limits, and the x-axis marks each day at 00:00 with the date, in UT. (server 260904.7, dashboard 260904.8)
- **Sustained statutory-breach alert.** Distinct from the instantaneous statutory crossing, a separate critical alert is raised once frequency has stayed beyond a statutory limit (49.5 / 50.5 Hz) continuously for more than 60 seconds — e.g. "Grid frequency has stayed below 49.5 Hz for 1m 35s …" — updating as it persists and recorded as one episode (with duration) in the alert history. (server 260904.8)
- **Frequency and system-risk alarms separated.** The escalating audible **tones** now fire only when the frequency itself leaves the band; the composite system-risk level and a sudden infeed loss instead give a single **pip** plus the on-screen banner — a notification, not a frequency alarm. Alerts are tagged separately (**FREQ** vs **RISK**) and each has its own toggle in the alarm panel. (server 260904.1, dashboard 260904.1)
- **Over-frequency alarming added (symmetric).** The frequency alarm now escalates for high frequency too — tiers at 50.2 / 50.3 / 50.5 Hz mirroring 49.8 / 49.7 / 49.5 Hz — with direction-aware wording and a matching "generation may trip" one-shot for a sustained high excursion. The alert wording for a statutory breach was also corrected to distinguish the statutory limit (49.5 Hz) from the point where automatic demand disconnection actually begins (48.8 Hz). (server 260903.8, dashboard 260903.8)
- **Panel colouring.** The System Frequency panel and the System Risk sub-panel now carry a severity tint — background = the live level, border = the highest level reached in the last 10 minutes (so a cleared excursion lingers briefly). Crucially each panel is coloured from its **own** source: the frequency panel from the frequency's band state (green in-band, amber outside operational, red beyond statutory — both directions), the risk sub-panel from the composite risk level. (dashboard 260903.10, 260904.2)
- **BMRS gap-fill extended to partial and current weeks.** A week that is only partly logged — including the **current, in-progress week** — can have just its already-elapsed gaps filled from BMRS (+ PVLive solar); self-logged readings are always kept (merge, live wins) and the not-yet-elapsed part of the current week is left untouched. (server 260903.7, dashboard 260903.7)
- **Gas flow: idle feeds shown as inactive.** Any supply/demand connector carrying no flow (e.g. an LNG terminal at 0.0 mcm/d) is drawn as a thin static dark-grey line with no wave animation, so it reads as present-but-inactive rather than a live stream. (dashboard 260903.7)
- **Open-Meteo daily-limit handling + self-host override.** Open-Meteo's free tier is enforced per IP and shared, so a busy/shared IP can report "daily limit exceeded" regardless of this app's own usage. On a daily-cap rejection the offshore feed now holds off until the genuine 00:00 UTC reset, re-probing every 15 minutes (which is also how quickly it recovers by itself after the connection moves to a fresh IP — no restart), while the OpenWeather OC4 nowcast covers the gap. A new `OPEN_METEO_BASE` environment variable lets you point the app at a self-hosted Open-Meteo for a private quota. (server 260904.4, `rain_probe.py`)

## 2026-09-03

- Generation archive reworked to fixed **Sunday–Saturday** weeks. The archive plot no longer stretches to fill the width: it always spans a full Sun→Sat week as 336 half-hour buckets, with every day present even where data is missing (gaps read as zero) and the in-progress week filling left to right. Weekly files are named and grouped by their week-start Sunday (00:00 UTC cutover); the miss-a-boundary catch-up is unchanged. The pop-up now navigates by whole weeks — ◀ ▶ arrows or year/month/day pickers, reaching back to 2017 — rather than by file. (server 260903.1, dashboard 260903.1)
- Generation gap-fill from **Elexon BMRS**. A past week with no local log can be reconstructed on demand with "fill from BMRS": 5-minute FUELINST generation-by-fuel (keyless, available back to at least 2017) averaged into the same 30-minute buckets and written as a permanent archive file tagged as externally sourced. Pulled only when you select a missing week — never automatically — and it refuses the current or a future week. (server 260903.1)
- Embedded **solar** added to reconstructed weeks. FUELINST carries no solar (it is unmetered), so the backfill also fetches Sheffield PVLive's historical national solar estimate for the week and merges it in as a modelled `SOLAR` band, matching the live panel. Only grid-scale battery remains absent (no historical feed is used for it). (server 260903.2, dashboard 260903.2)
- **Re-pull** for already-downloaded weeks. A week previously filled from BMRS shows a "re-pull" button to refresh it (for example, to add solar to a week fetched before that change). The guard is strict: re-pull only overwrites a prior BMRS file — a self-logged week's real rows are never overwritten, even if forced. (server 260903.3, dashboard 260903.3)
- **Interconnector colours** made distinct. The ten interconnectors were all one blue and merged together when stacked; they now use a graded navy→pale-azure ramp so each band is separable, deliberately kept off the cyan/teal that wind uses. (server 260903.3)
- Offshore **Open-Meteo budget** handling made honest and leaner. The offshore and land-probe tallies now count the locations Open-Meteo actually charges for (per-location weight — successful reads and over-limit rejections alike), and the previously un-metered block-4 cloud/description call is now counted, so the API readout reflects the true footprint against the shared per-IP daily budget. A daily-cap rejection is now distinguished from minute/hour throttling: it triggers a ~15-minute backoff instead of re-hitting an empty pool every cycle (still self-healing at the 00:00 UTC reset or when a shared pool refills), while minute/hour limits map to the shorter "throttled" state. (server 260903.4, `rain_probe.py`)
- Low-frequency **alarm cadence** curbed. The escalating tone no longer loops continuously until the frequency recovers. Each alert is now a capped burst followed by a long silence: tier 1 ≤10 s every 2 min, tier 2 ≤15 s every 2 min, tier 3 ≤15 s every 1 min, and after 4 minutes on any tier it winds down to ≤5 s every 15 min. An escalation restarts the cadence at full rate; each burst still speaks the frequency once, and the one-shot "power cuts may be imminent" warning is retained. (dashboard 260903.5)
- Frequency **trace keeps brief excursions**. The on-screen trace is now downsampled with min/max-envelope decimation (both extremes per bucket) instead of stride sampling, so a short dip below 49.8 Hz — or a spike — always shows on the graph. The full-resolution 15-second log always kept every point regardless; this only affected what the graph drew. (server 260903.6)

## 2026-08-28

- **System Risk Index** added. A composite grid-risk read derived from the live mix: a proxy system inertia (Σ generation × per-fuel inertia constant H, synchronous plant only), a notional post-fault RoCoF were the largest infeed to trip, and a Composite Grid Risk Index (CGRI) combining frequency deviation with inertia and RoCoF multipliers. A fast-attack / slow-release hysteresis stops the level flapping. Shown as a sub-panel in the frequency panel (level badge, inertia GW·s, "if largest trips" RoCoF, CGRI), with explanatory tooltips. (server 260828.x, dashboard 260828.x)
- Frequency alerting **unified onto the system-risk read**. The frequency cards, panel and voice now come from a single evaluation of the risk state rather than separate parallel thresholds, removing duplicate and occasionally conflicting frequency alerts. (server, dashboard)
- **Integrated escalating frequency alarm**. Sustained tiered tones (not decaying pips), with a tier-3 low-high siren plus spoken warning, a power-cut warning after a sustained critical spell, and a deadband so it doesn't chatter around a tier boundary. A standalone `alarm_tone_demo.html` demonstrates the tones. (dashboard 260828.x, `alarm_tone_demo.html`)
- **Interleaved 15-second data log**. The server writes a rolling daily JSONL (`logs/grid_log-YYYY-MM-DD.jsonl`) interleaving 15-second frequency points (`"f"` rows) with one generation-mix row per FUELINST publish (`"m"` rows carrying the mix, the derived demand split, and the risk summary), pruned after 30 days. (server 260828.x)
- **7-day generation history** pop-up. A full-window view with two live plots — output (MW) and share (%) — stacked by fuel, opened from a button beside the generators button. (server `/api/gen-history`, dashboard 260828.x)
- **Permanent weekly generation archive**. Each week's mix is rolled into a permanent per-week file that survives restarts, with catch-up if the server was off at the boundary; the pop-up gained an archive panel to view past weeks with a MW / %-share toggle and UTC axis labels. (server, dashboard)
- **API call metering**. Every upstream weather call is tallied per UTC day by source and purpose to a tagged log, and the engine view surfaces the day's running total, so budget use is visible at a glance. (server, `rain_probe.py`, `engine_view_live.html`)
- **Active forecast window**. An alarm-panel control sets local start/end hours to concentrate the paid API budget: inside the window the offshore model, land probes and wind sample at full cadence; outside it their cadence is stretched by a quiet factor to protect the daily quota. Persisted across restarts; may wrap past midnight. (server, dashboard)
- Rain engine: **duplicate alerting removed and cell tracking made more trustworthy**. The situational and nowcast voices were unified so one condition isn't announced twice; the pressure tendency was unified on a persisted 3-hour change (fixing a spoken "falling" against a panel showing "rising"); cell tracks gained a wind-led direction-trust gate so a mis-derived direction is withheld rather than shown; and the home land probes were reworked to deploy sparingly and upwind of home. (`rain_probe.py`, dashboard)

## 2026-08-27

- Rainfall unified to a mm/h rate. EA gauge readings arrive as a 15-minute bucket total and are now converted to a rate at ingestion (`mm_h = mm × 3600 / period`); mm/h is the single scale across land gauges, offshore sentinels and model cards, the intensity bands, and the front-tracking thresholds — removing a basis mismatch where 15-minute totals were being compared against mm/h limits. Cards show the mm/h rate as their headline (the raw bucket total is kept in the hover tooltip); the value line was reworked so the rate never truncates, and the rainfall column was widened against a narrower river column. (server 260827.1, dashboard, `rain_probe.py`)
- Snow separated from rain and shown honestly. Both offshore samplers now split precipitation into rain and snow (OpenWeather `snow.1h`; Open-Meteo total precipitation minus rain/showers). A snow-dominant point is flagged and drawn in bright pink (weather-radar convention) rather than folded into the rain intensity scale, and the offshore spoken approach says "wintry showers"/"snow" instead of "rain". (server, dashboard, `rain_probe.py`, `owm_onecall.py`)
- Local flood warnings surfaced. The national flood strip now highlights any warning/alert whose flood area falls within the EA page's chosen radius — resolved via the floods and floodAreas spatial queries — and floats it to the top of the list; the spoken flood alarm names the nearest local one and its distance ("four national flood warnings active, including one locally at Didworthy, less than five kilometres from you"). (server 260827.3, dashboard)
- Flood strip wording and list. The main-page badge now uses the Environment Agency umbrella term — "N flood warnings & alerts" when the set is mixed, and the specific noun ("N flood alerts", "N severe flood warnings") when it is all one kind — rather than labelling every in-force item with the highest severity's noun, which had shown 41 alerts plus 1 warning as "42 flood warnings". The EA-page flood list no longer caps at 12: it holds every in-force warning and alert in a scroll box about twelve rows tall (and never taller than 70% of the viewport), with local floods still highlighted and floated to the top. (dashboard 260827.13)
- Offshore rainfall nowcast rebuilt as a two-layer detector. The old scan/track arc is replaced by (a) a permanent NET — sentinels at 40 km, inner pickets at 20 km, and under-hood dithered fill points, all sampled from the free batched Open-Meteo, so the wide watch costs no OpenWeather budget — and (b) MOBILE tracker cards that spawn on a net detection (cap of four), chase the cell inward through the 5–35 km band, jump back periodically to sense whether heavier/lighter/no rain is following, then loiter → retreat to 35 km → vanish, re-locking if rain returns. OC4 radar reads are reserved for the mobile trackers (marked RADAR when confirmed), so paid calls are spent only on real detections. Front speed is measured from the stationary net's leading edge and passed through a shared "smells wrong" trust gate (physical band, consistency with 1.6× the wind, cross-cycle stability); when trusted the spoken approach carries the speed and ETA, otherwise the figure is withheld. Also fixes a latent bug where a feed dropout could freeze the offshore cards indefinitely. (server, dashboard, `rain_probe.py`, `owm_onecall.py`)
- Reading freshness made honest across the rainfall panel. The section header shows a relative "updated …ago"; a land gauge whose reading is older than ~2 publications (minimum 30 min) times out to 0 mm/h and greys rather than presenting an hours-old value as current, with the true last-reported time in the tooltip; the history plot is time-based and runs to the current time, so it advances during a data gap instead of freezing on the last reading; river-level ages are colour-coded green (≤1 h) / amber (1–4 h) / red (>4 h); the rain alarm treats a gauge as "raining now" only while fresh by the same per-gauge timeout (not the 2 h river window, which let it say "light rain nearby" on a reading the cards had timed out); and a plot end-label collision was fixed. (dashboard 260827.11, .14)
- Rainfall card border holds a 2 h peak. A gauge card's BORDER now shows the highest intensity band the gauge has reported in the last two hours (tracked server-side from the readings it already fetches, so no extra API calls); the fill/background reflects the current reading and the number still times out to 0 and greys. So a gauge that has stopped keeps a coloured border while recent activity is relevant, then decays as those readings age past 2 h. (server 260827.4, dashboard 260827.18)
- River level as a percentage of its typical range. Each station shows its level as a percentage of its own EA typical range (0 % at the typical low, 100 % at the typical high) next to the name in the list — coloured blue below 0 %, green to 80 %, amber to 100 %, red above — and on the selected station's plot, which now marks the range max (100 %) and min (0 %) as labelled lines with the current percentage read out at the top. (dashboard 260827.19)

## 2026-08-26

- Rainfall nowcast: the physical (land) gauges now get the same range-ring tracking as the offshore arc. The nearest wet upwind gauge is the leading edge, binned into 30/20/10 km rings; its inward speed is measured ring-to-ring across cycles (falling back to surface wind only until motion is measurable), and an intensity trend of the leading edge is tracked. Announcements now distinguish a front that is building ("approaching and getting stronger"), easing ("easing as it nears, lighter than before"), or fizzling ("may fizzle out before it reaches you"), and a one-shot fires when a front fades or veers away before arriving. Activation is corroborated — two-plus upwind wet gauges, or a single one confirmed by measured inbound motion — so one stray gauge no longer raises an approach. The offshore arc gained matching fizzle awareness ("weakening and may not reach the coast"). (`rain_probe.py`)
- EA page: the flood warning/alert list collapses to the first (highest-severity) item by default, with a "show N more" / "show fewer" button to expand to the full list (up to 12) or re-collapse. A single warning shows no button. (dashboard 260826.2)
- Rainfall nowcast: the offshore virtual gauges are now sampled from OpenWeather (radar/satellite-fed `rain.1h`) when a One Call 4.0 key is active, instead of the modelled Open-Meteo — one OWM call per sea point, budget-guarded with an automatic Open-Meteo fallback when the daily budget is spent. The wind daily-call ceiling was raised 300→600 to cover it. (server 260826.1, `owm_onecall.py`)
- EA page: a dismissible One Call 4.0 upgrade notice now shows only when a key is present but the weather fell back to the free 2.5 tier — it describes what the paid nowcast adds (minute-ahead precipitation, the offshore rain-approach tracker, spoken alerts) and links to the One Call 4.0 page. Hidden for OC4 users and when no key is set; remembered once dismissed. (dashboard 260826.1)
- Rainfall nowcast: spoken alerts made edge-triggered — a message speaks only when its condition first appears or its wording changes, so a persistent state is announced once rather than repeating on a timer. The all-clear ("conditions have settled") is now a one-shot on the rain→calm transition and never fires on a calm-from-start day. (`rain_probe.py`)

## 2026-08-25

- Weather nowcast can now speak. A new "Weather nowcast (One Call 4.0)" alarm category voices the rain-alert phrases (onset, intensity, approach, clearing) through the existing audible/spoken alarm engine — off by default, speaks only while sound is armed, and adopts the current state silently when first enabled. Enabling it triggers the background full-EA pull so it works with the panel closed. (dashboard .10)
- OpenWeather budget: the local-weather + nowcast daily call ceiling raised from 100 to 300 (a ~100 base plus the ~200 One Call 4.0 headroom), still well under the 900/day plan cap. The first-run key modal now explains the optional One Call 4.0 subscription, the automatic free-tier fallback, and the per-day throttle. (server .4, dashboard .9)
- Rainfall-alert diagnostic probe added (`rain_probe.py`, new; wired read-only into the EA overview). Each cycle it logs the phrase the alert would speak, from: at-home model signals (intensity band, rate trend, 3-hour pressure tendency, visibility); a physical gauge-sector approach with a measured-wind ETA; a sea-masked movable offshore Open-Meteo arc (scan→track, inward-edge advection speed, marked virtual gauges); and the OWM one-minute forward nowcast (onset / intensification). No tone is played — diagnostic only. (server .1)
- Probe tracker guards: trend, pressure and visibility signals require a minimum span of history before they classify, and a series whose newest sample is stale is treated as unknown; the pressure rate is capped at a physical limit — removes a spurious "pressure rising" alert on server restart. The offshore arc holds state through a data dropout instead of falsely retracting. (`rain_probe.py`)
- One Call API 4.0 support with auto-detect (`owm_onecall.py`, new). The EA weather panel uses OC4 when the key is subscribed — current conditions plus the one-minute precipitation nowcast — and falls back to the free 2.5 Current Weather call on 401/403, so the panel works without a subscription. Active tier is labelled on `wind.source` / `wind.api`. (server .2)
- EA rainfall map: modelled offshore points from the probe's arc are shown as dashed MODEL cards, counted separately, and excluded from the physical gauge count/cap, the "nearest" outline, and alert confirmation. (dashboard .3)
- EA rainfall map: directional grid widened to 5 columns so a same-distance offshore arc fans out by bearing instead of stacking. Collision spill keeps a gauge in its true direction (nudging apparent distance, not bearing, and stepping out only in its true N/S direction); fully-empty rows and columns are packed out; cards keep a minimum width with horizontal scroll; virtual cards lead with their azimuth. (dashboard .1, .5–.8)
- EA river panel: on a total river-stations failure the section collapses to a single full-width column, so the error notice centres on the page instead of sitting in the empty left column. (dashboard .2)
- Solar resource verdict now gated by sun elevation. `_rate_solar` uses computed solar elevation instead of a binary day/night flag: night (≤0°) reads "none", below 5° "minimal output", below 15° capped at "reduced" — so the Resource Conditions rollup can no longer read "solar strong" near dawn/dusk when a clear but low sun yields ~1% output. Each solar card exposes `sun_elev_deg`. Resolves the open Known Issue. (server .3)

## 2026-08-24

- Local wind: calm and variable states shown honestly. Direction below 0.5 m/s reads "CALM"; a missing bearing (or a wide 3h direction swing at low speed) reads "VAR". The dial replaces its arrow with a cyan CALM/VAR label and drops the degree readout when direction is undefined, and a missing bearing alone no longer triggers the "unavailable this cycle" notice. (server .1, dashboard .1)
- EA errors: upstream CDN error pages (e.g. a 503 HTML body) are reduced to a plain textual reason instead of leaking raw markup into `eaData.error`, and river-panel error notices are HTML-escaped before injection. (server .1, dashboard .1)

## 2026-08-23

- Weather block 4: cloud cover and sky description now sourced from Open-Meteo (primary), with OpenWeather as fallback when Open-Meteo is unavailable. Wind, temperature and pressure remain OpenWeather. (server .7)
- Weather block 4: cloud source shown as an "OM"/"OWM" tag by the "Cloud %" row. (dashboard .7/.8)
- Weather block 4: cached readings tagged stale with age and reason; a "cached" marker is shown when a fresh fetch is unavailable. (server .6, dashboard .6)
- Pressure tendency wording: "Falling gently" changed to "Falling slowly" (parallel with "Rising slowly"). (server .5)
- Footer server build now read from the snapshot instead of a hard-coded string. (server .4, dashboard .4)
- Gas config re-read on file change, fixing a stale in-memory config that survived until restart. (server .2)
- Gas unit resolution centralised; auto-detect demoted to a warning that never overrides the setting; added unit-confirmed/warning state. (server .2)
- Gas unit-check flag made clickable (opens settings) and added to the estimated-cost card as well as the gas cost cards. (dashboard .2/.3)

## 2026-08-20

- Frequency: dedicated `/api/frequency` endpoint serving a full-resolution 15s recent tail plus a 2h trace. (server)
- Frequency display reworked to show the newest sample (live edge) with a natural age tick, replacing earlier replay/queue approaches. BMRS publishes in ~2-minute bursts of 8 samples. (dashboard)

## 2026-08-17 (.1 – .15)

- Octopus matched-rate costing: account resolution with separate electricity/gas account numbers, dated rate-history fetch with DD/non-DD handling, and time-matched costing helpers. (server)
- Billing-period end-day setting added; consumption fetch window widened to cover a full billing month. (server)
- "Tariff and Estimated Costs" panel added; matched-rate costing wired into all cost badge cards, each reading priced at the rate in force at its own timestamp. (dashboard)
- Rainfall alarm feature designed but shelved, to avoid breaking the working rain/river display.

## 2026-08-14 – 2026-08-15 (builds 260814q – 260815k)

- Gas page: linepack history with forecast-minimum floor and a local-log tail to bridge publication lag; balance line from measured instantaneous supply−demand; wholesale gas price series. (server)
- Re-added missing `get_gas_linepack_history`. (server)
- "GB" not "UK" corrected throughout (BMRS and National Gas data cover Great Britain only). (both)
- Alarm system: Web Audio + Web Speech, selectable categories, tiered CRITICAL/WARNING/NOTICE tones, latching/hysteresis, stale-data suppression, first-poll baseline suppression, CRITICAL 20s repeat, per-category test buttons. (dashboard)
- Gas page balance/price plots: dual axes, UTC x-axis, split imbalance label, rounded bar ends. (dashboard)
- Responsiveness: overlay content capped and centred, graduated zoom for HD screens, EA flood-list alignment fix. (dashboard)
- Colourgramme: LOG made the default scale. (dashboard)

## 2026-08-11 and around

- Colourgramme backend: companion plots (periodicity spectrum, average-day, load-duration, day-of-week); rolling 24-month grid split into aligned year-rows. (server)
- BST timezone fix: parse Octopus `interval_start` via `fromisoformat` rather than reconstructing UTC keys, which had missed all summer months. (server)
- History fetch extended to ~25 months; distinguishes fetch failure from genuine data absence. (server)
- Grid-scale battery/pumped storage identification via the Terravolt BMU classification file, cached weekly. (server)
- Weather retrieval resilience: batched requests, independent 15-min cache, 429 backoff, last-good fallback. (server)
- Octopus "My Home" pop-out: cost cards, 48h/2-week bar charts, consumption-patterns insight cards, honest freshness reporting, auto-refresh. (dashboard)
- Colourgramme pop-out: spectrogram palette, SINGLE/STACKED toggle, month multi-select, clip sliders, lin/log toggle, gap handling. (dashboard)
- Terminology standardised from "carpet" to "Colourgramme". (both)

## ~2026-08-04 and earlier

- Solar: Sheffield PVLive added (Elexon FUELINST does not meter embedded solar); solar tagged "est". (both)
- Spinning reserve: MELS lookback widened to 30 min with an 80% coverage guard, fixing false near-zero reserve from sparse MEL publication; serves a stale-but-labelled reading rather than a misleading figure. (server)
- Frequency: switched from the lagging archive endpoint to `system/frequency` (~1-min latency). (server)
- Core dashboard panels: frequency heartbeat trace, capacity margin radial gauge, −24h/+24h margin graph, operating reserve panel, supply stack bar, generation-by-source table with trend arrows and "est" tag, weather panel, per-source status strip. (dashboard)

---

## Per-file build history

Each file's own build history, newest last. Tags are `YYMMDD.N` (UTC).

### `grid_dashboard.html`

Header history began at 260917.1; earlier builds are in the dated entries above.

- **260917.1** (2026-09-17) — Added a UK & Ireland power-cuts header chip (band-coloured total, planned/unplanned tooltip) that opens the /powercuts page full-screen.
- **260917.2** (2026-09-17) — Power-cuts badge — "UK & IRL cuts N", border+text green below the amber warning level then the warning colours, and a background flash.
- **260917.3** (2026-09-17) — Badge flash now driven by the RATE of new cuts/hr (from the history log's national.new), not the standing customers-off total — so it only flashes when cuts are actually arriving fast. 0.1 Hz >1k/hr → 1 Hz @10k/hr → 2 Hz @20k/hr.
- **260917.4** (2026-09-17) — New "Storm & power cuts" alarm category (escalating tones + speech) — fires as cuts climb and names the likely cause: a storm when a region's METAR gusts correlate, a supply/grid cause when an infeed loss is flagged, else unclear.
- **260917.5** (2026-09-17) — Power-cuts badge border dimmed to 0.35 alpha (matches the other header badges); flash ceiling reduced from 50% to 20% alpha.
- **260917.6** (2026-09-17) — "Storm & power cuts" alarm reworked — UNPLANNED cuts only; a count-threshold ladder (20k/50k/100k/500k/1M, then +500k) announcing the highest level reached, hysteresis + repeat-every-3h-while->50k, cause comment when known, and big- restoration notices (first 2 per 6h). Badge flash/tooltip now use unplanned rate.
- **260917.7** (2026-09-17) — Power-cut badge totals + alerts default to UNPLANNED, with a top-bar switch to include planned works; the switch drives the badge value/colour, flash and alarm.
- **260917.8** (2026-09-17) — Alarm settings modal capped to the viewport with a scrollable category list, so the whole panel is reachable on short screens (header/arm-row/footer stay fixed).
- **260918.1** (2026-09-18) — "Storm & power cuts" alarm gains a RATE speech alert — speaks a "climbing" warning when new cuts/hr crosses the flash threshold (edge-triggered + every 30 min while sustained), so a fast-developing event is heard before the count ladder trips.
- **260918.2** (2026-09-18) — Alarm now COMBINES the milestone + climbing + restoration signals into a single spoken alert per poll, names WHERE cuts are concentrated (worst-affected / fastest- climbing operator regions), and keeps monitoring/re-voicing while elevated.
- **260918.3** (2026-09-18) — Alarm surfaces operator-DECLARED major incidents from each feed's own category classification (the structured signal) — announced as they appear and named on the badge tooltip; conservative match so ordinary categories never trip it.
- **260918.4** (2026-09-18) — Per-provider MUTE — muted providers (set on the /powercuts By-Operator panel, shared via localStorage) are excluded from the badge total, flash and the whole power-cuts alarm; muted names shown in the badge tooltip. For unreliable feed data.
- **260923.1** (2026-09-23) — Frequency alarm judges and voices the latest publish burst's EXTREMES (server burst_min/burst_max), not just its newest point — a mid-burst excursion that recovered by the last 15s point now alarms, the voice says "dipped to X, now Y", hysteresis clears on the extreme, and the panel colour uses the extremes too. Animated chevron arrow in the dial centre (down = low, up = high; amber/red by tier) shown for >= 2 min from an excursion's first alert and while it lasts.
- **260923.2** (2026-09-23) — Dial excursion arrow no longer bobs — chevrons only light in sequence.
- **260923.3** (2026-09-23) — Nowcast channel no longer repeats the approach-episode lines (alert / confirm / fizzle) when the Rainfall category is armed — RAIN voices them once.
- **260926.1** (2026-09-26) — Build history moved from the file header to CHANGELOG.md; footer build label corrected (it still read 260918.4). "Swap panels" removed; gauge radar toggle in the EA plot area; "under the hood" button (Forecast view in a new tab); header "live powercuts map" button, history moved right of my home. "Power cut near you" alarm category with radius selector; chip tooltip "near you" line. Flood alarm holds its episode through EA outages and brief lifts (no repeat announcements); flood chip marks held counts with a "*"; EA page shows how long the EA backend has been unreadable.
- **260927.1** (2026-09-27) — Footer status strip gains an Open-Meteo chip: local / online (green), online with "local unavailable" reason and retry time, or rate-limited / daily limit (amber).
- **260928.1** (2026-09-28) — System-warnings alarm takes its tier from the server and speaks type and window; critical system warnings release the critical repeat when they clear; System Warnings panel shows notices in force with a severity chip plus recent ones not in force.
- **260928.3** (2026-09-28) — Per-gauge rain-alert mute switch (normal / <0.3 / mute) in the rain plot header, applied to the rain popup; mute dot on each gauge card; gauge name dimmed after 3 h without a report.
- **260928.2** (2026-09-28) — Frequency figures, dial and drift show "—" while the feed is down, with "no data since HH:MM UT"; trace kept; replay resumes on the first fresh reading.
- **260929.1** (2026-09-29) — RADAR chip on radar-fed sea-net and home-ring cards.
- **260929.2** (2026-09-29) — Weather panel rows Low today / High today (Open-Meteo daily forecast, OM-tagged) replace the always-blank Min/Max temp.
- **260929.3** (2026-09-29) — Gauge cards keep late readings (dimmed past 30 min), "—" only past 75 min; rain pop-up uses the same 75 min.
- **260930.1** (2026-09-30) — Spoken frequency alarm mentions inertia only when the inertia estimate is reduced; "limited reserve" dropped; CGRI tooltip.
- **260930.2** (2026-09-30) — Footer shows the port the page is served from.
- **260930.3** (2026-09-30) — 850 hPa wind on the wind dials: second compass arrow (green dotted outline, faint fill) and a green dash on the speed dial, with direction/speed lines and hover detail.
- **261014.1** (2026-10-14) — New "Monitored postcode areas" alarm category: siren then speech when a cut or planned works start in a monitored area (combined message for several areas; no customer count for SPEN), soft tone + speech on restoration.
- **261014.2** (2026-10-14) — Spoken power-cut times (near you, monitored areas) honour a UTC "Z"/offset; SSEN and NPg times were an hour early in BST.
- **261014.3** (2026-10-14) — Overdue incidents count as still present, so going overdue is never announced as a restoration.
- **261015.1** (2026-10-15) — Monitored-area alerts speak an area's name and place ("Les in Plymouth") when it has one.

- **261016.3** (2026-10-16) — Reservoirs: water-company cards for every region (year-by-year total chart, % full bars with last-year tick, click for a reservoir's full history), company badges on the local row, company totals and a Data sources table on the England tab.
- **261016.2** (2026-10-16) — Reservoirs: South West Water total, nearest and lowest badges; company reservoir bars and history chart in the region view. "N% full · below average" wording; England "normal ≈N%".
- **261016.1** (2026-10-16) — Reservoirs section at the top of the EA page: local and UK badges, drop-down with a view per EA region, monthly, weekly and London charts, drought status, area report text and live gauge cards (`/api/reservoirs`).
- **261017.1** (2026-10-17) — Balancing costs panel below Generation and imports: six summary figures (highest estimate coloured against the 30-day confirmed mean), ▾ chart in the panel (remembered), ⤢ details pop-out with the daily cost chart, other-services strip, driver strips, correlation table, who was paid and the half-hour heatmap; event markers and tooltips throughout.
- **261018.1** (2026-10-18) — Frequency alarm speaks the excursion's held worst point, not the latest burst's; new low-side tiers 4 (49.1 Hz) and 5 (48.8 Hz) with faster sirens, shorter cadence and their own wording.
- **261018.2** (2026-10-18) — 1-minute cycle: mini plot and in-panel expanded view of its size over time (replaced the same day by 261018.3).
- **261018.3** (2026-10-18) — Periodicity spectrum of frequency (30 s – 30 min) with the 1-minute line marked, mini plot and in-panel expanded view, averaging windows, vertical scale, average-minute profile and hover readout.
- **261018.4** (2026-10-18) — Periodicity spectrum: period-range selector (5m / 15m / 30m, remembered) with finer ticks on the 5-minute view.
- **261022.1** (2026-10-22) — Home pressure plot (logged history up to 72 h, last 3 h shaded, hover readout) under Pressure in the weather panel; sunrise/sunset moved to the temperature block.
- **261023.1** (2026-10-23) — "▦ wall" header button opening the transmission wall (/wall).
- **261023.2** (2026-10-23) — Embedded mode for the wall: `#embed=gas` / `#embed=ea` shows only that page.
- **261023.3** (2026-10-23) — "🗺 network" header button and full-page network map pop-up.
- **261023.4** (2026-10-23) — Network pop-up re-lays out the map each time it reopens.
- **261024.1** (2026-10-24) — Network pop-up reloads the map if it is not drawn after reopening.
- **261024.2** (2026-10-24) — `#embed=freq` view (System frequency card alone) for the wall; embedded views suppress pop-ups.
- **261024.3** (2026-10-24) — Treemap note: stations split by NESO-instructed level where applicable, else notified.
- **261024.4** (2026-10-24) — Treemap subtitle and tooltip: "unit levels (instructed or notified)".
- **261024.5** (2026-10-24) — Weather panel: THI replaced by apparent temperature (temperature, humidity and wind).
- **261024.6** (2026-10-24) — Monitored areas: 30-minute repeat alarms for areas ticked Repeat.
- **261024.7** (2026-10-24) — "▦ wall" header button made a real button so its text lines up.
- **261024.8** (2026-10-24) — "Republic of Ireland" shown as "Ireland".
- **261024.9** (2026-10-24) — Header buttons wrap on narrow screens; narrower phone margins.
- **261024.10** (2026-10-24) — `#embed=freq`: card fills the frame exactly (full border).
- **261024.11** (2026-10-24) — Embedded views for the wall: `#embed=gen`, `#embed=gh`, `#embed=octo`.
- **261024.12** (2026-10-24) — Gas linepack chart: measured tick/end-label clearance, legend fits narrow canvases; footer HTML build updated.
- **261024.13** (2026-10-24) — Gas chart: forecast minimum labelled with its time (UT).
- **261024.14** (2026-10-24) — Gas alarm category voices the gas probe's events with gas tones; Alarm levels strip on the gas page; 10 % chip no longer sounds.
- **261025.1** (2026-10-25) — Near-you power-cut alarm skips incidents flagged outside their operator's area.
- **261028.1** (2026-10-28) — Gas flow diagram: ◂ Storage & LNG slide-out (sites, LNG tanks, days of gas; closed by default); Rough shown as production; 🛢 Gas storage button and pop-up overlay; `#embed=gasflow` view.
- **261028.1** (2026-10-28) — Gas flow diagram: ◂ Storage & LNG slide-out (sites, LNG tanks, days of gas; closed by default); Rough shown as production; 🛢 Gas storage button and pop-up overlay; `#embed=gasflow` view.
- **261028.2** (2026-10-28) — Gas Alarm levels strip: nominal badges outlined green; official notices grey when unpublished.
- **261028.3** (2026-10-28) — Flow diagram with storage out: text 1.3× larger, column re-spaced, storage node "Storage", site lines joined into one trunk.
- **261028.4** (2026-10-28) — Flow diagram with storage out: small text 2 larger and brighter; days-of-gas and LNG cargo lines split; estimate note in the column header.
- **261028.5** (2026-10-28) — Storage lines use the flow-wave connector; lines clear of the % figures; "Interconnector Export"; spine moved right with storage out.
- **261028.6** (2026-10-28) — "storage at max withdrawal" badge label; "Days of gas at today's demand" in the storage column.
- **261028.7** (2026-10-28) — Storage-column flow waves use the main ribbons' wave spacing instead of a fixed 4 cycles.
- **261028.8** (2026-10-28) — Storage lines stacked into the trunk (own slots), gentler curves, thinner, square-ended wave segments.
- **261028.9** (2026-10-28) — Gas page: System flow | Gas storage switch above the diagram (storage view replaces it; header button and pop-up removed; `#gasview=gs`); stock headline in TWh; Europe trend draws the previous five years with a key.
- **261028.10** (2026-10-28) — Storage lines start at dots; their widths follow each site's live flow, the stack capped at the Storage → linepack ribbon's width.
- **261028.11** (2026-10-28) — Dot at the storage trunk's end beside the Storage label.
- **261028.12** (2026-10-28) — Storage out: spine and demand side shifted right by the room the labels allow; Hide storage drawn in the diagram above the header.
- **261028.13** (2026-10-28) — Gas Alarm levels strip centred at the page width like the other gas panels.
- **261028.14** (2026-10-28) — Alert bar and NESO system warnings collapsible (show N more; one card open at a time; recent notices behind show).
- **261028.15** (2026-10-28) — Gas alerts (NG / GAS / STORE) in the alert bar, kept out of the electricity status; dismiss 7 d on alerts open a day or more, with a dismissed list and restore.
- **261028.16** (2026-10-28) — LNG tanks box wider and one line shorter; LNG lines branch from one spine with rounded corners and end in dots a measured gap before each label.
- **261029.1** (2026-10-29) — Balancing: Other balancing services strip on a round ~4-line axis step, taller, markers in its key.
- **261029.2** (2026-10-29) — Flow diagram: per-source ◂/▾ (one open at a time): storage column, LNG column (tanks box + terminal rows), sub-terminal split for St Fergus / Teesside / Bacton; ◂ Storage & LNG button removed.
- **261029.3** (2026-10-29) — Terminal sub-terminals slide out as a column (rows, share bars, lines stacked into the label) instead of splitting in place.
- **261029.4** (2026-10-29) — Flow diagram: wall two-panel mode (from the wall's message) with one Expand all / Collapse button; expanded, every source column stacked level with its source, drawing taller to fit.
- **261029.5** (2026-10-29) — Wall two-panel expanded view widens to the panel's shape and re-fits on resize.
- **261029.6** (2026-10-29) — Wall expanded view: main lines, spine and labels scaled up; demand labels fit; measured label widths for where column lines end.
- **261029.7** (2026-10-29) — Wall expanded view: one-line column rows in wider columns (about a third of the panel).
- **261029.8** (2026-10-29) — Storage / terminal column lines: round-ended trunk, end dots sized to the line.
- **261029.9** (2026-10-29) — Column end dots back to their usual size (round-ended trunk kept).
- **261029.10** (2026-10-29) — Merged column lines: round line ends, square-ended flow waves (no blur).
- **261029.11** (2026-10-29) — Merged column lines rounded only at the label end (no seam at the join).
- **261029.12** (2026-10-29) — Column lines run unbroken (stacked) to the label instead of joining a separate merged line; waves continuous through the join.
- **261029.13** (2026-10-29) — Flow diagram: Storage (injection) always on the demand side; duplicate Interconnector Export line dropped when named exports are drawn.
- **261029.14** (2026-10-29) — Flow diagram: Bacton export routes always shown (grey idle), importing routes only on the supply side; interconnector imports blue like Langeled.
- **261029.15** (2026-10-29) — EA gauge radar re-placed on any layout change (no ghost behind the reservoir detail).
- **261029.16** (2026-10-29) — Reservoirs: "N not shown" note for long-silent reservoirs; South West Lakes Trust credited by name.
- **261029.17** (2026-10-29) — Header electricity chip leaves out the wall's modelled pinch-point alerts (domain "grid").
- **261030.1** (2026-10-30) — Header chip filter for the wall's pinch-point alerts removed again (the alerts are gone).
- **261030.2** (2026-10-30) — Constraint chip (orange, from 500 MW, from the snapshot's `constraint`); click opens the network map.
- **261030.3** (2026-10-30) — Constraint chip gets the ⚡ icon, so it is the same height as the others.
- **261030.4** (2026-10-30) — Power-cuts chip on one line; mode tag amber "unplanned" / blue "incl. planned".
- **261030.5** (2026-10-30) — Network map pop-up's own ✕ removed (the map page has its own, in line with its panels).
- **261030.6** (2026-10-30) — `#embed=wx`: the Rivers & floods page's local weather block alone (for the wall's rain radar view).
- **261030.7** (2026-10-30) — `eaRadarPos()` corrects for the page's body zoom on wide windows (the gauge radar landed up-left of its slot).
- **261031.1** (2026-10-31) — Gas flow: ribbons stacked where they meet the spine (`stackY`); column dots `gfDotR` (the main dots' size); column line ends 10 px on screen before the label (`gfGapU`).
- **261031.2** (2026-10-31) — Power-cuts pop-up: corner `#pcClose` removed; closes on the page's `{gbPc:"close"}` message.
- **261104.1** (2026-11-04) — Gas storage By country: `gsvCty` switch (% full | days left, `gbGsCty`); days bars to `GSV_DMAX` 180 d coloured by `GSV_SEV` bands (> 90, 60–90, 30–60, 7–30, < 7 d).
- **261104.2** (2026-11-04) — Frequency: `FREQ_BANDS` + `freqBandsFill()`; dial `MAX` 0.7 with purple zones; faint bands on `drawFreq` and `fhDraw`; `colOf`/`THS`/`fhLevel` add the 49.7/50.3 and purple levels.
- **261104.3** (2026-11-04) — `FREQ_BANDS` amber to 49.6/50.4, red 49.5–49.6/50.4–50.5; `drawFreq` range 49.3–50.7; `fhDraw` frame 49.3–50.7, padded only past it.
- **261104.4** (2026-11-04) — Frequency: `#mcMini` removed, `#mcExpand` moved to the footer (⤢ spectrum), `spInit` no longer preloads `/api/freq-raw`; `.freq-dial-read` moved under the reading; dial scale `K` 1.2 (1 in `embedfreq`); `freqDevMarks()` on `drawFreq` (raw points) and `fhDraw` (bucket min/max).
- **261104.5** (2026-11-04) — `freqDevMarks()`: one marker per block (readings < 60 px apart) at the largest deviation, labelled with the deviation from 50 Hz.
- **261104.6** (2026-11-04) — Gas `#gasTrend`: drawn at the canvas's content size (padding excluded), H 196, padT 27 / padB 30, CSS padding 5 / 4 px.
- **261104.7** (2026-11-04) — `freqDevMarks()`: value centred below ▲ / above ▼, 11.3 px (8.5 pt), `MERGE` 64.
- **261104.8** (2026-11-04) — Frequency history: opens at 48 h; `FH_MINSPAN` 900; `fhSeries()` raw 15 s samples (`frLoad`/`frMerge`, `/api/freq-raw`) at ≤ `FH_RAW_SPAN` 6 h, dots when ≥ 4 px per sample; hour / 10-min gridlines and 1 min – 12 h time labels.
- **261104.9** (2026-11-04) — Frequency history: `#fhPrev` / `#fhNext` relabelled ◀ step / step ▶, pan by `fhSpan` (was 7 days).
- **261104.10** (2026-11-04) — Frequency history: hour gridlines when `fhSpan` < 18 h, 10-minute gridlines when < 3 h.
- **261104.11** (2026-11-04) — Frequency history: envelope ribbon (`Path2D`) refilled in `FREQ_BANDS` colours clipped to each alert band; `fhZoom` calls `fhSetFollow(false)`.
- **261104.12** (2026-11-04) — Frequency history: `fhSeries()` aggregates the raw samples into ~2 px buckets for views ≤ `FH_FINE_SPAN` 7 d (cached in `FRA`); series carry `c` (0 raw, 0.5 bucket centre) used by drawing, hover and lock.
- **261104.13** (2026-11-04) — Frequency history: `fhDataHi()` includes the newest raw sample; `fhAtLive()` / `fhRelive()` after pan, zoom and drag end; FR reset on open; `frLoad` re-pins in live mode.
- **261104.14** (2026-11-04) — Frequency history: `fhLiveHi()` (now) as the live edge; `fhClock()` every 100 ms (redraws ≥ 1/s in live mode, continuously while pulsing); `FHP` new readings pulse for `FH_PULSE_MS` 5 s.
- **261104.15** (2026-11-04) — Frequency history: pulse draws the line only (no rings); `fhReadout()` (the main page's `freqPlaylist[freqPlayIdx]`) shown in a 34 px strip above the plot with a white dot on the plot; `fhClock` redraws when it steps.
- **261104.16** (2026-11-04) — Frequency history: `fhIngest()` / `fhFromMain()` take the main page's `freqSamples` as they arrive; readout time line; readout in `#freqVal`'s font (strip 44 px). `#freqDelta` hidden.
- **261104.17** (2026-11-04) — Frequency alarm: `#freqMuteBtn` (`srMuteToggle` / `srMuteCheck`); `SR.muted` skips bursts and the power-cut voice, cleared on a tier or side change or when the alarm clears.
- **261104.18** (2026-11-04) — Frequency history readout: box filled and outlined from `#freqCard`'s `sev-bg-*` / `sev-bd-*` classes.
- **261104.19** (2026-11-04) — `armPromptShow()` 2 min after load when not armed (`#armPrompt` under `#alarmBtn`); `arm()` removes it.
- **261104.20** (2026-11-04) — At-home plots: `wxPlotsHtml()` / `wxRainDraw()` / `wxTempDraw()` (−12 h to +12 h); `<details>` panel on the Rivers & floods page (`gbWxPlotsOpen`), plain block filling the space in `embedwx`; `embedwx` dials and text larger, one row.
- **261104.21** (2026-11-04) — `eaPressDraw()` caption: centre note / hover readout only where it fits between the end labels.
- **261104.22** (2026-11-04) — `FREQ_BANDS` amber to 49.65 / 50.35 (dial zones to 0.01 Hz, `fhLevel`, `colOf`, `THS`); frequency histogram `#freqHistBox` (`fhgLoad` / `fhgAdd` / `fhgDraw`, `gbFreqHistP`); dial `K` 1.1 (0.85 on narrow wall panels); compact risk box.
- **261104.23** (2026-11-04) — Frequency histogram resolution `fhgR` (0.01 / 0.02 / 0.05 / 0.1 Hz, `gbFreqHistR`), axis zoomed to the readings below 0.1 Hz.
- **261104.24** (2026-11-04) — Frequency histogram axis fixed at 49.3–50.7 Hz for all resolutions.
- **261104.25** (2026-11-04) — Histogram header text removed; buttons wrap when narrow; histogram width/height capped on the wall so the dial and risk panel keep their size.
- **261104.26** (2026-11-04) — `wxTempDraw()`: forecast joined to the latest reading (`WX_JOIN` 6 h cosine fade of the offset, per series; model values kept for the hover); forecast from now − 2 h.
- **261104.27** (2026-11-04) — Temperature plot legend and hover: "joined to the latest reading" removed.
- **261104.28** (2026-11-04) — `html.embedfreq .freq-hist` max-width none (fills to `#freqRisk`).
- **261104.29** (2026-11-04) — `fqFont()` (+4/3 px in `embedfreq`) for `fhgDraw`, `drawFreq` y labels and `drawFreqDial` labels; `embedfreq` CSS sizes +1.33 px for ≤ 10.5 px text; `.fh-p` wraps.
- **261104.30** (2026-11-04) — `embed=freq`: `#freqCard h2` size = the wall's `.pn h2` size ÷ the frame's scale (on load, after 1.5 s and on resize); 13.67 px fallback.
- **261104.31** (2026-11-04) — `embedfreq` `#freqCard h2` colour `--ink`.
- **261104.32** (2026-11-04) — `wxTempDraw` `fcAt()`: first forecast hour for a time up to 1 h before it.
- **261105.1** (2026-11-05) — `.ea-radar-slot` height `min(620px,70vh)` (was `min(560px,62vh)`).
- **261105.2** (2026-11-05) — `.ea-radar-slot` `aspect-ratio:5/6`, `max-height:min(820px,85vh)`.
- **261105.3** (2026-11-05) — `.ea-radar-slot` `aspect-ratio:10/13`, `max-height:min(880px,88vh)` (rate plot clear of the circle).
- **261105.4** (2026-11-05) — Light / dark theme block (`gbTheme`), `#themeBtn` in the header; no corner glows in light.
- **261105.5** (2026-11-05) — Theme lightness map darker for text and lines.
- **261105.6** (2026-11-05) — Theme: panels ~15 % darker, background/border tones desaturated.
- **261105.7** (2026-11-05) — `window.name` `gbwall` cleared on load.
- **261105.8** (2026-11-05) — "gauge radar" → "rain & gauge radar".
- **261105.9** (2026-11-05) — `wxTempDraw`: logged readings joined across gaps up to 2 h, a dot per reading.
- **261105.10** (2026-11-05) — Wind dials: merged first lines, `.wd-svg` fills the cell, surface arrow `aLen` 39, 850 hPa glyph ×0.7 on top.
- **261105.11** (2026-11-05) — `.ea-radar-slot` `aspect-ratio:20/29`, `max-height:min(920px,90vh)`.
- **261105.12** (2026-11-05) — 850 hPa line "mph | m/s"; `.wd-gust` / `.wd-sub` lines one size, no wrap.
- **261105.13** (2026-11-05) — `.ea-detail` `container-type:inline-size`; `.ea-radar-slot` `width:100%`, `height:min(145cqw,920px,90vh)` (no aspect-ratio).
- **261105.14** (2026-11-05) — `eaRadarPos()`: slot height fitted once to the visible space (480 px – min(1.45 × width, 920 px)).
- **261105.15** (2026-11-05) — `.wd-big` top lines: temp | dew point (block 3), rain / snow 1h (block 4).
- **261105.16** (2026-11-05) — Top lines as one line with labels in the value font and brightness, blank line after; `wdFitBig()` fits them to the panel width (cap 20/24 px, floor row size + 1).

### `powercuts_page.html`

Header history began at 260916.1; earlier builds are in the dated entries above.

- **260916.1** (2026-09-16) — Added Northern Ireland (NIE Networks) + Republic of Ireland (ESB PowerCheck) regions, dots and totals; map extended west to fit all Ireland
- **260916.2** (2026-09-16) — Widened map panel + right-gutter left-push so the key clears land; pinned NIE label (west Atlantic), SPEN North Wales (Irish Sea) and NGED South Wales (Celtic Sea) via fixed label anchors
- **260916.3** (2026-09-16) — Rebrand to "UK & Ireland Power Cuts" (GB->UK); customers-off readout scales 1pt per severity step with warning colours; National-now planned/unplanned rows; key notes + reporting note merged into the below-map legend
- **260917.1** (2026-09-17) — Window selector moved above the history plots; PSR note moved to the below-map legend; Isle of Wight split from the mainland ring; By-Operator panel gains an operators/countries toggle (per-country totals via point-in-country classification)
- **260917.2** (2026-09-17) — Wind vs power-cuts correlation — stats panel gains a stats/wind switch with a mini response curve + enlarge-to-full modal (region + faults/customers-off selectors, live prediction, learning states) fed by /api/windcuts; map/right columns rebalanced to stop word-wrap
- **260917.3** (2026-09-17) — Antivirus false-positive fix — embedded data arrays (DNO_AREAS/IE_AREAS/COUNTRIES) wrapped from single ~48KB lines to <=~300-char lines so heuristic scanners no longer read them as a packed/obfuscated payload. No behaviour change.
- **260917.4** (2026-09-17) — Map region label text colours by feed freshness — green live, amber stale, red down (grey when key needed / no feed); pairs with the backend's 2h grace so a briefly-silent feed reads amber, not red
- **260917.5** (2026-09-17) — Fix low-contrast selected segment buttons — the wind-modal metric toggle (new faults/hr · customers off/hr) lacked the .btn surface, so its selected state's accent text sat on the browser default pale button; gave it the .btn surface and made the shared selected state a solid accent fill with dark ink (also lifts the window + region selectors)
- **260917.6** (2026-09-17) — Wind-modal region buttons now stay lit when selected — the .on lit style was scoped to .win buttons only, so the #wcRegions buttons toggled the class but never highlighted; added a general .btn.on rule
- **260918.1** (2026-09-18) — Fixed the NIE Networks click-through map link (old www.nienetworks.co.uk/power-outages/power-outages-map 404s) → https://powercheck.nienetworks.co.uk/
- **260918.2** (2026-09-18) — Per-provider MUTE — a mute toggle per operator in the By-Operator panel; a muted provider is excluded from the national totals and greyed on the map (labels + dots). Shared (localStorage) with the dashboard badge/alarm, for when a provider warns its data may be unreliable.
- **260926.1** (2026-09-26) — Build history moved from the file header to CHANGELOG.md. Home marker and near-you radius ring on the map; "Near you" list at the top of the By-Operator panel.
- **260929.1** (2026-09-29) — Orange "active incidents" key on the customers-off plot.
- **260929.2** (2026-09-29) — Hover readouts on both plots and on map incidents; strokes, dots and labels stay screen-sized when zoomed.
- **260929.3** (2026-09-29) — ESB stock status text shown up to "as quickly as possible."
- **260929.4** (2026-09-29) — Other operators' status text ends at a sentence.
- **260929.5** (2026-09-29) — Customers off by operator (stacked bands); grid-event strip under both plots.
- **260929.6** (2026-09-29) — By-operator view moved from a switch to a pop-up.
- **261001.1** (2026-10-01) — Near-you box border coloured by nearest cut as a share of the set radius (yellow <100%, amber <50%, red <20%, hot pink <10%)
- **261001.2** (2026-10-01) — Near-you box: totals line (cuts, customers off, unplanned/planned split) when 2+ cuts are in range
- **261001.3** (2026-10-01) — Near-you box: nearest three cuts listed, the rest inside the radius summarised on one line
- **261014.1** (2026-10-14) — "Monitored areas" button and popup: 10 postcode slots with ✕ buttons and Save, place names from the server, cut count on the button.
- **261014.2** (2026-10-14) — Map tooltip says "customer count not yet confirmed" for incidents whose count is pending (UKPN); monitored-areas note updated.
- **261014.3** (2026-10-14) — Tooltip: pending counts show the predicted figure as "predicted" (or "planned"); "end time not given" for planned outages without one.
- **261014.4** (2026-10-14) — Overdue incidents drawn as small grey dots with an explanatory tooltip.
- **261015.1** (2026-10-15) — Monitored areas popup: optional name per slot, column headings.
- **261024.1** (2026-10-24) — Monitored areas popup: Repeat tick box per area.
- **261024.2** (2026-10-24) — "Republic of Ireland" / "Rep. of Ireland" shown as "Ireland" (key, notes, map area, countries).
- **261024.3** (2026-10-24) — Wind vs power cuts: duplicate area buttons for Ireland and Northern Ireland removed.
- **261024.4** (2026-10-24) — Wind vs power cuts chart: gust axis title centred below the ticks (no clash), chart text 1 pt larger.
- **261024.5** (2026-10-24) — Wind vs power cuts chart: y-axis values and title 1 pt larger, brighter.
- **261024.6** (2026-10-24) — Dashboard size rules: 1,320 px max width, margins, 980 px single column, phone margins, wide-screen zoom when standalone.
- **261025.1** (2026-10-25) — Hover card notes when an incident is shown at its postcodes' centre instead of the operator's location.
- **261025.2** (2026-10-25) — Incidents flagged outside their operator's area drawn faded with a dashed rim and left out of the near-you box.
- **261025.3** (2026-10-25) — Key settings list Northern Powergrid and UKPN with optional keys.
- **261028.1** (2026-10-28) — Key window shows "✓ key entered" / "key saved · feed down" for each operator with a saved key.
- **261028.1** (2026-10-28) — Key window shows "✓ key entered" / "key saved · feed down" for each operator with a saved key.
- **261031.1** (2026-10-31) — Severity bars (By operator, Countries) linear out of max(1,000, largest), coloured by the alert bands (`sevScale`, `sevBar`).
- **261031.2** (2026-10-31) — Severity bars out of max(10,000, largest).
- **261031.3** (2026-10-31) — By operator / By country tables share fixed column widths (`.ops` `table-layout:fixed`); country rows get the empty feed and mute cells.
- **261031.4** (2026-10-31) — `.ops th.num` right-aligned.
- **261031.5** (2026-10-31) — ✕ in the header when framed (`#popClose`, posts `{gbPc:"close"}` to the dashboard); Esc forwards the same.
- **261105.1** (2026-11-05) — Light / dark theme block, `#themeBtn` (top-level only).
- **261105.2** (2026-11-05) — Theme lightness map darker for text and lines.
- **261105.3** (2026-11-05) — Theme: panels ~15 % darker, background/border tones desaturated.
- **261105.4** (2026-11-05) — `div.top` (was `.top`, which also matched `html.top` and made the page a flex box).

### `powercuts.py`

Header history began at 260916.1; earlier builds are in the dated entries above.

- **260916.1** (2026-09-16) — Added NIE Networks (Northern Ireland) and ESB Networks PowerCheck (Republic of Ireland) feeds; national totals and history now span both.
- **260916.2** (2026-09-16) — Footnote attribution rebranded from "GB" to "UK & Ireland".
- **260917.1** (2026-09-17) — ESB list fetch: longer timeout (25s) + one short-backoff retry on transient errors (timeout/429/5xx) before the feed is flagged stale.
- **260917.2** (2026-09-17) — A feed's last good reading (incl. an empty 'no outages' one) is now served as 'stale' for up to STALE_GRACE (2h) of silence before it becomes 'down'. Fixes ESB dropping straight to 'down' when it failed after a legitimately empty reading.
- **260917.3** (2026-09-17) — History log now records national un_new (new UNPLANNED incidents/sample) and un_off (unplanned customers off) so the dashboard's storm/power-cuts alarm can track non-planned cuts and restorations. Older rows lack them; readers tolerate.
- **260926.1** (2026-09-26) — Build history moved from the file header to CHANGELOG.md.
- **260929.1** (2026-09-29) — Decompress gzip responses sent without being requested (ESB list endpoint).
- **261006.1** (2026-10-06) — Frozen-feed detection: unchanged incident set for 3 h of daytime → `stale` with reason, `frozen_since`; history rows flag `frozen`.
- **261014.1** (2026-10-14) — Monitored postcode areas: `powercut_watch.json` store, postcodes.io validation and place names, `watch_matches()`; NPg incidents keep the postcodes of all collapsed rows.
- **261014.2** (2026-10-14) — UKPN: restored and `MULT-` group rows dropped; unconfirmed counts null with `count_pending`; planned customer figure; full postcodes kept for monitored-area matching.
- **261014.3** (2026-10-14) — NPg: finished incidents dropped, planned flag from `natureofoutage`, pending counts. ESB: finished planned outages dropped, times to ISO, `end_unknown`. NGED: planned works window.
- **261014.4** (2026-10-14) — SPEN: planned works past their end time dropped, planned start date used. NIE: times converted to ISO local.
- **261014.5** (2026-10-14) — Incidents > 7 days past their ETR moved to `overdue` (uncounted; ids kept in new/cleared diffing).
- **261015.1** (2026-10-15) — Monitored areas: optional `alias` per area (stored, cleaned, passed in matches).
- **261024.1** (2026-10-24) — Monitored areas: optional `repeat` per area (stored, passed in matches).
- **261024.2** (2026-10-24) — "Republic of Ireland" renamed "Ireland" in source texts and comments.
- **261025.1** (2026-10-25) — SPEN locations checked against their postcode districts; moved to the district group's centre when over 35 km from all of them (`pos_note`, `pos_src`, `pos_published`).
- **261025.2** (2026-10-25) — Postcode-district lookups per poll raised from 40 to 250, so a fresh cache fills on the first poll.
- **261025.3** (2026-10-25) — OpenDataSoft feeds read through the export endpoint in one call (gzip), NPg filtered to live rows on the server; paging only as a fallback.
- **261025.4** (2026-10-25) — Incidents inside another operator's licence area and over 5 km outside their own flagged `outside_area` with a `pos_note`.
- **261025.5** (2026-10-25) — A stored key is used for open feeds too (NPg, UKPN).

### `windcuts.py`

New module at 260917.1.

- **260917.1** (2026-09-17) — New module.
- **260917.2** (2026-09-17) — NIE/ESB incidents are placed by operator identity (single area + country each), so an unresolved NIE postcode still counts to Northern Ireland rather than falling through to ALL-only. See OP_REGION.
- **260926.1** (2026-09-26) — Build history moved from the file header to CHANGELOG.md.
- **261024.1** (2026-10-24) — ESB region renamed "Ireland"; one-time automatic rename of the old region keys in state, bin statistics and corpus files.

### `rain_probe.py`

Header carried only the current build; builds before 260923.1 are in the dated entries above.

- **260923.1** (2026-09-23) — Approach episode: one approach voice, reading-based timing, episode and shadow-threat logs.
- **260923.2** (2026-09-23) — Steering wind, speed gate, hand-over tracks, gauge flags. Never deployed (file lost); rebuilt as 260926.1.
- **260926.1** (2026-09-26) — Stage 2 rebuilt: steering flow used throughout, speed gate, hand-over tracks from wet-now gauges, stale tracks, stationary/suspect gauge flags, shadow-log steering fields; sea-net OC4 fallback reads upwind-first with rotation, keeps last readings with ages, labels the real source. Unused local removed from `select()`. Header tidied to the current build + CHANGELOG pointer. Gauges keyed by EA reference and named by place (else grid reference); points carry id/ref/grid/place.
- **260927.1** (2026-09-27) — `OPEN_METEO_MODELS` sent as `models=` on the rain-net and steering requests.
- **260927.2** (2026-09-27) — Local-first Open-Meteo with automatic public fallback (`_om_request`), 5-min local cooldown and retry, all-null detection, `local_down`/`local_up` logging; public-only backoff; `OM-local` metering; `om_get_json()` and `om_status()` for the server.
- **260927.3** (2026-09-27) — Land-probe limits relaxed while self-hosted Open-Meteo is serving: no episode cap, 15-min heartbeat/clear-check, all points per heartbeat, third range at 25 km, quiet-hours stretch ignored.
- **260929.1** (2026-09-29) — Radar-sampled sea net / home ring / land probes (RainViewer) with Open-Meteo fallback; late EA gauges (>45 min) excluded from the situation field, all-late treated as EA unavailable.
- **260929.2** (2026-09-29) — Late-gauge threshold 75 min with 60 min all-late hysteresis; home ring keeps its cache, samples immediately on the free sampler, labels by real source.
- **260929.3** (2026-09-29) — Open-Meteo current amounts converted to mm/h using the response interval.
- **260929.4** (2026-09-29) — Virtual net/ring points keep `modelled: true`; new `measured` flag for radar/OpenWeather readings.
- **260930.1** (2026-09-30) — Radar net ignores the quiet-hours multiplier; mobiles keep source and radar frame time (confirmed for RV or OC4); approach ETA corrected for the lead reading's age (`data_age_s`).
- **261014.1** (2026-10-14) — Rainfall prediction fine tuning: radar at home counts as arrival; "fizzled out" only when radar shows the upwind sector clear (`fizzle_held` otherwise).
- **261022.1** (2026-10-22) — "At your location" intensity measured at home (radar box at home / nearest gauge < 5 km, two-reading smoothing) instead of the 40 km 90-min peak; at-home heartbeats 20/60 min; re-entry hold and upgrade window 30 min; hourly heavier-rain-nearby note.
- **261022.2** (2026-10-22) — Alert audit fixes: rain at home also from radar (two frames); area words from current measured readings, "Continuous rain across the area", quiet after rain at home, 20-min re-announce floor; nowcast lines from measured home reading, gauge confirm within 5 km, visibility-only fog line, no wind/persistence claims; all-clear only when the area is clear.
- **261024.1** (2026-10-24) — Forward nowcast "heavier" message: time from the minute series (`intensify_eta_min`), "soon" when unknown, "Rain is continuing at your location…" after 30 min of measured rain at home (`home_wet_since`); edge trigger keyed on a stable identity so the countdown doesn't re-speak.
- **261030.1** (2026-10-30) — `radar_grid()`: the newest RainViewer frame as 0.5 km rain-rate cells around home, with a motion vector from the frame before (`_rg_motion()`, correlation) or the steering flow; in `situational["radar_grid"]`.
- **261104.1** (2026-11-04) — Max-rate history: `maxh_radar()` (each new frame's `_rg_max()` within `RG_KM`; one-off background back-fill of past frames via `_rg_grid(tile_fn=…)`), `maxh_gauges()` (physical gauges within the plot, 5-minute slots, 15-minute readings), `maxh_public()` → `situational.max_hist`; `rain_max_hist.json`.
- **261104.2** (2026-11-04) — `maxh_home()`: radar box at home and nearest gauge within `SIT_HERE_KM` into the max-rate store (`home_radar` / `home_gauge` in `maxh_public()`).
- **261104.3** (2026-11-04) — `_rv_newest_frame()`: frame list every `RV_MAPS_DUE_S` once a frame is due, else `RV_MAPS_TTL_S`; records publication / first seen (`_rv['pub']`, on the grid as `pub` / `seen`); `radar_view_grid()` with the engine's last flow.
- **261104.4** (2026-11-04) — `om_get_json(..., public_only=True)` / `_om_request(public_only)`: public host only, for variables the local instance doesn't sync.
- **261105.1** (2026-11-05) — `_rg_motion_multi()` (`RG_PAIRS` 3, `RG_AGREE_KMH` 8, `RG_AGREE_FRAC` 0.35): motion `src` radar / uncertain / steering; `_rg_motion_grid()` per-frame 1 km grids (`_rg['mgrids']`); `_rg_fetch_tile()`; `steer` on the grid.
- **261105.2** (2026-11-05) — `radar_cell_motion()` / `_rg_patch_motion()` (`RG_CELL_BOX_KM` 12), `radar_area_motion()`; `_track_motion()` radar first, verified by `_gauge_motion()` agreement or `_motion_hits()` (`TRACK_HIT_KM` 6, `TRACK_HIT_MIN_MOVE` 4, 45 min); approaching by path miss ≤ `SIT_APPR_MISS_KM` 8; tracks carry `move_src`, `verified_by`; `sit_probe_hist`, mobile `hist`.
- **261105.3** (2026-11-05) — `approach_episode.threat` adds `eta_lo`, `eta_hi`, `measured`, `snow`, `too_far`.
- **261105.4** (2026-11-05) — `_eta_say()`: spoken arrival (about N min within 30 min; clock time when measured; minutes on the steering wind); `eta_say` on the threat, used by the alert and confirm phrases.
- **261105.5** (2026-11-05) — `RG_HAIL_DBZ` 55 / `RG_HAIL_MMH`: hail cells as byte 255, grid `hail` summary.
- **261105.6** (2026-11-05) — `radar_hail_cells()`; threat `hail` (cone, ≤ `APPR_MAX_KM`); `sit.hail_here` (≤ `HAIL_HERE_KM` 3); "possibly with hail" in the alert, confirm and at-home phrases.

### `forecast_view.html`

Formerly `engine_view_live.html`, an unversioned diagnostic page served at `/engine`.

- **260926.1** (2026-09-26) — Renamed Forecast view, served at `/forecast` (`/engine` removed). Licence/build header. Gauge tooltips with place, grid and EA reference, distance and direction from home. Radar-only mode (`?view=radar`) for the EA page. Location from the link or the EA page (no built-in default); connects by itself; bottom text replaced; paste-JSON box removed.
- **260929.1** (2026-09-29) — Late-gauge rings and count; radar net labels; OM-local counted separately in the API line; RainViewer credit.
- **260930.1** (2026-09-30) — Mobile tracker tooltip names the reading's source (radar / OpenWeather / Open-Meteo) instead of "OC4 confirmed".
- **261022.1** (2026-10-22) — "at-home voice" row: the measured at-home reading and its source.
- **261022.2** (2026-10-22) — State line shows the current area intensity; 90-min peak in brackets only when higher.
- **261024.1** (2026-10-24) — Dashboard size rules: 1,320 px max width, margins, 980 px single column, phone margins, wide-screen zoom when standalone.
- **261104.3** (2026-11-04) — `pullRadar()` each minute from `/api/radar-grid` (newer frame kept); key shows published time and minutes moved; redraw every 5 s.
- **261105.1** (2026-11-05) — Radar not moved when motion is uncertain; key: radar motion vs 850 hPa wind line, pairs agreeing, 13.3 px text on a dark backing.
- **261105.2** (2026-11-05) — Radar key 20 px lower, shorter wording.
- **261105.3** (2026-11-05) — Radar key lines wrapped at " · " to the room left of `MH_BOX`.
- **261105.4** (2026-11-05) — Portrait panel: `cy=min(H/2,R+70)`; key scale `bw` 22–36 to fit, units label on its own line when narrow.
- **261105.5** (2026-11-05) — Light / dark theme block, `#themeBtn` in the titlebar (top-level only); `rgBuild` uses the mapped key colours.
- **261105.6** (2026-11-05) — Theme lightness map darker for text and lines.
- **261105.7** (2026-11-05) — Theme: panels ~15 % darker, tones desaturated; radar circle filled darker grey in light.
- **261105.8** (2026-11-05) — Portrait margin `MG` 32 (landscape 70).
- **261105.9** (2026-11-05) — `mvSrc()`: cell movement source / confirmation in the plot tag, hover and tracked-cells list.
- **261105.10** (2026-11-05) — `homeNow()` / `drawHomeNow()`; portrait circle 58 px lower.
- **261105.11** (2026-11-05) — Key: every other scale number when swatches < 30 px; long part breaks after its colon.
- **261105.12** (2026-11-05) — `homeNow()` wording cut to a few words.
- **261105.13** (2026-11-05) — Hail cells white (unthemed), key "hail?" swatch, "· hail nearby"; `cd()` countdown; 15 s redraw.
- **261105.14** (2026-11-05) — Portrait circle +14 (was +58) from the top margin; nowcast wraps to 40 % of the width.
- **261105.15** (2026-11-05) — Tall layout only when height ≥ 1.3 × width (`PT`).
- **261104.2** (2026-11-04) — `placeTip()` keeps hover text inside the window; max-rate plot draggable by its title row (`fvMhPos.<shape>`, double-click resets) and hideable (`fvMhHidden`).
- **261104.1** (2026-11-04) — `drawMaxHist()`: max rain rate bar plot, bottom right (1h / 6h / 24h, radar / gauges / both, `fvMhSpan` / `fvMhSrc`); bar hover; redrawn each minute.
- **261031.1** (2026-10-31) — Radar overlay palette: rain green to red, sleet pinks, snow blues (Andy); opacity 45–75 %.
- **261030.2** (2026-10-30) — Radar overlay: `radar_grid` drawn under the plot at up to 50 % (75 % from 261031.1), coloured by rain rate (`RR_STEPS`, snow / sleet palettes by wet-bulb at home), moved to now by its motion; key with click toggle (`fvRadarProj`).
- **261030.1** (2026-10-30) — Radar-only mode draws at the frame's shape (`fitEmbed()`: 760 units high, width to the frame's proportions), so the plot stays round.

### `metar.py`

New module at 260917.1. File unchanged by the 260926.1 tidy (no history to move).

- **260917.1** (2026-09-17) — New module.
- **260929.1** (2026-09-29) — Header build line in the standard format (was `Build: 260917.1`). No code changes.
- **261006.1** (2026-10-06) — Second by-id call for stations the bbox omits (London City etc.); non-UK/IE stations untagged and excluded from rollups; METARs over 3 h old dropped; `status()` reports `foreign`.
- **261006.2** (2026-10-06) — `STAND_INS`: Heathrow supplies the London area's gust while EGLC has no current report, marked `stand_in`.

### `gas_probe.py`

Unversioned until 260929.1; earlier changes are in the dated entries above.

- **260929.1** (2026-09-29) — Header build line added. No code changes.
- **261024.1** (2026-10-24) — Margins Notice signal: NTS demand forecast vs Expected Available Supply estimate (non-storage supply now + LNG/storage deliverability); day-specific values from the current gas day only; PCLP 0 placeholders ignored; live linepack for the rate guard.
- **261024.2** (2026-10-24) — Alarm ladder: demand-level alerts, 90/95/100 % margin tiers, live linepack below forecast minimum, PCLP revision, storage cover, price stress; rate guard as corroborator; event list for the dashboard.
- **261028.1** (2026-10-28) — Rough no longer a storage site (paused): its flow counts as non-storage supply and its deliverability is left out of storage.
- **261028.1** (2026-10-28) — Rough no longer a storage site (paused): its flow counts as non-storage supply and its deliverability is left out of storage.

### `owm_onecall.py`

Unversioned until 260929.1; earlier changes are in the dated entries above.

- **260929.1** (2026-09-29) — Header build line added. No code changes.

### `reservoirs.py`

New module at 261016.1.

- **261016.1** (2026-10-16) — Reservoir storage for the EA page. Sources: EA national monthly regional table (PDF, via optional `pypdf`), weekly drought summaries, area summaries, flood-monitoring reservoir gauges (sanity-checked) and the London Datastore. Drought status is replayed from the report text. Background build every 6 h (gauges hourly), with a cache and a daily gauge log.
- **261016.2** (2026-10-16) — South West Water reservoir-levels page (total and eight reservoirs with SWW's dates), logged by published date to `reservoir_company_log.jsonl` for history.
- **261016.3** (2026-10-16) — Water companies from Stream open data (ArcGIS feature services, read incrementally when a layer changes, with new layers found by catalogue search) plus company websites (Severn Trent weekly pages, South West Water, Northumbrian, Essex & Suffolk, United Utilities, Bristol Water), merged into `reservoir_store.json` with a daily backup. Per-region capacity-weighted totals; per-source status for monitoring; `series=` for one reservoir's full history.
- **261024.1** (2026-10-24) — "Republic of Ireland" renamed "Ireland" in the module notes.
- **261029.1** (2026-10-29) — South West Water website: reads "levels are at" as well as "level is at"; no reservoir rows is now reported as a failure.
- **261029.2** (2026-10-29) — South West Lakes Trust weekly table (reservoirs South West Water's page doesn't list); reservoirs silent 90+ days left out while the company has current ones.

### `balancing.py`

New module at 261017.1.

- **261017.1** (2026-10-17) — Balancing costs: NESO confirmed daily costs (BSUoS cost data, constraint breakdown, thermal constraint costs by boundary, day-ahead Scottish flows over limits) and Elexon indicative cashflows and accepted bid volumes (by BM unit fuel), calibrated estimate with likely range, daily wind and frequency events from the local logs, event detection, summary figures and correlations. Background refresh, store `balancing_store.json` with daily backup.

### `wall.py`

New module at 261023.1.

- **261023.1** (2026-10-23) — New module: /api/wall, /api/wall-network, /api/wall-history, /api/wall-replay for the transmission wall.
- **261023.2** (2026-10-23) — Replay history and /api/wall-replay removed; 20-minute in-memory interconnector history for the 15-min change.
- **261024.1** (2026-10-24) — REMIT unavailability reports (units and interconnectors, now and next 24 h) in /api/wall as `outages`.
- **261025.1** (2026-10-25) — Background node naming from OpenStreetMap (Overpass), cached in `node_names.json` with a committed seed; names overlaid on /api/wall-network.
- **261029.1** (2026-10-29) — Unit positions from `osm_sites.py` (OpenStreetMap) where available; each unit row carries its position source and km moved.
- **261029.2** (2026-10-29) — `sites_bytes()` for the new `/api/wall-sites` endpoint (generator sites from `osm_sites.py`).
- **261030.4** (2026-10-30) — `so_summary()` for the dashboard chip (background PN/BOALF fetch when no fresh wall payload, `_so_areas()`, `_area()`); units' names in the turn-down; `post_b6()` keeps the page's `bnd`.
- **261030.3** (2026-10-30) — SO-flagged turn-down by node (`_so_turndown()`, `so` in /api/wall); `_seg()` keeps the SO flag; `_level(seg=True)`.
- **261030.2** (2026-10-30) — Pinch-point store and `pinch_alerts()` removed.
- **261030.1** (2026-10-30) — `_fuelinst_ic()` keeps the last good interconnector flows for up to 10 minutes through a FUELINST gap (`FI_HOLD_S`).
- **261029.16** (2026-10-29) — `post_b6()` keeps the page's pinch-point alerts (`_pinch_store()`, validated, at most 30); `pinch_alerts()` returns them while under 5 minutes old.
- **261029.15** (2026-10-29) — ETYS boundaries sent with /api/wall-network (`etysbnd`).
- **261029.14** (2026-10-29) — ETYS ratings (`etys.py`) matched to the network's lines and sent with /api/wall-network (`ratings`, `ratings_info`); unnamed nodes' nearby names offered as second-choice names.
- **261029.13** (2026-10-29) — `_measured()`: NGED live GSP imports matched to nodes (aliases; old, all-zero and shared-operator nodes left out), `meas` in /api/wall; embedded units behind a measured GSP left out of the reconciliation; `_gsp_log()` / `_gsp_check()` score the model's unmeasured estimate against the measurements.
- **261029.12** (2026-10-29) — `_recon()`: FUELINST reconciliation by fuel (gap at FUELINST's time spread over that fuel's located units, by capacity for OTHER and PS, by output for the rest), `recon` in /api/wall.
- **261029.11** (2026-10-29) — `_emb_now()`: NESO's embedded wind and solar estimate shared over the nodes by Embedded Capacity Register capacity (registered fraction only), `emb` in /api/wall; NESO embedded capacities read with the demand split.
- **261029.10** (2026-10-29) — Scotland's share of transmission demand fitted from NESO's Demand Data Update (half-hour x weekday/weekend + embedded wind and solar, refitted every 3 h) and sent as `split` in /api/wall; `post_b6()` keeps the page's modelled transfer per half-hour (`b6_log.json`) and checks it against NESO's metered Scottish transfer.
- **261029.9** (2026-10-29) — Gunfleet Sands 3: own landing at Waterglade, Clacton (GFS3 newsletter); a landing within 2 km of where another farm's line to the same node starts ends the line there ("to" kind "land").
- **261029.8** (2026-10-29) — Export cables mapped part way out from the connection (LINK_SEA_KM 15 km): line from the farm to the cable's sea end ("to"), one farm per cable; NODE_RENAME for Sizewell, Heysham, New Deer, Crystal Rig.
- **261029.7** (2026-10-29) — Offshore connection audit: OFFSHORE_UNITS (Hornsea 3 → Norwich Main, Walney 2 → Stanah, Walney 3, Burbo Bank 1 → Birkenhead); local-network landings ("near") for 12 small farms; nearest node never another wind farm's substation; sources noted per entry.
- **261029.6** (2026-10-29) — Offshore links follow a farm's mapped 132 kV export cable (OpenStreetMap or operator) to where it ends; Gwynt y Môr → Bodelwyddan.
- **261029.5** (2026-10-29) — `/api/wall-dnolines?kv=`; OpenStreetMap 132 kV lines an operator's map covers are left out of /api/wall-network; Northern Powergrid live readings by node in /api/wall (`local_live`).
- **261029.4** (2026-10-29) — `/api/wall-local` (embedded generation per node from `dno_data.py`); offshore farms not in `OFFSHORE_LINKS` use their DNO register GSP; unit rows carry registered capacity (Elexon).
- **261029.3** (2026-10-29) — `OFFSHORE_LINKS`: offshore wind farms' connection points (TEC register, published project information, mapped export cables); `offlinks` in /api/wall-network and each link's output in /api/wall; the farms' units counted at their connection.

### `grid_wall.html`

New page at 261023.1.

- **261023.1** (2026-10-23) — New page: transmission wall with six screen layouts, in-browser DC load flow, change cues, movers, replay.
- **261023.2** (2026-10-23) — Regional map: wheel zoom about the cursor, drag to pan, double-click zoom, +/− buttons. Western HVDC share against NESO's SCOTEX limit.
- **261023.3** (2026-10-23) — Regional panel source buttons: Network, Power cuts, Gas, Rivers & floods, Rain radar.
- **261023.4** (2026-10-23) — Sweep-arc trend marker (nodes and interconnectors), corner-bracket selection, key in every map; dashed ring and change flashes removed.
- **261023.5** (2026-10-23) — Interconnectors along their cable routes, MW-scaled width, flow dashes, labelled end boxes, totals box.
- **261023.6** (2026-10-23) — Interconnector width proportional to MW (2 px per GW), no halo; single-line labels and totals.
- **261023.7** (2026-10-23) — Network file fetched without the browser cache; stub route for a link with no route in the file.
- **261023.8** (2026-10-23) — Key as a strip under each map instead of over it; "≈" for HVDC estimates.
- **261023.9** (2026-10-23) — 132 kV distribution layer with a per-map toggle.
- **261023.10** (2026-10-23) — Clear-selection button, Esc and empty-map click; replay indicator with back-to-live on every screen; picker backdrop and full-screen prompt fixes; voltages de-duplicated.
- **261023.11** (2026-10-23) — Scotland → England badge in a fixed corner position instead of pinned to the map.
- **261023.12** (2026-10-23) — Border drawn and labelled with the transfer; transfer measured across the real border (was latitude 55.05°N).
- **261023.13** (2026-10-23) — Map-only pop-up view (`?screen=map`) with larger text and an All GB preset; names and MW labels only when zoomed in; replay removed.
- **261023.14** (2026-10-23) — Ignore zero-size resizes; re-lay out the pop-up map when shown again (fixes blank map on second open).
- **261024.1** (2026-10-24) — Animation loop survives a failed draw (Firefox, hidden pop-up); fixes blank map on reopening.
- **261024.2** (2026-10-24) — One Scotland → England label at the middle of the border; dashed border line removed (plain land outline).
- **261024.3** (2026-10-24) — Border label position chosen to avoid circuits, routes and names (left of the arrow preferred).
- **261024.4** (2026-10-24) — Circuit width by modelled flow; changed values highlighted for a minute; REMIT outages on map, tooltips, substation and interconnector panels, new Outages panel.
- **261024.5** (2026-10-24) — Circuit change highlight from 10 MW (was 50 MW), still 10 %.
- **261024.6** (2026-10-24) — Outage rings at half strength.
- **261024.7** (2026-10-24) — Frequency panel is the dashboard's card via `#embed=freq` (d2, h2, uw; replaces Demand & wind on uw).
- **261024.8** (2026-10-24) — Ultrawide single: frequency panel replaces System now at the top; Demand & wind restored.
- **261024.9** (2026-10-24) — System now without frequency on dual screen 2 (d2, h2).
- **261024.10** (2026-10-24) — Circuit changes as tinted values with ▲/▼ amount until the next run; values placed without overlaps and faded on wider views.
- **261024.11** (2026-10-24) — Flow dashes (circuits, interconnectors, HVDC) at half speed.
- **261024.12** (2026-10-24) — Internal HVDC links drawn with width by estimated flow.
- **261024.13** (2026-10-24) — Network pop-up: max 1,320 px wide, centred; stacked scrolling layout below 900 px with key hidden by default.
- **261024.14** (2026-10-24) — Frequency panel: no wall frame around the embedded card.
- **261024.15** (2026-10-24) — Regional panel sources: Generators, 7-day generation, My home; source buttons wrap.
- **261025.1** (2026-10-25) — Borrowed node names, generation-site names and locations for unnamed nodes, with their source in the hover card and panel; shorter map labels.
- **261028.1** (2026-10-28) — Regional panel: Gas storage source spreading over the map panel (⇤ one panel / ⇥ two panels); single-HD map panel gets the regional source buttons.
- **261028.1** (2026-10-28) — Regional panel: Gas storage source spreading over the map panel (⇤ one panel / ⇥ two panels); single-HD map panel gets the regional source buttons.
- **261029.1** (2026-10-29) — Regional source "Gas storage" renamed "Gas sources"; tells the embedded diagram when it spans two panels.
- **261029.2** (2026-10-29) — Gas panels over the columns either side of the two-panel Gas sources view, with selector buttons where space is short.
- **261029.3** (2026-10-29) — Unit position source (OpenStreetMap via UK Grid Atlas, or Terravolt) on hover in the substation panel; credit in Data sources.
- **261029.4** (2026-10-29) — Map layers fade in with zoom (400 kV only fully out); layer buttons in each key strip replace "132 kV dist."; flow dashes from 5 MW, faint below 40 MW.
- **261029.5** (2026-10-29) — Generators layer: OpenStreetMap generator sites as faint dots (area by capacity, colour by fuel, smaller ones fading in with zoom), hover card, map credit.
- **261029.6** (2026-10-29) — REPD status and capacity on generator-site hover cards; REPD layer (rings) for operational projects with no OSM site; map credits drawn on top.
- **261029.7** (2026-10-29) — Circuit values on every circuit carrying 5 MW or more (was 150 MW).
- **261029.8** (2026-10-29) — Generator and REPD site names when zoomed in, placed around substation names and circuit values, biggest first.
- **261029.9** (2026-10-29) — Offshore wind farms drawn straight to their connection points, with their published output as dashes and MW values; hover card with the source.
- **261031.1** (2026-10-31) — Counting tint on KPI figures limited to the digits (`.kpi .v .tn`).
- **261031.2** (2026-10-31) — ETYS boundary sides from substation position (`bndPolygon`, `inRing`) instead of a network walk; near-closed boundaries closed across their mouths (`loop`, inward / outward); pocket turn-down only when exporting.
- **261031.3** (2026-10-31) — Power-cuts panel bars out of max(1,000, largest), coloured by the alert bands (`pcCol`).
- **261031.4** (2026-10-31) — Power-cuts panel bars out of max(10,000, largest).
- **261104.1** (2026-11-04) — Gas sources: `GAS_L` gsys, galarm, gmet (table); `GAS_R` gmetbar, gbal, gstore, geu, glng, gpower; `gasMet()`, `gasBalChart()`, `axTicks()`, `axTime()`; `P_.price` Electricity | Gas SAP switch (`gbWallPrice.<layout>`) with `priceChart()`.
- **261104.2** (2026-11-04) — Rain radar view split 0.6 → 0.5 (radar top half; at-home plots and weather panels below).
- **261104.3** (2026-11-04) — Header readouts on `uw` (`{ read: 1 }`): `setRead()` / `hgPoll()` (`/api/gas`, 2 min); `tn` counts and `.tri` 1-hour arrows; `TF.d1`.
- **261104.4** (2026-11-04) — `.pn h2` `calc(var(--h2) + 2.67px)`; `.rt` and `.tag` keep `var(--h2)`; `uw` margin `h` 76.
- **261104.5** (2026-11-04) — `.pn h2` colour `--ink` (was `--ink-faint`).
- **261105.1** (2026-11-05) — Light / dark theme block, `#themeBtn` in the header.
- **261105.2** (2026-11-05) — Theme lightness map darker for text and lines.
- **261105.3** (2026-11-05) — Theme: panels ~15 % darker, background/border tones desaturated.
- **261105.4** (2026-11-05) — `BIG_MW` 500: `lineA()` draws a circuit / offshore link of 500 MW or more (fading in from 400) at every zoom; its end nodes via `_bigN`; link hover and line values follow.
- **261030.19** (2026-10-30) — Rain radar source: radar above (60 %), `#embed=wx` local weather below (`srcFrame()`, `url2`, `split`).
- **261030.18** (2026-10-30) — Interconnectors: "← export | import →" row above the bars, divided at their zero line (`.icdir`).
- **261030.17** (2026-10-30) — `fitSlot()`: a panel with `fit` grows into the panel below when its content overflows (uw: interconnectors over powercuts).
- **261030.16** (2026-10-30) — ▲ / ▼ flashing also in mix, interconnectors, boundaries and movers (`data-keep`: flash only).
- **261030.15** (2026-10-30) — Substation list and units: ▲ / ▼ column (`.tri`, `data-tk`) flashes green / red at 1 Hz while its number counts (`triPaint()`).
- **261030.14** (2026-10-30) — Substation panel ⤒ expand / ⤓ shrink button (`NODE_EXP`, `nodeBig()`), where the panel has `grow`.
- **261030.13** (2026-10-30) — Counting panel numbers: `tn()`, `tnScan()` after each panel draw, 10 Hz tick; tints from `chgRGB()`; tagged in system, mix, transfer, boundaries, ics, topflows, movers, margin and the substation panel.
- **261030.12** (2026-10-30) — `growNode()`: a node panel with `grow` covers that panel while a substation is selected (uw: over powercuts); `drawNodeChart()` stops if its canvas has gone.
- **261030.11** (2026-10-30) — Substation list: output bars (`.sgb`, sized to the panel) and % of GB supply (metered generation + imports).
- **261030.10** (2026-10-30) — Power cuts panel: severity bars (`.pcbar`, share of the worst operator's customers off).
- **261030.9** (2026-10-30) — Power cuts panel: operator rows count all cuts (as the power-cuts page), with planned beside; All cuts KPI.
- **261030.8** (2026-10-30) — Power cuts panel (`P_.powercuts`, `renderPC()`, `PC_OPN`; redrawn after each `/api/powercuts` fetch); uw column 4: ics 340, powercuts 400, node 386, margin 178.
- **261030.7** (2026-10-30) — Outages: every report in a scrolling list (`.olist`, place kept on refresh); uw layout: boundaries 450, movers 250, outages from y 966 (454 high).
- **261030.6** (2026-10-30) — Header chips: Grid, power cuts, gas margin, floods (`pollSums()`, `setSums()`); short labels below 2,600 px (`.hdr.cmp`).
- **261030.5** (2026-10-30) — Map pop-up's ✕ in its own bar (`#popClose`, only when framed), in line with the panels; bar's right padding 10 px (6 px narrow).
- **261030.4** (2026-10-30) — Live chip reads "updated at" (Andy).
- **261030.3** (2026-10-30) — Constraint badge in the header and map bars (`setCon()`, from 500 MW); boundaries shown as constrained sent with the transfer report (`bnd`); live chip reads "live · figures for" with a tooltip.
- **261030.2** (2026-10-30) — Constrained boundaries: `bndSides()`, `bndState()`, `BND_LIM` (ETYS 2025), `BND_NESO` adds B4 (SSE_SP2), `BNDON` each run; orange dashed when constrained; hover card with limit, % and SO turn-down; key.
- **261030.1** (2026-10-30) — Pinch points removed (evalPinch, glows, hover labels, layer, panel; topflows back in d1 and h2); ETYS boundaries at half opacity (.175).
- **261029.30** (2026-10-29) — Regional check per pinch point (`scotCheck()`, `conf`; Scotland held at watch while its transfer check fails); alerts sent with the transfer report (`pinch`); newest NPg live reading time in Data sources.
- **261029.29** (2026-10-29) — Pinch points on loading now only (N-1 / line-outage factors removed); watch ≥ 80 %, warning ≥ 90 %, critical > 95 %; hover label and panel simplified.
- **261029.28** (2026-10-29) — ETYS boundaries layer: faint dashed lines, hover highlight and card with the modelled transfer across (circuits and HVDC crossing an odd number of times) and NESO's SCOTEX day-ahead figures on B6.
- **261029.27** (2026-10-29) — Pinch-point labels on hover only (`pickAlert()`); glow opacity 30 % watch, 50 % warning and critical.
- **261029.26** (2026-10-29) — Pinch points: line-outage factors in the background, intact and N-1 loading each run, alert levels held for two runs (standard-rated circuits held at watch), glow and label on the maps (orange / yellow / blue-white), pinch points layer button, Pinch points panel (in place of Heaviest circuits in the d1 and h2 layouts).
- **261029.25** (2026-10-29) — Seasonal ETYS rating per circuit (`rating()`), standard rating marked * where unmatched; substation panel loading and hover; coverage in Data sources.
- **261029.24** (2026-10-29) — Measured GSP imports used at their nodes (rest of the demand spread over the others; embedded generation there left out); the model's estimate there reported with each run; measured section in the substation panel; summary and score in the transfer panel.
- **261029.23** (2026-10-29) — Flow model adds the server's FUELINST reconciliation at its nodes; transfer panel lists it.
- **261029.22** (2026-10-29) — Flow model injects the server's embedded wind and solar at its nodes and adds it to its region's demand; transfer panel says how much was placed and which registers are missing.
- **261029.21** (2026-10-29) — Flow model puts the server's Scotland share of demand on the Scottish nodes (fixed shares if none); reports each run's transfer to /api/wall-b6; transfer panel shows the estimate and the check; boundary panel says "day-ahead forecast".
- **261029.20** (2026-10-29) — A line that stops at its landing beside another farm's line says so in its hover card.
- **261029.19** (2026-10-29) — Offshore line ends at its export cable's sea end where the server gives one; hover card says so.
- **261029.18** (2026-10-29) — Drawn flows ease from the shown to the new values after each model run (80 % of the gap, 20–90 s); changed values count and their ▲▼⇄ flash at 1 Hz; dash phase integrated per line.
- **261029.17** (2026-10-29) — Offshore connection lines bend at a farm's local-network landing; hover tests every leg.
- **261029.16** (2026-10-29) — Offshore connection lines start where the farm's mapped export cable ends (none where it already reaches the connection).
- **261029.15** (2026-10-29) — Regional map zooms to the selected substation; Network source can span two panels (own setting).
- **261029.14** (2026-10-29) — Substation panel search box (name search over all named substations; Enter selects the first, Esc clears).
- **261029.13** (2026-10-29) — Gas sources panel: ⇤ two panels / ⇥ one panel (arrows swapped).
- **261029.12** (2026-10-29) — Substation panel: every generating substation listed; the panel scrolls and keeps its place across refreshes.
- **261029.11** (2026-10-29) — Operators' 132 / 66 / 33 kV line maps with 66 kV and 33 kV layers (fade in with zoom, loaded on demand), hover card; Northern Powergrid live 132 kV circuit readings in the substation panel; line-map and live-feed status in Data sources.
- **261029.10** (2026-10-29) — Substation panel: local network (DNO registers), generator sites within 8 km, circuit loading %, unit capacity; site hover cards say where the site connects; register status in Data sources.

### `osm_sites.py`

New module at 261029.1.

- **261029.1** (2026-10-29) — New module: UK Grid Atlas's OpenStreetMap generator sites and BM unit map, fetched weekly into `osm_sites.json`; OSM unit positions laid over Terravolt's (missing filled, >1 km corrected, >20 km only with matching fuel and name).
- **261029.2** (2026-10-29) — Generator-site list for `/api/wall-sites` (position, fuel, capacity, name, operator, start; biggest first).
- **261029.4** (2026-10-29) — Each generator site carries its DNO register entry (`conn`: operator, GSP, BSP, kV) in `/api/wall-sites`.
- **261029.3** (2026-10-29) — REPD: newest quarterly CSV found weekly via the gov.uk content API, operational and under-construction GB projects kept in `repd_sites.json`, OSGB36→WGS84; matched to OSM sites (fuel, size-scaled radius, names or capacity); REPD-only operational projects added to `/api/wall-sites`.

### `dno_data.py`

New module at 261029.1.

- **261029.1** (2026-10-29) — New module: the six DNOs' Embedded Capacity Registers (Opendatasoft with the power-cuts keys, CKAN without), weekly into `dno_ecr.json`; GSP → network node by name; per-node embedded generation; site → register entry matching.
- **261029.4** (2026-10-29) — NPg live 132 kV: newest reading's time asked for first, readings within 40 minutes of it exported with a 120 s limit (the 90-minute window from now found nothing); `newest` in its status.
- **261029.3** (2026-10-29) — NGED live GSP data (about 55 files, every 5 minutes, background, 150 s limit): `nged_gsp()`, `nged_gsp_status()`.
- **261029.2** (2026-10-29) — The operators' 132 / 66 / 33 kV line maps (Opendatasoft GeoJSON, SSEN shapefiles, any shapefile in `dno_lines/`), monthly into `dno_lines.json`, simplified; Northern Powergrid live 132 kV circuit readings (every 5 minutes).

### `etys.py`

- **261029.2** (2026-10-29) — ETYS study boundaries from NESO's shapefile (monthly, standard library, Web Mercator → lon/lat, simplified; closed loops in two halves), cached in `etys_boundaries.json`.
- **261029.1** (2026-10-29) — New: transmission circuit ratings from NESO's ETYS Appendix B (newest found on NESO's page, monthly check), read with the standard library, cached in `etys_ratings.json`; matched to network lines by name, nearby name, connections, tees and intermediate nodes; plausibility range per voltage.

### `build_network.py`

New script at 261023.1.

- **261023.1** (2026-10-23) — New script: builds grid_network.json from the gb-dispatch-model network and OpenStreetMap names.
- **261023.2** (2026-10-23) — Adds interconnector cable routes (`icroute`) from links.csv.
- **261023.3** (2026-10-23) — Adds 132 kV distribution lines outside Scotland (`dist`), display only.

### `start_wall.bat`

New launcher at 261023.1.

- **261023.1** (2026-10-23) — New launcher: opens the wall on two monitors as Chrome/Edge app windows.

### `gas_storage.py`

New module at 261028.1.

- **261028.1** (2026-10-28) — New module: National Gas per-site storage flows, stock, LNG tanks and deliverability; estimated per-site fill; GIE AGSI+ Europe; GIE key test and storage.
- **261028.2** (2026-10-28) — 12-month average NTS demand (PUBOBJ1030) for GB days at average use.
- **261028.3** (2026-10-28) — Europe trend rows carry each of the previous five years' same-day % full.

### `gas_storage.html`

New page at 261028.1.

- **261028.1** (2026-10-28) — New page: gas storage pop-up (UK figures, embedded flow diagram, Europe Trend / By country, GIE key window).
- **261028.2** (2026-10-28) — GB days at average use (table and neighbour tile); days labels name their basis.
- Retired at 261028.2 (2026-10-28): the view moved into the dashboard's gas page; `/gas-storage` redirects there.

