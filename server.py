"""
InfraShield — Nagpur Infrastructure Risk & Cascade Analysis
Pipeline + FastAPI server. Run: python server.py
"""

import math, json, time
from pathlib import Path
from typing import Optional

import requests as http
import numpy as np
import networkx as nx
from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, FileResponse

WPOP_PATH = Path(__file__).parent / "data" / "worldpop_ind_2020.tif"
_wpop = None  # lazy-loaded population raster

def load_population(tif=WPOP_PATH):
    """Lazy-load WorldPop India GeoTIFF → returns a sampler f(lat,lon)->pop.
    Falls back to all-zeros if the raster is missing so the server still runs."""
    global _wpop
    if _wpop is not None:
        return _wpop
    if not tif.exists():
        print("   ⚠ WorldPop raster missing; population impact unavailable")
        _wpop = lambda lat, lon: 0.0
        return _wpop
    import rasterio
    print("   Loading WorldPop raster...")
    try:
        ds = rasterio.open(tif)
        band = ds.read(1)
        nodata = ds.nodata if ds.nodata is not None else -99999
        af = ds.transform
    except Exception as e:
        print(f"   ⚠ WorldPop raster unreadable ({str(e)[:60]}); population impact unavailable")
        _wpop = lambda lat, lon: 0.0
        return _wpop

    def sample(lat, lon):
        try:
            px, py = ~af * (lon, lat)
            r, c = int(py), int(px)
            if 0 <= r < band.shape[0] and 0 <= c < band.shape[1]:
                v = band[r, c]
                return float(v) if v is not None and v != nodata and not np.isnan(v) else 0.0
            return 0.0
        except Exception:
            return 0.0
    _wpop = sample
    print("   WorldPop raster loaded")
    return _wpop

# ── Config ───────────────────────────────────────────────────────────────────
NAGPUR_BBOX = (21.140, 79.080, 21.155, 79.100)  # south, west, north, east
BBOX_STR = ",".join(str(c) for c in NAGPUR_BBOX)

# ARS weights (from report)
W_H, W_V, W_C = 0.35, 0.30, 0.35

# ── Pipeline: Data Fetch ─────────────────────────────────────────────────────

def fetch_osm():
    """Fetch roads, bridges, hospitals from Overpass, with mirror fallback."""
    q = f"""
    [out:json][timeout:90];
    (
      way["highway"~"primary|secondary|tertiary|residential"]({BBOX_STR});
      way["bridge"="yes"]({BBOX_STR});
      node["amenity"~"hospital|fire_station"]({BBOX_STR});
      way["amenity"~"hospital|fire_station"]({BBOX_STR});
    );
    out body;
    >;
    out skel qt;
    """
    mirrors = [
        "https://overpass.private.coffee/api/interpreter",
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]
    last_err = None
    for base in mirrors:
        try:
            r = http.post(
                base,
                data={"data": q},
                headers={"User-Agent": "InfraShield/1.0 (mailto:test@example.com)"},
                timeout=180,
            )
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_err = e
            print(f"   Overpass mirror {base} failed: {str(e)[:70]}")
    raise RuntimeError(f"All Overpass mirrors failed: {last_err}")


def fetch_hazard(bbox=BBOX_STR, n_pts=3):
    """Fetch recent rainfall from Open-Meteo."""
    south, west, north, east = NAGPUR_BBOX
    lats = [south + (north - south) * i / max(n_pts - 1, 1) for i in range(n_pts)]
    lons = [west + (east - west) * i / max(n_pts - 1, 1) for i in range(n_pts)]

    url = (
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={','.join(f'{l:.4f}' for l in lats)}"
        f"&longitude={','.join(f'{l:.4f}' for l in lons)}"
        "&hourly=precipitation,temperature_2m"
        "&past_days=7&forecast_days=0"
        "&timezone=Asia/Kolkata"
    )
    r = http.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()

    results = []
    items = data if isinstance(data, list) else [data]
    for d in items:
        precip = d.get("hourly", {}).get("precipitation", [])
        results.append({
            "lat": d["latitude"],
            "lon": d["longitude"],
            "max_rain_mm": max(precip) if precip else 0,
        })
    return results


