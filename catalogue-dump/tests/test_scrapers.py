"""Offline tests for the ceramics field parsing and the record contract."""

import json
import tempfile
import time
import timeit
import unittest
from pathlib import Path

from ateliera_catalogue import scrapers
from ateliera_catalogue.scrapers import base, domain, jsonld
from ateliera_catalogue.scrapers import cache as cache_module
from ateliera_catalogue.scrapers import record as record_module

ROOT = Path(__file__).resolve().parent.parent


class SourceConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "sources.json").read_text())

    def test_every_source_names_a_registered_scraper(self):
        # A count, not an exact number: sources are added over time, and a
        # hard-coded total fails on the addition rather than on a real fault.
        self.assertGreaterEqual(len(self.config), 20)
        for name, source in self.config.items():
            with self.subTest(source=name):
                self.assertIn("scraper", source, f"{name} declares no scraper")
                self.assertIn(source["scraper"], scrapers.REGISTRY)
                self.assertTrue(source.get("url", "").startswith("https://"))

    def test_every_registered_scraper_loads(self):
        for name in scrapers.REGISTRY:
            with self.subTest(scraper=name):
                self.assertTrue(callable(scrapers.load(name)))

    def test_robots_may_only_be_ignored_deliberately(self):
        """An ignore_robots source must record why and slow itself down."""
        for name, source in self.config.items():
            if not source.get("ignore_robots"):
                continue
            with self.subTest(source=name):
                self.assertIn("note", source, f"{name} ignores robots.txt without stating why")
                self.assertGreaterEqual(source.get("delay", 0), 2.0)


class FiringRangeTests(unittest.TestCase):
    def test_celsius_range_with_degree_on_the_first_value(self):
        result = domain.firing_range("BOTZ ENGOBE FLORIDA 1180° - 1280°C")
        self.assertEqual((1180, 1280), (result["min_celsius"], result["max_celsius"]))

    def test_cone_range_repeating_the_word(self):
        result = domain.firing_range("fires cone 06 to cone 10")
        self.assertEqual(("06", "10"), (result["cone_min"], result["cone_max"]))
        self.assertEqual("orton", result["cone_system"])
        self.assertEqual((999, 1305), (result["min_celsius"], result["max_celsius"]))

    def test_fahrenheit_is_converted(self):
        result = domain.firing_range("Fires 2000-2232°F")
        self.assertEqual((1093, 1222), (result["min_celsius"], result["max_celsius"]))
        self.assertEqual("F", result["published_unit"])

    def test_segerkegel_is_distinguished_from_orton(self):
        result = domain.firing_range("Glasur SK 6a")
        self.assertEqual("seger", result["cone_system"])
        self.assertEqual(1200, result["min_celsius"])

    def test_multilingual_separators(self):
        for text, expected in (
            ("entre 1220 et 1280°C", (1220, 1280)),
            ("tussen 1000 en 1100°C", (1000, 1100)),
            ("von 1020 und 1080°C", (1020, 1080)),
        ):
            with self.subTest(text=text):
                result = domain.firing_range(text)
                self.assertEqual(expected, (result["min_celsius"], result["max_celsius"]))

    def test_absent_range_is_none(self):
        self.assertIsNone(domain.firing_range("Blue glaze, 473 ml jar"))


class PackageAndUnitPriceTests(unittest.TestCase):
    def test_metric_volume(self):
        package = domain.package_size("473ml jar", liquid_hint=True)
        self.assertEqual(473.0, package["millilitres"])

    def test_fluid_ounces_for_a_liquid(self):
        package = domain.package_size("16 oz", liquid_hint=True)
        self.assertEqual("fl oz", package["unit"])
        self.assertAlmostEqual(473.176, package["millilitres"], places=2)

    def test_ounces_stay_weight_for_a_dry_product(self):
        package = domain.package_size("16 oz", liquid_hint=False)
        self.assertEqual("weight", package["dimension"])
        self.assertTrue(package["unit_ambiguous"])

    def test_named_container_without_a_number(self):
        """US suppliers name the container: "C-01 Obsidian Pint"."""
        pint = domain.package_size("C-01 Obsidian Pint", liquid_hint=True)
        self.assertAlmostEqual(473.176, pint["millilitres"], places=2)
        self.assertEqual("named_container", pint["basis"])
        gallon = domain.package_size("C-01 Obsidian Gallon", liquid_hint=True)
        self.assertAlmostEqual(3785.41, gallon["millilitres"], places=2)

    def test_a_model_number_is_not_a_quantity(self):
        """"SW-229 Pint" is one pint of SW-229, not 229 pints."""
        package = domain.package_size("SW-229 Pint", liquid_hint=True)
        self.assertAlmostEqual(473.176, package["millilitres"], places=2)
        # A genuinely sized pack still wins over the container name.
        self.assertAlmostEqual(946.35, domain.package_size("2 pint jar", liquid_hint=True)["millilitres"], places=1)

    def test_a_ratio_is_a_specification_not_a_package(self):
        """"DENSIMETRE 1000/2000 - 0.010g/ml" measures a density; it is not a 10 mg jar."""
        self.assertIsNone(domain.package_size("DENSIMETRE 1000/2000 - 0.010g/ml Tp.20C"))
        self.assertIsNone(domain.package_size("Engobe 15 g/l dilution"))

    def test_a_multipack_counts_every_unit(self):
        """"36x2,5ml" is a set of 36 pans, so the pack the buyer receives is 90 ml."""
        package = domain.package_size("Akvareliu rinkinys 36x2,5ml", liquid_hint=True)
        self.assertEqual(90.0, package["millilitres"])
        self.assertIn("36", package["evidence"])

    def test_unit_in_the_attribute_name(self):
        package = domain.package_size_from_attributes({"Volume (ml)": "200"}, liquid_hint=True)
        self.assertEqual(200.0, package["millilitres"])

    def test_unit_price_per_litre_and_kilogram(self):
        litre = domain.unit_price(11.8, "EUR", {"dimension": "volume", "millilitres": 200.0})
        self.assertEqual({"value": 59.0, "currency": "EUR", "per": "l"}, litre)
        kilo = domain.unit_price(20.0, "EUR", {"dimension": "weight", "grams": 500.0})
        self.assertEqual({"value": 40.0, "currency": "EUR", "per": "kg"}, kilo)


