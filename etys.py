"""
GB Energy Monitor - transmission circuit ratings from NESO's Electricity Ten Year Statement (ETYS)
Build 261029.2  (version = YYMMDD.N in UT; bump on every change to this file)
Change history: CHANGELOG.md

ETYS Appendix B ("System technical data", an .xlsx published each year) lists every transmission circuit
(SHE Transmission, SP Transmission, National Grid Electricity Transmission: sheets B-2-1a/b/c) with its winter,
spring, summer and autumn ratings in MVA, and an index of the substation codes its node names use (B-1-1a-d).
This module finds the newest Appendix B on NESO's ETYS page (checked monthly; downloaded only when a newer one
appears, about 1.2 MB), reads it with the standard library (an .xlsx is zipped XML), and keeps the circuits and
site names in etys_ratings.json (git-ignored). match(net, named) then gives each network line its ETYS ratings:

- an ETYS node name is a 4-letter site code, a voltage digit (4 = 400 kV, 2 = 275 kV, 1 = 132 kV) and a suffix;
  the site's name (from the index) is matched to a network node by name (as the operators' GSPs are);
- circuits between two sites that match two different network nodes are matched to the network line(s) between
  those nodes at that voltage; each line takes the mean ETYS rating per circuit for that pair times its own number
  of circuits (one or two, from its standard rating);
- a site no name matches (often a tee) takes the one unmatched network node whose neighbours are exactly the
  site's neighbours; failing that it is passed through: its two sides become one circuit with the
  lower rating of the two, if the network has a line there;
- where ETYS links two matched nodes and the network has no direct line but one intermediate node with exactly
  those two neighbours (an unnamed tee, or a substation ETYS routes differently), both sections take the rating;
- busbar sections and couplers within one site are ignored, as are entries whose rating is outside a plausible
  range for the voltage (PLAUSIBLE).
Unmatched lines keep the network file's standard rating; the coverage is reported.

boundaries() gives NESO's ETYS study boundaries (shapefile, see below) for drawing on the map.
"""
import io
import json
import re
import threading
import time
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = HERE / "etys_ratings.json"
PAGE = "https://www.neso.energy/publications/electricity-ten-year-statement-etys/etys-documents-and-appendices"
FALLBACK = ("ETYS 2025", "https://www.neso.energy/document/383936/download")
CHECK_TTL = 30 * 86400
UA = {"User-Agent": "Mozilla/5.0 (GB Energy Monitor)"}
SHEETS = {"B-2-1a": "SHE Transmission", "B-2-1b": "SP Transmission", "B-2-1c": "NGET"}
INDEX = ("B-1-1a", "B-1-1b", "B-1-1c", "B-1-1d")
KV = {"4": 400, "2": 275, "1": 132}
STD_CCT = {400: 1787, 275: 922, 132: 147}   # the network file's standard rating per circuit
PLAUSIBLE = {400: (500, 4500), 275: (300, 2500), 132: (40, 500)}   # MVA per circuit; outside this the ETYS entry
                                             # is taken as mismatched (e.g. a line built for 275 kV run at 132 kV)
SEASONS = ("winter", "spring", "summer", "autumn")
_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_RNS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_st = {"data": None, "loaded": False, "busy": False, "error": None, "next": 0.0}
_lock = threading.Lock()


def season(month):
    """ETYS season of a calendar month: winter Dec-Feb, spring Mar-Apr, summer May-Aug, autumn Sep-Nov."""
    return 0 if month in (12, 1, 2) else 1 if month in (3, 4) else 2 if month <= 8 else 3


# ---------------------------------------------------------------- reading the workbook
def _sheets(blob, wanted):
    """{sheet name: [[cell values...] rows]} for the wanted sheets of an .xlsx (values: str or float)."""
    z = zipfile.ZipFile(io.BytesIO(blob))
    ss = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(_NS + "si"):
            ss.append("".join(t.text or "" for t in si.iter(_NS + "t")))
    rels = {r.get("Id"): r.get("Target") for r in ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))}
    out = {}
    for s in ET.fromstring(z.read("xl/workbook.xml")).iter(_NS + "sheet"):
        name = s.get("name")
        if name not in wanted:
            continue
        tgt = rels.get(s.get(_RNS + "id"), "")
        path = "xl/" + tgt.lstrip("/").replace("xl/", "", 1) if not tgt.startswith("/xl/") else tgt.lstrip("/")
        rows = []
        for row in ET.fromstring(z.read(path)).iter(_NS + "row"):
            r = {}
            for c in row.iter(_NS + "c"):
                ref = re.match(r"[A-Z]+", c.get("r") or "")
                col = 0
                for ch in (ref.group(0) if ref else "A"):
                    col = col * 26 + ord(ch) - 64
                t, v = c.get("t"), c.find(_NS + "v")
                if t == "s" and v is not None:
                    val = ss[int(v.text)]
                elif t == "inlineStr":
                    val = "".join(x.text or "" for x in c.iter(_NS + "t"))
                elif v is not None and v.text is not None:
                    try:
                        val = float(v.text)
                    except ValueError:
                        val = v.text
                else:
                    val = None
                r[col - 1] = val
            if r:
                rows.append([r.get(i) for i in range(max(r) + 1)])
        out[name] = rows
    return out


