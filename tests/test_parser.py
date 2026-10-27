"""Parser tests: one real gateway message plus synthetic edge cases."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mqtt_ingest.parser import (
    ParseError,
    ParsedMessage,
    Reading,
    metric_name,
    parse_message,
    slugify,
)

FIXTURE = Path(__file__).parent / "fixtures" / "muc_sample.json"
PAYLOAD = FIXTURE.read_text(encoding="utf-8")

GAS_METER = "20046855"
HEAT_METER = "64821515"


@pytest.fixture(scope="module")
def parsed() -> ParsedMessage:
    return parse_message(PAYLOAD)


def _reading(result: ParsedMessage, meter_id: str, dif: str, vif: str) -> Reading:
    matches = [
        r for r in result.readings if r.meter_id == meter_id and r.dif == dif and r.vif == vif
    ]
    assert len(matches) == 1, f"expected one reading for {meter_id} {dif}/{vif}, found {len(matches)}"
    return matches[0]


def _payload(meters, *, muc_id: str = "muc-synthetic", timestamp: int | None = 1700000000) -> str:
    node = {"MUC_ID": muc_id, "meter": meters}
    if timestamp is not None:
        node["TIMESTAMP"] = timestamp
    return json.dumps({"muc": node})


def _meter(meter_id: str = "m1", blocks=None) -> dict:
    return {"METER_ID": meter_id, "data": blocks if blocks is not None else []}


def _block(
    *,
    dif: str = "0C",
    vif: str = "15",
    description: str = "Volume",
    unit: str = "m^3",
    scale: float | None = 1,
    entries=None,
) -> dict:
    block = {
        "DESCRIPTION": description,
        "UNIT": unit,
        "DIF": dif,
        "VIF": vif,
        "entry": entries if entries is not None else [],
    }
    if scale is not None:
        block["SCALE"] = scale
    return block


# --- the real message -------------------------------------------------------


def test_envelope_metadata(parsed: ParsedMessage) -> None:
    assert parsed.muc_id == "6891d080400c"
    assert parsed.source_timestamp == 1793110474
    assert parsed.warnings == ()


def test_every_entry_becomes_exactly_one_reading(parsed: ParsedMessage) -> None:
    assert len(parsed.readings) == 27


def test_meter_metadata(parsed: ParsedMessage) -> None:
    assert [m.meter_id for m in parsed.meters] == [GAS_METER, HEAT_METER]

    gas, heat = parsed.meters
    assert gas.interface == "MBus"
    assert gas.manufacturer == "REL"
    assert gas.medium == "Gas"
    assert gas.medium_id == 3
    assert gas.version == 9
    assert gas.user == "GZ"

    assert heat.manufacturer == "DME"
    assert heat.medium == "Heat (outlet)"
    assert heat.medium_id == 4
    assert heat.version == 64
    assert heat.user == "WMZ"


def test_gas_volume_series(parsed: ParsedMessage) -> None:
    gas = [r for r in parsed.readings if r.meter_id == GAS_METER]
    assert len(gas) == 6
    assert {r.metric for r in gas} == {"volume_m_3"}
    assert {r.unit for r in gas} == {"m^3"}
    assert {r.dif for r in gas} == {"06"}
    assert {r.vif for r in gas} == {"16"}
    assert {r.value for r in gas} == {115.0}
    assert [r.timestamp for r in gas] == [
        1793110381,
        1793110391,
        1793110400,
        1793110411,
        1793110421,
        1793110431,
    ]


def test_scale_is_applied_as_multiplier(parsed: ParsedMessage) -> None:
    reading = _reading(parsed, HEAT_METER, "0A", "5A")
    assert reading.metric == "flow_temperature_degree_c"
    assert reading.scale == 0.1
    assert reading.raw == 237
    assert reading.value == pytest.approx(23.7)


def test_large_scale(parsed: ParsedMessage) -> None:
    reading = _reading(parsed, HEAT_METER, "0C", "FB 00")
    assert reading.metric == "energy_kwh"
    assert reading.scale == 100
    assert reading.value == pytest.approx(1002600.0)


def test_negative_values_are_preserved(parsed: ParsedMessage) -> None:
    reading = _reading(parsed, HEAT_METER, "0A", "62")
    assert reading.raw == -2
    assert reading.value == pytest.approx(-0.2)


def test_fractional_scale_keeps_decimal_precision(parsed: ParsedMessage) -> None:
    reading = _reading(parsed, HEAT_METER, "8C 80 40", "15")
    assert reading.value == pytest.approx(0.1)
    assert reading.raw == 1


def test_repeated_description_is_not_one_register(parsed: ParsedMessage) -> None:
    volume = [r for r in parsed.readings if r.metric == "volume_m_3"]
    assert len(volume) == 13
    registers = {(r.meter_id, r.dif, r.vif) for r in volume}
    assert len(registers) == 8


def test_register_identity_is_unique_per_timestamp(parsed: ParsedMessage) -> None:
    keys = [(r.muc_id, r.meter_id, r.dif, r.vif, r.timestamp) for r in parsed.readings]
    assert len(keys) == len(set(keys))


def test_absent_meter_clock_is_zero(parsed: ParsedMessage) -> None:
    assert {r.meter_ts for r in parsed.readings if r.meter_id == GAS_METER} == {0}


def test_meter_clock_is_captured(parsed: ParsedMessage) -> None:
    assert _reading(parsed, HEAT_METER, "0C", "FB 00").meter_ts == 1793130300
    assert _reading(parsed, HEAT_METER, "4C", "FB 00").meter_ts == 1767139200
    assert _reading(parsed, HEAT_METER, "CC 02", "FB 00").meter_ts == 1790726400


def test_gateway_timestamp_used_for_every_heat_reading(parsed: ParsedMessage) -> None:
    heat = [r for r in parsed.readings if r.meter_id == HEAT_METER]
    assert len(heat) == 21
    assert {r.timestamp for r in heat} == {1793110432}


# --- naming ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("description", "unit", "expected"),
    [
        ("Volume", "m^3", "volume_m_3"),
        ("Volume flow", "m^3/h", "volume_flow_m_3_h"),
        ("Flow temperature", "Degree C", "flow_temperature_degree_c"),
        ("Operating time (Data error)", "h", "operating_time_data_error_h"),
        ("Energy", "kWh", "energy_kwh"),
    ],
)
def test_metric_name(description: str, unit: str, expected: str) -> None:
    assert metric_name(description, unit) == expected


def test_slugify_falls_back_to_unknown() -> None:
    assert slugify("") == "unknown"
    assert slugify("---") == "unknown"


# --- tolerance and synthetic cases ----------------------------------------


def test_bare_muc_object_is_accepted() -> None:
    inner = json.loads(PAYLOAD)["muc"]
    result = parse_message(json.dumps(inner))
    assert result.muc_id == "6891d080400c"
    assert len(result.readings) == 27


def test_bytes_payload_is_accepted() -> None:
    assert len(parse_message(PAYLOAD.encode("utf-8")).readings) == 27


def test_muc_list_is_merged() -> None:
    node = json.loads(PAYLOAD)["muc"]
    second = dict(node, MUC_ID="6891d080400d")
    result = parse_message(json.dumps({"muc": [node, second]}))
    assert len(result.readings) == 54
    assert {r.muc_id for r in result.readings} == {"6891d080400c", "6891d080400d"}


def test_dif_code_is_normalised() -> None:
    payload = _payload(
        [
            _meter(
                blocks=[
                    _block(
                        dif="8c  80 40",
                        vif=" 15 ",
                        scale=0.1,
                        entries=[{"T_MUC": 1700000001, "VAL": "1"}],
                    )
                ]
            )
        ]
    )
    reading = parse_message(payload).readings[0]
    assert reading.dif == "8C 80 40"
    assert reading.vif == "15"
    assert reading.value == pytest.approx(0.1)


def test_missing_scale_defaults_to_one() -> None:
    payload = _payload(
        [_meter(blocks=[_block(scale=None, entries=[{"T_MUC": 1700000001, "VAL": "7"}])])]
    )
    reading = parse_message(payload).readings[0]
    assert reading.scale == 1
    assert reading.value == pytest.approx(7.0)


def test_entry_without_t_muc_uses_gateway_timestamp() -> None:
    payload = _payload(
        [_meter(blocks=[_block(entries=[{"VAL": "7"}])])], timestamp=1700000123
    )
    assert parse_message(payload).readings[0].timestamp == 1700000123


def test_entry_without_any_timestamp_is_skipped() -> None:
    payload = _payload([_meter(blocks=[_block(entries=[{"VAL": "7"}])])], timestamp=None)
    result = parse_message(payload)
    assert result.readings == ()
    assert any("without any timestamp" in warning for warning in result.warnings)


def test_unparseable_value_is_kept_as_text() -> None:
    payload = _payload(
        [_meter(blocks=[_block(entries=[{"T_MUC": 1700000001, "VAL": "abc"}])])]
    )
    result = parse_message(payload)
    reading = result.readings[0]
    assert reading.value is None
    assert reading.raw is None
    assert reading.val_text == "abc"
    assert any("unparseable VAL" in warning for warning in result.warnings)


def test_duplicate_register_keeps_last_value() -> None:
    payload = _payload(
        [
            _meter(
                blocks=[
                    _block(
                        entries=[
                            {"T_MUC": 1700000001, "VAL": "1"},
                            {"T_MUC": 1700000001, "VAL": "2"},
                        ]
                    )
                ]
            )
        ]
    )
    result = parse_message(payload)
    assert len(result.readings) == 1
    assert result.readings[0].value == pytest.approx(2.0)


def test_meter_without_id_is_skipped() -> None:
    payload = _payload([{"data": [_block(entries=[{"T_MUC": 1700000001, "VAL": "1"}])]}])
    result = parse_message(payload)
    assert result.readings == ()
    assert result.meters == ()
    assert any("METER_ID" in warning for warning in result.warnings)


def test_data_block_without_entries_warns() -> None:
    result = parse_message(_payload([_meter(blocks=[_block(entries=[])])]))
    assert result.readings == ()
    assert any("without entries" in warning for warning in result.warnings)


def test_message_without_meters_warns() -> None:
    result = parse_message(_payload([]))
    assert result.readings == ()
    assert any("no meters" in warning for warning in result.warnings)


def test_warnings_are_capped() -> None:
    entries = [{"T_MUC": 1700000000 + i, "VAL": "abc"} for i in range(200)]
    result = parse_message(_payload([_meter(blocks=[_block(entries=entries)])]))
    assert len(result.readings) == 200
    assert len(result.warnings) == 101
    assert result.warnings[-1] == "further warnings suppressed"


def test_invalid_json_raises() -> None:
    with pytest.raises(ParseError, match="not valid JSON"):
        parse_message("{not json")


def test_invalid_utf8_raises() -> None:
    with pytest.raises(ParseError, match="UTF-8"):
        parse_message(b"\xff\xfe\x00")


@pytest.mark.parametrize("document", ["[]", "null", "123", '"text"'])
def test_document_without_muc_object_raises(document: str) -> None:
    with pytest.raises(ParseError, match="no MUC object"):
        parse_message(document)


def test_json_without_muc_id_is_reported_not_raised() -> None:
    result = parse_message('{"foo": 1}')
    assert result.readings == ()
    assert result.muc_id is None
    assert any("without MUC_ID" in warning for warning in result.warnings)
