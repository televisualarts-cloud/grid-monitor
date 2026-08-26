# test_rain_probe.py — scenario harness for rain_probe. Prints the diagnostic
# log line and the phrase(s) the probe WOULD speak for each situation. Run:
#   python3 test_rain_probe.py
import time
from rain_probe import (ProbeState, run_probe, offset_latlon, TREND_WINDOW_S)

HOME = (50.376, -4.143)          # Plymouth
NOW = 1_700_000_000.0            # fixed clock for reproducibility

def gauge(dist_km, bearing, mm):
    la, lo = offset_latlon(HOME[0], HOME[1], bearing, dist_km)
    return {"lat": la, "lon": lo, "mm": mm, "dist_km": dist_km}

def seed(state, hist_attr, series):
    """series: [(seconds_before_now, value)]"""
    setattr(state, hist_attr, [(NOW - dt, v) for dt, v in series])

def no_sea(_pts):                # offshore returns nothing
    return [None] * len(_pts)

# synthetic land/sea: Plymouth's sea arc is roughly SE..W (120..260 deg); land elsewhere.
def landsea_syn(cand):
    return [120 <= c['az'] <= 260 for c in cand]

def show(title, res):
    print(f"\n=== {title} ===")
    print("  log:", res["log"])
    if res["would_speak"]:
        for w in res["would_speak"]:
            print(f"  SPEAK [{w['tier']}] {w['phrase']}")
    else:
        print("  (no announcement — no state change / nothing to say)")


# 1. Confirmed heavy and building — rising history, wet gauge 5 km away
s = ProbeState()
seed(s, "rain_hist", [(1500, 2.0), (900, 5.0), (300, 8.0)])
res = run_probe(s, home=HOME, rain_mm_h=11.0, pressure_hpa=1006, visibility_m=9000,
                wind_from=225, wind_kmh=20, gauges=[gauge(5, 210, 3.0)],
                now=NOW, sample_fn=no_sea, landsea_fn=landsea_syn)
show("1 Confirmed heavy, building", res)

# 2. Compound: heavy + pressure falling fast
s = ProbeState()
seed(s, "rain_hist", [(600, 12.0)])
seed(s, "press_hist", [(3*3600, 1015), (0, 1008.5)])   # ~-2.2 hPa/hr
res = run_probe(s, home=HOME, rain_mm_h=14.0, pressure_hpa=1008.5, visibility_m=8000,
                wind_from=225, wind_kmh=30, gauges=[], now=NOW, sample_fn=no_sea, landsea_fn=landsea_syn)
show("2 Heavy + pressure falling fast", res)

# 3. Land approach from the NE, home dry
s = ProbeState()
res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1010, visibility_m=10000,
                wind_from=45, wind_kmh=22,
                gauges=[gauge(15, 40, 1.2), gauge(22, 55, 0.8), gauge(30, 30, 0.4)],
                now=NOW, sample_fn=no_sea, landsea_fn=landsea_syn)
show("3 Approach from NE (home dry)", res)

# 4. Model says rain, no gauge wet anywhere
s = ProbeState()
seed(s, "rain_hist", [(300, 12.0)])
res = run_probe(s, home=HOME, rain_mm_h=12.0, pressure_hpa=1011, visibility_m=9000,
                wind_from=225, wind_kmh=15, gauges=[gauge(6, 210, 0.0)],
                now=NOW, sample_fn=no_sea, landsea_fn=landsea_syn)
show("4 Model-only, unconfirmed", res)

# 5. Pre-onset: dry, visibility dropping, pressure falling
s = ProbeState()
seed(s, "vis_hist", [(40*60, 9000)])
seed(s, "press_hist", [(3*3600, 1014), (0, 1010)])     # ~-1.3 hPa/hr
res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1010, visibility_m=4000,
                wind_from=225, wind_kmh=18, gauges=[], now=NOW, sample_fn=no_sea, landsea_fn=landsea_syn)
show("5 Pre-onset (vis dropping, pressure falling)", res)

# 6. Clearing: rain easing, pressure rising
s = ProbeState()
seed(s, "rain_hist", [(1500, 9.0), (900, 6.0), (300, 3.0)])
seed(s, "press_hist", [(3*3600, 1006), (0, 1010)])     # +1.3 hPa/hr
res = run_probe(s, home=HOME, rain_mm_h=2.0, pressure_hpa=1010, visibility_m=10000,
                wind_from=270, wind_kmh=15, gauges=[gauge(10, 250, 4.0)],
                now=NOW, sample_fn=no_sea, landsea_fn=landsea_syn)
show("6 Clearing", res)

