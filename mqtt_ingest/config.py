"""Loading and validation of the ingest service configuration.

The service is configured from a single TOML file (``config.toml``, which is
git-ignored). Secrets are not written into that file; any string value may be
written as ``env:VARIABLE_NAME`` and is resolved from the process environment,
which is how the NSSM service injects them.

Relative paths are resolved against the directory holding the config file, so a
service can be deployed as a self-contained folder.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "Config",
    "ConfigError",
    "InfluxConfig",
    "LoggingConfig",
    "MqttConfig",
    "RetentionConfig",
    "SpoolConfig",
    "load",
]

ENV_PREFIX = "env:"
_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")


class ConfigError(ValueError):
    """config.toml is missing, malformed, or incomplete."""


@dataclass(frozen=True, slots=True)
class MqttConfig:
    host: str
    port: int = 1883
    topics: tuple[str, ...] = ("muc/#",)
    client_id: str = "muc-ingest"
    username: str | None = None
    password: str | None = None
    keepalive: int = 60
    qos: int = 1
    clean_session: bool = False


@dataclass(frozen=True, slots=True)
class InfluxConfig:
    url: str
    org: str
    token: str
    bucket: str = "meter"
    raw_bucket: str | None = "raw"
    batch_size: int = 5000
    flush_interval_ms: int = 2000
    write_timeout_ms: int = 20000
    verify_ssl: bool = True


@dataclass(frozen=True, slots=True)
class SpoolConfig:
    directory: Path
    max_megabytes: int = 2048


@dataclass(frozen=True, slots=True)
class LoggingConfig:
    level: str = "INFO"
    file: Path | None = None
    max_bytes: int = 52_428_800
    backups: int = 10


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    readings_days: int = 0
    raw_days: int = 30


@dataclass(frozen=True, slots=True)
class Config:
    """Fully resolved configuration; every path is absolute."""

    mqtt: MqttConfig
    influxdb: InfluxConfig
    spool: SpoolConfig
    logging: LoggingConfig
    retention: RetentionConfig
    source: Path


def load(path: str | Path) -> Config:
    """Read and validate a config file."""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"config file not found: {config_path}")

    try:
        with config_path.open("rb") as handle:
            document = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{config_path} is not valid TOML: {exc}") from exc

    base = config_path.parent
    return Config(
        mqtt=_build_mqtt(_table(document, "mqtt")),
        influxdb=_build_influx(_table(document, "influxdb")),
        spool=_build_spool(_table(document, "spool"), base),
        logging=_build_logging(_table(document, "logging"), base),
        retention=_build_retention(_table(document, "retention")),
        source=config_path.resolve(),
    )


def _build_mqtt(section: Mapping[str, Any]) -> MqttConfig:
    name = "mqtt"
    host = _text(section, name, "host", required=True)
    if not host:
        raise ConfigError("[mqtt] 'host' must not be empty")

    client_id = _text(section, name, "client_id", default="muc-ingest")
    if not client_id:
        raise ConfigError("[mqtt] 'client_id' must not be empty")

    return MqttConfig(
        host=host,
        port=_integer(section, name, "port", default=1883, minimum=1, maximum=65535),
        topics=_topics(section, name),
        client_id=client_id,
        username=_text(section, name, "username", default=None),
        password=_secret(section, name, "password"),
        keepalive=_integer(section, name, "keepalive", default=60, minimum=5, maximum=65535),
        qos=_integer(section, name, "qos", default=1, minimum=0, maximum=2),
        clean_session=_boolean(section, name, "clean_session", default=False),
    )


def _build_influx(section: Mapping[str, Any]) -> InfluxConfig:
    name = "influxdb"
    url = _text(section, name, "url", required=True)
    if not url:
        raise ConfigError("[influxdb] 'url' must not be empty")

    bucket = _text(section, name, "bucket", default="meter")
    if not bucket:
        raise ConfigError("[influxdb] 'bucket' must not be empty")

    raw_bucket = _text(section, name, "raw_bucket", default="raw")

    return InfluxConfig(
        url=url,
        org=_text(section, name, "org", required=True),
        token=_secret(section, name, "token", required=True) or "",
        bucket=bucket,
        raw_bucket=raw_bucket or None,
        batch_size=_integer(section, name, "batch_size", default=5000, minimum=1),
        flush_interval_ms=_integer(section, name, "flush_interval_ms", default=2000, minimum=100),
        write_timeout_ms=_integer(section, name, "write_timeout_ms", default=20000, minimum=1000),
        verify_ssl=_boolean(section, name, "verify_ssl", default=True),
    )


def _build_spool(section: Mapping[str, Any], base: Path) -> SpoolConfig:
    return SpoolConfig(
        directory=_path(section, "spool", "directory", base, default="spool"),
        max_megabytes=_integer(section, "spool", "max_megabytes", default=2048, minimum=1),
    )


def _build_logging(section: Mapping[str, Any], base: Path) -> LoggingConfig:
    name = "logging"
    level = _text(section, name, "level", default="INFO").upper()
    if level not in _LOG_LEVELS:
        raise ConfigError(f"[logging] 'level' must be one of {', '.join(_LOG_LEVELS)}")

    return LoggingConfig(
        level=level,
        file=_path(section, name, "file", base, default=None),
        max_bytes=_integer(section, name, "max_bytes", default=52_428_800, minimum=4096),
        backups=_integer(section, name, "backups", default=10, minimum=1),
    )


def _build_retention(section: Mapping[str, Any]) -> RetentionConfig:
    return RetentionConfig(
        readings_days=_integer(section, "retention", "readings_days", default=0, minimum=0),
        raw_days=_integer(section, "retention", "raw_days", default=30, minimum=0),
    )


def _table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] must be a table")
    return value


def _lookup(section: Mapping[str, Any], name: str, key: str, *, required: bool) -> Any:
    if key in section:
        return section[key]
    if required:
        raise ConfigError(f"[{name}] is missing the required key '{key}'")
    return None


def _text(
    section: Mapping[str, Any],
    name: str,
    key: str,
    *,
    required: bool = False,
    default: str | None = None,
) -> str | None:
    value = _lookup(section, name, key, required=required)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ConfigError(f"[{name}] '{key}' must be a string")
    return value.strip()


def _integer(
    section: Mapping[str, Any],
    name: str,
    key: str,
    *,
    default: int | None = None,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    value = _lookup(section, name, key, required=default is None)
    if value is None:
        assert default is not None
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"[{name}] '{key}' must be an integer")
    if minimum is not None and value < minimum:
        raise ConfigError(f"[{name}] '{key}' must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"[{name}] '{key}' must be <= {maximum}")
    return value


def _boolean(
    section: Mapping[str, Any],
    name: str,
    key: str,
    *,
    default: bool,
) -> bool:
    value = _lookup(section, name, key, required=False)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"[{name}] '{key}' must be true or false")
    return value


def _secret(
    section: Mapping[str, Any],
    name: str,
    key: str,
    *,
    required: bool = False,
) -> str | None:
    value = _text(section, name, key, required=required)
    if not value:
        if required:
            raise ConfigError(f"[{name}] '{key}' must not be empty")
        return None
    if not value.startswith(ENV_PREFIX):
        return value

    variable = value[len(ENV_PREFIX) :].strip()
    if not variable:
        raise ConfigError(f"[{name}] '{key}' has an empty 'env:' reference")
    resolved = os.environ.get(variable)
    if not resolved:
        raise ConfigError(
            f"[{name}] '{key}' is set to read {variable} from the environment, "
            "but that variable is not set"
        )
    return resolved


def _path(
    section: Mapping[str, Any],
    name: str,
    key: str,
    base: Path,
    *,
    default: str | None = None,
) -> Path | None:
    raw = _text(section, name, key, default=default)
    if raw is None:
        return None
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = base / candidate
    return candidate.resolve()


def _topics(section: Mapping[str, Any], name: str) -> tuple[str, ...]:
    if "topics" not in section:
        return ("muc/#",)
    value = section["topics"]
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ConfigError(f"[{name}] 'topics' must be a non-empty list of topic filters")

    topics: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"[{name}] 'topics' entries must be non-empty strings")
        topics.append(item.strip())
    return tuple(topics)