class ClassificationTests(unittest.TestCase):
    def test_families_across_languages(self):
        for text, expected in (
            ("Emaux transparent brillant", "glaze"),
            ("Glasur glänzend", "glaze"),
            ("Underglaze black", "underglaze"),
            ("Engobe pour grès", "engobe"),
            ("Argile de tournage", "clay_body"),
            ("Oxyde de cobalt", "oxide"),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, domain.family(text))

    def test_description_prose_does_not_reject_a_glaze(self):
        """Glaze copy mentions brushes and kilns; scope must ignore the prose."""
        described = domain.describe(
            "Penguin Pottery Underglaze - Black",
            "It can be brushed or sprayed on. Fire in a kiln to cone 6 on a kiln shelf.",
            ["underglaze-series"],
        )
        self.assertEqual("underglaze", described["family"])
        self.assertTrue(described["is_material"])

    def test_equipment_is_out_of_scope(self):
        self.assertTrue(domain.looks_non_material("Kiln shelf 30cm"))
        self.assertFalse(domain.is_material(None, "Rohde kiln KE 250N"))


class ManufacturerCodeTests(unittest.TestCase):
    def test_code_requires_a_named_manufacturer(self):
        self.assertEqual("SW229", domain.manufacturer_code("Mayco", "Mood Ring", "SW-229"))
        self.assertIsNone(domain.manufacturer_code("Les Cousins", "EMAIL GRES EG140-05B", "EG140-05B"))

    def test_a_firing_temperature_is_not_a_product_code(self):
        self.assertNotEqual(
            "1180", domain.manufacturer_code("Botz", "B9826 BOTZ ENGOBE FLORIDA 1180 - 1280 C", ""),
        )

    def test_zero_padding_is_normalised(self):
        """AMACO publishes C-01 where a reseller writes C-1; both are one product."""
        self.assertEqual("C1", domain.manufacturer_code("AMACO", "C-01 Obsidian Pint", ""))
        self.assertEqual("C1", domain.manufacturer_code("Amaco", "AMACO C-1 OBSIDIAN stoneware glaze", ""))
        self.assertEqual(
            domain.manufacturer_code("Mayco", "COULEUR MAYCO SC075 ORANGE A PEEL", ""),
            domain.manufacturer_code("Mayco", "COULEUR MAYCO SC75 ORANGE A PEEL", ""),
        )
        # Padding is stripped, not every zero: C-10 must not become C-1.
        self.assertEqual("C10", domain.manufacturer_code("AMACO", "C-10 Blue", ""))

    def test_a_bare_word_is_not_a_product_code(self):
        """'SIO' matched every SIO-2 clay and collapsed them into one product."""
        for name in ("SIO-2 FLUMO 1 lt", "SIO-2 ARGILA 5kg Red", "SIO-2 RAKU 12.5kg"):
            with self.subTest(name=name):
                self.assertIsNone(domain.manufacturer_code("SIO-2", name, ""))

    def test_a_curated_code_is_read_when_the_maker_is_named_too(self):
        """`PGV` and `PLV` carry no digit and are not in the PR series.

        On a shop that writes "Sio-2 Maiolica verde PLV" the maker is named and
        the code is right there, but no pattern can express it — so the curated
        vocabulary has to be consulted here as well as when inferring a maker
        from a code, or these clays join nothing.
        """
        for name, expected in (
            ("Sio-2 Maiolica verde PLV 5kg", "PLV"),
            ("Sio-2 Lut pentru veselă PGV 12.5kg", "PGV"),
            ("Pasta Cerâmica SiO-2 PLA Azul – Faiança", "PLA"),
        ):
            with self.subTest(name=name):
                self.assertEqual(expected, domain.manufacturer_code("SiO-2", name, ""))

    def test_the_makers_own_name_is_not_one_of_its_codes(self):
        """`SIO2` is letters-then-digit, exactly like a SIO-2 glaze code.

        So the brand token matched its own pattern, and sixteen unrelated
        products — a red clay, a white one, five chamotte grades, a litre of
        Flumo — promoted into a single canonical product keyed `SIO2`.
        """
        self.assertIsNone(
            domain.manufacturer_code("SiO-2", "SIO2 Chamotte 0 à 0.2mm rouge", "")
        )

    def test_a_real_code_still_wins_after_the_makers_name(self):
        """Rejecting the name must not abandon the search for a real code."""
        self.assertEqual(
            "PRAI", domain.manufacturer_code("SiO-2", "SIO2 PRAI white stoneware", "")
        )

    def test_the_sio_2_clay_series_is_a_code_despite_carrying_no_digit(self):
        """SIO-2 numbers its glazes and names its clay bodies.

        `PRGI` was read as a bare word by the rule above, so SIO-2's own
        catalogue carried no code for its clays and nothing could join a
        retailer's `PRGI` to them.
        """
        for name in ("SIO-2 PRGI stoneware 12.5kg", "SIO-2 PRAI white stoneware 0-0.2mm"):
            with self.subTest(name=name):
                self.assertEqual(
                    name.split()[1], domain.manufacturer_code("SIO-2", name, "")
                )


