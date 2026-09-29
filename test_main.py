import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

import main
from geo_utils import build_distance_info, calculate_distance_km, extract_coordinates


class ParserTests(unittest.TestCase):
    def test_parse_rupiah_supports_common_formats(self):
        self.assertEqual(main.parse_rupiah("Rp 250,000"), 250000)
        self.assertEqual(main.parse_rupiah("Rp250.000"), 250000)
        self.assertIsNone(main.parse_rupiah(None))

    def test_target_region_excludes_neighbouring_administrations(self):
        self.assertTrue(main.is_target_region("Jl. Contoh, Kota Tangerang, Banten"))
        self.assertFalse(main.is_target_region("Kota Tangerang Selatan, Banten"))
        self.assertFalse(main.is_target_region("Kabupaten Tangerang, Banten"))

    def test_output_filename_auto_increment(self):
        with TemporaryDirectory() as directory:
            output_directory = Path(directory)
            (output_directory / "padel_courts_100_1.json").touch()
            (output_directory / "padel_courts_100_3.json").touch()
            (output_directory / "padel_courts_10_99.json").touch()
            (output_directory / "other_100_50.json").touch()

            self.assertEqual(
                main.next_output_path(output_directory, "padel_courts", 100),
                output_directory / "padel_courts_100_4.json",
            )

    def test_listing_url_uses_interactive_ayo_filters(self):
        parsed = urlparse(main.list_page_url(3))
        params = parse_qs(parsed.query)

        self.assertEqual(parsed.path, "/venues")
        self.assertEqual(params["cabor"], ["12"])
        self.assertEqual(params["region_id"], ["01M0RH2700QY851T0H14ME99MG"])
        self.assertEqual(params["region_level"], ["2"])
        self.assertEqual(params["page"], ["3"])
        self.assertEqual(params["lokasi"], ["Kota Tangerang, Banten, Indonesia"])

    def test_extract_venue_urls_filters_listing_cards(self):
        soup = BeautifulSoup(
            """
            <div class="venue-card-item">
                <a href="https://ayo.co.id/v/venue-kota">Venue Kota Tangerang</a>
            </div>
            <div class="venue-card-item">
                <a href="https://ayo.co.id/v/venue-selatan">Venue Kota Tangerang Selatan</a>
            </div>
            <div class="venue-card-item">
                <a href="https://example.com/v/external">Kota Tangerang</a>
            </div>
            """,
            "html.parser",
        )

        self.assertEqual(
            main.extract_venue_urls(soup),
            ["https://ayo.co.id/v/venue-kota"],
        )

    def test_json_ld_is_used_if_card_markup_is_unavailable(self):
        soup = BeautifulSoup(
            """
            <script type="application/ld+json">
            {
                "@type": "ItemList",
                "itemListElement": [
                    {"url": "https://ayo.co.id/v/valid-venue"},
                    {"url": "https://example.com/v/not-ayo"}
                ]
            }
            </script>
            """,
            "html.parser",
        )

        self.assertEqual(
            main.extract_venue_urls(soup),
            ["https://ayo.co.id/v/valid-venue"],
        )

    def test_detail_fields_match_current_ayo_labels(self):
        html = """
        <html>
          <head><meta property="og:title" content="Booking lapangan Test Padel"></head>
          <body>
            <h1>Test Padel</h1>
            <p>Deskripsi</p>
            <p>Lapangan memakai rumput sintetis dan lampu LED.</p>
            <p>Aturan Venue</p>
            <p>Gunakan sepatu olahraga.</p>
            <p>Lokasi Venue</p>
            <p>Jl. Test, Kota Tangerang, Banten, Indonesia</p>
            <p>Fasilitas</p>
            <p>Toilet</p>
            <p>Sewa Raket</p>
            <p>Mulai dari</p>
            <p>Rp 120,000 Per Sesi</p>
            <p>Ulasan 4.8/5</p>
            <p>15 rating · 1 ulasan</p>
            <p>Kebersihan 4.60</p>
            <p>Kondisi Lapangan 4.70</p>
            <div id="map_container" onclick="open_map(-6.22724567, 106.61914341)"></div>
            <img alt="Test Padel" src="https://asset.ayo.co.id/test.jpg_large.jpeg">
            <img alt="Test Padel" src="https://asset.ayo.co.id/test.jpg.jpeg">
            <img alt="Test Padel" src="https://asset.ayo.co.id/">
          </body>
        </html>
        """
        soup = BeautifulSoup(html, "html.parser")
        lines = main.page_lines(soup)
        text = "\n".join(lines)

        self.assertEqual(main.extract_name(soup), "Test Padel")
        self.assertEqual(
            main.extract_address(lines),
            "Jl. Test, Kota Tangerang, Banten, Indonesia",
        )
        self.assertEqual(main.extract_price(text), 120000)
        self.assertEqual(main.extract_rating(text), 4.8)
        self.assertEqual(
            main.extract_review_stats(text),
            {"rating_count": 15, "review_count": 1},
        )
        self.assertEqual(main.extract_facilities(lines), ["Toilet", "Sewa Raket"])
        self.assertIn("lampu LED", main.extract_lighting(main.extract_description(lines), None))
        self.assertIn("rumput sintetis", main.extract_surface(main.extract_description(lines), None))
        self.assertEqual(
            main.extract_images(soup, "Test Padel"),
            ["https://asset.ayo.co.id/test.jpg_large.jpeg"],
        )
        self.assertEqual(extract_coordinates(soup), (-6.22724567, 106.61914341))

    def test_haversine_distance(self):
        self.assertEqual(calculate_distance_km(-6.2, 106.8, -6.2, 106.8), 0.0)
        self.assertEqual(calculate_distance_km(0, 0, 0, 1), 111.2)

        distance = build_distance_info(-6.2, 106.8, -6.3, 106.9)
        self.assertEqual(distance["origin"], {"latitude": -6.2, "longitude": 106.8})
        self.assertGreater(distance["distance_km"], 0)
        self.assertEqual(distance["method"], "haversine")

    def test_invalid_coordinates_are_rejected(self):
        with self.assertRaises(ValueError):
            calculate_distance_km(91, 0, 0, 0)


if __name__ == "__main__":
    unittest.main()
