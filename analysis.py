import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# parametros de segmentacao de trajeto
GAP_MINUTES = 5          # gap entre pontos considerado fim de trajeto
STATIONARY_MIN = 3       # minutos parado (speed<1) considerado fim de trajeto
MIN_TRIP_POINTS = 10
MIN_TRIP_METERS = 200
CLUSTER_PRECISION = 0.002  # ~220m pra agrupar origens/destinos


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


def parse_since(spec):
    if not spec:
        return None
    import re
    m = re.match(r"(\d+)([dhm])$", spec)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
        return datetime.now(timezone.utc) - delta
    return datetime.fromisoformat(spec).replace(tzinfo=timezone.utc)


def fetch_points(db_path, since=None):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    q = "SELECT timestamp, lat, lon, speed, altitude FROM locations WHERE 1=1"
    params = []
    if since:
        q += " AND timestamp >= ?"
        params.append(since.isoformat())
    q += " ORDER BY timestamp ASC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    points = []
    for r in rows:
        p = dict(r)
        p["_ts"] = datetime.fromisoformat(p["timestamp"].replace("Z", "+00:00"))
        points.append(p)
    return points


def segment_trips(points):
    trips = []
    current = []
    stationary_since = None
    for i, p in enumerate(points):
        if i == 0:
            current.append(p)
            continue
        prev = points[i-1]
        gap = (p["_ts"] - prev["_ts"]).total_seconds() / 60
        if p.get("speed") is not None and p["speed"] < 1:
            stationary_since = stationary_since or prev["_ts"]
        else:
            stationary_since = None
        long_stop = (stationary_since and
                     (p["_ts"] - stationary_since).total_seconds() / 60 > STATIONARY_MIN)
        if gap > GAP_MINUTES or long_stop:
            if len(current) >= MIN_TRIP_POINTS:
                trips.append(current)
            current = []
            stationary_since = None
        current.append(p)
    if len(current) >= MIN_TRIP_POINTS:
        trips.append(current)
    return trips


def trip_summary(trip):
    start, end = trip[0], trip[-1]
    duration_min = (end["_ts"] - start["_ts"]).total_seconds() / 60
    distance_m = 0
    for i in range(1, len(trip)):
        distance_m += haversine_m(trip[i-1]["lat"], trip[i-1]["lon"],
                                  trip[i]["lat"], trip[i]["lon"])
    return {
        "start_time": start["_ts"].isoformat(),
        "end_time": end["_ts"].isoformat(),
        "duration_min": round(duration_min, 1),
        "distance_km": round(distance_m / 1000, 2),
        "avg_speed_kmh": round((distance_m / 1000) / (duration_min / 60), 1) if duration_min > 0 else 0,
        "origin": [round(start["lat"], 5), round(start["lon"], 5)],
        "destination": [round(end["lat"], 5), round(end["lon"], 5)],
        "points": len(trip),
    }


def cluster_key(lat, lon):
    return (round(lat / CLUSTER_PRECISION) * CLUSTER_PRECISION,
            round(lon / CLUSTER_PRECISION) * CLUSTER_PRECISION)


def group_routes(summaries):
    groups = defaultdict(list)
    for s in summaries:
        o = cluster_key(*s["origin"])
        d = cluster_key(*s["destination"])
        groups[(o, d)].append(s)
    result = []
    for (o, d), trips in groups.items():
        if len(trips) < 2:
            continue
        durations = [t["duration_min"] for t in trips]
        result.append({
            "origin": [round(o[0], 4), round(o[1], 4)],
            "destination": [round(d[0], 4), round(d[1], 4)],
            "trips_count": len(trips),
            "avg_duration_min": round(sum(durations) / len(durations), 1),
            "min_duration_min": round(min(durations), 1),
            "max_duration_min": round(max(durations), 1),
            "variation_min": round(max(durations) - min(durations), 1),
            "avg_distance_km": round(sum(t["distance_km"] for t in trips) / len(trips), 2),
        })
    result.sort(key=lambda x: -x["trips_count"])
    return result


