"""Ceramics-domain field parsing shared by every supplier scraper.

Suppliers publish the same physical facts in different languages and units. This
module turns the published wording into comparable fields and always keeps the
matched evidence text, so an operator can audit a value instead of trusting it.
"""

from __future__ import annotations

import html
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urljoin, urlparse

# Orton pyrometric cones, self-supporting, 108 F/hour rise, peak temperature in C.
# Used both to read a published cone and to suggest one from a published range.
CONE_CELSIUS: dict[str, int] = {
    "022": 586, "021": 600, "020": 626, "019": 678, "018": 715, "017": 738,
    "016": 772, "015": 791, "014": 807, "013": 837, "012": 858, "011": 873,
    "010": 887, "09": 928, "08": 954, "07": 985, "06": 999, "05": 1046,
    "04": 1070, "03": 1101, "02": 1120, "01": 1137, "1": 1154, "2": 1162,
    "3": 1186, "4": 1196, "5": 1207, "6": 1231, "7": 1255, "8": 1269,
    "9": 1285, "10": 1305, "11": 1315, "12": 1326, "13": 1346, "14": 1366,
}

# Seger/Segerkegel numbering used by the German-language suppliers.
SEGER_CELSIUS: dict[str, int] = {
    "022": 600, "021": 620, "020": 640, "019": 660, "018": 680, "017": 700,
    "016": 720, "015": 740, "014": 760, "013": 800, "012": 840, "011": 880,
    "010": 900, "09": 920, "08": 940, "07": 960, "06": 980, "05": 1000,
    "04": 1020, "03": 1040, "02": 1060, "01": 1080, "1": 1100, "2": 1120,
    "3": 1140, "4": 1160, "5": 1180, "6": 1200, "7": 1230, "8": 1250,
    "9": 1280, "10": 1300, "11": 1320, "12": 1350, "13": 1380, "14": 1410,
}

VOLUME_ML = {
    "ml": 1.0, "millilitre": 1.0, "milliliter": 1.0, "cc": 1.0,
    "cl": 10.0, "dl": 100.0,
    "l": 1000.0, "lt": 1000.0, "ltr": 1000.0, "litre": 1000.0, "liter": 1000.0,
    "fl oz": 29.5735, "floz": 29.5735, "fl.oz": 29.5735, "oz fl": 29.5735,
    "pint": 473.176, "pt": 473.176, "quart": 946.353, "qt": 946.353,
    "gallon": 3785.41, "gal": 3785.41,
}

WEIGHT_G = {
    "g": 1.0, "gr": 1.0, "gram": 1.0, "gramme": 1.0, "gramm": 1.0,
    "kg": 1000.0, "kilo": 1000.0, "kilogram": 1000.0, "kilogramme": 1000.0,
    "oz": 28.3495, "ounce": 28.3495,
    "lb": 453.592, "lbs": 453.592, "pound": 453.592,
}

# Ounces are ambiguous: US glaze jars are fluid ounces, dry materials are weight.
# Only treat a bare "oz" as volume when the wording or product family says liquid.
_AMBIGUOUS_OUNCE = re.compile(r"\boz\b", re.I)

FAMILY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("underglaze", (
        "underglaze", "under glaze", "sous-email", "sous email", "sous-emaux",
        "unterglasur", "onderglazuur", "sottosmalto", "bajo cubierta",
        "podglazur", "underglasyr", "engobe de decor",
    )),
    ("engobe", ("engobe", "angobe", "angoba", "slip trailing", "barbotine", "casting slip")),
    ("glaze", (
        "glaze", "email", "emaux", "glasur", "glazuur", "glazura", "glazūra",
        "smalto", "esmalte", "glasyr", "couverte", "cristalline", "raku glaze",
        # "szkliw" covers the Polish declensions (szkliwo, szkliwa, szkliwie)
        "szkliw", "vidrado", "poleva",
    )),
    ("stain", (
        "stain", "colorant", "pigment", "farbkorper", "kleurstof", "teinte",
        "body stain", "mason stain", "dekorfarbe",
    )),
    ("oxide", ("oxide", "oxyde", "oxid", "ossido", "oxido", "carbonate", "carbonat")),
    ("clay_body", (
        "clay", "argile", "terre ", "terre-", "ton ", "klei", "molis", "argilla",
        "arcilla", "lera", "glinka", "masa ceramiczna", "stoneware", "earthenware", "gres", "grès", "faience",
        "faïence", "steingut", "steinzeug", "porcelain", "porcelaine", "porzellan",
        "porselein", "porcellana", "paperclay", "modelling clay", "pate ", "pâte ",
        "impasto", "massa", "masse", "stengods", "cloisonne",
    )),
    ("raw_material", (
        "feldspar", "feldspath", "feldspat", "kaolin", "silica", "silice",
        "quartz", "quarz", "frit", "fritte", "bentonite", "talc", "whiting",
        "dolomite", "wollastonite", "nepheline", "grog", "chamotte", "ball clay",
        "petalite", "spodumene", "zirconium", "zircon", "borax", "gerstley",
    )),
]

# Categories and products that are outside a ceramic-materials scope.
NON_MATERIAL_KEYWORDS: tuple[str, ...] = (
    "kiln", "four ", "ofen", "brennofen", "oven", "wheel", "tour de potier",
    "drehscheibe", "draaischijf", "extruder", "pugmill", "slab roller",
    "brush", "pinceau", "pinsel", "penseel", "tool", "outil", "werkzeug",
    "book", "livre", "buch", "dvd", "apron", "tablier", "banding wheel",
    "bisque", "biscuit", "shelf", "plaque d'enfournement", "regal", "prop",
    "element", "resistance", "thermocouple", "controller", "regulateur",
    "sieve", "tamis", "sieb", "scale", "balance", "waage", "respirator",
    "masque", "gloves", "gants", "sponge", "eponge", "schwamm", "turntable",
    "gift card", "carte cadeau", "voucher", "sample pack", "spare part",
)

SURFACE_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("gloss", ("gloss", "brillant", "glanz", "glänzend", "glanzend", "lucido", "blank", "brillante", "blizgus")),
    ("satin", ("satin", "satine", "satiné", "seiden", "zijde", "satinato", "satinado", "halbmatt")),
    ("matte", ("matte", "matt", "mat ", "mate", "opaco", "mattes", "matinis")),
    ("crystalline", ("crystal", "cristal", "kristall", "cristallin", "kristal")),
    ("metallic", ("metallic", "metallique", "métallique", "metallisch", "metallico", "lustre", "luster")),
    ("textured", ("texture", "textur", "structuur", "struttura", "rough", "stone effect")),
]

EFFECT_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("transparent", ("transparent", "transparente", "trasparente", "doorzichtig", "klar", "clear")),
    ("opaque", ("opaque", "opak", "dekkend", "coprente", "opaco")),
    ("speckled", ("speckle", "mouchete", "moucheté", "gesprenkelt", "gespikkeld", "screziato", "flecked")),
    ("flowing", ("flowing", "coulant", "laufend", "fliess", "vloeiend", "runny", "colante")),
    ("crackle", ("crackle", "craquele", "craquelé", "krakelee", "craquelure", "crackled")),
    ("iridescent", ("iridescent", "irise", "irisé", "schillernd", "iridescente")),
    ("breaking", ("breaks", "break over", "bricht", "breking")),
    ("mottled", ("mottled", "marbre", "marbré", "gewolkt", "marmorizzato")),
]

