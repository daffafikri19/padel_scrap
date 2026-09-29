import re
from math import asin, cos, radians, sin, sqrt


OPEN_MAP_PATTERN = re.compile(
    r"open_map\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)",
    re.I,
)
EARTH_RADIUS_KM = 6371.0088


def validate_coordinates(latitude, longitude):
    try:
        latitude = float(latitude)
        longitude = float(longitude)
    except (TypeError, ValueError) as error:
        raise ValueError("Latitude dan longitude harus berupa angka.") from error

    if not -90 <= latitude <= 90:
        raise ValueError(f"Latitude di luar rentang valid: {latitude}")
    if not -180 <= longitude <= 180:
        raise ValueError(f"Longitude di luar rentang valid: {longitude}")

    return latitude, longitude


def extract_coordinates(soup):
    map_container = soup.find(id="map_container")
    if not map_container:
        return None, None

    match = OPEN_MAP_PATTERN.search(map_container.get("onclick", ""))
    if not match:
        return None, None

    try:
        return validate_coordinates(match.group(1), match.group(2))
    except ValueError:
        return None, None


def calculate_distance_km(
    origin_latitude,
    origin_longitude,
    destination_latitude,
    destination_longitude,
    precision=2,
):
    origin_latitude, origin_longitude = validate_coordinates(
        origin_latitude,
        origin_longitude,
    )
    destination_latitude, destination_longitude = validate_coordinates(
        destination_latitude,
        destination_longitude,
    )

    latitude_delta = radians(destination_latitude - origin_latitude)
    longitude_delta = radians(destination_longitude - origin_longitude)
    origin_latitude = radians(origin_latitude)
    destination_latitude = radians(destination_latitude)

    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(origin_latitude)
        * cos(destination_latitude)
        * sin(longitude_delta / 2) ** 2
    )
    haversine = min(1.0, max(0.0, haversine))
    distance = 2 * EARTH_RADIUS_KM * asin(sqrt(haversine))
    return round(distance, precision)


def build_distance_info(
    user_latitude,
    user_longitude,
    venue_latitude,
    venue_longitude,
):
    values = (
        user_latitude,
        user_longitude,
        venue_latitude,
        venue_longitude,
    )
    if any(value is None for value in values):
        return None

    user_latitude, user_longitude = validate_coordinates(
        user_latitude,
        user_longitude,
    )

    return {
        "origin": {
            "latitude": user_latitude,
            "longitude": user_longitude,
        },
        "distance_km": calculate_distance_km(
            user_latitude,
            user_longitude,
            venue_latitude,
            venue_longitude,
        ),
        "method": "haversine",
    }