def parse(blob, title):
    """{"title", "sites": {code: name}, "circuits": [[owner, node1, node2, w, sp, su, au], ...]}."""
    sh = _sheets(blob, set(SHEETS) | set(INDEX))
    sites = {}
    for sn in INDEX:
        for r in sh.get(sn, [])[2:]:
            if len(r) >= 2 and isinstance(r[0], str) and isinstance(r[1], str) and r[0].strip():
                sites.setdefault(r[0].strip(), r[1].strip())
    circ = []
    for sn, owner in SHEETS.items():
        for r in sh.get(sn, [])[2:]:
            if len(r) < 12 or not isinstance(r[0], str) or not isinstance(r[1], str):
                continue
            rt = r[8:12]
            if all(isinstance(x, (int, float)) for x in rt):
                circ.append([owner, r[0].strip(), r[1].strip()] + [round(float(x)) for x in rt])
    if len(circ) < 500 or len(sites) < 500:
        raise ValueError(f"unexpected workbook ({len(circ)} circuits, {len(sites)} sites)")
    return {"title": title, "sites": sites, "circuits": circ, "at": time.time()}


# ---------------------------------------------------------------- finding and fetching the newest Appendix B
def _get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read()


def _newest():
    """(title, url) of the newest Appendix B listed on NESO's ETYS page, or the known 2025 one."""
    try:
        h = _get(PAGE, timeout=40).decode("utf-8", "replace")
        for m in re.finditer(r'href="([^"]*/document/\d+/download)"[^>]*>(.*?)</a>', h, re.S):
            t = " ".join(re.sub(r"<[^>]+>", " ", m.group(2)).split())
            if re.search(r"appendix\s*b\b", t, re.I) and re.search(r"(20\d\d|ETYS\s*\d\d)", t):
                u = m.group(1)
                return t, (u if u.startswith("http") else "https://www.neso.energy" + u)
    except Exception:
        pass
    return FALLBACK


def _worker():
    try:
        title, url = _newest()
        cur = _st["data"]
        if not cur or cur.get("url") != url:
            d = parse(_get(url, timeout=90), title)
            d["url"] = url
            CACHE.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
            with _lock:
                _st["data"] = d
        with _lock:
            _st.update(error=None, next=time.time() + CHECK_TTL)
    except Exception as e:
        with _lock:
            _st.update(error=f"{type(e).__name__}: {str(e)[:140]}", next=time.time() + 3600)
    finally:
        with _lock:
            _st["busy"] = False


def data():
    """The parsed Appendix B (see parse) or None; checks for a newer one in the background when due."""
    with _lock:
        if not _st["loaded"]:
            _st["loaded"] = True
            try:
                _st["data"] = json.loads(CACHE.read_text(encoding="utf-8"))
                _st["next"] = _st["data"].get("at", 0) + CHECK_TTL
            except Exception:
                _st["data"] = None
        if time.time() >= _st["next"] and not _st["busy"]:
            _st["busy"] = True
            _st["next"] = time.time() + 3600
            threading.Thread(target=_worker, name="etys", daemon=True).start()
        return _st["data"]


def status():
    d = _st["data"]
    return {"ok": bool(d), "title": d.get("title") if d else None, "circuits": len(d["circuits"]) if d else 0,
            "error": _st["error"]}


# ---------------------------------------------------------------- matching to the network
def _key(name):
    s = re.sub(r"\(.*?\)", " ", (name or "").lower()).replace("'", "").replace("’", "")
    s = re.sub(r"\d+\s*kv|\d+", " ", s)
    skip = {"kv", "s", "substation", "sub", "station", "grid", "supply", "point", "gsp", "power", "transmission",
            "converter", "switching", "the", "main"}
    return " ".join(w for w in re.findall(r"[a-z]+", s) if w not in skip)


