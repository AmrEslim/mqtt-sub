"""Configuration loading and validation tests."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from mqtt_ingest.config import ConfigError, load

EXAMPLE = Path(__file__).resolve().parent.parent / "config.example.toml"

MINIMAL = """
[mqtt]
host = "broker.local"

[influxdb]
url = "http://127.0.0.1:8086"
org = "test-org"
token = "test-token"
"""


def _write(tmp_path: Path, text: str, name: str = "config.toml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


def test_example_config_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MUC_MQTT_PASSWORD", "s3cret")
    monkeypatch.setenv("MUC_INFLUX_TOKEN", "tok")

    config = load(EXAMPLE)

    assert config.mqtt.host == "10.20.1.57"
    assert config.mqtt.port == 1883
    assert config.mqtt.topics == ("muc/#",)
    assert config.mqtt.client_id == "muc-ingest"
    assert config.mqtt.username == "muc-ingest"
    assert config.mqtt.password == "s3cret"
    assert config.mqtt.qos == 1
    assert config.mqtt.clean_session is False

    assert config.influxdb.url == "http://127.0.0.1:8086"
    assert config.influxdb.token == "tok"
    assert config.influxdb.bucket == "meter"
    assert config.influxdb.raw_bucket == "raw"

    assert config.spool.directory.is_absolute()
    assert config.spool.directory.name == "spool"
    assert config.logging.file is not None
    assert config.logging.file.name == "ingest.log"
    assert config.retention.readings_days == 0
    assert config.retention.raw_days == 30
    assert config.source == EXAMPLE.resolve()


def test_example_config_resolves_secrets_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MUC_MQTT_PASSWORD", raising=False)
    monkeypatch.setenv("MUC_INFLUX_TOKEN", "tok")

    with pytest.raises(ConfigError, match="MUC_MQTT_PASSWORD"):
        load(EXAMPLE)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load(tmp_path / "nope.toml")


def test_invalid_toml_raises(tmp_path: Path) -> None:
    path = _write(tmp_path, "[mqtt\nhost = 1")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(path)


def test_defaults_applied(tmp_path: Path) -> None:
    config = load(_write(tmp_path, MINIMAL))

    assert config.mqtt.port == 1883
    assert config.mqtt.topics == ("muc/#",)
    assert config.mqtt.client_id == "muc-ingest"
    assert config.mqtt.username is None
    assert config.mqtt.password is None
    assert config.mqtt.keepalive == 60
    assert config.mqtt.qos == 1
    assert config.mqtt.clean_session is False

    assert config.influxdb.bucket == "meter"
    assert config.influxdb.raw_bucket == "raw"
    assert config.influxdb.batch_size == 5000
    assert config.influxdb.flush_interval_ms == 2000
    assert config.influxdb.write_timeout_ms == 20000
    assert config.influxdb.verify_ssl is True

    assert config.spool.directory == tmp_path.resolve() / "spool"
    assert config.spool.max_megabytes == 2048
    assert config.logging.level == "INFO"
    assert config.logging.file is None
    assert config.retention.readings_days == 0
    assert config.retention.raw_days == 30


def test_missing_host_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        port = 1883

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = "t"
        """,
    )
    with pytest.raises(ConfigError, match="'host'"):
        load(path)


def test_missing_token_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        """,
    )
    with pytest.raises(ConfigError, match="'token'"):
        load(path)


def test_empty_token_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = ""
        """,
    )
    with pytest.raises(ConfigError, match="'token' must not be empty"):
        load(path)


def test_literal_secret_is_accepted(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path,
            """
            [mqtt]
            host = "broker.local"
            password = "admin"

            [influxdb]
            url = "http://127.0.0.1:8086"
            org = "o"
            token = "t"
            """,
        )
    )
    assert config.mqtt.password == "admin"


def test_unset_environment_reference_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"
        password = "env:MUC_MISSING_PASSWORD"

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = "t"
        """,
    )
    with pytest.raises(ConfigError, match="MUC_MISSING_PASSWORD"):
        load(path)


def test_qos_out_of_range_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"
        qos = 3

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = "t"
        """,
    )
    with pytest.raises(ConfigError, match="'qos'"):
        load(path)


def test_wrong_type_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"
        port = "1883"

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = "t"
        """,
    )
    with pytest.raises(ConfigError, match="'port' must be an integer"):
        load(path)


def test_keepalive_has_a_lower_bound(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"
        keepalive = 1

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = "t"
        """,
    )
    with pytest.raises(ConfigError, match="'keepalive'"):
        load(path)


def test_single_topic_string_is_accepted(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path,
            """
            [mqtt]
            host = "broker.local"
            topics = "muc/energy"

            [influxdb]
            url = "http://127.0.0.1:8086"
            org = "o"
            token = "t"
            """,
        )
    )
    assert config.mqtt.topics == ("muc/energy",)


def test_empty_topic_list_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"
        topics = []

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = "t"
        """,
    )
    with pytest.raises(ConfigError, match="non-empty"):
        load(path)


def test_unknown_log_level_raises(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        """
        [mqtt]
        host = "broker.local"

        [influxdb]
        url = "http://127.0.0.1:8086"
        org = "o"
        token = "t"

        [logging]
        level = "VERBOSE"
        """,
    )
    with pytest.raises(ConfigError, match="'level'"):
        load(path)


def test_raw_bucket_can_be_disabled(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path,
            """
            [mqtt]
            host = "broker.local"

            [influxdb]
            url = "http://127.0.0.1:8086"
            org = "o"
            token = "t"
            raw_bucket = ""
            """,
        )
    )
    assert config.influxdb.raw_bucket is None


def test_relative_paths_resolve_against_config_directory(tmp_path: Path) -> None:
    config = load(
        _write(
            tmp_path,
            """
            [mqtt]
            host = "broker.local"

            [influxdb]
            url = "http://127.0.0.1:8086"
            org = "o"
            token = "t"

            [spool]
            directory = "buffer"

            [logging]
            file = "log/service.log"
            """,
        )
    )
    assert config.spool.directory == tmp_path.resolve() / "buffer"
    assert config.logging.file == tmp_path.resolve() / "log" / "service.log"