APPLICATION_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("brushing", ("brush", "pinceau", "pinsel", "penseel", "pennello", "brossage", "brushing")),
    ("dipping", ("dip", "trempage", "tauch", "dompel", "immersione", "dipping")),
    ("pouring", ("pour", "coulage", "giess", "gieß", "gieten", "colatura", "pouring")),
    ("spraying", ("spray", "pistolet", "spritz", "sproei", "spruzzo", "airbrush", "spraying")),
]

#: The wording that makes a claim. Only the keywords are matched; the sentence
#: around a hit is cut out afterwards by looking for the nearest sentence
#: boundary. Written as one regex ending in `[^.!?\n]*` it was quadratic — the
#: engine retried the whole leading run at every position — and cost about six
#: milliseconds per record, which was ninety-five per cent of all parsing time.
CLAIM_KEYWORDS: list[tuple[str, str]] = [
    ("food_contact_suitability", (
        r"(?:food[\s-]?safe|dinnerware[\s-]?safe|food[\s-]?contact|"
        r"contact alimentaire|apte au contact alimentaire|alimentaire|"
        r"lebensmittel(?:echt|geeignet|sicher)|voedselveilig|voedsel geschikt|"
        r"idoneo al contatto alimentare|uso alimentare|apto para alimentos|"
        r"livsmedelsgodk[aä]nd|tinka maistui)"
    )),
    ("lead_free", (
        r"(?:lead[\s-]?free|sans plomb|bleifrei|loodvrij|senza piombo|"
        r"sin plomo|blyfri)"
    )),
    ("cadmium_free", r"(?:cadmium[\s-]?free|sans cadmium|cadmiumfrei|senza cadmio)"),
    ("non_toxic", (
        r"(?:non[\s-]?toxic|atoxique|non toxique|ungiftig|niet giftig|"
        r"atossico|no t[oó]xico|giftfri)"
    )),
    ("standard_conformity", r"(?:ASTM\s*D[\s-]?4236|EN\s*71(?:[\s-]?3)?|DIN\s*EN\s*71)"),
    ("certification_mark", r"(?:ACMI|AP\s+Seal|CL\s+Seal)"),
    ("dishwasher_safe", (
        r"(?:dishwasher[\s-]?safe|lave[\s-]?vaisselle|sp[uü]lmaschinen(?:fest|geeignet)|"
        r"vaatwasserbestendig|lavastoviglie)"
    )),
    ("microwave_safe", r"(?:microwave[\s-]?safe|micro[\s-]?ondes|mikrowellen(?:fest|geeignet)|magnetron)"),
]

CLAIM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (claim_type, re.compile(pattern, re.I)) for claim_type, pattern in CLAIM_KEYWORDS
]

#: Where one sentence ends and the next begins.
SENTENCE_END = ".!?\n"


def sentence_around(text: str, start: int, end: int) -> str:
    """The sentence a match sits in, found by scanning out to its boundaries."""
    left = max((text.rfind(mark, 0, start) for mark in SENTENCE_END), default=-1)
    right = min(
        (position for position in (text.find(mark, end) for mark in SENTENCE_END) if position != -1),
        default=len(text) - 1,
    )
    return text[left + 1:right + 1]


