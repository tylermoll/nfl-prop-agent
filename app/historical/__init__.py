"""Historical, football-only modeling data pipeline."""

from .features import build_modeling_table
from .ingestion import NflverseClient

__all__ = ["NflverseClient", "build_modeling_table"]