def fetch_elevation_grid(n=5):
    """Open-Meteo elevation over an n×n grid across the Nagpur bbox.
    Returns {(i,j): elev_m}. One batched request."""
    s, w, north, e = NAGPUR_BBOX
    lats = [s + (north - s) * i / (n - 1) for i in range(n)]
    lons = [w + (e - w) * j / (n - 1) for j in range(n)]
    flat_lats, flat_lons = [], []
    for i in range(n):
        for j in range(n):
            flat_lats.append(lats[i]); flat_lons.append(lons[j])
    url = ("https://api.open-meteo.com/v1/elevation?"
           f"latitude={','.join(f'{l:.4f}' for l in flat_lats)}"
           f"&longitude={','.join(f'{l:.4f}' for l in flat_lons)}")
    r = http.get(url, timeout=30)
    r.raise_for_status()
    elevs = r.json().get("elevation", [])
    # guard against partial/disabled returns
    if len(elevs) != n * n:
        return None
    grid = {}
    for i in range(n):
        for j in range(n):
            grid[(i, j)] = elevs[i * n + j]
    return grid


def bilinear_elevation(grid, lat, lon, n):
    """Interpolate elevation at (lat,lon) from the n×n grid. Returns meters."""
    s, w, north, e = NAGPUR_BBOX
    # fractional grid coords
    fi = (lat - s) / (north - s) * (n - 1)
    fj = (lon - w) / (e - w) * (n - 1)
    i0, j0 = int(fi), int(fj)
    i1, j1 = min(i0 + 1, n - 1), min(j0 + 1, n - 1)
    # clamp
    i0, j0 = max(0, i0), max(0, j0)
    ti, tj = fi - i0, fj - j0
    v00 = grid[(i0, j0)]; v10 = grid[(i1, j0)]
    v01 = grid[(i0, j1)]; v11 = grid[(i1, j1)]
    return (v00 * (1 - ti) * (1 - tj) + v10 * ti * (1 - tj) +
            v01 * (1 - ti) * tj + v11 * ti * tj)


# ── Pipeline: Graph Build ─────────────────────────────────────────────────────