class ProductLineTests(unittest.TestCase):
    """A product line is a trademark, so naming it names the maker.

    `POTTER'S CHOICE 21 ARCTIC BLUE` on lescousins.fr is AMACO's PC-21 and the
    page says AMACO nowhere, so the glaze never appeared beside the same glaze
    from any other shop.
    """

    def test_a_numbered_line_gives_the_maker_and_the_code(self):
        parsed = domain.parse_title("POTTER’S CHOICE 21 ARCTIC BLUE", supplier_sku="PC_21-0_472")
        self.assertEqual("AMACO", parsed["brand"])
        self.assertEqual("line_named_in_title", parsed["brand_basis"])
        self.assertEqual("PC21", parsed["code"])

    def test_the_number_is_padded_the_same_way_the_maker_pads_it(self):
        """AMACO publishes `PC-01`; the line writes `1`. One product, one key."""
        parsed = domain.parse_title("POTTER’S CHOICE 1 SATURATION METALLIC")
        self.assertEqual("PC1", parsed["code"])

    def test_an_unnumbered_line_still_names_the_maker(self):
        """Colpaert keeps the number in its own reference, `APC70`.

        That is the shop's numbering, so no code is invented from it — but the
        row is still AMACO's.
        """
        parsed = domain.parse_title("POTTERS CHOICE COPPER RED", supplier_sku="APC70")
        self.assertEqual("AMACO", parsed["brand"])
        self.assertIsNone(parsed["code"])

    def test_a_code_in_the_title_is_read_once_the_line_names_the_maker(self):
        parsed = domain.parse_title("SC-58 501 Blues | Stroke & Coat", supplier_sku="100781")
        self.assertEqual("Mayco", parsed["brand"])
        self.assertEqual("SC58", parsed["code"])

    def test_only_a_number_touching_the_line_name_is_its_number(self):
        """`472` is the pack, further along the same title."""
        parsed = domain.parse_title("POTTER’S CHOICE COPPER RED 472 ML")
        self.assertEqual("AMACO", parsed["brand"])
        self.assertIsNone(parsed["code"])

    def test_the_maker_named_outright_still_wins(self):
        parsed = domain.parse_title("AMACO Potter's Choice PC-21 Arctic Blue")
        self.assertEqual("named_in_title", parsed["brand_basis"])

    def test_an_unnumbered_line_never_reads_the_pack_as_a_code(self):
        """Designer Liner titles read "DESIGNER LINER 37 ML BLANC".

        37 is the pack; the code is `SG402` in the shop's reference. Giving the
        line a prefix would make every colour in it the one product `SG37`.
        """
        white = domain.parse_title("DESIGNER LINER 37 ML BLANC", supplier_sku="SG402")
        black = domain.parse_title("DESIGNER LINER 37 ML NOIR", supplier_sku="SG401")
        self.assertEqual("Mayco", white["brand"])
        self.assertEqual("SG402", white["code"])
        self.assertNotEqual(white["code"], black["code"], "two colours are two products")


