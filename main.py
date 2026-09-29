import utils.session
import json
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    MAX_LIST_PAGES,
    OUTPUT_DIRECTORY,
    OUTPUT_PREFIX,
    REQUEST_DELAY,
    REQUEST_TIMEOUT,
    SPORT_ID,
    TARGET_COUNTRY,
    TARGET_LIMIT,
    TARGET_PROVINCE,
    TARGET_REGION,
    TARGET_REGION_ID,
    TARGET_REGION_LEVEL,
    USER_LATITUDE,
    USER_LONGITUDE,
)
from geo_utils import build_distance_info, extract_coordinates


BASE_URL = "https://ayo.co.id"
LIST_URL = f"{BASE_URL}/venues"
LIST_PARAMS = {
    "sortby": 5,
    "tipe": "venue",
    "user_lat": "",
    "user_lng": "",
    "nameuser": "",
    "lokasi": f"{TARGET_REGION}, {TARGET_PROVINCE}, {TARGET_COUNTRY}",
    "region_id": TARGET_REGION_ID,
    "region_level": TARGET_REGION_LEVEL,
    "cabor": SPORT_ID,
    "biaya_min": "",
    "biaya_max": "",
}


def next_output_path(directory, prefix, limit):
    directory = Path(directory)
    pattern = re.compile(
        rf"^{re.escape(prefix)}_{limit}_(\d+)\.json$",
        re.I,
    )
    increments = []

    if directory.is_dir():
        for path in directory.iterdir():
            match = pattern.fullmatch(path.name)
            if match:
                increments.append(int(match.group(1)))

    increment = max(increments, default=0) + 1
    return directory / f"{prefix}_{limit}_{increment}.json"


OUTPUT_PATH = next_output_path(
    OUTPUT_DIRECTORY,
    OUTPUT_PREFIX,
    TARGET_LIMIT,
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/149.0 Safari/537.36"
    ),
    "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
}


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


session = build_session()


# =========================================================
# HTTP
# =========================================================