def haversine(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def build_graph(osm):
    """Parse Overpass JSON → NetworkX graph."""
    # Node lookup: id → (lat, lon)
    node_coords = {}
    for el in osm.get("elements", []):
        if el["type"] == "node":
            node_coords[el["id"]] = (el["lat"], el["lon"])

    G = nx.Graph()

    # Add all nodes
    for nid, (lat, lon) in node_coords.items():
        G.add_node(nid, lat=lat, lon=lon, node_type="junction")

    # Mark facility nodes from node-tagged amenities
    for el in osm.get("elements", []):
        if el["type"] == "node":
            tags = el.get("tags", {})
            amenity = tags.get("amenity")
            if amenity and el["id"] in G:
                G.nodes[el["id"]]["node_type"] = amenity
                G.nodes[el["id"]]["name"] = tags.get("name", "Unknown")

    # Process ways → edges
    for el in osm.get("elements", []):
        if el["type"] != "way":
            continue
        tags = el.get("tags", {})
        nids = el.get("nodes", [])

        # Determine edge type
        if tags.get("bridge") == "yes":
            etype = "bridge"
        elif tags.get("amenity") in ("hospital", "fire_station"):
            etype = tags["amenity"]
        else:
            etype = "road"

        # Mark facility way nodes
        if tags.get("amenity") in ("hospital", "fire_station"):
            for nid in nids:
                if nid in G:
                    G.nodes[nid]["node_type"] = tags["amenity"]
                    G.nodes[nid]["name"] = tags.get("name", "Unknown")

        # Create edges between consecutive nodes
        for i in range(len(nids) - 1):
            a, b = nids[i], nids[i + 1]
            if a not in node_coords or b not in node_coords:
                continue
            la, loa = node_coords[a]
            lb, lob = node_coords[b]
            length = haversine(la, loa, lb, lob)
            speed = 30 if etype == "road" else 20  # km/h estimate
            if etype == "bridge":
                speed = 25

            G.add_edge(
                a, b,
                length=length,
                travel_time=length / (speed * 1000 / 3600),  # seconds
                edge_type=etype,
                bridge=1 if etype == "bridge" else 0,
                highway=tags.get("highway", ""),
                name=tags.get("name", ""),
            )

    snap_facilities_to_roads(G)
    return G


def snap_facilities_to_roads(G):
    """Connect isolated facility nodes (hospitals/fire stations) to the nearest road edge,
    so they participate in the road network (otherwise they're isolated components)."""
    facilities = [n for n, d in G.nodes(data=True)
                  if d.get("node_type") in ("hospital", "fire_station")]

    # Build list of road edges (nontrivial connectivity)
    road_edges = [e for e in G.edges() if G[e[0]][e[1]].get("edge_type") in ("road", "bridge")]

    # Snap each facility to nearest road edge endpoint
    for fac in facilities:
        fla, flo = G.nodes[fac]["lat"], G.nodes[fac]["lon"]
        best = None
        best_d = float("inf")
        for a, b in road_edges:
            for nid in (a, b):
                d = haversine(fla, flo, G.nodes[nid]["lat"], G.nodes[nid]["lon"])
                if d < best_d:
                    best_d, best = d, nid
        if best is not None and not G.has_edge(fac, best):
            G.add_edge(fac, best,
                       length=best_d,
                       travel_time=best_d / (15 * 1000 / 3600),  # walk speed
                       edge_type="service_link",
                       bridge=0, highway="", name="service_link")


# ── Pipeline: ARS Scoring ────────────────────────────────────────────────────

def compute_ars(G, hazard, elev_grid=None, elev_n=5):
    """Compute Asset Risk Score for every edge.
    elev_grid: {(i,j):m} Open-Meteo elevation; used to derive slope-based vulnerability."""
    max_rain = max((h["max_rain_mm"] for h in hazard), default=1) or 1

    # Edge betweenness (sample for speed)
    n_edges = G.number_of_edges()
    k = min(n_edges, 50)  # sample up to 50 nodes
    bc = nx.edge_betweenness_centrality(G, k=k, weight="travel_time")
    max_bc = max(bc.values()) if bc else 1.0

    results = []
    for u, v, d in G.edges(data=True):
        H = min(d.get("length", 0) / 500, 1.0) * min(max_rain / 50, 1.0)
        # Vulnerability: bridge > residential > tertiary > secondary > primary
        vuln_map = {"bridge": 0.8, "residential": 0.7, "tertiary": 0.5, "secondary": 0.4, "primary": 0.3}
        V = vuln_map.get(d.get("edge_type", ""), 0.5)
        if d.get("bridge"):
            V = 0.8

        # DEM-derived slope: elevation difference across the edge / length.
        # Steeper → higher flood/slide vulnerability. Blend with base V.
        if elev_grid:
            mlat = (G.nodes[u]["lat"] + G.nodes[v]["lat"]) / 2
            mlon = (G.nodes[u]["lon"] + G.nodes[v]["lon"]) / 2
            try:
                e_u = bilinear_elevation(elev_grid, G.nodes[u]["lat"], G.nodes[u]["lon"], elev_n)
                e_v = bilinear_elevation(elev_grid, G.nodes[v]["lat"], G.nodes[v]["lon"], elev_n)
                slope = abs(e_u - e_v) / max(d.get("length", 1), 1)  # m/m
                # normalize: slope > 0.05 (5%) ~ high
                V = max(V, min(0.5 + slope / 0.05 * 0.5, 0.95))
            except Exception:
                pass

        # Criticality from betweenness
        key = (u, v) if (u, v) in bc else (v, u)
        C = bc.get(key, 0) / max_bc if max_bc else 0

        ARS = W_H * H + W_V * V + W_C * C

        results.append({
            "u": u, "v": v,
            "name": d.get("name", ""),
            "edge_type": d.get("edge_type", "road"),
            "length_m": round(d.get("length", 0)),
            "hazard": round(H, 3),
            "vulnerability": round(V, 3),
            "criticality": round(C, 3),
            "ars": round(ARS, 3),
            "mid_lat": (G.nodes[u]["lat"] + G.nodes[v]["lat"]) / 2,
            "mid_lon": (G.nodes[u]["lon"] + G.nodes[v]["lon"]) / 2,
        })

    results.sort(key=lambda x: x["ars"], reverse=True)
    return results


# ── Pipeline: Cascade Simulation ──────────────────────────────────────────────

def simulate_cascade(G, edge_list, ars_list, top_n=8):
    """Simulate failure of top-N edges. edge_list: [(u,v),...]. ars_list: scored entries."""
    hospital_nodes = [n for n, d in G.nodes(data=True) if d.get("node_type") == "hospital"]
    if not hospital_nodes:
        return []

    # Index ARS entries by (u,v)
    ars_by_edge = {}
    for a in ars_list:
        ars_by_edge[(a["u"], a["v"])] = a
        ars_by_edge[(a["v"], a["u"])] = a

    # Baseline: shortest paths to nearest hospital
    baseline = {}
    for h in hospital_nodes:
        sp = dict(nx.single_source_dijkstra_path_length(G, h, weight="travel_time"))
        for node, dist in sp.items():
            if node not in baseline or dist < baseline[node]:
                baseline[node] = dist

    targets = edge_list[:top_n]
    results = []

    for u, v in targets:
        if not G.has_edge(u, v):
            continue

        saved = G[u][v].copy()
        G.remove_edge(u, v)

        # Recompute after failure
        new_paths = {}
        for h in hospital_nodes:
            try:
                sp = dict(nx.single_source_dijkstra_path_length(G, h, weight="travel_time"))
                for node, dist in sp.items():
                    if node not in new_paths or dist < new_paths[node]:
                        new_paths[node] = dist
            except nx.NetworkXNoPath:
                continue

        G.add_edge(u, v, **saved)  # restore

        # Impact metrics (population-weighted via WorldPop)
        affected = 0
        total_delay = 0
        disconnected = 0
        wpop = load_population()
        pop_reached_affected = 0.0
        total_pop = 0.0
        for node in G.nodes():
            base = baseline.get(node, float("inf"))
            new = new_paths.get(node, float("inf"))
            # population at this road node
            pop = wpop(G.nodes[node]["lat"], G.nodes[node]["lon"])
            total_pop += pop
            if new > base + 0.5:  # >0.5s worse
                affected += 1
                total_delay += new - base
                pop_reached_affected += pop
            if new == float("inf") and base < float("inf"):
                disconnected += 1

        ci = min(affected / max(len(G.nodes()), 1), 1.0)
        # population-scaled impact: fraction of total sampled population affected
        pop_ci = min(pop_reached_affected / max(total_pop, 1e-9), 1.0)
        ars_val = ars_by_edge.get((u, v), {}).get("ars", 0)
        # blend topological CI with population-weighted reach
        ci_combined = 0.5 * ci + 0.5 * pop_ci
        priority = ars_val * (0.5 + 0.5 * ci_combined)

        results.append({
            "edge": [u, v],
            "affected_nodes": affected,
            "disconnected_nodes": disconnected,
            "avg_delay_s": round(total_delay / max(affected, 1), 1),
            "cascade_impact": round(ci, 3),
            "population_impact": round(pop_ci, 3),
            "affected_population": int(pop_reached_affected),
            "ars": ars_val,
            "priority_score": round(priority, 3),
        })

    results.sort(key=lambda x: x["priority_score"], reverse=True)
    return results


# ── Pipeline: GeoJSON Export ──────────────────────────────────────────────────

def graph_to_geojson(G, ars_lookup):
    """Convert graph + ARS data to GeoJSON FeatureCollection."""
    features = []

    # Roads as LineStrings
    seen = set()
    for u, v, d in G.edges(data=True):
        key = tuple(sorted((u, v)))
        if key in seen:
            continue
        seen.add(key)

        ars_data = ars_lookup.get(key, {})
        coords = [
            [G.nodes[u]["lon"], G.nodes[u]["lat"]],
            [G.nodes[v]["lon"], G.nodes[v]["lat"]],
        ]
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": {
                "u": u, "v": v,
                "edge_type": d.get("edge_type", "road"),
                "name": d.get("name", ""),
                "ars": ars_data.get("ars", 0),
                "hazard": ars_data.get("hazard", 0),
                "vulnerability": ars_data.get("vulnerability", 0),
                "criticality": ars_data.get("criticality", 0),
                "length_m": d.get("length", 0),
            },
        })

    # Hospitals & fire stations as Points
    for n, d in G.nodes(data=True):
        if d.get("node_type") in ("hospital", "fire_station"):
            features.append({
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [d["lon"], d["lat"]]},
                "properties": {
                    "node_id": n,
                    "node_type": d["node_type"],
                    "name": d.get("name", "Unknown"),
                },
            })

    return {"type": "FeatureCollection", "features": features}


