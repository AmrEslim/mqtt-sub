"""Parsing of Solvimus MUC / M-Bus JSON payloads into time-series ready readings.

The gateway publishes one JSON document per poll cycle::

    {"muc": {"MUC_ID": "...", "TIMESTAMP": 1793110474, "meter": [
        {"METER_ID": "...", "MAN": "DME", "data": [
            {"DESCRIPTION": "Energy", "UNIT": "kWh", "SCALE": 100,
             "DIF": "0C", "VIF": "FB 00",
             "entry": [{"T_MUC": 1793110432, "T": 1793130300, "VAL": "10026"}]}
        ]}
    ]}}

Three properties of that format drive the design:

* ``VAL`` is a raw integer, the engineering value is ``VAL * SCALE``.
* ``DESCRIPTION`` is *not* a register identity - a single heat meter reports
  several ``Volume`` blocks. The identity of a register is ``DIF`` + ``VIF``
  (M-Bus data / value information fields, including their extension bytes).
* ``T_MUC`` (gateway clock) is the only reliable timestamp; ``T`` is the
  meter's own clock and is ``0`` on meters that do not have one.

Nothing in this module performs I/O, so it can be unit tested offline.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator

__all__ = [
    "MeterInfo",
    "ParseError",
    "ParsedMessage",
    "Reading",
    "metric_name",
    "parse_message",
    "slugify",
]

_WARNING_LIMIT = 100
_WHITESPACE = re.compile(r"\s+")
_NON_ALPHANUMERIC = re.compile(r"[^a-zA-Z0-9]+")


class ParseError(ValueError):
    """The payload cannot be interpreted as a MUC message at all."""


@dataclass(frozen=True, slots=True)
class Reading:
    """One register of one meter at one instant."""

    muc_id: str
    meter_id: str
    metric: str
    unit: str
    dif: str
    vif: str
    timestamp: int
    value: float | None
    raw: int | None
    scale: float
    meter_ts: int
    val_text: str | None = None


@dataclass(frozen=True, slots=True)
class MeterInfo:
    """Per-meter metadata, effectively constant for the life of the device."""

    muc_id: str
    meter_id: str
    interface: str
    manufacturer: str
    version: int | None
    medium: str
    medium_id: int | None
    user: str


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """Result of parsing one MQTT payload."""

    muc_id: str | None
    source_timestamp: int | None
    readings: tuple[Reading, ...]
    meters: tuple[MeterInfo, ...]
    warnings: tuple[str, ...]


def slugify(text: str) -> str:
    """ASCII, lower-case, underscore separated - safe as a tag value."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_ALPHANUMERIC.sub("_", folded).strip("_").lower() or "unknown"


def metric_name(description: Any, unit: Any) -> str:
    """Human readable but stable name, e.g. ``m^3`` + ``Volume`` -> ``volume_m_3``."""
    return f"{slugify(_text(description))}_{slugify(_text(unit))}"


def parse_message(payload: str | bytes | bytearray) -> ParsedMessage:
    """Parse one MQTT payload into flat readings.

    Raises :class:`ParseError` only when the payload is unusable as a whole
    (not UTF-8, not JSON, no MUC object). Individual meters, data blocks and
    entries that cannot be understood are skipped and reported in
    ``ParsedMessage.warnings`` so one bad device never stops the pipeline.
    """
    if isinstance(payload, (bytes, bytearray)):
        try:
            text = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ParseError(f"payload is not valid UTF-8: {exc}") from exc
    else:
        text = payload

    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ParseError(f"payload is not valid JSON: {exc}") from exc

    if isinstance(document, dict) and "muc" in document:
        muc_nodes = list(_iter_objects(document["muc"]))
    else:
        muc_nodes = list(_iter_objects(document))

    if not muc_nodes:
        raise ParseError("payload contains no MUC object")

    readings: list[Reading] = []
    meters: list[MeterInfo] = []
    warnings: list[str] = []
    first_muc_id: str | None = None
    source_timestamp: int | None = None

    for node in muc_nodes:
        muc_id = _text(node.get("MUC_ID"))
        timestamp = _to_int(node.get("TIMESTAMP"))
        if not muc_id:
            _warn(warnings, "MUC object without MUC_ID was skipped")
            continue
        if first_muc_id is None:
            first_muc_id = muc_id
            source_timestamp = timestamp
        _parse_muc(node, muc_id, timestamp, readings, meters, warnings)

    deduplicated: dict[tuple[str, str, str, str, int], Reading] = {}
    for reading in readings:
        key = (reading.muc_id, reading.meter_id, reading.dif, reading.vif, reading.timestamp)
        deduplicated[key] = reading

    return ParsedMessage(
        muc_id=first_muc_id,
        source_timestamp=source_timestamp,
        readings=tuple(deduplicated.values()),
        meters=tuple(dict.fromkeys(meters)),
        warnings=tuple(warnings),
    )