def get_soup(url):
    print(f"GET {url}")
    response = session.get(url, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()
    return BeautifulSoup(response.text, "html.parser")


# =========================================================
# BASIC UTILITIES
# =========================================================


def clean_text(value):
    if not value:
        return None
    return re.sub(r"\s+", " ", str(value)).strip()


def page_lines(soup):
    text = soup.get_text("\n", strip=True)
    lines = []

    for line in text.splitlines():
        cleaned = clean_text(line)
        if cleaned:
            lines.append(cleaned)

    return lines


def parse_number(value):
    if value is None:
        return None

    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def parse_rupiah(value):
    if not value:
        return None

    numbers = re.sub(r"[^\d]", "", str(value))
    return int(numbers) if numbers else None


# =========================================================
# SECTION UTILITIES
# =========================================================


def find_value_after_label(lines, label):
    wanted = label.lower()

    for index, line in enumerate(lines):
        if line.lower() == wanted and index + 1 < len(lines):
            return lines[index + 1]

    return None


def extract_section(lines, start_label, stop_labels):
    result = []
    capture = False
    wanted_start = start_label.lower()
    wanted_stops = {label.lower() for label in stop_labels}

    for line in lines:
        lowered = line.lower()

        if lowered == wanted_start:
            capture = True
            continue

        if capture and lowered in wanted_stops:
            break

        if capture and lowered not in {"baca selengkapnya", "lihat selengkapnya"}:
            result.append(line)

    return clean_text(" ".join(result)) if result else None


# =========================================================
# REGION AND LIST PAGE
# =========================================================


def is_target_region(value):
    if not value:
        return False

    normalized = clean_text(value).lower()
    excluded_regions = (
        "kota tangerang selatan",
        "tangerang selatan",
        "tangsel",
        "kabupaten tangerang",
        "kab. tangerang",
        "kab tangerang",
    )

    if any(region in normalized for region in excluded_regions):
        return False

    return TARGET_REGION.lower() in normalized


def list_page_url(page):
    params = {**LIST_PARAMS, "page": max(1, page)}
    return f"{LIST_URL}?{urlencode(params)}"


def is_venue_detail_url(url):
    parsed = urlparse(url)
    return parsed.netloc in {"ayo.co.id", "www.ayo.co.id"} and bool(
        re.fullmatch(r"/v/[^/]+/?", parsed.path)
    )


def extract_json_ld_venue_urls(soup):
    urls = []

    for script in soup.find_all("script", type="application/ld+json"):
        if not script.string:
            continue

        try:
            payload = json.loads(script.string)
        except (TypeError, json.JSONDecodeError):
            continue

        documents = payload if isinstance(payload, list) else [payload]
        for document in documents:
            if not isinstance(document, dict) or document.get("@type") != "ItemList":
                continue

            for item in document.get("itemListElement", []):
                if not isinstance(item, dict):
                    continue

                url = urljoin(BASE_URL, item.get("url", ""))
                if is_venue_detail_url(url) and url not in urls:
                    urls.append(url)

    return urls


def extract_venue_urls(soup, target_only=True):
    urls = []
    cards = soup.select(".venue-card-item")

    for card in cards:
        link = card.select_one("a[href]")
        if not link:
            continue

        url = urljoin(BASE_URL, link.get("href", ""))
        if not is_venue_detail_url(url):
            continue

        if target_only and not is_target_region(card.get_text(" ", strip=True)):
            continue

        if url not in urls:
            urls.append(url)

    # JSON-LD tidak memuat kota. Gunakan hanya jika markup kartu berubah total;
    # alamat detail tetap diverifikasi sebelum hasil diterima.
    if not cards:
        return extract_json_ld_venue_urls(soup)

    return urls


def get_venue_urls(page):
    soup = get_soup(list_page_url(page))
    return extract_venue_urls(soup, target_only=True)


# =========================================================
# DETAIL FIELDS
# =========================================================


def extract_name(soup):
    h1 = soup.find("h1")
    if h1:
        return clean_text(h1.get_text(" ", strip=True))

    og = soup.find("meta", property="og:title")
    if not og or not og.get("content"):
        return None

    title = re.sub(r"^Booking\s+lapangan\s+", "", og["content"], flags=re.I)
    title = re.sub(r"\s*-\s*Ayo Indonesia.*$", "", title, flags=re.I)
    return clean_text(title)


def extract_address(lines):
    return find_value_after_label(lines, "Lokasi Venue")


def extract_price(text):
    patterns = (
        r"Mulai dari\s*Rp\.?\s*([\d.,]+)",
        r"Mulai\s*Rp\.?\s*([\d.,]+)",
        r"Rp\.?\s*([\d.,]+)\s*(?:Per Sesi|/\s*sesi)",
    )

    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return parse_rupiah(match.group(1))

    return None


def extract_rating(text):
    match = re.search(r"Ulasan\s*([0-5](?:[.,]\d+)?)\s*/\s*5", text, re.I)
    return parse_number(match.group(1)) if match else None


def extract_review_stats(text):
    match = re.search(r"(\d+)\s+rating\s*[·•]\s*(\d+)\s+ulasan", text, re.I)
    if not match:
        return {"rating_count": None, "review_count": None}

    return {
        "rating_count": int(match.group(1)),
        "review_count": int(match.group(2)),
    }


def extract_cleanliness(text):
    match = re.search(r"Kebersihan\s*([0-5](?:[.,]\d+)?)", text, re.I)
    return parse_number(match.group(1)) if match else None


def extract_court_condition(text):
    match = re.search(r"Kondisi Lapangan\s*([0-5](?:[.,]\d+)?)", text, re.I)
    return parse_number(match.group(1)) if match else None


KNOWN_FACILITIES = {
    "Jual Makanan Ringan",
    "Jual Minuman",
    "Jual Bola",
    "Musholla",
    "Parkir Mobil",
    "Parkir Motor",
    "Shower",
    "Hot Shower",
    "Ruang Ganti",
    "Toilet",
    "Cafe & Resto",
    "Wi-fi",
    "WiFi",
    "Tempat Bermain Anak",
    "Locker",
    "Rental Raket",
    "Sewa Raket",
}


def extract_facilities(lines):
    facilities = []
    lower_mapping = {facility.lower(): facility for facility in KNOWN_FACILITIES}

    for line in lines:
        facility = lower_mapping.get(line.strip().lower())
        if facility and facility not in facilities:
            facilities.append(facility)

    return facilities


def extract_description(lines):
    return extract_section(
        lines,
        "Deskripsi",
        ["Aturan Venue", "Lokasi Venue", "Fasilitas", "Mulai dari"],
    )


def extract_rules(lines):
    return extract_section(
        lines,
        "Aturan Venue",
        ["Lokasi Venue", "Fasilitas", "Mulai dari"],
    )


LIGHTING_KEYWORDS = (
    "professional led",
    "led lighting",
    "lighting",
    "pencahayaan",
    "lampu led",
    "lampu",
    "floodlight",
)

SURFACE_KEYWORDS = (
    "synthetic turf",
    "synthetic grass",
    "artificial turf",
    "artificial grass",
    "premium turf",
    "turf",
    "rumput sintetis",
    "rumput",
    "mondo",
)

COACH_KEYWORDS = (
    "coaching",
    "coach available",
    "private coach",
    "private lesson",
    "academy",
    "pelatih tersedia",
    "kelas padel",
)


def find_sentence_by_keywords(text, keywords):
    if not text:
        return None

    for sentence in re.split(r"(?<=[.!?])\s+", text):
        lowered = sentence.lower()
        if any(keyword in lowered for keyword in keywords):
            return clean_text(sentence)

    return None


def extract_lighting(description, rules):
    combined = " ".join(value for value in (description, rules) if value)
    return find_sentence_by_keywords(combined, LIGHTING_KEYWORDS)


def extract_surface(description, rules):
    combined = " ".join(value for value in (description, rules) if value)
    return find_sentence_by_keywords(combined, SURFACE_KEYWORDS)


def extract_coach(description, rules):
    combined = " ".join(value for value in (description, rules) if value)
    evidence = find_sentence_by_keywords(combined, COACH_KEYWORDS)
    return {"available": True if evidence else None, "evidence": evidence}


# =========================================================
# IMAGES
# =========================================================


def normalize_image_url(url):
    if not url:
        return None

    absolute_url = urljoin(BASE_URL, url)
    parsed = urlparse(absolute_url)

    if "/_next/image" in parsed.path:
        original = parse_qs(parsed.query).get("url")
        if original:
            return unquote(original[0])

    return absolute_url


def image_identity(url):
    path = urlparse(url).path.lower()
    return re.sub(r"_(?:large|medium|middle|small)\.jpe?g$", ".jpeg", path)


def extract_images(soup, venue_name):
    images = []
    image_identities = set()
    ignore_keywords = (
        "icon",
        "avatar",
        "logo",
        "banner",
        "wallet",
        "arrow",
        "warning",
        "mobile preview",
        "qr",
    )

    for image in soup.find_all("img"):
        src = image.get("src") or image.get("data-src") or image.get("data-lazy-src")
        if not src:
            continue

        alt = clean_text(image.get("alt") or "") or ""
        alt_lower = alt.lower()
        if any(keyword in alt_lower for keyword in ignore_keywords):
            continue

        is_venue_image = (
            bool(venue_name and venue_name.lower() in alt_lower)
            or "photo" in alt_lower
            or "foto utama venue" in alt_lower
        )
        if not is_venue_image:
            continue

        url = normalize_image_url(src)
        if not url:
            continue

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            continue
        if not parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            continue

        identity = image_identity(url)
        if identity not in image_identities:
            images.append(url)
            image_identities.add(identity)

    return images


# =========================================================
# DETAIL SCRAPER AND OUTPUT
# =========================================================


def scrape_venue(url):
    soup = get_soup(url)
    lines = page_lines(soup)
    full_text = "\n".join(lines)

    name = extract_name(soup)
    address = extract_address(lines)
    description = extract_description(lines)
    rules = extract_rules(lines)
    review_stats = extract_review_stats(full_text)
    latitude, longitude = extract_coordinates(soup)

    if not name:
        raise ValueError(f"Nama venue tidak ditemukan pada {url}")

    venue = {
        "name": name,
        "location": {
            "address": address,
            "city": TARGET_REGION if is_target_region(address) else None,
            "province": TARGET_PROVINCE if is_target_region(address) else None,
            "latitude": latitude,
            "longitude": longitude,
        },
        "criteria": {
            "starting_price_idr": extract_price(full_text),
            "rating": extract_rating(full_text),
            **review_stats,
            "cleanliness_rating": extract_cleanliness(full_text),
            "court_condition_rating": extract_court_condition(full_text),
            "facilities": extract_facilities(lines),
            "lighting_evidence": extract_lighting(description, rules),
            "surface_evidence": extract_surface(description, rules),
            "coach": extract_coach(description, rules),
        },
        "description": description,
        "rules": rules,
        "images": extract_images(soup, name),
        "source": {
            "platform": "AYO Indonesia",
            "url": url,
        },
        "scraped_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    distance = build_distance_info(
        USER_LATITUDE,
        USER_LONGITUDE,
        latitude,
        longitude,
    )
    if distance:
        venue["distance_from_user"] = distance

    return venue


def save_json(data):
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=4, ensure_ascii=False)