class NamedManufacturerTests(unittest.TestCase):
    """Makers written into titles all over the dumps and absent from the list.

    The row said who made it and nothing read it.
    """

    def test_makers_seen_in_two_or_more_shops_are_recognised(self):
        for name, expected in (
            ("Segerkegel Orton Standard Nr.03 1085°C", "Orton"),
            ("COULEUR DECOR PORCELAINE SCHJERNING N°102 VERT", "Schjerning"),
            ("COULEUR VITRIFIABLE HERAEUS 64115 BLEU – 10 G", "Heraeus"),
        ):
            with self.subTest(name=name):
                self.assertEqual(expected, domain.parse_title(name)["brand"])

    def test_a_maker_is_not_matched_inside_a_longer_word(self):
        self.assertIsNone(domain.parse_title("Norton abrasive disc")["brand"])


class CodeImpliedManufacturerTests(unittest.TestCase):
    """A code specific enough to name its maker on a page that names nobody.

    lescousins.fr sells SIO-2 clay as `PRAI - GRES REFRACTAIRE COULEUR PIERRE`
    and never writes SIO-2 anywhere, so the maker can only come from the code.
    """

    def test_a_known_code_names_its_maker(self):
        parsed = domain.parse_title(
            "PRAI – GRES REFRACTAIRE COULEUR PIERRE – CHAMOTTE IMPALPABLE 0-0.2 mm",
            supplier_sku="PRAI",
        )
        self.assertEqual("SiO-2", parsed["brand"])
        self.assertEqual("manufacturer_code", parsed["brand_basis"])
        self.assertEqual("PRAI", parsed["code"])

    def test_it_reads_the_code_from_mid_title_too(self):
        parsed = domain.parse_title("GRES BLANCO CHAMOTA FINA PRAF*E", supplier_sku="PRAF")
        self.assertEqual("SiO-2", parsed["brand"])
        self.assertEqual("PRAF", parsed["code"])

    def test_a_longer_word_that_merely_starts_with_a_code_is_not_one(self):
        """`PRAIRIE` is a colour on Les Cousins' own glazes."""
        parsed = domain.parse_title(
            "EMAIL TRANSPARENT T333B VERT PRAIRIE Poids: 25kg", supplier_sku="T333B"
        )
        self.assertIsNone(parsed["brand"])

    def test_lower_case_is_not_a_code(self):
        """Every shop quoting one writes it in capitals, and `pram` is a word."""
        self.assertIsNone(domain.manufacturer_by_code("Baby pram sponge"))

    def test_a_maker_named_on_the_page_outranks_a_code(self):
        """SIO-2 resells Colorobbia's BLS line, so the page is the better source."""
        parsed = domain.parse_title("COLOROBBIA BLS 900 Limoncello 236ml", supplier_sku="900")
        self.assertEqual("Colorobbia", parsed["brand"])
        self.assertEqual("named_in_title", parsed["brand_basis"])

    def test_a_code_outranks_the_shops_own_label(self):
        """A retailer's house brand is what the shop is, not what the product is."""
        parsed = domain.parse_title(
            "PRNI – GRES REFRACTAIRE NOIR", supplier_sku="PRNI", source_brand="Les Cousins"
        )
        self.assertEqual("SiO-2", parsed["brand"])

    def test_a_mention_names_the_maker_without_claiming_the_code(self):
        """Les Cousins sells `PRAIDEFAUT`: the same clay with a voiding defect.

        Its title mentions PRAI and its own reference does not, so it is a SIO-2
        product — but taking PRAI as *its* code makes it and the regular PRAI
        one product to `dedupe_key`, at the same price and pack, and the shop's
        two offers silently become one.
        """
        parsed = domain.parse_title(
            "GRES PRAI PRESENTANT UN DEFAUT DE VIDE – A MALAXER – DESTOCKAGE",
            supplier_sku="PRAIDEFAUT",
        )
        self.assertEqual("SiO-2", parsed["brand"])
        self.assertIsNone(parsed["code"])

    def test_a_shops_own_article_number_does_not_block_the_code(self):
        """e-cibas files the same clay under `10000085-12K`.

        That is the shop's numbering, not a statement about the product, so the
        code is still this row's — and it is the only thing that can join the
        row to the same clay in another shop.
        """
        parsed = domain.parse_title("PRGI", supplier_sku="10000085-12K")
        self.assertEqual("SiO-2", parsed["brand"])
        self.assertEqual("PRGI", parsed["code"])

    def test_the_inferred_maker_does_not_feed_back_into_code_extraction(self):
        """Naming SIO-2 from a code puts SIO-2 in the pattern search's haystack.

        Left alone, `manufacturer_code` then lifts the very code the branch
        above declined to take, straight back out of the title.
        """
        parsed = domain.parse_title(
            "GRES PRAI PRESENTANT UN DEFAUT DE VIDE", supplier_sku="PRAIDEFAUT"
        )
        self.assertNotEqual("manufacturer_pattern", parsed["code_basis"])

    def test_a_line_or_a_resold_range_is_not_a_code(self):
        for text in ("FLUMO 1 lt", "SIO-2 VIVO glaze", "COLOROBBIA BLS 900", "PA/CHF grogged white"):
            with self.subTest(text=text):
                self.assertIsNone(domain.manufacturer_by_code(text))


