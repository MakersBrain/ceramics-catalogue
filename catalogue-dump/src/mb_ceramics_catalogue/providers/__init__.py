"""Typed paid-proxy provider boundary."""

from .base import ProviderError, ProxyProvider
from .decodo import DecodoProvider

__all__ = ["DecodoProvider", "ProviderError", "ProxyProvider"]