DOCUMENT_PATTERNS: list[tuple[str, str]] = [
    ("safety_data_sheet", r"\b(?:msds|sds|safety data|fiche de s[eé]curit[eé]|s[eé]curit[eé]|sicherheitsdatenblatt|veiligheidsblad|scheda di sicurezza)\b"),
    ("technical_sheet", r"\b(?:tds|technical data|technical sheet|fiche technique|datenblatt|technisch|scheda tecnica|ficha t[eé]cnica)\b"),
    ("certificate", r"\b(?:certificat|certificate|zertifikat|certificaat|certificato)\b"),
    ("lab_report", r"\b(?:lab(?:oratory)? (?:report|result)|rapport d'analyse|pr[uü]fbericht)\b"),
    ("instructions", r"\b(?:instruction|mode d'emploi|anleitung|handleiding|istruzioni)\b"),
]

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def clean(value: Any) -> str:
    """Strip markup and collapse whitespace, mirroring the importer's behaviour."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub(" ", str(value or "")))).strip()


def fold(value: Any) -> str:
    """Accent-insensitive, case-insensitive text for keyword matching."""
    text = clean(value).casefold()
    for accented, plain in (
        ("àâäá", "a"), ("èéêë", "e"), ("îïí", "i"), ("ôöó", "o"),
        ("ûüùú", "u"), ("ç", "c"), ("ñ", "n"), ("ø", "o"), ("å", "a"), ("æ", "ae"),
    ):
        for character in accented:
            text = text.replace(character, plain)
    return text


def _number(value: str) -> float | None:
    """Read a decimal written with either a comma or a dot separator."""
    text = value.strip().replace(" ", "")
    if text.count(",") and text.count("."):
        text = text.replace(".", "").replace(",", ".") if text.rindex(",") > text.rindex(".") else text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def cone_sort_key(label: str) -> int:
    """Order cones on one axis: 022 is the coldest, 14 the hottest."""
    normalized = label.strip().lstrip("^Δ").strip()
    if normalized.startswith("0") and len(normalized) > 1:
        return -int(normalized[1:])
    try:
        return int(normalized)
    except ValueError:
        return 0


def _fahrenheit_to_celsius(value: float) -> int:
    return round((value - 32) * 5 / 9)


def firing_range(*texts: Any) -> dict[str, Any] | None:
    """Read a firing window as temperature and/or pyrometric cone.

    Handles Celsius and Fahrenheit ranges, Orton cones ("cone 05-6", "^6") and
    German Segerkegel ("SK 6a"). The matched wording is kept as evidence.
    """
    haystack = " \n ".join(clean(text) for text in texts if text)
    if not haystack:
        return None

    result: dict[str, Any] = {}
    evidence: list[str] = []

    # The degree sign may sit on the first number alone ("1180° - 1280°C"), so the
    # unit after the lower bound is optional in both parts.
    separator = (
        r"(?:[-–—]|\bto\b|\ba\b|\bbis\b|\btot\b|\bà\b|\bligi\b|\bhasta\b"
        r"|\bet\b|\bund\b|\ben\b|\be\b|\by\b|\bir\b|\boch\b)"
    )
    celsius = re.search(
        rf"(?P<min>\d{{3,4}})\s*(?:[º°]\s*C?)?\s*{separator}\s*(?P<max>\d{{3,4}})\s*[º°]?\s*C\b",
        haystack, re.I,
    )
    single_celsius = None if celsius else re.search(r"(?P<value>\d{3,4})\s*[º°]\s*C\b", haystack, re.I)
    fahrenheit = re.search(
        rf"(?P<min>\d{{3,4}})\s*(?:[º°]\s*F?)?\s*{separator}\s*(?P<max>\d{{3,4}})\s*[º°]?\s*F\b",
        haystack, re.I,
    )
    single_fahrenheit = None if fahrenheit else re.search(r"(?P<value>\d{3,4})\s*[º°]\s*F\b", haystack, re.I)

    if celsius:
        result["min_celsius"] = int(celsius.group("min"))
        result["max_celsius"] = int(celsius.group("max"))
        evidence.append(celsius.group(0))
    elif fahrenheit:
        result["min_celsius"] = _fahrenheit_to_celsius(int(fahrenheit.group("min")))
        result["max_celsius"] = _fahrenheit_to_celsius(int(fahrenheit.group("max")))
        result["published_unit"] = "F"
        evidence.append(fahrenheit.group(0))
    elif single_celsius:
        result["min_celsius"] = result["max_celsius"] = int(single_celsius.group("value"))
        evidence.append(single_celsius.group(0))
    elif single_fahrenheit:
        value = _fahrenheit_to_celsius(int(single_fahrenheit.group("value")))
        result["min_celsius"] = result["max_celsius"] = value
        result["published_unit"] = "F"
        evidence.append(single_fahrenheit.group(0))

    # "cone 06 to cone 10" repeats the word before the upper bound.
    cone = re.search(
        rf"(?:cone|c[oó]ne|kegel|konus|\^|Δ)\s*[:.]?\s*(?P<min>0?\d{{1,2}})"
        rf"(?:\s*{separator}\s*(?:cone|c[oó]ne|kegel|konus|\^|Δ)?\s*(?P<max>0?\d{{1,2}}))?",
        haystack, re.I,
    )
    if cone and cone.group("min") in CONE_CELSIUS:
        result["cone_min"] = cone.group("min")
        result["cone_max"] = cone.group("max") if cone.group("max") in CONE_CELSIUS else cone.group("min")
        result["cone_system"] = "orton"
        evidence.append(cone.group(0))
    seger = re.search(r"\bSK\s*(?P<min>0?\d{1,2})[a-c]?(?:\s*[-–—]\s*(?P<max>0?\d{1,2})[a-c]?)?", haystack, re.I)
    if seger and not cone and seger.group("min") in SEGER_CELSIUS:
        result["cone_min"] = seger.group("min")
        result["cone_max"] = seger.group("max") if seger.group("max") in SEGER_CELSIUS else seger.group("min")
        result["cone_system"] = "seger"
        evidence.append(seger.group(0))
        if "min_celsius" not in result:
            result["min_celsius"] = SEGER_CELSIUS[result["cone_min"]]
            result["max_celsius"] = SEGER_CELSIUS[result["cone_max"]]
            result["temperature_basis"] = "derived_from_cone"

    if "cone_min" in result and "min_celsius" not in result and result.get("cone_system") == "orton":
        result["min_celsius"] = CONE_CELSIUS[result["cone_min"]]
        result["max_celsius"] = CONE_CELSIUS[result["cone_max"]]
        result["temperature_basis"] = "derived_from_cone"

    if not result:
        return None

    atmosphere = None
    lowered = fold(haystack)
    if re.search(r"\breduction\b|\breduzier|\breductie\b|riduzione", lowered):
        atmosphere = "reduction"
    elif re.search(r"\boxidation\b|\boxidierend|\boxydation\b|ossidazione", lowered):
        atmosphere = "oxidation"
    if re.search(r"\braku\b", lowered):
        atmosphere = "raku"
    if atmosphere:
        result["atmosphere"] = atmosphere

    result["evidence"] = " | ".join(dict.fromkeys(evidence))[:300]
    result.setdefault("basis", "published_text")
    return result


def package_size(*texts: Any, liquid_hint: bool = False) -> dict[str, Any] | None:
    """Read a package volume or weight and normalise it to ml or g."""
    for text in texts:
        candidate = clean(text)
        if not candidate:
            continue
        # "36x2,5ml" hides the size behind a letter, which the lookbehind below
        # treats as part of a word. Spacing the multiplier out exposes both.
        candidate = re.sub(r"(?<=\d)\s*[x\u00d7*]\s*(?=\d)", " \u00d7 ", candidate)
        # A hyphen before the digits means a model number, not a quantity:
        # "SW-229 Pint" is one pint of SW-229, not 229 pints.
        pattern = (
            r"(?<![\w.,-])(?P<value>\d+(?:[.,]\d+)?)\s*"
            r"(?P<unit>fl\.?\s?oz|floz|ml|cl|dl|lt?r?|litre|liter|pint|pt|quart|qt|gallon|gal|"
            r"kg|kilo(?:gram(?:me)?)?|g|gr|gram(?:me)?|gramm|oz|ounce|lbs?|pound)(?![\w])"
        )
        for match in re.finditer(pattern, candidate, re.I):
            value = _number(match.group("value"))
            if value is None or value <= 0:
                continue
            # "0.010g/ml" on a hydrometer is a density it measures, not a pack
            # it is sold in. Anything written as one unit per another is a
            # specification, and reading it as a package invents a 0.01 g jar.
            if re.match(r"\s*(?:/|per\b)", candidate[match.end():], re.I):
                continue
            unit = re.sub(r"[\s.]+", " ", match.group("unit").casefold()).strip()
            # "36 x 2,5 ml" is a set of 36 pans holding 90 ml in total; the pack
            # is what the buyer receives, so the multiplier counts.
            multiple = re.search(r"(\d+)\s*\u00d7\s*$", candidate[:match.start()])
            evidence = match.group(0)
            if multiple and (count := _number(multiple.group(1))) and 1 < count <= 500:
                value *= count
                evidence = f"{multiple.group(0)}{evidence}"
            unit = {"ltr": "l", "lr": "l", "lt": "l", "fl oz": "fl oz", "fl.oz": "fl oz"}.get(unit, unit)
            if unit == "oz" and not liquid_hint:
                grams = value * WEIGHT_G["oz"]
                return {"value": value, "unit": "oz", "dimension": "weight",
                        "grams": round(grams, 3), "evidence": evidence, "unit_ambiguous": True}
            if unit == "oz":
                unit = "fl oz"
            if unit in VOLUME_ML:
                return {"value": value, "unit": unit, "dimension": "volume",
                        "millilitres": round(value * VOLUME_ML[unit], 3), "evidence": evidence}
            if unit in WEIGHT_G:
                return {"value": value, "unit": unit, "dimension": "weight",
                        "grams": round(value * WEIGHT_G[unit], 3), "evidence": evidence}
    return bare_package(*texts)


#: US suppliers name the container instead of sizing it ("Obsidian Pint").
BARE_PACKAGES: dict[str, tuple[str, float]] = {
    "pint": ("pint", 473.176),
    "half pint": ("fl oz", 236.588),
    "quart": ("quart", 946.353),
    "gallon": ("gallon", 3785.41),
    "half gallon": ("quart", 1892.71),
}


def bare_package(*texts: Any) -> dict[str, Any] | None:
    """Read a container named without a number, such as "Obsidian Pint"."""
    for text in texts:
        candidate = clean(text)
        if not candidate:
            continue
        for name in sorted(BARE_PACKAGES, key=len, reverse=True):
            # Only reached when no "<number> <unit>" was found, so a sized pack
            # such as "2 pint" has already been handled.
            if re.search(rf"(?<!\w){re.escape(name)}s?(?!\w)", candidate, re.I):
                unit, millilitres = BARE_PACKAGES[name]
                return {
                    "value": 1.0 if " " not in name else 0.5,
                    "unit": unit,
                    "dimension": "volume",
                    "millilitres": millilitres,
                    "evidence": name,
                    "basis": "named_container",
                }
    return None


PACKAGE_ATTRIBUTE_NAMES = (
    "volume", "poids", "weight", "gewicht", "gewicht kg", "size", "taille",
    "inhalt", "contenu", "contenuto", "contents", "formaat", "grootte",
    "package", "conditionnement", "verpakking", "svoris", "turis",
)


def package_size_from_attributes(attributes: dict[str, Any] | None, liquid_hint: bool = False) -> dict[str, Any] | None:
    """Read a package size from a specification table.

    Suppliers often put the unit in the field name and the bare number in the
    value ("Volume (ml)": "200"), so name and value are parsed together.
    """
    for key, value in (attributes or {}).items():
        name, text = clean(key), clean(value)
        if not text or (fold(name) not in PACKAGE_ATTRIBUTE_NAMES and not any(
            word in fold(name) for word in PACKAGE_ATTRIBUTE_NAMES
        )):
            continue
        if found := package_size(text, liquid_hint=liquid_hint):
            return found
        # The value is a bare number; take the unit from the field name.
        number = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*", text)
        unit = re.search(r"\(?\b(ml|cl|dl|l|litre|liter|g|gr|kg|oz|lb)\b\)?", name, re.I)
        if number and unit:
            if found := package_size(f"{number.group(1)} {unit.group(1)}", liquid_hint=liquid_hint):
                return found
    return None


def unit_price(price: float | None, currency: str | None, package: dict[str, Any] | None) -> dict[str, Any] | None:
    """Express a price per litre or per kilogram so suppliers become comparable."""
    if price is None or price < 0 or not package:
        return None
    if package.get("dimension") == "volume" and package.get("millilitres"):
        return {"value": round(price / (package["millilitres"] / 1000.0), 4), "currency": currency, "per": "l"}
    if package.get("dimension") == "weight" and package.get("grams"):
        return {"value": round(price / (package["grams"] / 1000.0), 4), "currency": currency, "per": "kg"}
    return None


def _first_keyword(text: str, table: Iterable[tuple[str, tuple[str, ...]]]) -> str | None:
    for label, keywords in table:
        if any(keyword in text for keyword in keywords):
            return label
    return None


def _all_keywords(text: str, table: Iterable[tuple[str, tuple[str, ...]]]) -> list[str]:
    return [label for label, keywords in table if any(keyword in text for keyword in keywords)]


def family(*texts: Any) -> str | None:
    """Classify a product into a ceramic-materials family, or None if unclear."""
    text = fold(" ".join(clean(value) for value in texts if value))
    if not text:
        return None
    return _first_keyword(text, FAMILY_KEYWORDS)


def looks_non_material(*texts: Any) -> bool:
    """True when the wording names equipment, tooling or another non-material.

    Only ever pass a product name and its categories. A glaze description
    routinely mentions brushes, kilns, cones and shelves, so running these
    keywords over free description text rejects the very products we want.
    """
    text = fold(" ".join(clean(value) for value in texts if value))
    return any(keyword in text for keyword in NON_MATERIAL_KEYWORDS)


def is_material(family_label: str | None, *texts: Any) -> bool:
    """Decide whether a row belongs in a ceramic-materials scope."""
    if looks_non_material(*texts):
        return False
    return family_label is not None


def form(*texts: Any) -> str | None:
    text = fold(" ".join(clean(value) for value in texts if value))
    if not text:
        return None
    if re.search(r"\bpowder|poudre|pulver|poeder|polvere|polvo|milteliai|pulverform", text):
        return "powder"
    if re.search(r"\bliquid|liquide|fl[uü]ssig|vloeibaar|liquido|ready[\s-]?to[\s-]?use|pret a l'emploi|brush[\s-]?on", text):
        return "liquid"
    if re.search(r"\bgranul|pellet", text):
        return "granulate"
    return None


def surface(*texts: Any) -> str | None:
    return _first_keyword(fold(" ".join(clean(value) for value in texts if value)), SURFACE_KEYWORDS)


def effects(*texts: Any) -> list[str]:
    return _all_keywords(fold(" ".join(clean(value) for value in texts if value)), EFFECT_KEYWORDS)


def application_methods(*texts: Any) -> list[str]:
    return _all_keywords(fold(" ".join(clean(value) for value in texts if value)), APPLICATION_KEYWORDS)


def coats(*texts: Any) -> dict[str, Any] | None:
    text = clean(" ".join(clean(value) for value in texts if value))
    match = re.search(
        r"(?P<min>\d)\s*(?:[-–—]|to|a|bis|à)?\s*(?P<max>\d)?\s*"
        r"(?:coats?|couches?|schichten|lagen|mani|capas)\b",
        text, re.I,
    )
    if not match:
        return None
    minimum = int(match.group("min"))
    maximum = int(match.group("max")) if match.group("max") else minimum
    if not 1 <= minimum <= maximum <= 9:
        return None
    return {"minimum": minimum, "maximum": maximum, "evidence": match.group(0)}


# Manufacturers whose product codes are worth recording as a cross-supplier key,
# with the code shapes each of them publishes.
MANUFACTURER_CODES: dict[str, str] = {
    "mayco": r"\b((?:SW|SC|UG|FN|EL|JG|SG|CG|AC|NT|MS|CC|SD|ST|MT|GD)-?\d{2,4}[A-Z]?)\b",
    # KI (Kilnfire), O (Opalescent), SH (Shino), CR (Crawl) and the rest are all
    # AMACO lines seen in retailer titles; a missing prefix costs the whole code.
    "amaco": r"\b((?:PC|LG|LUG|GDC|HF|SD|SM|CTL|KI|SH|CR|TP|VS|PG|DL|TH|CO|V|F|O|C)-?\d{1,3}[A-Z]?)\b",
    "duncan": r"\b((?:OS|EZ|CN|SY|GL|IN|CR)-?\d{2,4}[A-Z]?)\b",
    "botz": r"\b((?:B\s?)?\d{4})\b",
    "coyote": r"\b([A-Z]{1,3}-?\d{2,4})\b",
    "speedball": r"\b([A-Z]{1,3}-?\d{2,4})\b",
    "terracolor": r"\b([A-Z]{1,3}-?\d{3,4})\b",
    # SIO-2 numbers its glazes but *names* its clay bodies: PR, then the colour
    # (A white, G golden brown, N black), then the grain (I, M, F, G). The
    # digit rule below cannot see those, so the series is spelled out — a bare
    # uppercase word is otherwise just a word, and "RAKU" and "TOFFEE" sit in
    # these same titles.
    "sio-2": r"\b(PR[AGN][A-Z]|[A-Z]{2,4}\d{1,3})\b",
}

#: Alphabetic product codes, per maker, that identify the maker on their own.
#:
#: `MANUFACTURER_CODES` above only reads a code once the maker is already known,
#: which is the right rule for a numeric code: "SC-16" means Mayco only because
#: Mayco is named beside it. These are the opposite case — a token specific
#: enough that seeing it *is* the evidence. `PRAI` on lescousins.fr names no
#: maker anywhere on the page, and it is still a SIO-2 stoneware.
#:
#: The bar for an entry is deliberately high, because a wrong one silently
#: attributes another company's product:
#:
#: * it must be a code, not a line or a range — `FLUMO` and `VIVO` appear in
#:   SIO-2 titles and are neither;
#: * it must be the maker's *own* product — `BLS` is all over SIO-2's shop and
#:   is Colorobbia's, resold;
#: * it must stand alone rather than qualify something else — `CHF` only ever
#:   appears as the grog suffix in `PA/CHF`;
#: * three letters or fewer needs stronger evidence than a catalogue listing,
#:   since short tokens collide with words and with other shops' references.
#:
#: Every entry below was read off SIO-2's own catalogue, cross-checked against
#: the retailers that quote it.
MANUFACTURER_CODE_WORDS: dict[str, tuple[str, ...]] = {
    "SiO-2": (
        # White stoneware and sculpture bodies.
        "PRAI", "PRAM", "PRAF",
        # Golden brown.
        "PRGI", "PRGM", "PRGF",
        # Black.
        "PRNI", "PRNM", "PRNF", "PRNG",
        # Outside the PR series, so the pattern above cannot reach them:
        # paper clay, tableware stoneware, and the two maiolica bodies. Each
        # was read from shops that name SIO-2 beside the code rather than from
        # SIO-2's own catalogue, which is the stronger evidence of the two —
        # two unrelated shops agreeing is what tells a code from a house label.
        "PCLI", "PGV", "PLA", "PLV",
    ),
}

CODE_WORD_MAKERS: dict[str, str] = {
    code: canonical
    for canonical, codes in MANUFACTURER_CODE_WORDS.items()
    for code in codes
}

#: The same vocabulary keyed the way `MANUFACTURER_CODES` is, so the pattern
#: search can consult it for a maker it has already found named.
MAKER_KEY_CODE_WORDS: dict[str, frozenset[str]] = {
    fold(canonical): frozenset(codes)
    for canonical, codes in MANUFACTURER_CODE_WORDS.items()
}

#: Manufacturers as they are written in the wild, canonical name first.
#:
#: A retailer's own label is not the maker: Ceradel sells AMACO and Mayco under
#: `brand: "Harry-Ceradel"`, and on more than a thousand of its rows the real
#: manufacturer is named in the product title and nowhere else. Reading it there
#: is what lets those rows join the same maker's products from another shop.
MANUFACTURERS: dict[str, tuple[str, ...]] = {
    "AMACO": ("amaco",),
    "Mayco": ("mayco",),
    "Botz": ("botz",),
    "Duncan": ("duncan",),
    "Spectrum": ("spectrum",),
    "Speedball": ("speedball",),
    "Terracolor": ("terracolor", "terra color"),
    "SiO-2": ("sio-2", "sio2"),
    "Witgert": ("witgert",),
    "Colorobbia": ("colorobbia",),
    "Welte": ("welte",),
    "Cesco": ("cesco",),
    "Gare": ("gare",),
    "Ferro": ("ferro",),
    "Scarva": ("scarva",),
    "Xiem": ("xiem",),
    "Laguna": ("laguna",),
    "Mason Color": ("mason color", "mason stain"),
    "Prodesco": ("prodesco",),
    "Penguin Pottery": ("penguin pottery",),
    "Sibelco": ("sibelco",),
    "Imerys": ("imerys",),
    "Kentucky Mudworks": ("kentucky mudworks",),
    "Georgies": ("georgies",),
    "Coyote": ("coyote",),
    # Added from the dumps themselves: a name several unrelated shops attach to
    # products is a manufacturer, whereas a name only its own shop uses is a
    # house label. Every entry below is published by two or more of them.
    "Potclays": ("potclays",),
    "Goerg & Schneider": ("goerg & schneider", "goerg und schneider", "g&s"),
    "Carl Jäger": ("carl jäger", "carl jaeger"),
    "Rohde": ("rohde",),
    "Royal & Langnickel": ("royal & langnickel", "royal and langnickel"),
    "Centrado": ("centrado",),
    # Named in titles all over the dumps and missing from this list, so the
    # rows said who made them and nothing read it. Same two-or-more-shops bar
    # as the block above: Orton appears unbranded in 9 shops, Schjerning and
    # Heraeus in 2 each.
    #
    # Lascaux (297 rows) and Bergoin (222) are deliberately *not* here. Both
    # are real makers and both appear in exactly one shop, which is the shape
    # of a house label as much as of a manufacturer — and this list cannot tell
    # them apart on one shop's word. They want a second source, not a guess.
    "Orton": ("orton",),
    "Schjerning": ("schjerning",),
    "Heraeus": ("heraeus",),
}

#: Product lines, and the maker each one belongs to.
#:
#: A line is a trademark, so naming it names the maker: "POTTER'S CHOICE 21
#: ARCTIC BLUE" on lescousins.fr is AMACO's, and the page says AMACO nowhere.
#: `MANUFACTURERS` cannot carry these — a line is not another spelling of the
#: company — and without them 160 Potter's Choice rows across two shops and 442
#: Stroke & Coat rows across four sat with no maker at all.
#:
#: The second element is the prefix the maker's codes take in that line, when
#: the line is numbered. Les Cousins writes "POTTER'S CHOICE 21" where AMACO
#: writes "PC-21", and the bare number is only a code once the line says which
#: series it counts within. `None` for a line whose products are not numbered.
#: `None` is not "unknown", it is "this line's products are not numbered in
#: their titles", and getting that wrong invents codes. Designer Liner is the
#: worked example: its titles read "DESIGNER LINER 37 ML BLANC", where 37 is the
#: pack and the code is `SG402` in the shop's reference. A prefix here would
#: have made every colour in the line the same product, `SG37`.
MANUFACTURER_LINES: dict[str, tuple[tuple[str, str | None], ...]] = {
    "AMACO": ((r"potter'?[’']?s?\s+choice", "PC"),),
    "Mayco": (
        (r"stroke\s*&?\s*(?:and\s+)?coat", "SC"),
        (r"designer\s+liner", None),
    ),
}

MANUFACTURER_LINE_PATTERNS: list[tuple[str, str | None, re.Pattern[str]]] = [
    (canonical, prefix, re.compile(rf"(?<![\w-]){expression}(?![\w-])", re.I))
    for canonical, lines in MANUFACTURER_LINES.items()
    for expression, prefix in lines
]

MANUFACTURER_ALIASES: list[tuple[str, re.Pattern[str]]] = [
    (canonical, re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(alias) for alias in aliases) + r")(?![\w-])", re.I))
    for canonical, aliases in MANUFACTURERS.items()
]


def named_manufacturer(*texts: Any) -> tuple[str, str] | None:
    """The maker named in this wording, if it is one we know by name."""
    for text in (clean(value) for value in texts):
        if not text:
            continue
        for canonical, pattern in MANUFACTURER_ALIASES:
            if match := pattern.search(text):
                return canonical, match.group(0)
    return None

def named_line(*texts: Any) -> tuple[str, str | None, str, str | None] | None:
    """The maker, line and code a product line's name gives away.

    Returns the canonical maker, the code it implies (when the line is numbered
    and a number follows it), and the wording that said so. A number is only
    read when it sits directly after the line name: "POTTER'S CHOICE 21 ARCTIC
    BLUE" is PC21, while the 472 in a pack size further along the title is not.
    """
    for text in (clean(value) for value in texts):
        if not text:
            continue
        for canonical, prefix, pattern in MANUFACTURER_LINE_PATTERNS:
            match = pattern.search(text)
            if not match:
                continue
            code = None
            if prefix:
                following = re.match(r"\s*(?:n[o°]?\.?\s*)?(\d{1,3})(?![\w-])", text[match.end():])
                if following:
                    code = f"{prefix}{int(following.group(1))}"
            return canonical, prefix, match.group(0), code
    return None


COLOUR_ATTRIBUTE_NAMES = (
    "colour", "color", "couleur", "couleurs", "farbe", "kleur", "colore",
    "color principal", "spalva", "farg", "färg",
)


def manufacturer_code(brand: Any, *texts: Any) -> str | None:
    """Read a manufacturer's own product code, only when that maker is named.

    A retailer's internal reference is not a manufacturer code, so a code is
    returned only when a known manufacturer appears in the brand or wording.
    """
    haystack = fold(" ".join(clean(value) for value in (brand, *texts) if value))
    for maker, pattern in MANUFACTURER_CODES.items():
        if maker not in haystack:
            continue
        # The curated words first, because a pattern cannot express them: `PGV`
        # and `PLV` carry no digit and are not in the PR series, so on a shop
        # that writes "Sio-2 Maiolica verde PLV" the maker is named, the code is
        # right there, and the pattern below still finds nothing.
        words = MAKER_KEY_CODE_WORDS.get(maker, frozenset())
        if words:
            for text in (clean(brand), *(clean(value) for value in texts)):
                for token in re.findall(r"(?<![\w-])([A-Z]{3,6})(?![\w-])", text):
                    if token in words:
                        return token
        for text in (clean(brand), *(clean(value) for value in texts)):
            for match in re.finditer(pattern, text):
                code = match.group(1).upper().replace("-", "")
                # Suppliers pad the number inconsistently: AMACO publishes
                # "C-01" where a reseller writes "C-1", and one reseller uses
                # both "SC075" and "SC75" for the same Mayco colour. Strip the
                # padding so the same product carries the same key.
                code = re.sub(r"^([A-Z]+)0+(\d)", r"\1\2", code)
                # A bare number in the ceramic firing range is a temperature, not
                # a product code ("BOTZ ENGOBE 1180 - 1280 C").
                if code.isdigit() and 400 <= int(code) <= 1500:
                    continue
                # The maker's own name is not one of its product codes. `SIO2`
                # is letters-then-digit like every SIO-2 glaze code, so the
                # brand token matched its own pattern and sixteen unrelated
                # products — a red clay, a white one, five chamotte grades, a
                # litre of Flumo — promoted into one canonical product on a
                # shared key of `SIO2`. Merging distinct products is worse than
                # leaving them unattributed, which is why this is a `continue`:
                # a real code later in the same text still wins.
                if _letters(code) and _letters(code) == _letters(maker):
                    continue
                return code
    return None


def _letters(value: str) -> str:
    """The alphabetic skeleton of a token, for comparing a code with a name."""
    return fold(re.sub(r"[^A-Za-z]", "", value))


def manufacturer_by_code(*texts: Any) -> tuple[str, str] | None:
    """The maker a code identifies by itself, when no maker is named.

    Only `MANUFACTURER_CODE_WORDS` is consulted, and only as a whole uppercase
    token: this runs where nothing else names a manufacturer, so a loose match
    here writes a maker onto a product that is not theirs. `PRAIRIE` must not
    read as `PRAI`, which is why the boundaries reject a longer surrounding
    word, and matching is case-sensitive because every shop that quotes one of
    these writes it in capitals — while `pram` in lower case is an English word
    and not a clay body.
    """
    for text in (clean(value) for value in texts):
        if not text:
            continue
        for token in re.findall(r"(?<![\w-])([A-Z]{3,6})(?![\w-])", text):
            if maker := CODE_WORD_MAKERS.get(token):
                return maker, token
    return None


#: Wording a storefront appends to a title that is packaging, not the product.
TITLE_NOISE = (
    # A "size:" field appended to the title, whatever follows it: Spectrum
    # writes "size: $/GAL" and Keramiek en Glazuur writes "size: 473 ml".
    re.compile(r"\s*[-–—|,]?\s*\b(?:size|maat|taille|gr[oö]sse|formato)\s*:\s*.+$", re.I),
    re.compile(r"\s*[-–—]\s*d[ée]stockage\s*$", re.I),
    re.compile(r"\s*\(\s*\)\s*$"),
)

#: A leading article number: "011SF2502 - GRES BIANCO", "1050 UNDERGLAZE BASE".
LEADING_CODE = re.compile(r"^\s*(?P<code>[0-9][0-9A-Z./-]{1,14}|[A-Z]{1,4}[\s-]?\d{1,5}[A-Z]?)\s*[-–—:]?\s+(?=\S)")


def plausible_code(value: Any, brand: Any = None) -> bool:
    """Whether a token can be a manufacturer's code rather than a shop's own.

    A manufacturer numbers a product for a catalogue people read: a digit or
    two behind a short prefix. A shop numbers a row in its database, which is
    why `303020000607` is SiO-2's article number for a Colorobbia engobe and
    not a code anyone else would recognise.
    """
    code = clean(value)
    if not code or not (2 <= len(code) <= 12):
        return False
    if not re.search(r"\d", code):
        return False
    if re.search(r"\d{7,}", code):
        return False
    if brand and fold(re.sub(r"[^A-Za-z]", "", code)) == fold(re.sub(r"[^A-Za-z]", "", str(brand))):
        return False
    return True


def parse_title(
    name: Any,
    *,
    package: dict[str, Any] | None = None,
    supplier_sku: Any = None,
    source_brand: Any = None,
    source_is_manufacturer: bool = False,
    published_brand: Any = None,
) -> dict[str, Any]:
    """Split a published title into the product, its maker and its code.

    Storefronts write a title as a sentence: a description, the maker, the
    maker's code, a colour, and the pack size, in whatever order suits them.
    Stored whole it is unsearchable and uncomparable, and stored parsed it is no
    longer what the supplier published — so both are kept. `name_raw` is the
    title exactly as it arrived and is never edited; `name` is what is left once
    the packaging wording, the leading article number and a trailing maker token
    have been lifted out, and each thing lifted out is returned beside it.

    Nothing here promotes a retailer's article number to a manufacturer code.
    That happens only when the shop *is* the manufacturer, or when the maker is
    named in the title and the code matches that maker's own pattern — the same
    rule `manufacturer_code` has always applied, reading a wider text.
    """
    raw = clean(name)
    result: dict[str, Any] = {
        "name": raw, "name_raw": raw, "brand": None, "brand_basis": None,
        "code": None, "code_basis": None, "evidence": [],
    }
    if not raw:
        return result

    title = raw
    for pattern in TITLE_NOISE:
        if match := pattern.search(title):
            result["evidence"].append(match.group(0).strip())
            title = pattern.sub("", title).strip()

    def trim(text: str) -> str:
        """Drop the pack and the firing range: both are their own fields.

        A trailing number is the pack size only when it is the pack size read
        from elsewhere; otherwise it belongs to the product's name ("GRES
        BIANCO 11") and removing it would rename the product.
        """
        changed = True
        while changed:
            changed = False
            if firing := re.search(r"\s*[-–—]?\s*\d{3,4}\s*°?\s*[-–—]\s*\d{3,4}\s*°?\s*[CF]\b\s*$", text, re.I):
                result["evidence"].append(firing.group(0).strip())
                text, changed = text[: firing.start()].strip(" -–—,:"), True
            if package and (value := package.get("value")) is not None:
                for pattern in (
                    r"\s*[-–—]?\s*(\d+(?:[.,]\d+)?)\s*(?:ml|cl|l|kg|g|gr)\s*$",
                    r"\s+(\d+(?:[.,]\d+)?)\s*$",
                ):
                    trailing = re.search(pattern, text, re.I)
                    if trailing and _number(trailing.group(1)) in {value, round(value, 3)}:
                        result["evidence"].append(trailing.group(0).strip())
                        text, changed = text[: trailing.start()].strip(" -–—,:"), True
        return text

    title = trim(title)

    # The maker, wherever it is written. A trailing token is stripped from the
    # name (it is a label, not part of the product); one written mid-sentence
    # stays, because the sentence reads as the product's name without it.
    if found := named_manufacturer(title):
        canonical, evidence = found
        result["brand"], result["brand_basis"] = canonical, "named_in_title"
        result["evidence"].append(evidence)
        if title.upper().endswith(evidence.upper()):
            title = title[: -len(evidence)].strip(" -–—,:")
    elif line := named_line(title):
        # A line is the maker's trademark, so naming it names them, and it is a
        # fact about the product rather than about who is selling it — which is
        # why it outranks the brand the shop filed the row under.
        canonical, _prefix, evidence, line_code = line
        result["brand"], result["brand_basis"] = canonical, "line_named_in_title"
        result["evidence"].append(evidence)
        if line_code:
            result["code"], result["code_basis"] = line_code, "product_line"
    elif published_brand:
        result["brand"], result["brand_basis"] = clean(published_brand), "published"
    elif coded := manufacturer_by_code(supplier_sku, title):
        # A code specific enough to name its maker, on a page that names nobody:
        # `PRAI - GRES REFRACTAIRE COULEUR PIERRE` is SIO-2's white stoneware
        # whatever the shop calls it. Ranked below a maker the page actually
        # states and above `source_default`, because a retailer's own label is
        # the weakest claim of the three — it is what the shop is, not what the
        # product is.
        canonical, code = coded
        result["brand"], result["brand_basis"] = canonical, "manufacturer_code"
        result["evidence"].append(code)
        # Naming the maker is safe from a mention anywhere in the title. Taking
        # the code as *this row's* is not, when the shop's own reference is that
        # code carrying a qualifier: Les Cousins sells `PRAI` and `PRAIDEFAUT`,
        # the second being the same clay with a voiding defect, at the same
        # price and pack. Both are SIO-2, but giving the clearance row the
        # regular row's code makes them one product to `dedupe_key`, and the
        # shop's two offers silently become one.
        #
        # A reference that merely differs — e-cibas files the same clay under
        # `10000085-12K` — is the shop's article number rather than a statement
        # about the product, and the code is this row's after all.
        own = clean(supplier_sku).upper().replace(" ", "").replace("-", "")
        if not own.startswith(code) or own == code:
            result["code"], result["code_basis"] = code, "manufacturer_code"
    elif source_brand:
        result["brand"], result["brand_basis"] = clean(source_brand), "source_default"

    # A shop that opens every title with its own name is labelling, not naming:
    # "Centrado Suedette - 1400mic - Black" is Centrado's Suedette, and leaving
    # the label in front makes every one of its products sort under C.
    if (own := clean(source_brand)) and re.match(rf"^{re.escape(own)}\b[\s,:-]*", title, re.I):
        title = re.sub(rf"^{re.escape(own)}\b[\s,:–—-]*", "", title, flags=re.I).strip()
        result["evidence"].append(own)
        if not result["brand"]:
            result["brand"], result["brand_basis"] = own, "source_default"

    if match := LEADING_CODE.match(title):
        code = match.group("code")
        remainder = title[match.end():].strip()
        # Only when what follows still names something; "472 ml" is a pack, and
        # a code with nothing after it is the whole name.
        if remainder and re.search(r"[A-Za-z]{3}", remainder):
            result["evidence"].append(code)
            # A manufacturer's shop also resells other makers, and on those rows
            # its article number is its own, not the maker's.
            own_shop = source_is_manufacturer and result["brand_basis"] != "named_in_title"
            if own_shop and plausible_code(code, result["brand"]):
                result["code"] = code.upper().replace(" ", "").replace("-", "")
                result["code_basis"] = "manufacturer_shop"
            title = remainder

    # `brand_basis == "manufacturer_code"` means the maker was read *from* a
    # code, and the branch that did it has already ruled on whether that code is
    # this row's. Letting the pattern search run again here would undo it: the
    # brand it now has puts SIO-2 in the haystack, so `PRAI` would be lifted
    # straight back out of the clearance row's title.
    if not result["code"] and result["brand_basis"] != "manufacturer_code":
        if maker_code := manufacturer_code(result["brand"], raw, supplier_sku):
            result["code"], result["code_basis"] = maker_code, "manufacturer_pattern"
        elif (
            source_is_manufacturer
            and result["brand_basis"] != "named_in_title"
            and plausible_code(supplier_sku, result["brand"])
        ):
            result["code"] = clean(supplier_sku).upper().replace(" ", "")
            result["code_basis"] = "manufacturer_shop"

    # A retailer writes the product after the code it quotes: "Émail à effets
    # pour grès Amaco - KI18 Artic Blush" is the AMACO product "Artic Blush",
    # sold with a sentence of French in front of it. Once the maker and the code
    # are both known, what follows the code is the product's own name.
    if result["code"] and result["brand_basis"] == "named_in_title":
        spelled = re.escape(result["code"])
        loose = re.sub(r"(?<=[A-Z])(?=\d)", r"[\\s-]?0*", spelled)
        if match := re.search(rf"(?<![\w-]){loose}(?![\w-])", title, re.I):
            after = trim(title[match.end():].strip(" -–—,:;|"))
            before = trim(title[: match.start()].strip(" -–—,:;|"))
            if len(after) >= 3 and re.search(r"[A-Za-z]{3}", after):
                title = after
            elif len(before) >= 3 and re.search(r"[A-Za-z]{3}", before):
                # The code closes the title instead of opening it, so the
                # product is what came before it.
                title = before

    cleaned = re.sub(r"\s{2,}", " ", title).strip(" -–—,:;|/")
    result["name"] = cleaned or raw
    return result


def colour(name: Any, hint: Any = None, *, code_prefix: bool = True) -> dict[str, Any] | None:
    """Keep the published colour wording; hex is filled in by image sampling.

    A supplier's own colour attribute is trusted first. Falling back to the
    product name only works when the name is short enough to *be* a colour, so a
    full descriptive title is rejected rather than stored as a fake colour.
    """
    if explicit := clean(hint):
        return {"name": explicit, "basis": "product_attribute"}
    label = clean(name)
    if not label:
        return None
    stripped = label
    if code_prefix:
        # "SW-140 Sea Blue" -> "Sea Blue"
        stripped = re.sub(r"^\s*[A-Z]{1,4}[\s-]?\d{1,4}[A-Z]?\s*[-–—:]?\s*", "", stripped).strip()
    stripped = re.sub(r"\s*\d+(?:[.,]\d+)?\s*(?:ml|l|cl|g|kg|oz|pint|gal)\b.*$", "", stripped, flags=re.I).strip()
    # A container named without a number is packaging too: AMACO titles read
    # "PC-2 Saturation Gold Gallon", and the colour is "Saturation Gold".
    while True:
        shortened = re.sub(
            r"[\s,;-]*\b(?:half\s+)?"
            r"(?:pint|quart|gallon|jar|pot|bottle|tub|bucket|pail|can|tube|syringe|bag|sack|set)s?\b\.?\s*$",
            "", stripped, flags=re.I,
        ).strip()
        if shortened == stripped:
            break
        stripped = shortened
    stripped = re.sub(r"[-–—:,]\s*$", "", stripped).strip()
    words = stripped.split()
    if not stripped or len(words) > 4 or len(stripped) > 34:
        return None
    return {"name": stripped, "basis": "product_name"}


def claims(*texts: Any) -> list[dict[str, Any]]:
    """Record safety wording as a supplier claim with its evidence, never as fact."""
    haystack = " \n ".join(clean(text) for text in texts if text)
    if not haystack:
        return []
    found: list[dict[str, Any]] = []
    for claim_type, pattern in CLAIM_PATTERNS:
        match = pattern.search(haystack)
        if not match:
            continue
        evidence = sentence_around(haystack, match.start(), match.end()).strip()
        # Negation appears as words ("not food safe"), as a hyphenated slug in an
        # icon URL ("not-dinnerware-safe.png"), or as a prefix ("non-food-safe").
        # A sentence-bounded match stops at the first dot, which a URL is full
        # of, so polarity is judged on a window around the match instead.
        window = haystack[max(0, match.start() - 90):match.end() + 90]
        negated = bool(re.search(
            r"(?:\b|[-_/])(?:not|non|nicht|niet|no|pas|sans|senza)[\s\-_]*"
            r"(?:food|dinnerware|dishwasher|microwave|suitable|geeignet|adatto|alimentaire)",
            window, re.I,
        ))
        found.append({
            "type": claim_type,
            "claim": not negated,
            "evidence": evidence[:300],
            "basis": "published_text",
        })
    return found


def documents(links: Iterable[tuple[str, str]], page_url: str = "") -> list[dict[str, Any]]:
    """Classify linked technical and safety files from (url, label) pairs."""
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw_url, raw_label in links:
        url = urljoin(page_url, clean(raw_url)) if page_url else clean(raw_url)
        label = clean(raw_label)
        if not url or url in seen:
            continue
        haystack = f"{label} {urlparse(url).path}"
        document_type = next(
            (name for name, pattern in DOCUMENT_PATTERNS if re.search(pattern, haystack, re.I)),
            None,
        )
        if document_type is None:
            continue
        seen.add(url)
        result.append({"type": document_type, "name": label or None, "url": url})
    return result


#: Specification fields that state a safety property directly.
CLAIM_ATTRIBUTES: list[tuple[str, str]] = [
    ("food_contact_suitability", r"food[\s-]?safe|dinnerware|contact alimentaire|lebensmittel|voedselveilig|uso alimentare"),
    ("lead_free", r"lead[\s-]?free|sans plomb|bleifrei|loodvrij|senza piombo"),
    ("cadmium_free", r"cadmium[\s-]?free|sans cadmium|cadmiumfrei"),
    ("non_toxic", r"non[\s-]?toxic|atoxique|ungiftig|atossico"),
    ("dishwasher_safe", r"dishwasher|lave[\s-]?vaisselle|sp[uü]lmaschinen"),
    ("microwave_safe", r"microwave|micro[\s-]?ondes|mikrowellen"),
]

NEGATIVE_VALUE = re.compile(r"^\s*(?:no|non|nein|nee|nope|false|0|not\b)", re.I)
POSITIVE_VALUE = re.compile(r"^\s*(?:yes|oui|ja|si|sì|true|1|x)\b", re.I)


def attribute_claims(attributes: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Read safety properties a supplier states as a specification field.

    "Food-safe: yes" is a claim the supplier published just as much as a
    sentence is, so it is recorded the same way, with its evidence.
    """
    found: list[dict[str, Any]] = []
    for key, value in (attributes or {}).items():
        name, text = clean(key), clean(value)
        if not name or not text:
            continue
        for claim_type, pattern in CLAIM_ATTRIBUTES:
            if not re.search(pattern, name, re.I):
                continue
            if NEGATIVE_VALUE.match(text):
                claim = False
            elif POSITIVE_VALUE.match(text) or re.search(pattern, text, re.I):
                claim = True
            else:
                continue
            found.append({
                "type": claim_type,
                "claim": claim,
                "evidence": f"{name}: {text}"[:300],
                "basis": "product_attribute",
            })
            break
    return found