class ClaimTests(unittest.TestCase):
    def test_positive_food_contact_claim(self):
        claims = domain.claims("", "Lead free and dinnerware safe when fired as directed.")
        self.assertEqual("food_contact_suitability", claims[0]["type"])
        self.assertTrue(claims[0]["claim"])

    def test_negation_in_a_hyphenated_slug(self):
        """Mayco publishes safety as an icon URL; polarity comes from the name."""
        claims = domain.claims("", "Dinnerware Safe: http://example.test/not-dinnerware-safe.png")
        self.assertFalse(claims[0]["claim"])

    def test_a_specification_field_states_a_claim(self):
        """1240.design publishes "Food-safe: yes" as a spec row, not prose."""
        found = {c["type"]: c for c in domain.attribute_claims(
            {"Food-safe": "yes", "Lead free": "no", "Hue": "Black"},
        )}
        self.assertTrue(found["food_contact_suitability"]["claim"])
        self.assertFalse(found["lead_free"]["claim"])
        self.assertEqual("product_attribute", found["food_contact_suitability"]["basis"])

    def test_cookie_tables_are_not_specifications(self):
        """A cookie policy sits in the same markup as a spec table."""
        document = """
          <table><tr><th>Firing temperature</th><td>1200-1240 C</td></tr>
          <tr><th>Cookie name</th><td>Provider Purpose Expiry</td></tr>
          <tr><th>cookiesplus</th><td>Remembers cookie preferences. 1 year</td></tr></table>
        """
        self.assertEqual({"Firing temperature": "1200-1240 C"}, jsonld.specification_table(document))

    def test_claims_always_carry_their_evidence(self):
        for claim in domain.claims("", "Sans plomb, apte au contact alimentaire."):
            self.assertTrue(claim["evidence"])
            self.assertEqual("published_text", claim["basis"])


class HostLimiterTests(unittest.TestCase):
    def test_no_delay_means_no_wait(self):
        """The default is to ask again as soon as the host has answered."""
        limiter = base.HostLimiter(0.0, 8, start=2)
        self.assertEqual(0.0, limiter.spacing("example.com"))
        self.assertEqual(0.0, limiter._jittered("example.com"))

    def test_slots_space_request_starts_when_a_delay_is_asked_for(self):
        """Two slots leaving 0.8 s each is a request start every 0.4 s."""
        limiter = base.HostLimiter(0.8, 4, start=2)
        self.assertAlmostEqual(0.4, limiter.spacing("example.com"))

    def test_a_configured_delay_is_a_floor(self):
        """A slow rate the operator asked for is never divided."""
        limiter = base.HostLimiter(0.8, 4)
        limiter.set_delay("https://example.com/x", 5.0)
        self.assertAlmostEqual(5.0, limiter.spacing("example.com"))
        # The strictest request wins, whichever arrives second.
        limiter.set_delay("https://example.com/y", 2.0)
        self.assertAlmostEqual(5.0, limiter.spacing("example.com"))

    def test_failure_halves_the_slots_and_success_earns_them_back(self):
        limiter = base.HostLimiter(0.8, 8, start=8)
        limiter.record_failure("https://example.com/x", 429)
        self.assertEqual(4, limiter.slots["example.com"])
        limiter.record_failure("https://example.com/x", 503)
        self.assertEqual(2, limiter.slots["example.com"])
        for _ in range(base.HostLimiter.RECOVERY):
            limiter.record_success("https://example.com/x")
        self.assertEqual(3, limiter.slots["example.com"])
        self.assertLessEqual(base.HostLimiter.RECOVERY, 4)

    def test_slots_never_fall_below_one(self):
        limiter = base.HostLimiter(0.8, 4, start=1)
        for _ in range(5):
            limiter.record_failure("https://example.com/x", 500)
        self.assertEqual(1, limiter.slots["example.com"])

    def test_a_failing_host_earns_a_gap_that_doubles_and_is_released(self):
        limiter = base.HostLimiter(0.0, 4, start=4)
        limiter.record_failure("https://example.com/x", 429)
        self.assertAlmostEqual(base.HostLimiter.BACKOFF_START, limiter.spacing("example.com"))
        limiter.record_failure("https://example.com/x", 429)
        self.assertAlmostEqual(base.HostLimiter.BACKOFF_START * 2, limiter.spacing("example.com"))
        # Recovering every lost slot spends the gap the failures earned.
        for _ in range(base.HostLimiter.RECOVERY * 4):
            limiter.record_success("https://example.com/x")
        self.assertEqual(4, limiter.slots["example.com"])
        self.assertEqual(0.0, limiter.spacing("example.com"))

    def test_backoff_stops_doubling_at_the_ceiling(self):
        limiter = base.HostLimiter(0.0, 4)
        for _ in range(20):
            limiter.record_failure("https://example.com/x", 503)
        self.assertAlmostEqual(base.HostLimiter.BACKOFF_MAX, limiter.spacing("example.com"))

    def test_a_published_crawl_delay_applies_only_after_a_failure(self):
        """A healthy host is crawled at our pace; a failing one gets its own."""
        limiter = base.HostLimiter(0.0, 4, start=2)
        limiter.remember_crawl_delay("https://example.com/x", 10.0)
        self.assertEqual(0.0, limiter.spacing("example.com"))
        limiter.record_failure("https://example.com/x", 429)
        self.assertAlmostEqual(10.0, limiter.spacing("example.com"))

    def test_jitter_stays_inside_its_band_and_above_a_floor(self):
        limiter = base.HostLimiter(1.0, 1)  # one slot, so the gap is the delay
        gaps = [limiter._jittered("example.com") for _ in range(200)]
        self.assertTrue(all(0.7 <= gap <= 1.3 for gap in gaps))
        self.assertGreater(len(set(gaps)), 1)
        limiter.set_delay("https://example.com/x", 2.0)
        floored = [limiter._jittered("example.com") for _ in range(200)]
        self.assertTrue(all(gap >= 2.0 for gap in floored))


