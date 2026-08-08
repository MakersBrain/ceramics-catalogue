#!/usr/bin/env python3
"""Extract embedded catalogue tiles and associate them with NDJSON SKUs."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path


def run(*command: str) -> None:
    subprocess.run(command, check=True)


def image_colorspace(path: Path) -> str:
    return subprocess.check_output(
        ["magick", "identify", "-format", "%[colorspace]", str(path)], text=True
    ).strip()


def distance(text: dict, image: dict) -> float:
    text_left = text["left"]
    text_right = text_left + text["width"]
    text_top = text["top"]
    text_bottom = text_top + text["height"]
    image_left = min(image["left"], image["left"] + image["width"])
    image_right = max(image["left"], image["left"] + image["width"])
    image_top = min(image["top"], image["top"] + image["height"])
    image_bottom = max(image["top"], image["top"] + image["height"])
    x_distance = abs((text_left + text_right) / 2 - (image_left + image_right) / 2)
    if image_top >= text_bottom:
        y_distance = image_top - text_bottom
    elif image_bottom <= text_top:
        y_distance = text_top - image_bottom
    else:
        y_distance = 0
    return x_distance + 3 * y_distance


def extract(pdf: Path, ndjson: Path, output_root: Path) -> tuple[int, int]:
    rows = [json.loads(line) for line in ndjson.read_text().splitlines() if line.strip()]
    source = rows[0]["source"]
    wanted = defaultdict(list)
    for row in rows:
        page = row.get("raw", {}).get("pdf_page")
        if page and row.get("sku"):
            wanted[int(page)].append(row)

    with tempfile.TemporaryDirectory(prefix="catalogue-tiles-") as temporary:
        temporary_path = Path(temporary)
        xml_path = temporary_path / "catalogue.xml"
        run("pdftohtml", "-xml", "-hidden", "-nodrm", str(pdf), str(xml_path))
        root = ET.parse(xml_path).getroot()
        matched: dict[str, Path] = {}

        for page_element in root.findall("page"):
            page_number = int(page_element.attrib["number"])
            page_rows = wanted.get(page_number, [])
            if not page_rows:
                continue

            texts = []
            for element in page_element.findall("text"):
                value = "".join(element.itertext()).strip()
                texts.append(
                    {
                        "value": value,
                        "top": int(element.attrib["top"]),
                        "left": int(element.attrib["left"]),
                        "width": int(element.attrib["width"]),
                        "height": int(element.attrib["height"]),
                    }
                )

            images = []
            for element in page_element.findall("image"):
                width = int(element.attrib["width"])
                height = int(element.attrib["height"])
                absolute_width, absolute_height = abs(width), abs(height)
                if not (30 <= absolute_width <= 220 and 30 <= absolute_height <= 220):
                    continue
                if not 0.72 <= absolute_width / absolute_height <= 1.38:
                    continue
                path = Path(element.attrib["src"])
                if not path.exists() or image_colorspace(path).lower() in {"gray", "graya"}:
                    continue
                images.append(
                    {
                        "path": path,
                        "top": int(element.attrib["top"]),
                        "left": int(element.attrib["left"]),
                        "width": width,
                        "height": height,
                    }
                )

            used: set[Path] = set()
            for row in page_rows:
                sku = row["sku"]
                labels = [
                    text for text in texts
                    if re.search(rf"(?<![A-Z0-9]){re.escape(sku)}(?![A-Z0-9])", text["value"], re.I)
                ]
                choices = []
                for label in labels:
                    for image in images:
                        score = distance(label, image)
                        if score <= 280 and image["path"] not in used:
                            choices.append((score, image))
                if not choices:
                    continue
                _, selected = min(choices, key=lambda choice: choice[0])
                used.add(selected["path"])
                matched[sku] = selected["path"]

        target_directory = output_root / source
        target_directory.mkdir(parents=True, exist_ok=True)
        for row in rows:
            source_image = matched.get(row.get("sku"))
            if not source_image:
                continue
            target = target_directory / f"{row['sku']}.png"
            run("magick", str(source_image), "-colorspace", "sRGB", str(target))
            row["image_path"] = target.relative_to(output_root.parent).as_posix()

        temporary_output = ndjson.with_suffix(".ndjson.tmp")
        temporary_output.write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
        )
        temporary_output.replace(ndjson)
        return len(matched), len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("ndjson", type=Path)
    parser.add_argument("--out", type=Path, default=Path("catalogue-images"))
    options = parser.parse_args()
    if not options.pdf.is_file() or not options.ndjson.is_file():
        parser.error("PDF and NDJSON paths must exist")
    matched, total = extract(options.pdf, options.ndjson, options.out)
    print(f"matched {matched}/{total} products")


if __name__ == "__main__":
    main()