def main():
    print("=" * 60)
    print("AYO Indonesia - Padel Court Scraper")
    print(f"Research Area: {TARGET_REGION}")
    print(f"Listing: {list_page_url(1)}")
    print("=" * 60)

    results = []
    visited_urls = set()
    candidate_urls = set()
    rejected_regions = []
    pages_scanned = 0
    listing_exhausted = False

    for page in range(1, MAX_LIST_PAGES + 1):
        if len(results) >= TARGET_LIMIT:
            break

        pages_scanned = page
        print(f"\nScanning listing page {page}")

        try:
            venue_urls = get_venue_urls(page)
        except requests.RequestException as error:
            print(f"ERROR listing page {page}: {error}")
            continue

        if not venue_urls:
            listing_exhausted = True
            print("Tidak ada venue pada halaman ini; pagination selesai.")
            break

        candidate_urls.update(venue_urls)

        for url in venue_urls:
            if len(results) >= TARGET_LIMIT:
                break
            if url in visited_urls:
                continue

            visited_urls.add(url)
            print(f"\nChecking: {url}")

            try:
                venue = scrape_venue(url)
                address = venue.get("location", {}).get("address")

                if not is_target_region(address):
                    rejected_regions.append(
                        {
                            "name": venue.get("name"),
                            "address": address,
                            "url": url,
                        }
                    )
                    print(f"SKIP: bukan {TARGET_REGION}")
                    print(f"Address: {address}")
                else:
                    results.append(venue)
                    save_json(results)
                    print(f"ACCEPT [{len(results)}/{TARGET_LIMIT}]: {venue['name']}")
            except (requests.RequestException, ValueError) as error:
                print(f"ERROR: {url}")
                print(error)

            time.sleep(REQUEST_DELAY)

    save_json(results)

    print("\n" + "=" * 60)
    print(f"Halaman listing diperiksa: {pages_scanned}")
    print(f"Kandidat dari filter AYO: {len(candidate_urls)}")
    print(f"Ditolak karena wilayah detail tidak cocok: {len(rejected_regions)}")
    print(f"Venue valid {TARGET_REGION}: {len(results)}")
    print(f"Output: {OUTPUT_PATH}")

    if rejected_regions:
        print("\nKandidat salah klasifikasi dari AYO:")
        for rejected in rejected_regions:
            print(
                f"- {rejected['name']}: {rejected['address']} "
                f"({rejected['url']})"
            )

    if len(results) < TARGET_LIMIT:
        if listing_exhausted:
            print(
                f"Peringatan: listing AYO selesai sebelum target "
                f"{TARGET_LIMIT} venue tercapai."
            )
        else:
            print(
                f"Peringatan: batas {MAX_LIST_PAGES} halaman tercapai "
                f"sebelum target {TARGET_LIMIT} venue."
            )


if __name__ == "__main__":
    main()
