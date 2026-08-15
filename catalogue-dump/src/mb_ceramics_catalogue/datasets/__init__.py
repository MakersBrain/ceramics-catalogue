"""Versioned dataset projection contracts and registration."""

from .base import DatasetDefinition, DatasetRecord, ProjectionContext
from .ceramics import (
    CeramicsCatalogueProjector,
    CeramicsCatalogueRecord,
    CeramicsIdentityProjector,
    CeramicsIdentityRecord,
)
from .commerce import (
    CommerceDocumentProjector,
    CommerceDocumentRecord,
    PriceObservationProjector,
    PriceObservationRecord,
    StockObservationProjector,
    StockObservationRecord,
)
from .registry import DATASET_NAMES, DatasetRegistry, built_in_registry

__all__ = [
    "DATASET_NAMES",
    "CeramicsCatalogueProjector",
    "CeramicsCatalogueRecord",
    "CeramicsIdentityProjector",
    "CeramicsIdentityRecord",
    "CommerceDocumentProjector",
    "CommerceDocumentRecord",
    "DatasetDefinition",
    "DatasetRecord",
    "DatasetRegistry",
    "PriceObservationProjector",
    "PriceObservationRecord",
    "ProjectionContext",
    "StockObservationProjector",
    "StockObservationRecord",
    "built_in_registry",
]
