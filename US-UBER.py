import sqlite3
import sys
import folium
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
import folium
import requests
conn = sqlite3.connect("uber.db")
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS ride (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT,
    email TEXT,
    password TEXT, 
    phone TEXT
    )
""")
cursor.execute("""
    CREATE TABLE IF NOT EXISTS driver (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name_driver TEXT,
    phone_driver TEXT,
    car TEXT,
    car_number TEXT,
    status TEXT
    )

""")

print("\n Swagatam! to US-UBER🙏")
user = input("Who you are: Rider/Driver🤔 \n:")

if(user == "driver"): # status:- available or not
    print("Earn with US-UBER\nDecide when, where, and how you want to earn")
    name = input("Driver name: ")
    phone = input("Phone: ")
    car = input("Car name(CAR🚗): ")
    car_number = input("Your CAR's registered number: ")
    status = input("'available'/'busy': ")
    cursor.execute(
        "INSERT INTO driver (name_driver, phone_driver, car, car_number, status) VALUES (?, ?, ?, ?, ?)",
        (name, phone, car, car_number , status)
        )
    conn.commit()
    print("✅Driver added succesfully!")
    if status == 'available':
        print("As any rider was find we assigned you😀\n" , "We are finding.....")
    else:
        sys.exit()

elif user == "rider":
    user_input = input("\nDo you have alredy account🤔?(yes/no): ")
    if user_input == 'yes':
        user_1 = input("\nEnter your e-mail👉 ")
        user_2 = int(input("\nEnter your mobile number👉 "))

        cursor.execute("SELECT * FROM ride WHERE email=? OR phone=?", (user_1, user_2))
        user = cursor.fetchone()
        if user:
            print("\n✅ Welcome back,", user[1])  
        else:
            print("❌ User not found, please register")
            sys.exit()
    else:
        print("🆕 New user, please register")
        name = input("\nYour name: ")
        email = input("\nYour email: ")
        password = input("\nYour google password: ")
        phone = input("\nYour phone: ")
        if len(phone) != 10 or not phone.isdigit():
            print("❌ Invalid input")
            sys.exit()

        cursor.execute("INSERT INTO ride (name, email, phone , password) VALUES (?, ?, ?, ?)", (name, email, phone , password))
        conn.commit()

        print("✅ You loged in successfully!\n")
        print("--------------------------")
        print("Namaste ,👋" , name)
# DATABASE KA KAAM KHATAM:-
        

    print("Request a ride🚗")
    # conn = sqlite3.connect("driver.db")
    cursor.execute("SELECT * FROM driver WHERE status='available' LIMIT 1")
    driver = cursor.fetchone()
    # conn = sqlite3.connect("uber.db")
    you = input("Enter locaion📍: ")
    destination = input("Enter destination🗺️: ")
    if driver:
        print("🚗 Driver found!\n")
        print(f"Driver Name: {driver[1]}\n")
        print(f"Car: {driver[3]}\n")
        print(f"Car number: {driver[4]}\n")
        print(f"Phone: {driver[2]}\n")

        cursor.execute("UPDATE driver SET status='busy' WHERE id=?", (driver[0],))
        conn.commit()

        import random
        arrival_time = random.randint(3, 10)
        print(f"🚗 Driver will arrive in {arrival_time} minutes\n")


    else:
        print("Driver was not available❌")
# driver ka kaam khatam ✔
# map ka kaam:-
    geo = Nominatim(user_agent="my_app")

    location = geo.geocode(you)
    loco = geo.geocode(destination)
    if location and loco:
        m = folium.Map(location=[location.latitude, location.longitude], zoom_start=13)

    # Pickup marker
        folium.Marker(
            location=[location.latitude, location.longitude],
            popup="Pickup Location 📍",
            tooltip="Pickup"
        ).add_to(m)

    # Drop marker
        folium.Marker(
            location=[loco.latitude, loco.longitude],
            popup="Drop Location 🔵",
            tooltip="Drop"
        ).add_to(m)



# OSRM API URL
        url = f"http://router.project-osrm.org/route/v1/driving/{location.longitude},{location.latitude};{loco.longitude},{loco.latitude}?overview=full&geometries=geojson"

        response = requests.get(url)
        data = response.json()

# Extract route coordinates
        route = data['routes'][0]['geometry']['coordinates']

# Convert (lon, lat) → (lat, lon)
        route_coords = [(coord[1], coord[0]) for coord in route]

# Draw route
        folium.PolyLine(route_coords, color="black", weight=5).add_to(m)


        m.save("map.html")

        import webbrowser
        webbrowser.open("map.html")

    else:   
        print("❌ Location not found")

    place1 = you
    place2 = destination

# Convert to coordinates
    loc1 = geo.geocode(place1)
    loc2 = geo.geocode(place2)

    if loc1 and loc2:
        coord1 = (loc1.latitude, loc1.longitude)
        coord2 = (loc2.latitude, loc2.longitude)

    # Calculate distance
        distance = geodesic(coord1, coord2).kilometers

        print(f"Distance between {place1} and {place2} is {distance:.2f} km\n")
        base_fare = 40
        fare_per_km = 10
        normal_speed = 40
        total_fare = base_fare + (distance * fare_per_km)
        time     = distance / normal_speed
        print(f"Please pay {total_fare} to driver🙏\n")
        print("We hope your ride was greatfull..😊\n")
        time_minutes = (distance / normal_speed) * 60
        print(f"Estimated time to reach your destinaton was: {time_minutes:.0f} minutes ⏱️\n")

    else:
        print("❌ One of the locations not found")
        cursor.execute("DELETE FROM ride")
        sys.exit()

    conn.close()  