def _same(a, b):
    if a == b or a.startswith(b + " ") or b.startswith(a + " "):
        return True
    s, t = sorted((a, b), key=len)
    return len(s) >= 5 and t.startswith(s) and len(t) - len(s) <= 2


def _vclass(v):
    return 400 if v >= 380 else 275 if v >= 260 else 132 if 100 <= v < 200 else None


def match(net, named, alt=None):
    """({line index: [winter, spring, summer, autumn] MVA}, stats). named = [(node index, name, lon, lat)];
    alt = [(node index, name)] second-choice names for unnamed nodes (a nearby substation or place), used for a
    site that matches no named node, and only when exactly one such node matches."""
    d = data()
    if not d:
        return {}, {"ok": False, "error": _st["error"] or "not loaded yet"}
    nk = [(i, _key(nm)) for i, nm, _, _ in named if nm]
    nkd = dict(nk)
    ak = [(i, _key(nm)) for i, nm in (alt or []) if nm]
    used_alt = set()
    memo = {}

    def node_of(site):
        if site not in memo:
            nm = d["sites"].get(site)
            k = _key(nm) if nm else ""
            c = [i for i, x in nk if x and k and _same(x, k)]
            if not c and k:
                c2 = {i for i, x in ak if x and _same(x, k)}
                c = list(c2) if len(c2) == 1 else []
                if c:
                    used_alt.add(site)
            memo[site] = c[0] if len(c) == 1 else (min(c, key=lambda i: len(nkd.get(i, ""))) if c else None)
        return memo[site]

    # ETYS circuits as (site, kV) edges; same-site edges (busbars, couplers) dropped
    edges, bad = [], []
    for owner, a, b, *r in d["circuits"]:
        ka, kb = KV.get(a[4:5]), KV.get(b[4:5])
        sa, sb = a[:4], b[:4]
        if ka is None or ka != kb or sa == sb:
            continue
        lo, hi = PLAUSIBLE[ka]
        if not lo <= r[0] <= hi:
            bad.append((a, b, r[0]))
            continue
        edges.append((sa, sb, ka, r))
    # network lines by (node pair, voltage class), and each node's neighbours at each voltage
    lines = {}
    for li, l in enumerate(net["lines"]):
        vc = _vclass(l[2])
        if vc:
            lines.setdefault((frozenset((l[0], l[1])), vc), []).append(li)
    nbr = {}
    for (pair, vc), lis in lines.items():
        if len(pair) == 2:
            a, b = tuple(pair)
            nbr.setdefault((a, vc), set()).add(b)
            nbr.setdefault((b, vc), set()).add(a)
    adj = {}
    for sa, sb, kv, r in edges:
        adj.setdefault((sa, kv), []).append((sb, r))
        adj.setdefault((sb, kv), []).append((sa, r))
    # a site no name matches (a tee such as Fenwick Tee, or a substation the map names differently) takes the one
    # network node that no site matches and whose neighbours at that voltage are exactly the site's neighbours
    used = {node_of(s_) for (s_, kv) in adj} - {None}
    topo = 0
    for (t, kv), nb in adj.items():
        if node_of(t) is not None:
            continue
        S = {node_of(x) for x, _ in nb}
        if None in S or len(S) < 2:
            continue
        c = [x for (x, vc), ns in nbr.items() if vc == kv and x not in used and ns == S]
        if len(c) == 1:
            memo[t] = c[0]
            used.add(c[0])
            topo += 1
    # pass through sites that still match no node (tees, unmapped substations): A - T - B becomes A - B
    direct = {(frozenset((sa, sb)), kv) for sa, sb, kv, _ in edges}
    virt = []
    for (t, kv), nb in adj.items():
        if node_of(t) is not None or len(nb) < 2:
            continue
        if len(nb) != 2 and "tee" not in (d["sites"].get(t) or "").lower():
            continue                                      # only real tees and two-ended pass-through sites
        for i in range(len(nb)):
            for j in range(i + 1, len(nb)):
                (a, ra), (b, rb) = nb[i], nb[j]
                if a != b and (frozenset((a, b)), kv) not in direct and node_of(a) is not None and node_of(b) is not None:
                    virt.append((a, b, kv, [min(x, y) for x, y in zip(ra, rb)]))
    groups, why = {}, {"matched": 0, "site not matched": 0, "same node": 0, "no network line": 0, "via a tee": 0}
    unres = []
    for src, lst in (("d", edges), ("v", virt)):
        for sa, sb, kv, r in lst:
            na, nb = node_of(sa), node_of(sb)
            if na is None or nb is None:
                why["site not matched"] += src == "d"
                continue
            if na == nb:
                why["same node"] += src == "d"
                continue
            key = (frozenset((na, nb)), kv)
            if key not in lines:
                why["no network line"] += src == "d"
                unres.append((na, nb, kv, r))
                continue
            g = groups.setdefault(key, [[0, 0, 0, 0], 0])
            g[0] = [x + y for x, y in zip(g[0], r)]
            g[1] += 1
            why["matched" if src == "d" else "via a tee"] += 1
    # ETYS links two matched nodes but the network goes through an intermediate node (an unnamed tee, or a
    # substation ETYS routes differently): if one node has exactly those two neighbours at that voltage, both
    # of its sections take the ETYS rating (unless already matched directly)
    direct_keys = set(groups)
    why["via an intermediate network node"] = 0
    for na, nb, kv, r in unres:
        for x in (nbr.get((na, kv), set()) & nbr.get((nb, kv), set())):
            if nbr.get((x, kv)) != {na, nb}:
                continue
            for seg in ((na, x), (x, nb)):
                key = (frozenset(seg), kv)
                if key in direct_keys:
                    continue
                g = groups.setdefault(key, [[0, 0, 0, 0], 0])
                g[0] = [p + q for p, q in zip(g[0], r)]
                g[1] += 1
            why["via an intermediate network node"] += 1
            break
    # each network line: the mean ETYS rating per circuit between the pair, times the line's own number of
    # circuits (from its standard rating: one or two). ETYS often lists more entries for a pair than the network
    # has circuits (sections, tee passes), so summing them would overstate the line.
    out = {}
    for key, (tot, n) in groups.items():
        per = [t / n for t in tot]
        for li in lines[key]:
            l = net["lines"][li]
            nc = max(1, round(float(l[5] or 0) / STD_CCT.get(_vclass(l[2]), 1e9)))
            out[li] = [round(x * nc) for x in per]
    ni = {i for i, n in enumerate(net["nodes"]) if n[4]}
    n_tx = sum(1 for l in net["lines"] if _vclass(l[2]) in (400, 275) and l[0] not in ni and l[1] not in ni)
    n_cov = sum(1 for li in out if _vclass(net["lines"][li][2]) in (400, 275))
    why["outside the plausible range"] = len(bad)
    why["sites matched by a nearby name"] = len(used_alt)
    why["sites matched by their connections"] = topo
    return out, {"ok": True, "title": d.get("title"), "circuits": len(d["circuits"]), "etys": why,
                 "lines_rated": len(out), "lines_400_275": n_tx, "lines_400_275_rated": n_cov}


