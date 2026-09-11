# InfraShield

**Graph-based urban infrastructure risk assessment and cascade impact prediction for smart cities.**

InfraShield models a city's critical road network from live OpenStreetMap data, scores every edge for hazard × vulnerability × criticality, and simulates how a failure at one point cascades through the network — reporting how many people are affected and how long before impact. Built for Nagpur, India.

---

## Problem

Infrastructure failures in cities like Nagpur cause:
- Widespread transport disruption
- Delayed emergency response (hospitals and services cut off)
- Economic losses and public safety risk

**Root cause:** Criticality of a road or junction is judged by intuition, not data. Failure impacts are assumed to stay local, so no one knows which single point of failure would disconnect a hospital or strand the most people — until it happens.

---

## Solution

InfraShield builds a live graph of the city network and reasons about risk quantitatively:

1. **Fetch real network data** from OpenStreetMap (3-mirror Overpass fallback for reliability)
2. **Score every edge** as ARS (Aggregate Risk Score) = hazard × vulnerability × criticality, weighted by local population and terrain elevation
3. **Snap facilities to the network** — hospitals and critical services are linked to the nearest road node so their exposure is real, not isolated
4. **Simulate cascades** — fail one edge and watch the failure propagate through connected nodes, counting affected population and impact delay
5. **Visualize it** in an interactive dark-HUD dashboard with a cinematic 3D network intro

---

## Key Features

- **Live data pipeline:** OSM road network, Open-Meteo weather/elevation, WorldPop population density — fetched and cached to disk, so the server boots in seconds, not minutes
- **ARS risk scoring:** Hazard × vulnerability × criticality per network edge, blended with population-weighted reach
- **Facility snapping:** Hospitals automatically connected to the nearest road node, so real-world exposure shows up in cascade results
- **Population-weighted cascades:** A single edge failure reports affected node count, affected population, and estimated delay before impact
- **Cinematic dashboard:** Leaflet dark-HUD interface with glassmorphism panels, count-up metrics, and a Three.js particle-network intro of the city's real junctions
- **Resilient by design:** Overpass mirror fallback, graceful population-raster fallback, background pipeline so first boot isn't blocking

---

## Technology Stack

**Core Application:**
- Python 3.10+
- FastAPI + Uvicorn (ASGI server)
- NetworkX (graph construction, ARS scoring, cascade simulation)
- NumPy (numerical computation)

**Data Sources:**
- OpenStreetMap (Overpass API, 3-mirror fallback) — road network and hospitals
- Open-Meteo — weather + elevation grid
- WorldPop — population density raster (via Rasterio)

**Analysis Tools:**
- Rasterio (GeoTIFF population processing)
- Bilinear interpolation (elevation grid sampling)

**Frontend:**
- Leaflet (interactive map)
- Three.js (cinematic 3D network intro)
- Vanilla HTML/CSS/JS (dark HUD, glassmorphism panels)

---

## Project Structure

```
infra-shield/
├── server.py             # FastAPI server: pipeline, graph, ARS, cascade (+ disk cache)
├── requirements.txt      # Python dependencies
├── README.md
└── static/
    └── index.html        # Leaflet dark-HUD dashboard + Three.js intro
```