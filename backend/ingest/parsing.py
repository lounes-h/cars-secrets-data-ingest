"""Streaming CSV parser: turns an export into validated rows without loading it in memory."""

from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from .converters import ConversionError, canonical_value, normalize_headers
from .providers import Provider
from .schema import COLUMN_INDEX, COLUMN_NAMES

csv.field_size_limit(10 * 1024 * 1024)

MAX_STORED_REJECTS = 1000


class FileRejected(Exception):
    """The whole file cannot be ingested (empty, missing the lot key, unreadable)."""


@dataclass(slots=True)
class ParsedRow:
    line_no: int
    lot_number: str
    values: list[Any]  # aligned with schema.COLUMN_NAMES
    attributes: str  # JSON object of every non-empty, non-ignored source value
    row_hash: bytes


@dataclass(slots=True)
class RejectedRow:
    line_no: int
    reason: str
    raw: list[str]


@dataclass
class ParseStats:
    rows_total: int = 0
    rows_rejected: int = 0
    # Records the provider re-split (e.g. Copart's unescaped quotes) instead of rejecting or
    # mangling them.
    rows_repaired: int = 0
    rejected: list[RejectedRow] = field(default_factory=list)
    field_errors: Counter[str] = field(default_factory=Counter)
    # Lot numbers of rows rejected for their shape: still in the export, just unreadable, so
    # they must not be taken for removed lots.
    unreadable_lots: set[str] = field(default_factory=set)

    def reject(self, row: RejectedRow) -> None:
        self.rows_rejected += 1
        if len(self.rejected) < MAX_STORED_REJECTS:
            self.rejected.append(row)


def row_hash(
    attributes: dict[str, str], volatile: frozenset[str] = frozenset()
) -> tuple[str, bytes]:
    """JSON of the attributes, and the SHA-256 of the non-volatile ones in canonical key order."""
    payload = json.dumps(attributes, ensure_ascii=False, separators=(",", ":"))
    compared = {k: v for k, v in attributes.items() if k not in volatile}
    canonical = json.dumps(compared, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return payload, hashlib.sha256(canonical.encode()).digest()


class _LineTap:
    """Feeds lines to csv.reader and keeps the raw text of the record being read."""

    def __init__(self, fh: TextIO):
        self._fh = fh
        self.lines: list[str] = []

    def __iter__(self) -> _LineTap:
        return self

    def __next__(self) -> str:
        line = next(self._fh)
        self.lines.append(line)
        return line

    def take(self) -> str:
        text = "".join(self.lines)
        self.lines.clear()
        return text


class CsvFile:
    """Reads the header eagerly (so schema checks happen before any row) and rows lazily."""

    def __init__(self, path: Path, provider: Provider):
        self.path = path
        self.provider = provider
        self._fh: TextIO = open(path, newline="", encoding="utf-8-sig", errors="replace")  # noqa: SIM115
        self._tap = _LineTap(self._fh)
        self._reader = csv.reader(self._tap)
        try:
            source = next(self._reader)
        except StopIteration:
            self.close()
            raise FileRejected("file is empty") from None
        except csv.Error as exc:
            self.close()
            raise FileRejected(f"unreadable header: {exc}") from exc
        self._tap.take()
        self.source_header = [h.strip() for h in source]
        self.header = normalize_headers(self.source_header)
        self.stats = ParseStats()

        positions = {name: i for i, name in enumerate(self.header)}
        self._positions = positions
        self._key_idx = self._first(provider.key_aliases)
        if self._key_idx is None:
            self.close()
            raise FileRejected(
                f"missing required lot key column (one of: {', '.join(provider.key_aliases)})"
            )
        self._fields = [
            (COLUMN_INDEX[m.column], m.column, idx, m.convert)
            for m in provider.fields
            if (idx := self._first(m.aliases)) is not None
        ]
        self._attr_keys = [
            (i, key) for i, key in enumerate(self.header) if key not in provider.ignored_columns
        ]

    def _first(self, aliases: tuple[str, ...]) -> int | None:
        return next((self._positions[a] for a in aliases if a in self._positions), None)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> CsvFile:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def rows(self) -> Iterator[ParsedRow]:
        width = len(self.header)
        split = self.provider.split_record
        for raw in self._reader:
            line_no = self._reader.line_num
            text = self._tap.take()
            if not raw or (len(raw) == 1 and not raw[0].strip()):
                continue
            self.stats.rows_total += 1
            if split is not None:
                fields = split(text)
                if fields is not None and len(fields) == width and fields != raw:
                    raw = fields
                    self.stats.rows_repaired += 1
            if len(raw) != width:
                # A missing/extra field shifts every following column: never load such a row.
                self.stats.reject(
                    RejectedRow(line_no, f"expected {width} fields, got {len(raw)}", raw)
                )
                if self._key_idx < len(raw) and (lot := self.provider.key_convert(
                        raw[self._key_idx])) is not None:
                    self.stats.unreadable_lots.add(lot)
                continue
            parsed = self._parse(line_no, raw)
            if parsed is not None:
                yield parsed

    def _parse(self, line_no: int, raw: list[str]) -> ParsedRow | None:
        lot_number = self.provider.key_convert(raw[self._key_idx])
        if lot_number is None:
            self.stats.reject(RejectedRow(line_no, "invalid or missing lot number", raw))
            return None

        values: list[Any] = [None] * len(COLUMN_NAMES)
        for pos, column, idx, convert in self._fields:
            try:
                values[pos] = convert(raw[idx])
            except ConversionError:
                # Bad value in a typed column: keep the row, the raw value stays in attributes.
                self.stats.field_errors[column] += 1

        if self.provider.derive is not None:
            named = {name: values[i] for i, name in enumerate(COLUMN_NAMES)}

            def get(aliases: tuple[str, ...]) -> str | None:
                idx = self._first(aliases)
                return raw[idx] if idx is not None else None

            self.provider.derive(get, named)
            values = [named[name] for name in COLUMN_NAMES]

        attributes = {key: v for i, key in self._attr_keys if (v := canonical_value(raw[i]))}
        payload, digest = row_hash(attributes, self.provider.volatile_columns)
        return ParsedRow(line_no, lot_number, values, payload, digest)
