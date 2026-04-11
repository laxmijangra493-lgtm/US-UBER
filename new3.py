import os
import logging
import sqlite3
from functools import wraps
from datetime import datetime, timedelta, timezone

import requests
import jwt
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from werkzeug.security import generate_password_hash, check_password_hash

# ──────────────────────────────────────────────
#  APP & CONFIG
# ──────────────────────────────────────────────
app = Flask(__name__)
CORS(app)  # allow all origins – tighten in prod with origins=[...]

app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["DB_PATH"]    = os.environ.get("DB_PATH", "uber.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
)
log = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  DATABASE
# ──────────────────────────────────────────────
def get_db():
    if "db" not in g:
        conn = sqlite3.connect(app.config["DB_PATH"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS riders (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        email         TEXT    NOT NULL UNIQUE,
        phone         TEXT    NOT NULL UNIQUE,
        password_hash TEXT    NOT NULL,
        created_at    TEXT    DEFAULT (datetime('now'))
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS drivers (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        email         TEXT    NOT NULL UNIQUE,
        phone         TEXT    NOT NULL UNIQUE,
        password_hash TEXT    NOT NULL,
        car           TEXT    NOT NULL,
        car_number    TEXT    NOT NULL UNIQUE,
        status        TEXT    NOT NULL DEFAULT 'available',
        latitude      REAL,
        longitude     REAL,
        rating        REAL    DEFAULT 5.0,
        total_rides   INTEGER DEFAULT 0,
        created_at    TEXT    DEFAULT (datetime('now'))
    )""")

    c.execute("""
    CREATE TABLE IF NOT EXISTS rides (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        rider_id      INTEGER NOT NULL REFERENCES riders(id),
        driver_id     INTEGER REFERENCES drivers(id),
        pickup        TEXT    NOT NULL,
        destination   TEXT    NOT NULL,
        pickup_lat    REAL,
        pickup_lon    REAL,
        dest_lat      REAL,
        dest_lon      REAL,
        distance_km   REAL,
        fare          REAL,
        eta_min       REAL,
        status        TEXT    NOT NULL DEFAULT 'requested',
        driver_rating INTEGER,
        created_at    TEXT    DEFAULT (datetime('now')),
        completed_at  TEXT
    )""")

    conn.commit()
    conn.close()
    log.info("Database initialised.")


init_db()


# ──────────────────────────────────────────────
#  JWT AUTH HELPERS
# ──────────────────────────────────────────────
def create_token(user_id: int, role: str) -> str:
    payload = {
        "sub":  user_id,
        "role": role,
        "exp":  datetime.now(timezone.utc) + timedelta(hours=24),
    }
    return jwt.encode(payload, app.config["SECRET_KEY"], algorithm="HS256")


def decode_token(token: str) -> dict:
    return jwt.decode(token, app.config["SECRET_KEY"], algorithms=["HS256"])


def login_required(role=None):
    """Decorator – validates Bearer JWT and optionally checks role."""
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            auth = request.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return jsonify({"error": "Missing or invalid Authorization header"}), 401
            token = auth.split(" ", 1)[1]
            try:
                payload = decode_token(token)
            except jwt.ExpiredSignatureError:
                return jsonify({"error": "Token expired"}), 401
            except jwt.InvalidTokenError:
                return jsonify({"error": "Invalid token"}), 401
            if role and payload.get("role") != role:
                return jsonify({"error": "Forbidden – wrong role"}), 403
            g.user_id   = payload["sub"]
            g.user_role = payload["role"]
            return f(*args, **kwargs)
        return wrapper
    return decorator


# ──────────────────────────────────────────────
#  VALIDATION HELPERS
# ──────────────────────────────────────────────
def require_fields(data: dict, *fields):
    missing = [f for f in fields if not data.get(f)]
    if missing:
        return jsonify({"error": f"Missing fields: {', '.join(missing)}"}), 400
    return None


def validate_phone(phone: str) -> bool:
    return phone.isdigit() and 7 <= len(phone) <= 15


# ──────────────────────────────────────────────
#  GEOCODER
# ──────────────────────────────────────────────
geo = Nominatim(user_agent="uber_clone_app", timeout=5)


def safe_geocode(place: str):
    try:
        loc = geo.geocode(place)
        return loc
    except Exception as exc:
        log.warning("Geocode failed for '%s': %s", place, exc)
        return None


# ──────────────────────────────────────────────
#  NEAREST DRIVER  (real distance logic)
# ──────────────────────────────────────────────
def find_nearest_driver(pickup_lat: float, pickup_lon: float):
    db = get_db()
    drivers = db.execute(
        "SELECT * FROM drivers WHERE status='available' AND latitude IS NOT NULL"
    ).fetchall()

    if not drivers:
        return None

    pickup = (pickup_lat, pickup_lon)
    nearest, best_dist = None, float("inf")
    for d in drivers:
        dist = geodesic(pickup, (d["latitude"], d["longitude"])).km
        if dist < best_dist:
            best_dist, nearest = dist, d

    return nearest


# ──────────────────────────────────────────────
#  FARE CALCULATOR  (simple surge stub)
# ──────────────────────────────────────────────
BASE_FARE   = 40.0   # flat fee (₹ or $)
RATE_PER_KM = 10.0   # per km
SPEED_KMH   = 40.0   # assumed average speed

def calculate_fare(distance_km: float) -> tuple[float, float]:
    fare    = BASE_FARE + distance_km * RATE_PER_KM
    eta_min = (distance_km / SPEED_KMH) * 60
    return round(fare, 2), round(eta_min, 2)


# ══════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════

# ── REGISTER RIDER ──────────────────────────
@app.route("/api/riders/register", methods=["POST"])
def register_rider():
    data = request.get_json(silent=True) or {}
    err  = require_fields(data, "name", "email", "phone", "password")
    if err:
        return err

    if not validate_phone(data["phone"]):
        return jsonify({"error": "Invalid phone number (digits only, 7–15 chars)"}), 400

    db = get_db()
    try:
        db.execute(
            "INSERT INTO riders (name, email, phone, password_hash) VALUES (?,?,?,?)",
            (data["name"], data["email"], data["phone"],
             generate_password_hash(data["password"]))
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email or phone already registered"}), 409

    rider = db.execute("SELECT * FROM riders WHERE phone=?", (data["phone"],)).fetchone()
    token = create_token(rider["id"], "rider")
    return jsonify({"message": "Rider registered", "token": token, "rider_id": rider["id"]}), 201


# ── LOGIN RIDER ─────────────────────────────
@app.route("/api/riders/login", methods=["POST"])
def login_rider():
    data = request.get_json(silent=True) or {}
    err  = require_fields(data, "email", "password")
    if err:
        return err

    db    = get_db()
    rider = db.execute("SELECT * FROM riders WHERE email=?", (data["email"],)).fetchone()
    if not rider or not check_password_hash(rider["password_hash"], data["password"]):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_token(rider["id"], "rider")
    return jsonify({"token": token, "rider_id": rider["id"], "name": rider["name"]})


# ── REGISTER DRIVER ─────────────────────────
@app.route("/api/drivers/register", methods=["POST"])
def register_driver():
    data = request.get_json(silent=True) or {}
    err  = require_fields(data, "name", "email", "phone", "password", "car", "car_number")
    if err:
        return err

    if not validate_phone(data["phone"]):
        return jsonify({"error": "Invalid phone number"}), 400

    db = get_db()
    try:
        db.execute(
            """INSERT INTO drivers (name, email, phone, password_hash, car, car_number)
               VALUES (?,?,?,?,?,?)""",
            (data["name"], data["email"], data["phone"],
             generate_password_hash(data["password"]),
             data["car"], data["car_number"])
        )
        db.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Email, phone, or car number already registered"}), 409

    driver = db.execute("SELECT * FROM drivers WHERE phone=?", (data["phone"],)).fetchone()
    token  = create_token(driver["id"], "driver")
    return jsonify({"message": "Driver registered", "token": token, "driver_id": driver["id"]}), 201


# ── LOGIN DRIVER ────────────────────────────
@app.route("/api/drivers/login", methods=["POST"])
def login_driver():
    data   = request.get_json(silent=True) or {}
    err    = require_fields(data, "email", "password")
    if err:
        return err

    db     = get_db()
    driver = db.execute("SELECT * FROM drivers WHERE email=?", (data["email"],)).fetchone()
    if not driver or not check_password_hash(driver["password_hash"], data["password"]):
        return jsonify({"error": "Invalid credentials"}), 401

    token = create_token(driver["id"], "driver")
    return jsonify({"token": token, "driver_id": driver["id"], "name": driver["name"]})


# ── UPDATE DRIVER LOCATION ──────────────────
@app.route("/api/drivers/location", methods=["PUT"])
@login_required(role="driver")
def update_driver_location():
    data = request.get_json(silent=True) or {}
    err  = require_fields(data, "latitude", "longitude")
    if err:
        return err

    db = get_db()
    db.execute(
        "UPDATE drivers SET latitude=?, longitude=? WHERE id=?",
        (data["latitude"], data["longitude"], g.user_id)
    )
    db.commit()
    return jsonify({"message": "Location updated"})


# ── DRIVER GO ONLINE / OFFLINE ──────────────
@app.route("/api/drivers/status", methods=["PUT"])
@login_required(role="driver")
def update_driver_status():
    data   = request.get_json(silent=True) or {}
    status = data.get("status")
    if status not in ("available", "offline"):
        return jsonify({"error": "status must be 'available' or 'offline'"}), 400

    db = get_db()
    db.execute("UPDATE drivers SET status=? WHERE id=?", (status, g.user_id))
    db.commit()
    return jsonify({"message": f"Driver is now {status}"})


# ── BOOK RIDE ───────────────────────────────
@app.route("/api/rides/book", methods=["POST"])
@login_required(role="rider")
def book_ride():
    data = request.get_json(silent=True) or {}
    err  = require_fields(data, "pickup", "destination")
    if err:
        return err

    pickup      = safe_geocode(data["pickup"])
    destination = safe_geocode(data["destination"])

    if not pickup:
        return jsonify({"error": "Pickup location not found"}), 400
    if not destination:
        return jsonify({"error": "Destination not found"}), 400

    pickup_coord = (pickup.latitude, pickup.longitude)
    dest_coord   = (destination.latitude, destination.longitude)

    driver = find_nearest_driver(pickup.latitude, pickup.longitude)
    if not driver:
        return jsonify({"error": "No drivers available right now"}), 503

    distance_km        = geodesic(pickup_coord, dest_coord).km
    fare, eta_min      = calculate_fare(distance_km)

    # Fetch OSRM route
    route_coords = []
    try:
        osrm_url = (
            f"http://router.project-osrm.org/route/v1/driving/"
            f"{pickup.longitude},{pickup.latitude};"
            f"{destination.longitude},{destination.latitude}"
            f"?overview=full&geometries=geojson"
        )
        resp       = requests.get(osrm_url, timeout=5)
        resp.raise_for_status()
        route_data = resp.json()
        coords     = route_data["routes"][0]["geometry"]["coordinates"]
        route_coords = [[c[1], c[0]] for c in coords]
    except requests.RequestException as exc:
        log.warning("OSRM request failed: %s", exc)
    except (KeyError, IndexError) as exc:
        log.warning("OSRM response parse error: %s", exc)

    db = get_db()
    db.execute("UPDATE drivers SET status='busy' WHERE id=?", (driver["id"],))
    cur = db.execute(
        """INSERT INTO rides
           (rider_id, driver_id, pickup, destination,
            pickup_lat, pickup_lon, dest_lat, dest_lon,
            distance_km, fare, eta_min, status)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (g.user_id, driver["id"],
         data["pickup"], data["destination"],
         pickup.latitude, pickup.longitude,
         destination.latitude, destination.longitude,
         distance_km, fare, eta_min, "ongoing")
    )
    ride_id = cur.lastrowid
    db.commit()

    return jsonify({
        "ride_id": ride_id,
        "driver": {
            "id":         driver["id"],
            "name":       driver["name"],
            "phone":      driver["phone"],
            "car":        driver["car"],
            "car_number": driver["car_number"],
            "rating":     driver["rating"],
            "latitude":   driver["latitude"],
            "longitude":  driver["longitude"],
        },
        "trip": {
            "distance_km": round(distance_km, 2),
            "fare":        fare,
            "eta_min":     eta_min,
        },
        "route": route_coords,
    }), 201


# ── COMPLETE RIDE ───────────────────────────
@app.route("/api/rides/<int:ride_id>/complete", methods=["POST"])
@login_required(role="driver")
def complete_ride(ride_id: int):
    db   = get_db()
    ride = db.execute(
        "SELECT * FROM rides WHERE id=? AND driver_id=? AND status='ongoing'",
        (ride_id, g.user_id)
    ).fetchone()

    if not ride:
        return jsonify({"error": "Ride not found or not yours"}), 404

    now = datetime.now(timezone.utc).isoformat()
    db.execute(
        "UPDATE rides SET status='completed', completed_at=? WHERE id=?",
        (now, ride_id)
    )
    db.execute(
        "UPDATE drivers SET status='available', total_rides=total_rides+1 WHERE id=?",
        (g.user_id,)
    )
    db.commit()
    return jsonify({"message": "Ride completed", "completed_at": now})


# ── CANCEL RIDE ─────────────────────────────
@app.route("/api/rides/<int:ride_id>/cancel", methods=["POST"])
@login_required()
def cancel_ride(ride_id: int):
    db   = get_db()
    ride = db.execute("SELECT * FROM rides WHERE id=?", (ride_id,)).fetchone()

    if not ride:
        return jsonify({"error": "Ride not found"}), 404

    # Only the rider who booked or the assigned driver may cancel
    is_rider  = g.user_role == "rider"  and ride["rider_id"]  == g.user_id
    is_driver = g.user_role == "driver" and ride["driver_id"] == g.user_id
    if not (is_rider or is_driver):
        return jsonify({"error": "Not authorised to cancel this ride"}), 403

    if ride["status"] not in ("requested", "ongoing"):
        return jsonify({"error": "Ride cannot be cancelled"}), 409

    db.execute("UPDATE rides SET status='cancelled' WHERE id=?", (ride_id,))
    if ride["driver_id"]:
        db.execute("UPDATE drivers SET status='available' WHERE id=?", (ride["driver_id"],))
    db.commit()
    return jsonify({"message": "Ride cancelled"})


# ── RATE DRIVER ─────────────────────────────
@app.route("/api/rides/<int:ride_id>/rate", methods=["POST"])
@login_required(role="rider")
def rate_driver(ride_id: int):
    data   = request.get_json(silent=True) or {}
    rating = data.get("rating")

    if rating not in (1, 2, 3, 4, 5):
        return jsonify({"error": "Rating must be an integer between 1 and 5"}), 400

    db   = get_db()
    ride = db.execute(
        "SELECT * FROM rides WHERE id=? AND rider_id=? AND status='completed'",
        (ride_id, g.user_id)
    ).fetchone()

    if not ride:
        return jsonify({"error": "Ride not found or not completed"}), 404
    if ride["driver_rating"]:
        return jsonify({"error": "Already rated"}), 409

    db.execute("UPDATE rides SET driver_rating=? WHERE id=?", (rating, ride_id))

    # Recalculate driver average rating
    result = db.execute(
        "SELECT AVG(driver_rating) as avg FROM rides WHERE driver_id=? AND driver_rating IS NOT NULL",
        (ride["driver_id"],)
    ).fetchone()
    db.execute("UPDATE drivers SET rating=? WHERE id=?", (round(result["avg"], 2), ride["driver_id"]))
    db.commit()

    return jsonify({"message": "Rating submitted", "new_driver_rating": round(result["avg"], 2)})


# ── RIDE HISTORY (RIDER) ────────────────────
@app.route("/api/riders/rides", methods=["GET"])
@login_required(role="rider")
def rider_history():
    db    = get_db()
    rides = db.execute(
        """SELECT r.*, d.name as driver_name, d.car, d.car_number
           FROM rides r
           LEFT JOIN drivers d ON r.driver_id = d.id
           WHERE r.rider_id=?
           ORDER BY r.created_at DESC""",
        (g.user_id,)
    ).fetchall()
    return jsonify([dict(row) for row in rides])


# ── RIDE HISTORY (DRIVER) ───────────────────
@app.route("/api/drivers/rides", methods=["GET"])
@login_required(role="driver")
def driver_history():
    db    = get_db()
    rides = db.execute(
        """SELECT r.*, u.name as rider_name, u.phone as rider_phone
           FROM rides r
           LEFT JOIN riders u ON r.rider_id = u.id
           WHERE r.driver_id=?
           ORDER BY r.created_at DESC""",
        (g.user_id,)
    ).fetchall()
    return jsonify([dict(row) for row in rides])


# ── RIDER PROFILE ───────────────────────────
@app.route("/api/riders/me", methods=["GET"])
@login_required(role="rider")
def rider_profile():
    db    = get_db()
    rider = db.execute(
        "SELECT id, name, email, phone, created_at FROM riders WHERE id=?",
        (g.user_id,)
    ).fetchone()
    return jsonify(dict(rider))


# ── DRIVER PROFILE ──────────────────────────
@app.route("/api/drivers/me", methods=["GET"])
@login_required(role="driver")
def driver_profile():
    db     = get_db()
    driver = db.execute(
        "SELECT id, name, email, phone, car, car_number, status, rating, total_rides, created_at FROM drivers WHERE id=?",
        (g.user_id,)
    ).fetchone()
    return jsonify(dict(driver))


# ── HEALTH CHECK ────────────────────────────
@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()})


# ── GLOBAL ERROR HANDLERS ───────────────────
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    log.exception("Unhandled exception")
    return jsonify({"error": "Internal server error"}), 500


# ──────────────────────────────────────────────
#  RUN
# ──────────────────────────────────────────────
if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(debug=debug, port=5000)
