"""
Layered configuration: defaults → platform profile → env overrides → request overrides.

Layers (lowest to highest priority):
  1. Hardcoded defaults (in this module)
  2. Platform profile YAML (platforms/<platform>.yaml)
  3. Environment variables (AUTOPILOT_* prefix)
  4. Per-request overrides (passed at runtime)

Config values are plain strings or nested dicts. Access via config["key"] or
config.get("key", default). Integer/float conversion is done at the call site.

Platform profiles live in platforms/ relative to the project root.
The project root is the directory containing this file's parent package (model/).

Usage:
    config = Config.load(platform="orin-agx")
    tty0 = config.get("tty0", "/dev/ttyACM0")
    config_with_overrides = config.overlay({"tty0": "/dev/ttyUSB0"})
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog

log = structlog.get_logger()

# Project root: the directory containing the autopilot-rewrite package
_PROJECT_ROOT = Path(__file__).parent.parent

# Environment variable prefix for autopilot config overrides
_ENV_PREFIX = "AUTOPILOT_"

# Hardcoded defaults
_DEFAULTS: dict[str, Any] = {
    "baudrate": 115200,
    "result_dir_base": "results",
    "platform": "generic",
}


class Config:
    """
    Immutable config snapshot. Merge layers with overlay().

    Keys are lower-case strings. Values are strings, ints, floats, or dicts.
    """

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def overlay(self, overrides: dict[str, Any]) -> Config:
        """Return a new Config with overrides merged on top."""
        merged = {**self._data, **overrides}
        return Config(merged)

    @classmethod
    def load(
        cls,
        platform: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> Config:
        """
        Build a Config from all layers.

        platform: name of a YAML file in platforms/ (without .yaml suffix).
            If None, uses AUTOPILOT_PLATFORM env var, then "generic".
        overrides: per-request overrides applied on top of everything else.
        """
        # Layer 1: hardcoded defaults
        data: dict[str, Any] = dict(_DEFAULTS)

        # Layer 2: platform YAML
        resolved_platform = (
            platform
            or os.environ.get("AUTOPILOT_PLATFORM")
            or _DEFAULTS["platform"]
        )
        platform_data = _load_platform(resolved_platform)
        data.update(platform_data)
        data["platform"] = resolved_platform

        # Layer 3: environment variables (AUTOPILOT_<KEY>=<value>)
        for key, value in os.environ.items():
            if key.startswith(_ENV_PREFIX):
                config_key = key[len(_ENV_PREFIX):].lower()
                data[config_key] = value

        # Layer 4: per-request overrides
        if overrides:
            data.update(overrides)

        cfg = cls(data)
        log.debug(
            "config.loaded",
            platform=resolved_platform,
            keys=sorted(data.keys()),
        )
        return cfg


def _load_platform(platform: str) -> dict[str, Any]:
    """Load a platform YAML file. Returns empty dict if not found."""
    yaml_path = _PROJECT_ROOT / "platforms" / f"{platform}.yaml"
    if not yaml_path.exists():
        if platform != "generic":
            log.warning("config.platform_file_missing", path=str(yaml_path))
        return {}

    try:
        import yaml
        with yaml_path.open() as f:
            data = yaml.safe_load(f) or {}
        # Flatten nested 'defaults' key if present
        if "defaults" in data and isinstance(data["defaults"], dict):
            defaults = data.pop("defaults")
            data.update(defaults)
        return {k: v for k, v in data.items() if v is not None}
    except ImportError:
        log.warning("config.yaml_unavailable", note="pip install pyyaml to enable platform YAML")
        return {}
    except Exception as exc:
        log.warning("config.platform_load_error", path=str(yaml_path), error=repr(exc))
        return {}