class TitleTests(unittest.TestCase):
    """The published title, split into the product, its maker and its code."""

    def test_the_raw_title_is_kept_untouched(self):
        raw = "1050 UNDERGLAZE BASE size: $/GAL"
        parsed = domain.parse_title(raw, source_is_manufacturer=True, supplier_sku="1050")
        self.assertEqual(raw, parsed["name_raw"])
        self.assertEqual("UNDERGLAZE BASE", parsed["name"])

    def test_a_manufacturers_own_shop_numbers_its_own_products(self):
        parsed = domain.parse_title(
            "1050 UNDERGLAZE BASE size: $/GAL",
            source_brand="Spectrum", source_is_manufacturer=True, supplier_sku="1050",
        )
        self.assertEqual("1050", parsed["code"])
        self.assertEqual("manufacturer_shop", parsed["code_basis"])

    def test_a_retailers_article_number_is_never_a_manufacturer_code(self):
        """The same shape on a reseller's shelf means nothing to anyone else."""
        parsed = domain.parse_title(
            "011SF2502 - GRES BIANCO 11SF0-0,2 WITGERT", supplier_sku="011SF2502",
        )
        self.assertIsNone(parsed["code"])
        self.assertEqual("Witgert", parsed["brand"])

    def test_a_manufacturer_shop_reselling_another_maker_keeps_its_number(self):
        """SiO-2's article number for a Colorobbia engobe is SiO-2's, not Colorobbia's."""
        parsed = domain.parse_title(
            "COLOROBBIA HC-0607 engobe berenjena 59ml (2oz)",
            source_brand="SiO-2", source_is_manufacturer=True, supplier_sku="303020000607",
        )
        self.assertIsNone(parsed["code"])
        self.assertEqual("Colorobbia", parsed["brand"])

    def test_the_maker_named_in_the_title_beats_the_shops_own_label(self):
        parsed = domain.parse_title(
            "Émail à effets pour grès Amaco - KI18 Artic Blush - 472ml 472",
            published_brand="Harry-Ceradel", package={"value": 472.0},
        )
        self.assertEqual("AMACO", parsed["brand"])
        self.assertEqual("named_in_title", parsed["brand_basis"])
        self.assertEqual("KI18", parsed["code"])
        self.assertEqual("Artic Blush", parsed["name"])

    def test_an_appended_size_field_is_packaging_whatever_follows_it(self):
        """One shop writes "size: $/GAL", another "size: 473 ml"."""
        parsed = domain.parse_title("MBG051 Pumpkin – Coyote size: 473 ml")
        self.assertEqual("Pumpkin", parsed["name"])
        self.assertEqual("Coyote", parsed["brand"])

    def test_the_product_is_read_after_the_code_or_before_it(self):
        after = domain.parse_title("CR-61 Speckled Yellow | AMACO")
        self.assertEqual("Speckled Yellow", after["name"])
        before = domain.parse_title("Émail liquide Terra Color pour faïence Rose FG1061")
        self.assertEqual("FG1061", before["code"])
        self.assertEqual("Émail liquide Terra Color pour faïence Rose", before["name"])

    def test_a_trailing_number_is_only_dropped_when_it_is_the_pack(self):
        """"GRES BIANCO 11" is a product; "Cobalt 472" is a product and a pack."""
        kept = domain.parse_title("GRES BIANCO 11 WITGERT")
        self.assertIn("11", kept["name"])
        dropped = domain.parse_title(
            "Émail brillant Amaco – C20 Cobalt 472", package={"value": 472.0},
        )
        self.assertEqual("Cobalt", dropped["name"])

    def test_a_shop_number_that_is_only_a_database_row_is_rejected(self):
        self.assertFalse(domain.plausible_code("303020000607"))
        self.assertFalse(domain.plausible_code("SPECTRUM", brand="Spectrum"))
        self.assertFalse(domain.plausible_code("BASE"))
        self.assertTrue(domain.plausible_code("1050"))
        self.assertTrue(domain.plausible_code("PC-20"))

    def test_an_empty_parse_never_loses_the_title(self):
        parsed = domain.parse_title("472")
        self.assertEqual("472", parsed["name"])


