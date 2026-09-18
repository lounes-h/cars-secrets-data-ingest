"""Provider registry. To add a provider: write a module defining a `Provider`, register it here,
and drop its files in `inbox/<name>/`. No migration is needed."""

from .base import FieldMapping, Provider, RawGetter
from .copart import COPART

PROVIDERS: dict[str, Provider] = {p.name: p for p in (COPART,)}


def get_provider(name: str) -> Provider | None:
    return PROVIDERS.get(name)


__all__ = ["PROVIDERS", "FieldMapping", "Provider", "RawGetter", "get_provider"]
