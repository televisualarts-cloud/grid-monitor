#!/usr/bin/env python3
"""build_network.py — build grid_network.json for the GB Energy Monitor wall (/wall).

Build 261023.3  (version = YYMMDD.N in UT; bump on every change to this file)
Copyright (c) 2026 Andy Smith, G7IZU. Same MIT licence as grid_server.py.

Run by hand, rarely (the network changes slowly). Stdlib only.

    python build_network.py            # downloads into ./network_src/, writes ./grid_network.json

Sources
  * gb-dispatch-model prebuilt electricity network (PyPSA-Eur soft fork), built from
    OpenStreetMap: https://zenodo.org/records/18161054 (buses, lines, transformers, links)
  * Substation names: OpenStreetMap via the Overpass API (matched on OSM element id)
  * Region per node: region_polys.json (DNO licence areas) next to this script

Output (compact JSON, read by grid_server.py and served to grid_wall.html)
  nodes   [[lon, lat, [kV...], name, ni_flag, region], ...]
  lines   [[a, b, kV, underground, km, s_nom_MVA, x_pu, [[lon,lat],...]], ...]   (x_pu on 100 MVA)
  hvdc    [{name, a, b, cap, x_pu, route}]   internal HVDC modelled as capped parallel paths
  ic      {FUELINST code: node}              interconnector landing nodes
  icroute {FUELINST code: [[lon,lat],...]}   interconnector route, GB landing first (OSM geometry)
  dist    [[kV, underground, [[lon,lat],...]], ...]   132 kV outside Scotland (DNO distribution), display only
  regions {region: share}                    approximate share of GB demand (customer counts)
  land    [{name, rings}]                    country outlines for the map background
"""
import csv, json, math, re, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "network_src"
OUT = HERE / "grid_network.json"
ZENODO = "https://zenodo.org/api/records/18161054/files/{}/content"
OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
UA = {"User-Agent": "GB-Energy-Monitor/1.0 (personal grid dashboard; network builder)"}

# Approximate share of GB electricity customers by DNO licence area (millions of
# customers). Used only to spread national transmission demand across regions.
REGION_SHARE = {
    "North Scotland": 0.78, "South and Central Scotland": 2.0, "North West England": 2.4,
    "North East England": 1.6, "Yorkshire": 2.3, "North Wales, Merseyside and Cheshire": 1.5,
    "East Midlands": 2.6, "West Midlands": 2.5, "East England": 3.6, "South Wales": 1.1,
    "South West England": 1.6, "London": 2.3, "South East England": 2.3, "Southern England": 3.0,
}
# Interconnector GB-side landing points (lon, lat), from the OSM HVDC link geometry;
# Greenlink (2025) is not in the Jan-2026 network build, so its Pembroke landing is given.
IC_LANDING = {
    "INTIFA2": (-1.194, 50.818), "INTNSL": (-1.540, 55.147), "INTNED": (0.716, 51.440),
    "INTELEC": (1.145, 51.098), "INTVKL": (-0.236, 52.921), "INTFR": (0.977, 51.105),
    "INTIRL": (-4.981, 55.070), "INTEW": (-3.050, 53.237), "INTNEM": (1.347, 51.310),
    "INTGRNL": (-4.990, 51.690),
}
# Far ends for interconnectors without an OSM route in the network build (drawn straight).
IC_FAR = {"INTGRNL": (-6.98, 52.27)}      # Greenlink: Pembroke -> Great Island (Co. Wexford)
# Internal HVDC links in service. Flows are not published, so the wall models each as a
# parallel path (reactance ~ an equivalent 400 kV double circuit of the same length) whose
# flow is capped at the link rating. Eastern Green Link 1/2 are excluded (not yet in service).
HVDC = [
    {"name": "Western HVDC", "a": (-4.895, 55.718), "b": (-3.033, 53.230), "cap": 2250, "km": 422,
     "bend": (-5.35, 54.5)},
    {"name": "Caithness–Moray HVDC", "a": (-3.082, 58.464), "b": (-2.949, 57.521), "cap": 1200, "km": 160,
     "bend": (-2.6, 58.0)},
]
X_OHM_PER_KM_400 = 0.30 / 2      # double circuit equivalent


def fetch(url, data=None, timeout=180):
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def ensure_sources():
    SRC.mkdir(exist_ok=True)
    for f in ("buses.csv", "lines.csv", "transformers.csv", "links.csv"):
        p = SRC / f
        if not p.exists() or p.stat().st_size < 1000:
            print("downloading", f, "...", flush=True)
            p.write_bytes(fetch(ZENODO.format(f)))
    p = SRC / "osm_substations.json"
    if not p.exists() or p.stat().st_size < 50000:
        q = ('[out:json][timeout:120];(nwr["power"="substation"]["voltage"~"(400000|275000|132000|220000)"]'
             '(49.8,-8.7,61.0,2.0););out center tags qt;')
        for attempt in range(6):
            for u in OVERPASS:
                try:
                    b = fetch(u, urllib.parse.urlencode({"data": q}).encode(), timeout=150)
                    if b.lstrip().startswith(b"{"):
                        p.write_bytes(b); print("substation names from", u); return
                except Exception as e:
                    print("overpass", u, e)
            time.sleep(15 * (attempt + 1))
        print("WARNING: no substation names (Overpass unavailable); nodes will be unnamed")


