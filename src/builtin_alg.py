"""Discovery of the built-in "Generate XYZ tiles (GeoPackage)" algorithm and
mapping of defaults.json onto its real parameter names.

Nothing is hardcoded as the single truth: the algorithm is searched in the
processing registry (id / display name containing "xyz" and "gpkg" or
"geopackage") and each logical setting is matched against the algorithm's
``parameterDefinitions()`` by candidate names, then by parameter type and
description keywords. Parameters that are not mapped keep the built-in default
(they are simply not passed to qgis_process).

Observed in QGIS 4.3: id ``native:tilesxyzgpkg``, display name
"Generate XYZ tiles (GeoPackage)". QGIS 4.2 does not have this algorithm.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from qgis.core import QgsApplication, QgsProcessingAlgorithm, QgsProcessingParameterEnum

from .config import TileConfig
from .tile_grid import Rect


class BuiltinAlgorithmError(RuntimeError):
    pass


# logical key -> (candidate parameter names, required parameter type(s), description keywords)
_SPEC: Dict[str, Tuple[Sequence[str], Sequence[str], Sequence[str]]] = {
    "extent": (("EXTENT",), ("extent",), ("extent",)),
    "zoom_min": (("ZOOM_MIN", "MIN_ZOOM", "MINZOOM"), ("number",), ("minimum zoom",)),
    "zoom_max": (("ZOOM_MAX", "MAX_ZOOM", "MAXZOOM"), ("number",), ("maximum zoom",)),
    "dpi": (("DPI",), ("number",), ("dpi",)),
    "background_color": (("BACKGROUND_COLOR", "BACKGROUND"), ("color",), ("background",)),
    "antialias": (("ANTIALIAS", "ANTIALIASING"), ("boolean",), ("antialias",)),
    "tile_format": (("TILE_FORMAT", "FORMAT"), ("enum",), ("format",)),
    "quality": (("QUALITY", "JPG_QUALITY"), ("number",), ("quality",)),
    "metatile_size": (("METATILESIZE", "METATILE_SIZE"), ("number",), ("metatile",)),
    "skip_empty_tiles": (("SKIP_EMPTY_TILES", "SKIP_EMPTY"), ("boolean",), ("empty",)),
    "target_crs": (("TARGET_CRS", "CRS"), ("crs",), ("crs",)),
    "zoom0_extent": (("Z0_EXTENT", "ZOOM0_EXTENT"), ("extent",), ("zoom 0 extent",)),
    "zoom0_matrix_width": (("Z0_MATRIX_WIDTH", "ZOOM0_MATRIX_WIDTH"), ("number",), ("matrix width",)),
    "zoom0_matrix_height": (("Z0_MATRIX_HEIGHT", "ZOOM0_MATRIX_HEIGHT"), ("number",), ("matrix height",)),
    "tile_width": (("TILE_WIDTH",), ("number",), ("tile width",)),
    "tile_height": (("TILE_HEIGHT",), ("number",), ("tile height",)),
    "output_file": (("OUTPUT_FILE", "OUTPUT"), ("fileDestination",), ("output",)),
}


def _norm(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


def find_xyz_gpkg_algorithm(registry=None) -> QgsProcessingAlgorithm:
    """Find the built-in XYZ → GeoPackage algorithm in the processing registry."""
    registry = registry or QgsApplication.processingRegistry()
    matches = []
    for alg in registry.algorithms():
        hay = (alg.id() + " " + alg.displayName()).lower()
        if "xyz" in hay and ("gpkg" in hay or "geopackage" in hay):
            matches.append(alg)
    if not matches:
        related = sorted(f"{a.id()} ({a.displayName()})" for a in registry.algorithms()
                         if "xyz" in (a.id() + a.displayName()).lower())
        raise BuiltinAlgorithmError(
            "The built-in 'Generate XYZ tiles (GeoPackage)' algorithm was not found in this QGIS "
            "(it exists in QGIS 4.3 as native:tilesxyzgpkg). XYZ algorithms available: "
            + (", ".join(related) or "none"))
    # prefer the native provider and our own plugin is never a candidate
    matches = [a for a in matches if not a.id().startswith("qgis2rastertiles:")] or matches
    matches.sort(key=lambda a: (a.provider() is None or a.provider().id() != "native", a.id()))
    return matches[0]


@dataclass
class ParameterMapping:
    """Logical setting -> real parameter name of the built-in algorithm."""

    algorithm_id: str
    names: Dict[str, str]
    tile_format_options: List[str]
    unmapped_builtin: List[str] = field(default_factory=list)

    def describe(self) -> str:
        rows = [f"  {k:<20} -> {v}" for k, v in self.names.items()]
        if self.unmapped_builtin:
            rows.append("  (built-in defaults used for: " + ", ".join(self.unmapped_builtin) + ")")
        return f"Parameter mapping for {self.algorithm_id}:\n" + "\n".join(rows)

    def tile_format_value(self, fmt: str) -> int:
        """Enum index of a tile format (JPG/JPEG treated as equal)."""
        wanted = {"JPG", "JPEG"} if fmt.upper() in ("JPG", "JPEG") else {fmt.upper()}
        for i, opt in enumerate(self.tile_format_options):
            if opt.strip().upper() in wanted:
                return i
        raise BuiltinAlgorithmError(f"tile_format '{fmt}' is not offered by {self.algorithm_id}; "
                                    f"options: {self.tile_format_options}")

    def job_inputs(self, cfg: TileConfig, extent: Rect, crs_authid: str, zoom_min: int, zoom_max: int,
                   output_file: str) -> Dict[str, Any]:
        """JSON inputs for one qgis_process run."""
        z0 = cfg.zoom0_extent  # xmin, xmax, ymin, ymax
        n = self.names
        return {
            n["extent"]: extent.to_processing_string(crs_authid),
            n["zoom_min"]: zoom_min,
            n["zoom_max"]: zoom_max,
            n["dpi"]: cfg.dpi,
            n["background_color"]: cfg.background_color_string(),
            n["antialias"]: cfg.antialias,
            n["tile_format"]: self.tile_format_value(cfg.tile_format),
            n["quality"]: cfg.quality,
            n["metatile_size"]: cfg.metatile_size,
            n["skip_empty_tiles"]: cfg.skip_empty_tiles,
            n["target_crs"]: crs_authid,
            n["zoom0_extent"]: f"{z0[0]!r},{z0[1]!r},{z0[2]!r},{z0[3]!r} [{crs_authid}]",
            n["zoom0_matrix_width"]: cfg.zoom0_matrix_width,
            n["zoom0_matrix_height"]: cfg.zoom0_matrix_height,
            n["tile_width"]: cfg.tile_width,
            n["tile_height"]: cfg.tile_height,
            n["output_file"]: output_file,
        }


def map_parameters(alg: QgsProcessingAlgorithm) -> ParameterMapping:
    """Map every logical setting onto a parameter of ``alg`` or fail clearly."""
    defs = list(alg.parameterDefinitions())
    by_norm = {_norm(d.name()): d for d in defs}
    used: Dict[str, str] = {}
    missing: List[str] = []
    for key, (cands, types, words) in _SPEC.items():
        found = None
        for c in cands:
            d = by_norm.get(_norm(c))
            if d is not None and d.type() in types and d.name() not in used.values():
                found = d
                break
        if found is None:  # heuristic fallback: type + description keywords
            for d in defs:
                if d.type() in types and d.name() not in used.values() and \
                        any(w in d.description().lower() for w in words):
                    if key == "extent" and "zoom" in d.description().lower():
                        continue
                    found = d
                    break
        if found is None:
            missing.append(key)
        else:
            used[key] = found.name()
    if missing:
        avail = "\n  ".join(f"{d.name()} [{d.type()}] {d.description()}" for d in defs)
        raise BuiltinAlgorithmError(
            f"Could not map setting(s) {', '.join(missing)} onto parameters of {alg.id()}.\n"
            f"Available parameters:\n  {avail}")
    fmt_def = alg.parameterDefinition(used["tile_format"])
    options = list(fmt_def.options()) if isinstance(fmt_def, QgsProcessingParameterEnum) else []
    unmapped = [d.name() for d in defs if d.name() not in used.values()]
    return ParameterMapping(alg.id(), used, options, unmapped)
