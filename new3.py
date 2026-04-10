import sqlite3
from flask import Flask, request, jsonify
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import requests

app = Flask(__name__)

# ---------------- DB ----------------
def get_db():
    conn = sqlite3.connect("us_uber.db")
    conn.row_factory = sqlite3.Row
    return conn


# ---------------- INIT DB ----------------
def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        email TEXT,
        phone TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS drive (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name_driver TEXT,
        phone_driver TEXT,
        car TEXT,
        car_number TEXT,
        status TEXT
    )
    """)

    # ✅ NEW TABLE ADDED (RIDE HISTORY)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS rides (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rider_phone TEXT,
        driver_id INTEGER,
        pickup TEXT,
        destination TEXT,
        distance REAL,
        fare REAL,
        eta REAL,
        status TEXT
    )
    """)

    conn.commit()
    conn.close()

init_db()


# ---------------- GEO ----------------
geo = Nominatim(user_agent="uber_app")


def safe_geocode(place):
    try:
        loc = geo.geocode(place)
        if loc:
            return loc
    except:
        pass
    return None


# ---------------- REGISTER DRIVER ----------------
@app.route("/register_driver", methods=["POST"])
def register_driver():
    data = request.json

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO drive (name_driver, phone_driver, car, car_number, status)
        VALUES (?, ?, ?, ?, ?)
    """, (
        data["name"],
        data["phone"],
        data["car"],
        data["car_number"],
        data["status"]
    ))

    conn.commit()
    conn.close()

    return jsonify({"message": "Driver registered successfully 🚗"})


# ---------------- REGISTER RIDER ----------------
@app.route("/register_rider", methods=["POST"])
def register_rider():
    data = request.json

    if len(data["phone"]) != 10:
        return jsonify({"error": "Invalid phone number"}), 400

    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO user (name, email, phone)
        VALUES (?, ?, ?)
    """, (data["name"], data["email"], data["phone"]))

    conn.commit()
    conn.close()

    return jsonify({"message": "Rider registered successfully ✅"})


# ---------------- FIND BEST DRIVER ----------------
def find_nearest_driver(pickup_lat, pickup_lon):
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM drive WHERE status='available'")
    drivers = cursor.fetchall()

    conn.close()

    if not drivers:
        return None

    return drivers[0]  # (kept same logic style)


# ---------------- BOOK RIDE ----------------
@app.route("/book_ride", methods=["POST"])
def book_ride():
    data = request.json

    pickup = safe_geocode(data["pickup"])
    destination = safe_geocode(data["destination"])

    if not pickup or not destination:
        return jsonify({"error": "Location not found ❌"}), 400

    pickup_coord = (pickup.latitude, pickup.longitude)
    dest_coord = (destination.latitude, destination.longitude)

    driver = find_nearest_driver(pickup.latitude, pickup.longitude)

    if not driver:
        return jsonify({"error": "No driver available ❌"}), 400

    distance = geodesic(pickup_coord, dest_coord).km
    fare = 40 + (distance * 10)
    eta = (distance / 40) * 60

    # OSRM route
    try:
        url = f"http://router.project-osrm.org/route/v1/driving/{pickup.longitude},{pickup.latitude};{destination.longitude},{destination.latitude}?overview=full&geometries=geojson"
        res = requests.get(url)
        route_data = res.json()

        route = route_data["routes"][0]["geometry"]["coordinates"]
        route_coords = [[c[1], c[0]] for c in route]
    except:
        route_coords = []

    # mark driver busy
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("UPDATE drive SET status='busy' WHERE id=?", (driver["id"],))

    # ✅ SAVE RIDE INFO (NEW PART)
    cursor.execute("""
        INSERT INTO rides (
            rider_phone, driver_id, pickup, destination,
            distance, fare, eta, status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        data["phone"],
        driver["id"],
        data["pickup"],
        data["destination"],
        distance,
        fare,
        eta,
        "ongoing"
    ))

    conn.commit()
    conn.close()

    return jsonify({
        "driver": {
            "name": driver["name_driver"],
            "phone": driver["phone_driver"],
            "car": driver["car"],
            "car_number": driver["car_number"]
        },
        "trip": {
            "distance_km": round(distance, 2),
            "fare": round(fare, 2),
            "eta_min": round(eta, 2)
        },
        "route": route_coords
    })


# ---------------- RUN ----------------
if __name__ == "__main__":
    app.run(debug=True)