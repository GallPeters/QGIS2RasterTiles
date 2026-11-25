"""The built-in "Generate XYZ tiles (GeoPackage)" algorithm and defaults.json.

defaults.json holds fixed values for parameters of the built-in algorithm, by
their real names (e.g. "TILE_FORMAT", "METATILESIZE"). Parameters the user sets
in the plugin (extent, zooms, output) cannot be put there; parameters not listed
keep the built-in default.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Union

from qgis.core import (
    QgsApplication,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingParameterEnum,
)

DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "defaults.json"

#: set by the plugin from its own parameters
USER_PARAMETERS = ("EXTENT", "ZOOM_MIN", "ZOOM_MAX", "OUTPUT_FILE")


class ConfigError(ValueError):
    pass


def find_xyz_gpkg_algorithm() -> QgsProcessingAlgorithm:
    """The built-in XYZ -> GeoPackage algorithm (native:tilesxyzgpkg in QGIS 4.3)."""
    reg = QgsApplication.processingRegistry()
    found = [a for a in reg.algorithms()
             if "xyz" in (a.id() + a.displayName()).lower()
             and ("gpkg" in (a.id() + a.displayName()).lower() or "geopackage" in a.displayName().lower())
             and not a.id().startswith("qgis2rastertiles:")]
    if not found:
        raise ConfigError("The built-in 'Generate XYZ tiles (GeoPackage)' algorithm was not found "
                          "(it exists from QGIS 4.3 as native:tilesxyzgpkg).")
    found.sort(key=lambda a: (not a.id().startswith("native:"), a.id()))
    return found[0]


def load_defaults(alg: QgsProcessingAlgorithm, path: Optional[Union[str, Path]] = None) -> Dict[str, Any]:
    """Read defaults.json and check every entry against the built-in algorithm's parameters.

    Enum values may be given as text (e.g. "PNG"); they are converted to the option index.
    """
    p = Path(path) if path else DEFAULTS_PATH
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"defaults.json not found: {p}") from e
    except json.JSONDecodeError as e:
        raise ConfigError(f"defaults.json is not valid JSON: line {e.lineno} column {e.colno}: {e.msg}") from e
    if not isinstance(raw, dict):
        raise ConfigError("defaults.json must contain a JSON object")

    allowed = [d.name() for d in alg.parameterDefinitions() if d.name() not in USER_PARAMETERS]
    errors = []
    values: Dict[str, Any] = {}
    ctx = QgsProcessingContext()
    for key, value in raw.items():
        d = alg.parameterDefinition(key)
        if d is None or key in USER_PARAMETERS:
            errors.append(f"'{key}' is not a parameter you can set here (allowed: {', '.join(allowed)})")
            continue
        if isinstance(d, QgsProcessingParameterEnum) and isinstance(value, str):
            options = [o.upper() for o in d.options()]
            if value.upper() not in options:
                errors.append(f"{key}: '{value}' is not one of {d.options()}")
                continue
            value = options.index(value.upper())
        if not d.checkValueIsAcceptable(value, ctx):
            errors.append(f"{key}: value {value!r} is not acceptable")
            continue
        values[key] = value
    if errors:
        raise ConfigError("Invalid defaults.json:\n  - " + "\n  - ".join(errors))
    return values


def as_argument(value: Any) -> str:
    """A parameter value as qgis_process expects it on the command line."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)