# ── FastAPI App ───────────────────────────────────────────────────────────────

app = FastAPI(title="InfraShield")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

CACHE_PATH = Path(__file__).parent / "pipeline_cache.json"

# Will hold pipeline results (thread populate; API reads)
STATE = {}


def run_pipeline(fetch=True):
    """Full pipeline. fetch=False → rebuild everything except the slow OSM call."""
    global STATE
    print("⏳ Fetching OSM data for Nagpur..." if fetch else "⏳ Rebuilding from cache...")
    t0 = time.time()

    if fetch:
        osm = fetch_osm()
    else:
        if not CACHE_PATH.exists():
            raise RuntimeError("No cache file and fetch disabled")
        cache = json.loads(CACHE_PATH.read_text())
        osm = cache["osm"]
        # rebuild graph/ars/cascade is cheap; but simplest is return cached results
        STATE = cache["state"]
        STATE["graph"] = build_graph(cache["osm"])
        print(f"   Loaded cache: {STATE['graph'].number_of_nodes()} nodes")
        return

    n_elements = len(osm.get("elements", []))
    print(f"   Got {n_elements} elements in {time.time()-t0:.1f}s")

    print("⏳ Building graph...")
    G = build_graph(osm)
    print(f"   {G.number_of_nodes()} nodes, {G.number_of_edges()} edges")

    print("⏳ Fetching hazard data...")
    hazard = fetch_hazard()
    print(f"   {len(hazard)} data points")

    print("⏳ Fetching elevation (DEM slope proxy)...")
    elev_grid = fetch_elevation_grid(n=5)
    if elev_grid:
        print("   elevation grid OK")
    else:
        print("   elevation unavailable; falling back to road-type V")

    print("⏳ Computing ARS...")
    ars = compute_ars(G, hazard, elev_grid=elev_grid, elev_n=5)
    print(f"   {len(ars)} scored edges, top ARS: {ars[0]['ars'] if ars else 'N/A'}")

    print("⏳ Running cascade simulations...")
    cascade = simulate_cascade(G, [(a["u"], a["v"]) for a in ars], ars)
    print(f"   {len(cascade)} simulations done")

    # Build ARS lookup for GeoJSON
    ars_lookup = {}
    for a in ars:
        ars_lookup[tuple(sorted((a["u"], a["v"]))) ] = a

    geojson = graph_to_geojson(G, ars_lookup)

    STATE["graph"] = G
    STATE["ars"] = ars
    STATE["cascade"] = cascade
    STATE["geojson"] = geojson
    STATE["hazard"] = hazard
    STATE["bbox"] = NAGPUR_BBOX
    STATE["osm"] = osm

    # Persist cache (everything except NetworkX graph, which is rebuilt on demand)
    try:
        cache = {
            "osm": osm,
            "state": {
                "ars": ars,
                "cascade": cascade,
                "geojson": geojson,
                "hazard": hazard,
                "bbox": list(NAGPUR_BBOX),
                "osm": osm,
            },
        }
        CACHE_PATH.write_text(json.dumps(cache))
    except Exception as e:
        print(f"   (cache write failed: {e})")

    print(f"\n✅ InfraShield ready ({time.time()-t0:.1f}s total)\n")