# 7. Offshore movable arc: scan -> lock -> track, edge drawing inward
print("\n=== 7 Offshore arc scan->track (SW sea) ===")
s = ProbeState()
# sample_fn driven by a moving edge: rain reaches 40km at t0, 30km +15m, 20km +30m
edge_schedule = {0: 40, 1: 40, 2: 30, 3: 20}
calln = {"i": 0}
def moving_sea(pts):
    e = edge_schedule.get(calln["i"], 10)
    return [(0.8 if p["range_km"] >= e else 0.0) for p in pts]
for step in range(4):
    calln["i"] = step
    t = NOW + step * 15 * 60
    res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1009, visibility_m=9000,
                    wind_from=225, wind_kmh=24, gauges=[], now=t, sample_fn=moving_sea, landsea_fn=landsea_syn)
    arc = res["signals"]["arc"]
    spk = "; ".join(w["phrase"] for w in res["would_speak"]) or "-"
    vg_wet = [v["name"] for v in res["virtual_gauges"] if (v["mm"] or 0) > 0]
    print(f"  step {step}: mode={arc['mode']} edge={arc['edge_km']} "
          f"speed={arc['speed_kmh']} eta={arc['eta_text']} | wet_vgauges={vg_wet}")
    print(f"           SPEAK: {spk}")

# 8. Stale feed
s = ProbeState()
res = run_probe(s, home=HOME, rain_mm_h=None, pressure_hpa=None, visibility_m=None,
                wind_from=None, wind_kmh=None, gauges=[], feed_stale=True,
                now=NOW, sample_fn=no_sea, landsea_fn=landsea_syn)
show("8 Stale feed", res)

# virtual-gauge marking sample (what the grid receives)
print("\n=== virtual-gauge records (marked, modelled) — sample ===")
s = ProbeState(); s.arc_mode = "track"; s.arc_track_az = [200, 220]
res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1009, visibility_m=9000,
                wind_from=225, wind_kmh=24, gauges=[], now=NOW,
                sample_fn=lambda pts: [0.5 if p["range_km"] <= 30 else 0.0 for p in pts], landsea_fn=landsea_syn)
for v in res["virtual_gauges"][:6]:
    print(f"  {v['name']:<16} modelled={v['modelled']} mm={v['mm']} "
          f"dist={v['dist_km']}km bearing={v['bearing']:.0f}")

# 9. Forward nowcast (OWM one-minute timeline)
print("\n=== 9 Forward nowcast (minute timeline) ===")
# onset: dry now, rain begins ~10 min out
onset_series = [{"dt": int(NOW + i*60), "mm_h": (0.0 if i < 10 else 1.2)} for i in range(60)]
s = ProbeState()
res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1012, visibility_m=10000,
                wind_from=225, wind_kmh=18, gauges=[], now=NOW,
                sample_fn=no_sea, landsea_fn=landsea_syn, forward_precip=onset_series)
show("9a onset expected", res)
# intensify: raining moderate now, nowcast peaks heavy within horizon
heavier_series = [{"dt": int(NOW + i*60), "mm_h": (3.0 if i < 8 else 15.0)} for i in range(60)]
s = ProbeState(); seed(s, "rain_hist", [(300, 3.0)])
res = run_probe(s, home=HOME, rain_mm_h=3.0, pressure_hpa=1010, visibility_m=9000,
                wind_from=225, wind_kmh=18, gauges=[], now=NOW,
                sample_fn=no_sea, landsea_fn=landsea_syn, forward_precip=heavier_series)
show("9b intensification expected", res)

# 10. Cold start: two pressure samples 60s apart (the +30 hPa/h artifact) -> guarded
print("\n=== 10 Cold-start pressure guard ===")
s = ProbeState()
seed(s, "press_hist", [(60, 1010.5), (0, 1011.0)])   # 0.5 hPa over 60s = +30 hPa/h raw
res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1011.0, visibility_m=10000,
                wind_from=90, wind_kmh=26, gauges=[], now=NOW,
                sample_fn=no_sea, landsea_fn=landsea_syn)
sig = res["signals"]
print(f"  press_rate={sig['press_rate']} press_cls={sig['press_cls']} warming_up={sig['warming_up']}")
print("  SPEAK:", [w["phrase"] for w in res["would_speak"]] or "-")
assert sig["press_cls"] == "steady", "cold-start pressure not guarded!"
print("  OK: no spurious pressure alert")

# 11. Offshore data dropout while tracking -> hold state, no false retract
print("\n=== 11 Offshore dropout hold ===")
s = ProbeState(); s.arc_mode = "track"; s.arc_track_az = [200, 220]
s.arc_edge_km = 30; s.arc_lock_ts = NOW - 600; s.arc_edge_ts = NOW - 600
res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1009, visibility_m=9000,
                wind_from=225, wind_kmh=24, gauges=[], now=NOW,
                sample_fn=no_sea, landsea_fn=landsea_syn)   # no_sea = all None = dropout
