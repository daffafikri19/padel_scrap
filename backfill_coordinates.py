import argparse
import json
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    BACKFILL_DEFAULT_FILE,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    USER_LATITUDE,
    USER_LONGITUDE,
)
from geo_utils import build_distance_info, extract_coordinates, validate_coordinates


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}
ALLOWED_HOSTS = {"ayo.co.id", "www.ayo.co.id"}


def build_session():
    session = requests.Session()
    session.headers.update(HEADERS)
    retries = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session


def is_allowed_source_url(url):
    if not isinstance(url, str):
        return False

    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() in ALLOWED_HOSTS
        and parsed.path.startswith("/v/")
    )


def load_records(path):
    with path.open(encoding="utf-8") as file:
        records = json.load(file)

    if not isinstance(records, list):
        raise ValueError("Root JSON harus berupa array venue.")

    return records


def write_json_atomic(path, records, create_backup=True):
    backup_path = path.with_suffix(f"{path.suffix}.bak")
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")

    if create_backup and not backup_path.exists():
        shutil.copy2(path, backup_path)
        print(f"Backup: {backup_path}")

    try:
        with temporary_path.open("w", encoding="utf-8") as file:
            json.dump(records, file, indent=4, ensure_ascii=False)

        # Pastikan file hasil benar-benar dapat dibaca sebelum mengganti file asli.
        load_records(temporary_path)
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def has_coordinates(location):
    return (
        isinstance(location, dict)
        and location.get("latitude") is not None
        and location.get("longitude") is not None
    )


def backfill_records(
    records,
    session,
    delay,
    overwrite_coordinates=False,
    user_latitude=None,
    user_longitude=None,
):
    stats = {
        "total": len(records),
        "coordinates_updated": 0,
        "coordinates_existing": 0,
        "distance_updated": 0,
        "failed": 0,
    }
    failures = []

    for index, record in enumerate(records, start=1):
        name = record.get("name") or f"record #{index}"
        location = record.setdefault("location", {})
        latitude = location.get("latitude")
        longitude = location.get("longitude")

        should_fetch = overwrite_coordinates or not has_coordinates(location)
        if should_fetch:
            source_url = record.get("source", {}).get("url")
            if not is_allowed_source_url(source_url):
                stats["failed"] += 1
                failures.append(f"{name}: source URL AYO tidak valid")
                print(f"[{index}/{len(records)}] SKIP {name}: source URL tidak valid")
                continue

            print(f"[{index}/{len(records)}] GET {name}: {source_url}")
            try:
                response = session.get(source_url, timeout=REQUEST_TIMEOUT)
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                latitude, longitude = extract_coordinates(soup)
            except requests.RequestException as error:
                stats["failed"] += 1
                failures.append(f"{name}: {error}")
                print(f"  ERROR: {error}")
                time.sleep(delay)
                continue

            if latitude is None or longitude is None:
                stats["failed"] += 1
                failures.append(f"{name}: koordinat tidak ditemukan")
                print("  ERROR: koordinat tidak ditemukan")
                time.sleep(delay)
                continue

            location["latitude"] = latitude
            location["longitude"] = longitude
            stats["coordinates_updated"] += 1
            print(f"  OK: {latitude}, {longitude}")
            time.sleep(delay)
        else:
            stats["coordinates_existing"] += 1

        distance = build_distance_info(
            user_latitude,
            user_longitude,
            latitude,
            longitude,
        )
        if distance:
            record["distance_from_user"] = distance
            stats["distance_updated"] += 1

    return stats, failures


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Isi koordinat venue dari halaman detail AYO dan, secara opsional, "
            "hitung jarak garis lurus dari pengguna."
        )
    )
    parser.add_argument(
        "json_file",
        nargs="?",
        default=BACKFILL_DEFAULT_FILE,
        help=f"File JSON yang diperbarui (default: {BACKFILL_DEFAULT_FILE})",
    )
    parser.add_argument("--user-latitude", type=float)
    parser.add_argument("--user-longitude", type=float)
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY,
        help=f"Jeda antar-request dalam detik (default: {REQUEST_DELAY})",
    )
    parser.add_argument(
        "--overwrite-coordinates",
        action="store_true",
        help="Ambil ulang koordinat yang sudah terisi.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Proses data tanpa menulis perubahan ke file.",
    )
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Jangan membuat file .bak sebelum menulis.",
    )
    return parser.parse_args()


def resolve_user_coordinates(args):
    latitude = args.user_latitude
    longitude = args.user_longitude

    if latitude is None and longitude is None:
        latitude = USER_LATITUDE
        longitude = USER_LONGITUDE

    if (latitude is None) != (longitude is None):
        raise ValueError(
            "Koordinat pengguna harus menyertakan latitude dan longitude sekaligus."
        )

    if latitude is not None:
        return validate_coordinates(latitude, longitude)

    return None, None


def main():
    args = parse_args()
    path = Path(args.json_file)

    if not path.is_file():
        raise SystemExit(f"File tidak ditemukan: {path}")
    if args.delay < 0:
        raise SystemExit("--delay tidak boleh negatif.")

    try:
        user_latitude, user_longitude = resolve_user_coordinates(args)
        records = load_records(path)
    except (OSError, json.JSONDecodeError, ValueError) as error:
        raise SystemExit(str(error)) from error

    print(f"File: {path}")
    print(f"Total venue: {len(records)}")
    if user_latitude is not None:
        print(f"Koordinat pengguna: {user_latitude}, {user_longitude}")

    stats, failures = backfill_records(
        records,
        build_session(),
        args.delay,
        overwrite_coordinates=args.overwrite_coordinates,
        user_latitude=user_latitude,
        user_longitude=user_longitude,
    )

    changed = stats["coordinates_updated"] or stats["distance_updated"]
    if args.dry_run:
        print("\nDRY RUN: file tidak diubah.")
    elif changed:
        write_json_atomic(path, records, create_backup=not args.no_backup)
        print(f"\nFile diperbarui: {path}")
    else:
        print("\nTidak ada perubahan untuk disimpan.")

    print("\nRingkasan")
    print(f"- Total venue: {stats['total']}")
    print(f"- Koordinat diperbarui: {stats['coordinates_updated']}")
    print(f"- Koordinat sudah ada: {stats['coordinates_existing']}")
    print(f"- Jarak diperbarui: {stats['distance_updated']}")
    print(f"- Gagal: {stats['failed']}")

    if failures:
        print("\nKegagalan:")
        for failure in failures:
            print(f"- {failure}")


if __name__ == "__main__":
    main()