def rows(path):
    csv.field_size_limit(1 << 30)
    with open(path, newline="", encoding="utf-8") as fh:
        rd = csv.reader(fh); h = next(rd); gi = h.index("geometry")
        for r in rd:
            d = dict(zip(h[:gi], r[:gi])); d["geometry"] = ",".join(r[gi:]).strip("'\" ")
            yield d


def coords(g):
    m = re.search(r"\((.*)\)", g)
    return [tuple(map(float, p.split())) for p in m.group(1).split(",")] if m else []


def simplify(pts, eps):
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1; L = math.hypot(dx, dy) or 1e-12
    dm, im = 0.0, 0
    for i in range(1, len(pts) - 1):
        x, y = pts[i]; d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / L
        if d > dm: dm, im = d, i
    if dm > eps:
        return simplify(pts[:im + 1], eps)[:-1] + simplify(pts[im:], eps)
    return [pts[0], pts[-1]]


def pip(x, y, rings):
    ins = False
    for ring in rings:
        j = len(ring) - 1
        for i in range(len(ring)):
            xi, yi = ring[i]; xj, yj = ring[j]
            if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
                ins = not ins
            j = i
    return ins


def km(ax, ay, bx, by):
    return math.hypot((ax - bx) * math.cos(math.radians((ay + by) / 2)) * 111.32, (ay - by) * 110.57)


