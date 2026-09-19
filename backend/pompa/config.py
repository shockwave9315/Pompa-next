"""Environment configuration. Invalid startup configuration is rejected."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Settings:
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_client_id: str
    mqtt_topic_prefix: str
    db_host: str
    db_port: int
    db_user: str
    db_password: str
    db_name: str
    api_host: str
    api_port: int
    # Stage 1 bootstrap value only; the >=24h measurement decides the real policy.
    stale_after_seconds: int
    write_buffer_rows: int
    log_level: str


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} must be in [{minimum}, {maximum}], got {value}")
    return value


def _str(env: Mapping[str, str], name: str, default: str | None = None) -> str:
    value = env.get(name, "").strip() or default
    if not value:
        raise ConfigError(f"{name} is required")
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env

    prefix = _str(env, "MQTT_TOPIC_PREFIX", "panasonic_heat_pump")
    if prefix.endswith("/") or "#" in prefix or "+" in prefix:
        raise ConfigError(f"MQTT_TOPIC_PREFIX must be a plain topic without wildcards or trailing '/', got {prefix!r}")

    log_level = _str(env, "LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
        raise ConfigError(f"LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR, got {log_level!r}")

    return Settings(
        mqtt_host=_str(env, "MQTT_HOST"),
        mqtt_port=_int(env, "MQTT_PORT", 1883, 1, 65535),
        mqtt_username=env.get("MQTT_USERNAME", "").strip() or None,
        mqtt_password=env.get("MQTT_PASSWORD") or None,
        mqtt_client_id=_str(env, "MQTT_CLIENT_ID", "pompa-next"),
        mqtt_topic_prefix=prefix,
        db_host=_str(env, "DB_HOST"),
        db_port=_int(env, "DB_PORT", 3306, 1, 65535),
        db_user=_str(env, "DB_USER"),
        db_password=env.get("DB_PASSWORD", ""),
        db_name=_str(env, "DB_NAME", "pompa_next"),
        api_host=_str(env, "API_HOST", "0.0.0.0"),
        api_port=_int(env, "API_PORT", 8001, 1, 65535),
        stale_after_seconds=_int(env, "STALE_AFTER_SECONDS", 600, 60, 86400),
        write_buffer_rows=_int(env, "WRITE_BUFFER_ROWS", 60, 1, 10000),
        log_level=log_level,
    )