arc = res["signals"]["arc"]
print(f"  mode={arc['mode']} detected={arc['detected']} dropout={arc.get('dropout')}")
assert arc["mode"] == "track" and arc.get("dropout"), "dropout did not hold track state!"
print("  OK: held track through the dropout, no false retract")

# 12. All-clear is a one-shot on active->calm; silent from a calm start; no repeat
print("\n=== 12 All-clear one-shot ===")
s = ProbeState()
r0 = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1013, visibility_m=10000,
               wind_from=225, wind_kmh=15, gauges=[], now=NOW,
               sample_fn=no_sea, landsea_fn=landsea_syn)
print("  calm start :", [w['phrase'] for w in r0['would_speak']] or "(silence)")
assert not any(w['key']=="settled" for w in r0['would_speak']), "settled fired from calm start!"
r1 = run_probe(s, home=HOME, rain_mm_h=6.0, pressure_hpa=1013, visibility_m=9000,
               wind_from=225, wind_kmh=15, gauges=[gauge(5,210,3.0)], now=NOW+600,
               sample_fn=no_sea, landsea_fn=landsea_syn)
print("  rain       :", [w['phrase'] for w in r1['would_speak']])
r2 = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1013, visibility_m=10000,
               wind_from=225, wind_kmh=15, gauges=[], now=NOW+1200,
               sample_fn=no_sea, landsea_fn=landsea_syn)
print("  clears     :", [w['phrase'] for w in r2['would_speak']])
assert any(w['key']=="settled" for w in r2['would_speak']), "settled did not fire on clear!"
r3 = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1013, visibility_m=10000,
               wind_from=225, wind_kmh=15, gauges=[], now=NOW+1800,
               sample_fn=no_sea, landsea_fn=landsea_syn)
print("  still calm :", [w['phrase'] for w in r3['would_speak']] or "(silence)")
assert not r3['would_speak'], "something repeated on continued calm!"
print("  OK: silent from calm start, all-clear once on clear, no repeat")


# 13. Land front approaching across the rings and STRENGTHENING (NE, wind NE)
print("\n=== 13 Land approach strengthening across rings ===")
s = ProbeState()
# edge draws in 30 -> 22 -> 14 km, leading intensity 2 -> 5 -> 9 mm/h, 15 min apart
land_sched = [(30, 2.0), (22, 5.0), (14, 9.0)]
spoke13 = []
for step, (dist, mm) in enumerate(land_sched):
    t = NOW + step * 15 * 60
    res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1009, visibility_m=9000,
                    wind_from=45, wind_kmh=25,
                    gauges=[gauge(dist, 45, mm), gauge(dist + 6, 55, mm * 0.6)],
                    now=t, sample_fn=no_sea, landsea_fn=landsea_syn)
    ph = "; ".join(w["phrase"] for w in res["would_speak"]) or "-"
    spoke13.append(ph)
    print(f"  step {step} (edge {dist}km, {mm}mm): {res['log'].split('| land')[-1].split('| WOULD')[0].strip()}")
    print(f"           SPEAK: {ph}")
assert any("getting stronger" in p or "building" in p for p in spoke13), \
    "strengthening front never announced as building!"
print("  OK: strengthening called out as it closed in")

# 14. Land front WEAKENING as it nears, then FIZZLING, then FADED (one-shot)
print("\n=== 14 Land approach weakening -> fizzle -> faded ===")
s = ProbeState()
weak_sched = [(30, 5.0), (22, 2.0), (16, 0.4)]
spoke14 = []
for step, (dist, mm) in enumerate(weak_sched):
    t = NOW + step * 15 * 60
    res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1011, visibility_m=10000,
                    wind_from=45, wind_kmh=25,
                    gauges=[gauge(dist, 45, mm), gauge(dist + 6, 40, mm * 0.7)],
                    now=t, sample_fn=no_sea, landsea_fn=landsea_syn)
    ph = "; ".join(w["phrase"] for w in res["would_speak"]) or "-"
    spoke14.append(ph)
    print(f"  step {step} (edge {dist}km, {mm}mm): SPEAK: {ph}")
# final cycle: nothing wet upwind any more -> the front faded before arriving
res = run_probe(s, home=HOME, rain_mm_h=0.0, pressure_hpa=1012, visibility_m=10000,
                wind_from=45, wind_kmh=25, gauges=[], now=NOW + 3 * 15 * 60,
                sample_fn=no_sea, landsea_fn=landsea_syn)
faded = [w["phrase"] for w in res["would_speak"]]
print("  step 3 (dry)          : SPEAK:", faded or "-")
assert any("fizzle" in p for p in spoke14), "fizzling shower never called out!"
assert any(w["key"] == "land_fade" for w in res["would_speak"]), \
    "faded front did not emit the one-shot all-clear!"
print("  OK: weakening -> fizzle -> faded one-shot")