def main():
    ensure_sources()
    buses = {b["bus_id"]: b for b in rows(SRC / "buses.csv") if b["country"] == "GB"}
    is_ni = lambda x, y: x < -5.35 and 54.0 < y < 55.35

    def keep(b):
        v = int(float(b["voltage"])); x, y = float(b["x"]), float(b["y"])
        return v >= 200 or (v >= 132 and y >= 55.05 and not is_ni(x, y))   # 132 kV is transmission only in Scotland
    B = {k: b for k, b in buses.items() if keep(b)}
    base = lambda bid: re.sub(r"^GB_", "", bid).rsplit("-", 1)[0] if re.search(r"-\d+$", bid) else bid
    par = {}

    def f(a):
        par.setdefault(a, a)
        while par[a] != a:
            par[a] = par[par[a]]; a = par[a]
        return a

    def u(a, b): par[f(a)] = f(b)
    for k in B: u(k, base(k))
    for t in rows(SRC / "transformers.csv"):
        if t["bus0"] in B and t["bus1"] in B: u(t["bus0"], t["bus1"])       # one node per site
    lines = []
    for l in rows(SRC / "lines.csv"):
        if l["bus0"] in B and l["bus1"] in B and f(l["bus0"]) != f(l["bus1"]):
            v = int(float(l["voltage"]))
            if v < 132 or l.get("under_construction") == "t":
                continue
            xpu = max(float(l["x"]), 0.01) / (v * v / 100.0)
            g = simplify(coords(l["geometry"]), 0.004)
            lines.append([f(l["bus0"]), f(l["bus1"]), v, int(l["underground"] == "t"),
                          round(float(l["length"]) / 1000, 1), round(float(l["s_nom"])), round(xpu, 6),
                          [[round(p[0], 4), round(p[1], 4)] for p in g]])
    # 132 kV outside Scotland (distribution, run by the DNOs): drawn for detail only, not in the flow model
    dist = []
    for l in rows(SRC / "lines.csv"):
        b0, b1 = buses.get(l["bus0"]), buses.get(l["bus1"])
        if not b0 or not b1 or l.get("under_construction") == "t":
            continue
        v = int(float(l["voltage"]))
        if v != 132 or keep(b0) or keep(b1):
            continue
        g = simplify(coords(l["geometry"]), 0.003)
        if len(g) >= 2:
            dist.append([v, int(l["underground"] == "t"), [[round(p[0], 4), round(p[1], 4)] for p in g]])
    # names
    names = {}
    sp = SRC / "osm_substations.json"
    if sp.exists():
        for e in json.loads(sp.read_text(encoding="utf-8")).get("elements", []):
            t = e.get("tags", {}); n = t.get("name") or t.get("ref") or ""
            if n: names[f"{e['type']}/{e['id']}"] = n
    grp = defaultdict(list)
    for k in B: grp[f(k)].append(k)
    # connected components: keep the main GB island and Northern Ireland
    adj = defaultdict(set)
    for l in lines: adj[l[0]].add(l[1]); adj[l[1]].add(l[0])
    seen, comps = {}, []
    for n in grp:
        if n in seen or n not in adj: continue
        st, c = [n], []; seen[n] = len(comps)
        while st:
            a = st.pop(); c.append(a)
            for b in adj[a]:
                if b not in seen: seen[b] = len(comps); st.append(b)
        comps.append(c)
    comps.sort(key=len, reverse=True)
    main_c = set(comps[0])
    ni_c = next((set(c) for c in comps[1:] if any(is_ni(float(B[grp[n][0]]["x"]), float(B[grp[n][0]]["y"])) for n in c)), set())
    keepn = [n for n in grp if n in main_c or n in ni_c]
    regions = json.loads((HERE / "region_polys.json").read_text(encoding="utf-8"))["areas"]
    idx, nodes = {}, []
    for r in keepn:
        ks = grp[r]
        x = sum(float(B[k]["x"]) for k in ks) / len(ks); y = sum(float(B[k]["y"]) for k in ks) / len(ks)
        vs = sorted({int(float(B[k]["voltage"])) for k in ks}, reverse=True)
        nm = next((names.get(base(k)) or names.get(re.sub(r"^GB_", "", k)) for k in ks
                   if names.get(base(k)) or names.get(re.sub(r"^GB_", "", k))), "")
        reg = ""
        offshore_only = set(vs) <= {220, 150, 525, 320}
        if not offshore_only and r in main_c:
            reg = next((a["name"] for a in regions if a["name"] in REGION_SHARE and pip(x, y, a["rings"])), "")
        idx[r] = len(nodes)
        nodes.append([round(x, 4), round(y, 4), vs, nm, 1 if r in ni_c else 0, reg])
    L = [[idx[a], idx[b]] + rest for a, b, *rest in lines if a in idx and b in idx]

    def nearest(x, y, main_only=True):
        best = None
        for i, n in enumerate(nodes):
            if main_only and n[4]: continue
            d = km(x, y, n[0], n[1])
            if best is None or d < best[0]: best = (d, i)
        return best
    ic = {}
    for code, (x, y) in IC_LANDING.items():
        d, i = nearest(x, y); ic[code] = i
        print(f"  {code:8s} -> {nodes[i][3] or '(unnamed)'} ({d:.1f} km)")
    # interconnector routes (subsea/tunnel geometry from the OSM HVDC links), ordered GB landing -> abroad
    icroute = {}
    links = list(rows(SRC / "links.csv"))
    for code, (x, y) in IC_LANDING.items():
        best = None
        for k in links:
            c = coords(k["geometry"])
            if len(c) < 2:
                continue
            for end, far in ((c[0], c[-1]), (c[-1], c[0])):
                d = km(x, y, end[0], end[1])
                if d < 5 and km(far[0], far[1], end[0], end[1]) > 20 and (best is None or d < best[0]):
                    best = (d, c if end is c[0] else c[::-1])
        if best:
            icroute[code] = [[round(p[0], 4), round(p[1], 4)] for p in simplify(best[1], 0.01)]
        elif code in IC_FAR:
            icroute[code] = [[x, y], list(IC_FAR[code])]
        print(f"  route {code:8s} {'OSM' if best else ('straight' if code in IC_FAR else 'none')}")
    hv = []
    for h in HVDC:
        da, a = nearest(*h["a"]); db, b = nearest(*h["b"])
        xpu = X_OHM_PER_KM_400 * h["km"] / (400 * 400 / 100.0)
        hv.append({"name": h["name"], "a": a, "b": b, "cap": h["cap"], "x_pu": round(xpu, 6),
                   "route": [list(h["a"]), list(h["bend"]), list(h["b"])]})
        print(f"  {h['name']}: {nodes[a][3]} -> {nodes[b][3]} ({da:.1f}/{db:.1f} km)")
    land = [{"name": c["name"], "rings": c["rings"]}
            for c in json.loads((HERE / "region_polys.json").read_text(encoding="utf-8"))["countries"]]
    out = {"built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "source": "gb-dispatch-model prebuilt network (OpenStreetMap), zenodo.org/records/18161054; names: OpenStreetMap",
           "nodes": nodes, "lines": L, "hvdc": hv, "ic": ic, "icroute": icroute, "dist": dist, "regions": REGION_SHARE, "land": land}
    OUT.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    named = sum(1 for n in nodes if n[3])
    print(f"wrote {OUT.name}: {len(nodes)} nodes ({named} named, {sum(n[4] for n in nodes)} NI), "
          f"{len(L)} circuits, {len(dist)} distribution 132 kV lines, {OUT.stat().st_size // 1024} KB")


if __name__ == "__main__":
    sys.exit(main())