def attribute_colour(attributes: dict[str, Any] | None) -> str | None:
    """Find a supplier's own colour attribute, whatever it is called."""
    for key, value in (attributes or {}).items():
        if fold(key) in COLOUR_ATTRIBUTE_NAMES and clean(value):
            return clean(value)
    return None


def describe(
    name: Any,
    description: Any = "",
    category_path: Iterable[str] = (),
    extra: Any = "",
    colour_hint: Any = None,
) -> dict[str, Any]:
    """Derive the full ceramics field block from a product's published text."""
    categories = " ".join(clean(value) for value in category_path)
    corpus = (clean(name), clean(description), categories, clean(extra))
    # Descriptive marketing prose names every property a product does *not* have
    # ("unlike glazes", "apply a clear glaze on top"), so classification reads the
    # product's identity and its specification table. Firing ranges, coats and
    # safety claims stay on the full text because they carry their own evidence.
    identity = (clean(name), categories, clean(extra))
    family_label = family(*identity) or family(*corpus)
    liquid = form(*corpus) == "liquid" or family_label in {"glaze", "underglaze", "engobe"}
    package = package_size(clean(name), clean(description), liquid_hint=liquid)
    return {
        "family": family_label,
        "form": form(*identity),
        "firing": firing_range(*corpus),
        "surface": surface(*identity),
        "effects": effects(*identity),
        "colour": colour(name, colour_hint),
        "application_methods": application_methods(*corpus),
        "coats": coats(clean(description), clean(extra)),
        # Attribute blocks are turned into claims by the scraper that understands
        # them; reading them here would mistake a "not dinnerware safe" icon for
        # a positive claim.
        "claims": claims(clean(name), clean(description)),
        "package_size": package,
        "is_material": is_material(family_label, *identity),
    }