# ---------------------------------------------------------------- ETYS boundaries (map lines)
# NESO publishes the ETYS study boundaries (B0-B17, EC, NW, SC, SW, LE) as a shapefile on its data portal
# (Web Mercator polylines). Fetched monthly, read with the standard library, converted to lon/lat, simplified
# to about 300 m and kept in etys_boundaries.json (git-ignored).
BND_PKG = "https://api.neso.energy/api/3/action/package_show?id=etys-gb-transmission-system-boundaries"
BND_CACHE = HERE / "etys_boundaries.json"
BND_VER = 2                   # bump to re-read the cached file after a change to the reading
_bst = {"data": None, "loaded": False, "busy": False, "error": None, "next": 0.0}


def _merc(x, y):
    import math
    return x / 6378137.0 * 180 / math.pi, (2 * math.atan(math.exp(y / 6378137.0)) - math.pi / 2) * 180 / math.pi


def _simplify(pts, eps):
    if len(pts) < 3:
        return pts
    (x1, y1), (x2, y2) = pts[0], pts[-1]
    dx, dy = x2 - x1, y2 - y1
    L = (dx * dx + dy * dy) ** 0.5 or 1e-12
    dm, im = 0.0, 0
    for i in range(1, len(pts) - 1):
        x, y = pts[i]
        d = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / L
        if d > dm:
            dm, im = d, i
    if dm > eps:
        return _simplify(pts[:im + 1], eps)[:-1] + _simplify(pts[im:], eps)
    return [pts[0], pts[-1]]