def run_pipeline_bg():
    """Run pipeline in a background thread so server boots immediately."""
    import threading
    def worker():
        try:
            if CACHE_PATH.exists():
                run_pipeline(fetch=False)
            else:
                run_pipeline(fetch=True)
        except Exception as e:
            STATE["error"] = str(e)
            print(f"❌ Pipeline failed: {e}")
    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


@app.on_event("startup")
def startup():
    run_pipeline_bg()


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/data")
def api_data():
    """Return all pipeline results."""
    return JSONResponse({
        "geojson": STATE.get("geojson", {}),
        "ars": STATE.get("ars", []),
        "cascade": STATE.get("cascade", []),
        "hazard": STATE.get("hazard", []),
        "bbox": STATE.get("bbox", list(NAGPUR_BBOX)),
        "stats": {
            "nodes": STATE["graph"].number_of_nodes() if "graph" in STATE else 0,
            "edges": STATE["graph"].number_of_edges() if "graph" in STATE else 0,
        },
        "ready": "graph" in STATE,
        "error": STATE.get("error"),
    })


@app.get("/api/simulate")
def api_simulate(u: int = Query(...), v: int = Query(...)):
    """Simulate failure of a specific edge."""
    G = STATE.get("graph")
    ars_list = STATE.get("ars", [])

    if G is None:
        return JSONResponse({"error": "Pipeline not ready"}, status_code=503)
    if not G.has_edge(u, v):
        return JSONResponse({"error": "Edge not found"}, status_code=404)

    result = simulate_cascade(G, [(u, v)], ars_list, top_n=1)
    return JSONResponse(result[0] if result else {"error": "Simulation failed"})


@app.get("/api/health")
def health():
    return {"status": "ok", "ready": "graph" in STATE, "error": STATE.get("error")}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