def analyze(db_path, since_spec=None):
    since = parse_since(since_spec)
    points = fetch_points(db_path, since)
    if not points:
        return {"error": "no_points", "since": since_spec}
    trips_raw = segment_trips(points)
    summaries = []
    for t in trips_raw:
        dist = sum(haversine_m(t[i-1]["lat"], t[i-1]["lon"], t[i]["lat"], t[i]["lon"])
                   for i in range(1, len(t)))
        if dist >= MIN_TRIP_METERS:
            summaries.append(trip_summary(t))
    total_km = sum(s["distance_km"] for s in summaries)
    total_min = sum(s["duration_min"] for s in summaries)
    return {
        "period": {
            "first": points[0]["_ts"].isoformat(),
            "last": points[-1]["_ts"].isoformat(),
            "since": since.isoformat() if since else None,
        },
        "totals": {
            "points": len(points),
            "trips": len(summaries),
            "distance_km": round(total_km, 1),
            "duration_hours": round(total_min / 60, 1),
        },
        "trips": sorted(summaries, key=lambda x: x["start_time"], reverse=True),
        "recurring_routes": group_routes(summaries),
    }


def fetch_radares(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT id, lat, lon, tipo, maxspeed FROM radares").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return [dict(r) for r in rows]


def geojson_of_trips(trips_raw):
    features = []
    for i, trip in enumerate(trips_raw):
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString",
                         "coordinates": [[p["lon"], p["lat"]] for p in trip]},
            "properties": {
                "trip_id": i,
                "start_time": trip[0]["_ts"].isoformat(),
                "end_time": trip[-1]["_ts"].isoformat(),
                "points": len(trip),
            },
        })
    return {"type": "FeatureCollection", "features": features}


MAP_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <title>GPS</title>
  <meta charset="utf-8"/>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
  <style>
    body,html,#map{margin:0;padding:0;height:100vh;width:100%;font-family:sans-serif}
    #info{position:absolute;top:10px;right:10px;z-index:1000;background:white;
          padding:10px;border-radius:6px;box-shadow:0 2px 4px rgba(0,0,0,.3);
          max-width:300px;font-size:13px}
  </style>
</head>
<body>
  <div id="map"></div>
  <div id="info">__INFO__</div>
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script>
    const data = __GEOJSON__;
    const center = __CENTER__;
    const map = L.map('map').setView(center, 12);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
      {attribution:'&copy; OpenStreetMap',maxZoom:19}).addTo(map);
    const colors = ['#1f77b4','#d62728','#2ca02c','#9467bd','#ff7f0e','#8c564b'];
    data.features.forEach((f, i) => {
      const coords = f.geometry.coordinates.map(c => [c[1], c[0]]);
      L.polyline(coords, {color: colors[i % colors.length], weight: 3, opacity: .7})
        .bindPopup(`trajeto ${f.properties.trip_id+1}<br>${f.properties.start_time}<br>${f.properties.points} pontos`)
        .addTo(map);
      if (coords.length) {
        L.circleMarker(coords[0], {color:'green', radius:6, fill:true}).bindTooltip('inicio').addTo(map);
        L.circleMarker(coords[coords.length-1], {color:'red', radius:6, fill:true}).bindTooltip('fim').addTo(map);
      }
    });
    const radares = __RADARES__;
    radares.forEach(r => {
      const label = r.maxspeed ? `${r.tipo} - ${r.maxspeed}km/h` : r.tipo;
      L.circleMarker([r.lat, r.lon], {color:'#ff1744', radius:5, fill:true, fillOpacity:.8})
        .bindTooltip(label).addTo(map);
    });
  </script>
</body>
</html>"""


def render_map(db_path, since_spec=None, show_radares=False):
    since = parse_since(since_spec)
    points = fetch_points(db_path, since)
    if not points:
        return "<h1>sem pontos no periodo</h1>"
    trips = segment_trips(points)
    if not trips:
        return "<h1>sem trajetos detectados</h1>"
    import json as _json
    geojson = geojson_of_trips(trips)
    all_pts = [p for t in trips for p in t]
    center = [sum(p["lat"] for p in all_pts) / len(all_pts),
              sum(p["lon"] for p in all_pts) / len(all_pts)]
    info = f"{len(trips)} trajeto(s) - {len(all_pts)} pontos"
    if since_spec:
        info += f" - desde {since_spec}"
    rad_json = _json.dumps(fetch_radares(db_path) if show_radares else [])
    return (MAP_HTML_TEMPLATE
            .replace("__GEOJSON__", _json.dumps(geojson))
            .replace("__CENTER__", _json.dumps(center))
            .replace("__INFO__", info)
            .replace("__RADARES__", rad_json))