class ClaimsTests(unittest.TestCase):
    SENTENCE = (
        "Deze glazuur is loodvrij en voedselveilig. "
        "Not dinnerware safe when applied too thickly. "
    )

    def test_the_sentence_around_the_wording_is_the_evidence(self):
        found = {claim["type"]: claim for claim in domain.claims(self.SENTENCE)}
        self.assertIn("lead_free", found)
        self.assertEqual(
            "Deze glazuur is loodvrij en voedselveilig.", found["lead_free"]["evidence"],
        )

    def test_negation_is_read_from_the_wording(self):
        found = {claim["type"]: claim for claim in domain.claims("Not dinnerware safe.")}
        self.assertFalse(found["food_contact_suitability"]["claim"])

    def test_claims_stay_linear_in_the_length_of_the_text(self):
        """A guard on the shape of the patterns, not on the speed of the machine.

        Written as one regex ending in `[^.!?\n]*` this was quadratic and cost
        about six milliseconds per record - ninety-five per cent of all parsing
        time. Ten times the text must cost roughly ten times as much, not a
        hundred, so the ratio is what is asserted.
        """
        short = "Loodvrij glazuur. " + "beschrijving " * 40
        long = "Loodvrij glazuur. " + "beschrijving " * 400
        short_time = min(timeit.repeat(lambda: domain.claims(short), number=20, repeat=3))
        long_time = min(timeit.repeat(lambda: domain.claims(long), number=20, repeat=3))
        self.assertLess(long_time, short_time * 40)


class ResponseCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def cache(self, mode="auto", max_age=None):
        return cache_module.ResponseCache(self.directory.name, mode=mode, max_age=max_age)

    def entry(self, body="<html>hi</html>", url="https://example.com/p.html"):
        return cache_module.CachedResponse(
            status=200, url=url, body=body, headers={"content-type": "text/html"},
            fetched_at=time.time(),
        )

    def test_a_stored_response_comes_back_verbatim(self):
        store = self.cache()
        key = store.key("http", "https://example.com/p.html", method="GET")
        store.write(key, self.entry())
        found = store.read(key, "https://example.com/p.html")
        self.assertEqual("<html>hi</html>", found.body)
        self.assertEqual(1, store.hits)

    def test_the_key_separates_requests_that_differ(self):
        """A page fetched as a browser is not served to the research agent."""
        store = self.cache()
        url = "https://example.com/p.html"
        self.assertNotEqual(
            store.key("http", url, agent=True), store.key("http", url, agent=False),
        )
        self.assertNotEqual(store.key("http", url), store.key("render", url))

    def test_an_entry_older_than_the_max_age_is_a_miss(self):
        store = self.cache(max_age=60)
        key = store.key("http", "https://example.com/p.html")
        stale = self.entry()
        stale.fetched_at = time.time() - 3600
        store.write(key, stale)
        self.assertIsNone(store.read(key, "https://example.com/p.html"))
        self.assertEqual(1, store.misses)

    def test_refresh_ignores_what_is_stored(self):
        store = self.cache(mode="refresh")
        key = store.key("http", "https://example.com/p.html")
        store.write(key, self.entry())
        self.assertIsNone(store.read(key, "https://example.com/p.html"))

    def test_off_stores_nothing(self):
        store = self.cache(mode="off")
        key = store.key("http", "https://example.com/p.html")
        store.write(key, self.entry())
        self.assertFalse(any(Path(self.directory.name).rglob("*.json.gz")))

    def test_a_replay_gap_is_handled_like_any_blocked_fetch(self):
        self.assertTrue(issubclass(base.NotCached, base.Blocked))


