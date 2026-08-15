"""Dataset-owned source policies applied after neutral projection."""

from __future__ import annotations

from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from mb_ceramics_catalogue.scrapers import domain


class Sio2ProjectionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    manufacturer_brand: str = "sio-2"
    own_material_categories: frozenset[str] = frozenset({
        "low-fire-ceramic-clays", "high-fire-ceramic-clays", "specialty-ceramic-clays",
        "porcelain", "3d-printing-ceramic-clays", "modelling-clay", "liquid-clay",
        "other-materials", "raw-materials-and-oxides", "auxiliary-products",
    })
    glaze_categories: frozenset[str] = frozenset({
        "prepared-glazes", "powdered-glazes", "prepared-underglazes", "powdered-underglazes",
    })

    def apply(self, row: dict[str, object], product_url: str) -> dict[str, object] | None:
        parts = urlparse(product_url).path.strip("/").split("/")
        category = parts[-2] if len(parts) >= 2 else ""
        if category not in self.own_material_categories | self.glaze_categories:
            return None
        brand = domain.fold(row.get("brand")).replace("®", "")
        if category in self.own_material_categories and brand != self.manufacturer_brand:
            return None
        family = (
            "underglaze" if "underglazes" in category else
            "glaze" if category in self.glaze_categories else
            "material" if category in {"other-materials", "raw-materials-and-oxides", "auxiliary-products"}
            else "clay"
        )
        return {**row, "family": row.get("family") or family, "material_kind": category}
