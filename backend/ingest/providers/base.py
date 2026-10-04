from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..schema import COLUMN_INDEX

# Looks up the raw value of the first alias present in the file, or None.
RawGetter = Callable[[tuple[str, ...]], str | None]


@dataclass(frozen=True)
class LotImage:
    """One photo of a lot in the sizes the provider offers (HTTPS URLs)."""

    thumb: str | None
    full: str | None
    hd: str | None = None


@dataclass(frozen=True)
class LotImages:
    """A lot's photos. `complete` is False when the provider's photo list was unavailable and
    only what the export carries (e.g. one thumbnail) could be shown."""

    images: list[LotImage]
    complete: bool


@dataclass(frozen=True)
class FieldMapping:
    """One canonical column fed by the first matching (normalized) source header."""

    column: str
    aliases: tuple[str, ...]
    convert: Callable[[str], Any]

    def __post_init__(self) -> None:
        if self.column not in COLUMN_INDEX:
            raise ValueError(f"unknown canonical column {self.column!r}")


@dataclass(frozen=True)
class Provider:
    """Everything provider-specific about turning an export into `lots` rows.

    A provider is chosen by the inbox sub-folder a file lands in (`inbox/<name>/`).
    """

    name: str
    display_name: str
    # Normalized headers that may hold the lot identifier, in order of preference.
    key_aliases: tuple[str, ...]
    # Validates/normalizes the raw lot identifier; returns None if it is unusable.
    key_convert: Callable[[str], str | None]
    fields: tuple[FieldMapping, ...]
    # Source columns that carry no information about the lot (e.g. a row counter): not stored.
    ignored_columns: frozenset[str] = frozenset()
    # Stored, but a different value is not a change of the lot (e.g. export batch timestamps).
    volatile_columns: frozenset[str] = frozenset()
    # Computes canonical columns that need several source values; mutates `values`.
    derive: Callable[[RawGetter, dict[str, Any]], None] | None = None
    # Normalized source header -> canonical column it feeds through `derive`.
    derived_sources: dict[str, str] = field(default_factory=dict)
    # Re-reads one CSV record (its raw text) when the provider writes CSV that standard
    # parsing gets wrong, e.g. unescaped quotes. Returns the fields, or None to keep the
    # standard parse; the result is used only if it has one field per header column.
    split_record: Callable[[str], list[str] | None] | None = None
    # A lot missing from an export is marked removed only if the export covers its scope
    # (e.g. its yard). None: every export is a full snapshot of the provider's inventory.
    removal_scope: str | None = None
    # How a removal scope is called in alerts ("yard").
    removal_scope_label: str = "scope"
    # Lists a lot's photos from its stored columns (`image_url`, `thumbnail_url`, …). Called by
    # the API on demand; may do network I/O and must never raise.
    fetch_images: Callable[[Mapping[str, Any]], LotImages] | None = None
    # The lot's page on the provider's site: where dealers see everything and go to bid.
    listing_url: Callable[[str], str] | None = None
    # SQL condition over `lots` that holds for the provider's own test lots, which its exports
    # carry like real ones: they stay here but never reach the API database (`%` as `%%`).
    test_lots: str | None = None

    def __post_init__(self) -> None:
        if self.removal_scope is not None and self.removal_scope not in COLUMN_INDEX:
            raise ValueError(f"unknown removal scope column {self.removal_scope!r}")

    def mapped_to(self, normalized: str) -> str | None:
        """Canonical column a normalized source header feeds, if any (for the registry)."""
        if normalized in self.key_aliases:
            return "lot_number"
        for mapping in self.fields:
            if normalized in mapping.aliases:
                return mapping.column
        return self.derived_sources.get(normalized)