class ColourTests(unittest.TestCase):
    def test_supplier_attribute_wins(self):
        self.assertEqual(
            {"name": "Noir", "basis": "product_attribute"},
            domain.colour("EMAIL GRES NOIR EG140-05B DESTOCKAGE", "Noir"),
        )

    def test_code_prefix_is_stripped(self):
        self.assertEqual("Sea Blue", domain.colour("SW-140 Sea Blue")["name"])

    def test_a_named_container_is_not_part_of_the_colour(self):
        """AMACO titles the pack: "PC-2 Saturation Gold Gallon" is one colour."""
        self.assertEqual("Saturation Gold", domain.colour("PC-2 Saturation Gold Gallon")["name"])
        self.assertEqual("Blue Rutile", domain.colour("PC-20 Blue Rutile Pint")["name"])
        self.assertEqual("Obsidian", domain.colour("C-1 Obsidian 473 ml jar")["name"])

    def test_a_long_title_is_not_a_colour(self):
        self.assertIsNone(
            domain.colour("Penguin Pottery - Underglaze for Ceramics - Black - Cone 04 to Cone 6"),
        )


class RecordTests(unittest.TestCase):
    def build(self, **overrides):
        defaults = dict(
            source="test", product_url="https://example.test/products/glaze",
            name="Transparent gloss glaze 500ml", price=12.5, currency="EUR",
            extraction_method="api_json",
        )
        return record_module.build(**{**defaults, **overrides})

    def test_variant_rows_share_a_clean_parent(self):
        row = self.build(
            product_url="https://example.test/p?attribute=25kg", variant_id="42", variant_title="25kg",
        )
        self.assertEqual("test:https://example.test/p#42", row["external_id"])
        self.assertEqual("test:https://example.test/p", row["parent_external_id"])

    def test_identity_rows_carry_no_price(self):
        row = self.build(identity_only=True, price=0.0)
        self.assertEqual(record_module.IDENTITY_FORMAT, row["format"])
        self.assertIsNone(row["price"])
        self.assertTrue(record_module.is_valid(row))

    def test_a_priced_row_needs_a_price(self):
        self.assertFalse(record_module.is_valid(self.build(price=None)))
        self.assertTrue(record_module.is_valid(self.build(price=0.0)))

    def test_derived_fields_are_populated(self):
        row = self.build(name="Emaux transparent brillant 1020-1060°C 500ml")
        self.assertEqual("glaze", row["family"])
        self.assertEqual("gloss", row["surface"])
        self.assertIn("transparent", row["effects"])
        self.assertEqual(1020, row["firing"]["min_celsius"])
        self.assertEqual(500.0, row["package_size"]["millilitres"])
        self.assertEqual(25.0, row["unit_price"]["value"])

    def test_variants_are_not_deduplicated_away(self):
        rows = [
            self.build(variant_id="1", variant_title="500ml", price=12.5),
            self.build(variant_id="2", variant_title="1L", price=22.0),
        ]
        keys = {record_module.dedupe_key(row) for row in rows}
        self.assertEqual(2, len(keys))

    def test_price_fingerprint_tracks_package_changes(self):
        first = self.build(variant_title="500ml")
        second = self.build(variant_title="1L")
        self.assertNotEqual(record_module.price_fingerprint(first), record_module.price_fingerprint(second))


class PriceParsingTests(unittest.TestCase):
    def test_separators_and_symbols(self):
        for text, expected in (
            ("1 234,56 €", (1234.56, "EUR")),
            ("$1,234.56", (1234.56, "USD")),
            ("24.3 EUR", (24.3, "EUR")),
            ("5,22€", (5.22, "EUR")),
        ):
            with self.subTest(text=text):
                self.assertEqual(expected, record_module.parse_price(text))

    def test_vat_wording(self):
        self.assertEqual("exclusive", record_module.vat_status("Prix HT"))
        self.assertEqual("inclusive", record_module.vat_status("Prix TTC"))
        self.assertIsNone(record_module.vat_status("12,50 €"))


class JsonLdTests(unittest.TestCase):
    DOCUMENT = """
      <script type="application/ld+json">
      {"@graph":[{"@type":"BreadcrumbList","itemListElement":[
        {"@type":"ListItem","item":{"name":"Glazes"}}]},
       {"@type":"Product","name":"C-1 Obsidian","sku":"C-1","gtin13":"1234567890123",
        "offers":{"price":"14.26","priceCurrency":"EUR","availability":"https://schema.org/InStock"}}]}
      </script>
      <table><tr><th>Firing</th><td>1220-1260°C</td></tr></table>
    """

    def test_products_are_selected_from_a_graph(self):
        found = jsonld.products(self.DOCUMENT)
        self.assertEqual(["C-1 Obsidian"], [item["name"] for item in found])

    def test_breadcrumbs_and_specification_table(self):
        self.assertEqual(["Glazes"], jsonld.breadcrumbs(self.DOCUMENT))
        self.assertEqual({"Firing": "1220-1260°C"}, jsonld.specification_table(self.DOCUMENT))

    def test_offer_and_gtin(self):
        item = jsonld.products(self.DOCUMENT)[0]
        self.assertEqual("14.26", jsonld.offer(item)["price"])
        self.assertEqual("1234567890123", jsonld.gtin(item))


if __name__ == "__main__":
    unittest.main()
