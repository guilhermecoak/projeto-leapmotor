import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import requests

# tópico do ntfy.sh pra push — setado via env var
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_URL = f"https://ntfy.sh"

ALERT_RADIUS_M = 700
MIN_SPEED_KMH = 25       # nao alerta se tiver a pe
DEDUP_MINUTES = 5        # nao realerta mesmo radar em menos de X min
OVERPASS_URL = "https://overpass-api.de/api/interpreter"


SCHEMA = """
CREATE TABLE IF NOT EXISTS radares (
    id INTEGER PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    tipo TEXT,
    maxspeed INTEGER,
    direction TEXT,
    source TEXT DEFAULT 'osm',
    raw TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_radares_loc ON radares(lat, lon);

CREATE TABLE IF NOT EXISTS device_modes (
    device_id TEXT PRIMARY KEY,
    in_car_mode INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS radar_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    radar_id INTEGER NOT NULL,
    alerted_at TEXT DEFAULT (datetime('now')),
    distance_m REAL
);
CREATE INDEX IF NOT EXISTS idx_alerts_dedup ON radar_alerts(device_id, radar_id, alerted_at);
"""


def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))


@contextmanager
def _db(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def fetch_osm_radares(state_name="São Paulo"):
    query = f"""
    [out:json][timeout:90];
    area["name"="{state_name}"]["admin_level"="4"]->.st;
    (
      node["highway"="speed_camera"](area.st);
      node["enforcement"="maxspeed"](area.st);
    );
    out body;
    """
    r = requests.post(OVERPASS_URL, data={"data": query}, timeout=120)
    r.raise_for_status()
    elements = r.json().get("elements", [])
    radares = []
    for el in elements:
        tags = el.get("tags", {})
        radares.append({
            "id": el["id"],
            "lat": el["lat"],
            "lon": el["lon"],
            "tipo": tags.get("highway") or tags.get("enforcement") or "speed_camera",
            "maxspeed": int(tags["maxspeed"]) if tags.get("maxspeed", "").isdigit() else None,
            "direction": tags.get("direction"),
            "raw": json.dumps(tags),
        })
    return radares


def save_radares(db_path, radares):
    with _db(db_path) as conn:
        for r in radares:
            conn.execute(
                """INSERT OR REPLACE INTO radares
                   (id, lat, lon, tipo, maxspeed, direction, source, raw, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,datetime('now'))""",
                (r["id"], r["lat"], r["lon"], r["tipo"],
                 r.get("maxspeed"), r.get("direction"), r.get("source", "osm"), r["raw"])
            )
    return len(radares)


def refresh_radares(db_path, state_name="São Paulo"):
    radares = fetch_osm_radares(state_name)
    saved = save_radares(db_path, radares)
    return {"state": state_name, "fetched": len(radares), "saved": saved}


def find_nearby(db_path, lat, lon, radius_m=ALERT_RADIUS_M):
    deg_lat = radius_m / 111_000
    deg_lon = radius_m / (111_000 * math.cos(math.radians(lat))) if abs(lat) < 85 else 1
    with _db(db_path) as conn:
        rows = conn.execute(
            """SELECT * FROM radares
               WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?""",
            (lat - deg_lat, lat + deg_lat, lon - deg_lon, lon + deg_lon)
        ).fetchall()
    result = []
    for r in rows:
        d = haversine_m(lat, lon, r["lat"], r["lon"])
        if d <= radius_m:
            item = dict(r)
            item["distance_m"] = round(d, 1)
            result.append(item)
    result.sort(key=lambda x: x["distance_m"])
    return result


def get_car_mode(db_path, device_id):
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT in_car_mode FROM device_modes WHERE device_id = ?", (device_id,)
        ).fetchone()
    return bool(row and row["in_car_mode"])


def set_car_mode(db_path, device_id, enabled):
    with _db(db_path) as conn:
        conn.execute(
            """INSERT INTO device_modes(device_id, in_car_mode, updated_at)
               VALUES (?, ?, datetime('now'))
               ON CONFLICT(device_id) DO UPDATE SET
                 in_car_mode = excluded.in_car_mode,
                 updated_at = datetime('now')""",
            (device_id, 1 if enabled else 0)
        )


def should_alert(db_path, device_id, radar_id):
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=DEDUP_MINUTES)).isoformat()
    with _db(db_path) as conn:
        row = conn.execute(
            """SELECT 1 FROM radar_alerts
               WHERE device_id = ? AND radar_id = ? AND alerted_at > ?""",
            (device_id, radar_id, cutoff)
        ).fetchone()
    return row is None


def record_alert(db_path, device_id, radar_id, distance_m):
    with _db(db_path) as conn:
        conn.execute(
            """INSERT INTO radar_alerts(device_id, radar_id, distance_m) VALUES (?,?,?)""",
            (device_id, radar_id, distance_m)
        )


# ntfy JSON API aceita utf-8 (header nao aceita emoji por ser latin-1)
# priority no JSON eh int 1-5
_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5}

def send_ntfy(title, body, priority="high", tags="rotating_light"):
    if not NTFY_TOPIC:
        return
    try:
        requests.post(
            NTFY_URL,
            json={
                "topic": NTFY_TOPIC,
                "title": title,
                "message": body,
                "priority": _PRIORITY.get(priority, 3),
                "tags": tags.split(","),
            },
            timeout=5,
        )
    except Exception as e:
        print(f"ntfy error: {e}")


def check_and_alert(db_path, device_id, lat, lon, speed_mps):
    if not get_car_mode(db_path, device_id):
        return []
    speed_kmh = (speed_mps * 3.6) if speed_mps and speed_mps > 0 else 0
    if speed_kmh < MIN_SPEED_KMH:
        return []
    nearby = find_nearby(db_path, lat, lon, ALERT_RADIUS_M)
    sent = []
    for r in nearby:
        if not should_alert(db_path, device_id, r["id"]):
            continue
        maxspd = f" - {r['maxspeed']} km/h" if r["maxspeed"] else ""
        title = f"radar em {int(r['distance_m'])}m{maxspd}"
        body = f"voce: {int(speed_kmh)} km/h - radar tipo {r['tipo']}"
        send_ntfy(title, body, "high", "rotating_light,warning")
        record_alert(db_path, device_id, r["id"], r["distance_m"])
        sent.append(r["id"])
    return sent