def parse_boundaries(blob):
    """[[name, [[[lon, lat], ...] part], ...]] from the zipped shapefile."""
    import struct
    z = zipfile.ZipFile(io.BytesIO(blob))
    shp = next(n for n in z.namelist() if n.lower().endswith(".shp"))
    dbf = next(n for n in z.namelist() if n.lower().endswith(".dbf"))
    b = z.read(dbf)
    nrec, hl, rl = struct.unpack("<I", b[4:8])[0], struct.unpack("<H", b[8:10])[0], struct.unpack("<H", b[10:12])[0]
    flds, o = [], 32
    while b[o] != 0x0D:
        flds.append((b[o:o + 11].split(b"\0")[0].decode("latin1"), b[o + 16]))
        o += 32
    names = []
    for r in range(nrec):
        rec, pos, vals = b[hl + r * rl: hl + (r + 1) * rl], 1, {}
        for f, l in flds:
            vals[f] = rec[pos:pos + l].decode("latin1").strip()
            pos += l
        names.append(vals.get("Boundary_n") or vals.get("ID") or f"#{r}")
    s = z.read(shp)
    out, p, i = [], 100, 0
    while p + 8 <= len(s) and i < len(names):
        clen = struct.unpack(">I", s[p + 4:p + 8])[0] * 2
        c = s[p + 8:p + 8 + clen]
        p += 8 + clen
        st = struct.unpack("<i", c[0:4])[0]
        if st in (3, 13, 23):                           # polyline (with Z / M variants)
            np_, nq = struct.unpack("<ii", c[36:44])
            parts = list(struct.unpack(f"<{np_}i", c[44:44 + 4 * np_])) + [nq]
            base = 44 + 4 * np_
            pts = [struct.unpack("<dd", c[base + 16 * k: base + 16 * k + 16]) for k in range(nq)]
            segs = []
            for a, e in zip(parts[:-1], parts[1:]):
                ll = [_merc(x, y) for x, y in pts[a:e]]
                if len(ll) > 3 and abs(ll[0][0] - ll[-1][0]) + abs(ll[0][1] - ll[-1][1]) < 1e-6:
                    # a closed loop (the London boundaries): simplify the two halves either side of the far point
                    far = max(range(len(ll)), key=lambda k: (ll[k][0] - ll[0][0]) ** 2 + (ll[k][1] - ll[0][1]) ** 2)
                    sm = _simplify(ll[:far + 1], 0.003)[:-1] + _simplify(ll[far:], 0.003)
                else:
                    sm = _simplify(ll, 0.003)
                segs.append([[round(x, 4), round(y, 4)] for x, y in sm])
            out.append([names[i], segs])
        i += 1
    if len(out) < 10:
        raise ValueError(f"unexpected boundary file ({len(out)} lines)")
    return out


def _bnd_worker():
    try:
        p = json.loads(_get(BND_PKG, timeout=40))["result"]
        url = next(r["url"] for r in p["resources"] if (r.get("url") or "").lower().endswith(".zip"))
        cur = _bst["data"]
        if not cur or cur.get("url") != url or cur.get("v") != BND_VER:
            d = {"url": url, "v": BND_VER, "title": p["resources"][0].get("name"), "at": time.time(),
                 "lines": parse_boundaries(_get(url, timeout=90))}
            BND_CACHE.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
            with _lock:
                _bst["data"] = d
        with _lock:
            _bst.update(error=None, next=time.time() + CHECK_TTL)
    except Exception as e:
        with _lock:
            _bst.update(error=f"{type(e).__name__}: {str(e)[:140]}", next=time.time() + 3600)
    finally:
        with _lock:
            _bst["busy"] = False


def boundaries():
    """{"title", "lines": [[name, [[[lon, lat], ...]]], ...]} or None; refreshed monthly in the background."""
    with _lock:
        if not _bst["loaded"]:
            _bst["loaded"] = True
            try:
                _bst["data"] = json.loads(BND_CACHE.read_text(encoding="utf-8"))
                _bst["next"] = _bst["data"].get("at", 0) + CHECK_TTL if _bst["data"].get("v") == BND_VER else 0
            except Exception:
                _bst["data"] = None
        if time.time() >= _bst["next"] and not _bst["busy"]:
            _bst["busy"] = True
            _bst["next"] = time.time() + 3600
            threading.Thread(target=_bnd_worker, name="etys-bnd", daemon=True).start()
        return _bst["data"]
