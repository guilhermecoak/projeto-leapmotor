import json
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse

import analysis
import radares

DB_PATH = Path(os.environ.get("DB_PATH", "/data/gps.sqlite"))
TOKEN = os.environ["GPS_TOKEN"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    altitude REAL,
    speed REAL,
    horizontal_accuracy REAL,
    vertical_accuracy REAL,
    motion TEXT,
    battery_level REAL,
    battery_state TEXT,
    wifi TEXT,
    device_id TEXT,
    raw TEXT,
    received_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_timestamp ON locations(timestamp);
CREATE INDEX IF NOT EXISTS idx_device_ts ON locations(device_id, timestamp);
"""


@contextmanager
def db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(radares.SCHEMA)
    yield


app = FastAPI(title="GPS API", version="1.2", lifespan=lifespan)


def check_token(authorization, access_token):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    elif access_token:
        token = access_token
    if token != TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")


@app.post("/overland")
async def receive(
    payload: dict,
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
):
    check_token(authorization, access_token)
    locations = payload.get("locations", [])
    inserted = 0
    alerts_sent = []
    with db() as conn:
        for feat in locations:
            geom = feat.get("geometry", {})
            coords = geom.get("coordinates") or [None, None]
            props = feat.get("properties", {})
            conn.execute(
                """INSERT INTO locations(
                    timestamp, lat, lon, altitude, speed,
                    horizontal_accuracy, vertical_accuracy,
                    motion, battery_level, battery_state,
                    wifi, device_id, raw
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    props.get("timestamp"),
                    coords[1],
                    coords[0],
                    props.get("altitude"),
                    props.get("speed"),
                    props.get("horizontal_accuracy"),
                    props.get("vertical_accuracy"),
                    json.dumps(props.get("motion")) if props.get("motion") else None,
                    props.get("battery_level"),
                    props.get("battery_state"),
                    props.get("wifi"),
                    props.get("device_id"),
                    json.dumps(feat),
                ),
            )
            inserted += 1

    # se device em modo carro, checa radar pra cada ponto
    for feat in locations:
        props = feat.get("properties", {})
        coords = feat.get("geometry", {}).get("coordinates") or [None, None]
        device_id = props.get("device_id")
        if not device_id or coords[0] is None:
            continue
        sent = radares.check_and_alert(
            DB_PATH, device_id, coords[1], coords[0], props.get("speed")
        )
        alerts_sent.extend(sent)

    return {"result": "ok", "saved": inserted, "radar_alerts": alerts_sent}


@app.get("/stats")
def stats(authorization: str | None = Header(None), access_token: str | None = Query(None)):
    check_token(authorization, access_token)
    with db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS total, MIN(timestamp) AS first_ts,
                      MAX(timestamp) AS last_ts, COUNT(DISTINCT device_id) AS devices
               FROM locations"""
        ).fetchone()
        today = conn.execute(
            "SELECT COUNT(*) AS c FROM locations WHERE date(timestamp) = date('now')"
        ).fetchone()
        last = conn.execute(
            "SELECT timestamp, lat, lon, speed FROM locations ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        rcount = conn.execute("SELECT COUNT(*) AS c FROM radares").fetchone()
    return {
        "total_points": row["total"],
        "first_timestamp": row["first_ts"],
        "last_timestamp": row["last_ts"],
        "devices": row["devices"],
        "points_today": today["c"],
        "last_point": dict(last) if last else None,
        "radares_count": rcount["c"],
    }


@app.get("/points")
def points(
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
    since: str | None = Query(None),
    until: str | None = Query(None),
    limit: int = Query(1000, le=50000),
):
    check_token(authorization, access_token)
    q = "SELECT timestamp, lat, lon, speed, altitude, motion FROM locations WHERE 1=1"
    params = []
    if since:
        q += " AND timestamp >= ?"
        params.append(since)
    if until:
        q += " AND timestamp <= ?"
        params.append(until)
    q += " ORDER BY timestamp ASC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


@app.get("/analysis")
def full_analysis(
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
    since: str | None = Query(None),
):
    check_token(authorization, access_token)
    return analysis.analyze(DB_PATH, since)


@app.get("/map", response_class=HTMLResponse)
def render_map(
    access_token: str | None = Query(None),
    since: str | None = Query(None),
    show_radares: bool = Query(False),
):
    check_token(None, access_token)
    return analysis.render_map(DB_PATH, since, show_radares=show_radares)


@app.get("/radares")
def list_radares(
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
):
    check_token(authorization, access_token)
    with db() as conn:
        rows = conn.execute("SELECT id, lat, lon, tipo, maxspeed FROM radares").fetchall()
    return [dict(r) for r in rows]


@app.get("/radares/near")
def radares_near(
    lat: float,
    lon: float,
    radius: int = 1000,
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
):
    check_token(authorization, access_token)
    return radares.find_nearby(DB_PATH, lat, lon, radius)


@app.post("/radares/refresh")
def radares_refresh(
    state: str = Query("São Paulo"),
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
):
    check_token(authorization, access_token)
    return radares.refresh_radares(DB_PATH, state)


@app.post("/mode/car")
def set_mode(
    device_id: str,
    enabled: bool,
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
):
    check_token(authorization, access_token)
    radares.set_car_mode(DB_PATH, device_id, enabled)
    if enabled:
        radares.send_ntfy(
            title="Modo carro ATIVO",
            body=f"alertas de radar ligados pra {device_id}",
            priority="low",
            tags="car",
        )
    else:
        radares.send_ntfy(
            title="Modo carro DESLIGADO",
            body=f"alertas de radar desativados pra {device_id}",
            priority="low",
            tags="house",
        )
    return {"device_id": device_id, "in_car_mode": enabled}


@app.get("/mode/car")
def get_mode(
    device_id: str,
    authorization: str | None = Header(None),
    access_token: str | None = Query(None),
):
    check_token(authorization, access_token)
    return {
        "device_id": device_id,
        "in_car_mode": radares.get_car_mode(DB_PATH, device_id),
    }


@app.get("/health")
def health():
    return {"status": "ok"}