def _parse_muc(
    node: dict[str, Any],
    muc_id: str,
    timestamp: int | None,
    readings: list[Reading],
    meters: list[MeterInfo],
    warnings: list[str],
) -> None:
    meter_nodes = list(_iter_objects(node.get("meter")))
    if not meter_nodes:
        _warn(warnings, f"{muc_id}: message has no meters")
        return

    for meter_node in meter_nodes:
        meter_id = _text(meter_node.get("METER_ID"))
        if not meter_id:
            _warn(warnings, f"{muc_id}: meter without METER_ID was skipped")
            continue

        meters.append(
            MeterInfo(
                muc_id=muc_id,
                meter_id=meter_id,
                interface=_text(meter_node.get("INTERFACE")),
                manufacturer=_text(meter_node.get("MAN")),
                version=_to_int(meter_node.get("VER")),
                medium=_text(meter_node.get("MED")),
                medium_id=_to_int(meter_node.get("MED_ID")),
                user=_text(meter_node.get("USER")),
            )
        )

        for block in _iter_objects(meter_node.get("data")):
            _parse_block(block, muc_id, meter_id, timestamp, readings, warnings)


def _parse_block(
    block: dict[str, Any],
    muc_id: str,
    meter_id: str,
    fallback_timestamp: int | None,
    readings: list[Reading],
    warnings: list[str],
) -> None:
    description = _text(block.get("DESCRIPTION"))
    unit = _text(block.get("UNIT"))
    dif = _code(block.get("DIF"))
    vif = _code(block.get("VIF"))
    metric = metric_name(description, unit)
    scale = _to_decimal(block.get("SCALE"))
    if scale is None:
        scale = Decimal(1)

    entries = list(_iter_objects(block.get("entry")))
    if not entries:
        _warn(warnings, f"{muc_id}/{meter_id}/{dif} {vif}: data block without entries")
        return

    for entry in entries:
        timestamp = _to_int(entry.get("T_MUC"))
        if timestamp is None:
            timestamp = fallback_timestamp
        if timestamp is None:
            _warn(warnings, f"{muc_id}/{meter_id}/{dif} {vif}: entry without any timestamp was skipped")
            continue

        meter_ts = _to_int(entry.get("T")) or 0
        raw_value = entry.get("VAL")
        number = _to_decimal(raw_value)
        val_text: str | None = None
        value: float | None = None
        raw: int | None = None

        if number is None:
            val_text = "" if raw_value is None else _text(raw_value)
            _warn(warnings, f"{muc_id}/{meter_id}/{dif} {vif} @ {timestamp}: unparseable VAL {val_text!r}")
        else:
            if number == number.to_integral_value():
                raw = int(number)
            value = float(number * scale)

        readings.append(
            Reading(
                muc_id=muc_id,
                meter_id=meter_id,
                metric=metric,
                unit=unit,
                dif=dif,
                vif=vif,
                timestamp=timestamp,
                value=value,
                raw=raw,
                scale=float(scale),
                meter_ts=meter_ts,
                val_text=val_text,
            )
        )


def _warn(warnings: list[str], message: str) -> None:
    if len(warnings) < _WARNING_LIMIT:
        warnings.append(message)
    elif len(warnings) == _WARNING_LIMIT:
        warnings.append("further warnings suppressed")


def _iter_objects(node: Any) -> Iterator[dict[str, Any]]:
    """Accept either a single JSON object or a list of them."""
    if isinstance(node, dict):
        yield node
    elif isinstance(node, list):
        for item in node:
            if isinstance(item, dict):
                yield item


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _code(value: Any) -> str:
    """Normalise an M-Bus code, e.g. ``'8c  80 40'`` -> ``'8C 80 40'``."""
    return _WHITESPACE.sub(" ", _text(value)).upper()


def _to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text, 10)
        except ValueError:
            number = _to_decimal(text)
            if number is None:
                return None
            return int(number) if number == number.to_integral_value() else None
    return None


def _to_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None
