"""Loading and validation of ``defaults.json``.

The file holds every setting that is not exposed as an algorithm parameter.
Required keys are the ones shipped in ``defaults.json``; a few advanced keys are
optional and fall back to the values in :data:`ADVANCED_DEFAULTS`.

This module has no QGIS dependency so it can be unit tested with plain Python.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

#: defaults.json lives in the plugin root folder (next to metadata.txt).
DEFAULTS_PATH = Path(__file__).resolve().parent.parent / "defaults.json"

#: Optional keys and their values when absent from defaults.json.
ADVANCED_DEFAULTS: Dict[str, Any] = {
    # split_zoom "auto": first zoom whose full-extent tile count exceeds this.
    "split_tile_threshold": 20000,
    # Workload model (seconds). Only relative values matter for balancing,
    # but they are compared with the measured job start-up time.
    "cost_per_metatile_s": 0.15,
    "cost_per_feature_s": 0.0005,
    # "auto" = measure qgis_process start-up + project load once, or a number of seconds.
    "job_startup_s": "auto",
    # RAM budget per parallel job, used to cap max_parallel_jobs "auto".
    "ram_per_job_gb": 2.0,
    "max_job_retries": 1,
    "output_filename": "tiles.gpkg",
    "vacuum": False,
    # Pass --skip-loading-plugins to qgis_process (faster start-up). Disable if
    # a style uses expression functions registered by a plugin.
    "qgis_process_skip_plugins": True,
    # Explicit path to qgis_process (.bat/.exe/binary); "auto" = detect.
    "qgis_process_path": "auto",
    # FlatGeobuf's spatial index stores features in Hilbert order, which would
    # change the drawing order of overlapping features. When true, the source
    # feature id is exported and used as rendering order for layers that do
    # not already define "control feature rendering order".
    "preserve_feature_order": True,
}

REQUIRED_KEYS = (
    "background_color", "tile_format", "tile_width", "tile_height", "dpi",
    "antialias", "quality", "metatile_size", "skip_empty_tiles", "target_crs",
    "zoom0_extent", "zoom0_matrix_width", "zoom0_matrix_height",
    "max_parallel_jobs", "split_zoom", "edge_margin_px", "keep_temp_files",
)

TILE_FORMATS = {"PNG": "PNG", "JPG": "JPG", "JPEG": "JPG", "WEBP": "WEBP"}


class ConfigError(ValueError):
    """Raised when defaults.json is missing, malformed or invalid."""


@dataclass(frozen=True)
class TileConfig:
    """Validated settings from defaults.json."""

    background_color: str
    tile_format: str
    tile_width: int
    tile_height: int
    dpi: int
    antialias: bool
    quality: int
    metatile_size: int
    skip_empty_tiles: bool
    target_crs: str
    zoom0_extent: Tuple[float, float, float, float]  # xmin, xmax, ymin, ymax
    zoom0_matrix_width: int
    zoom0_matrix_height: int
    max_parallel_jobs: Union[str, int]
    split_zoom: Union[str, int]
    edge_margin_px: float
    keep_temp_files: bool
    split_tile_threshold: int = 20000
    cost_per_metatile_s: float = 0.15
    cost_per_feature_s: float = 0.0005
    job_startup_s: Union[str, float] = "auto"
    ram_per_job_gb: float = 2.0
    max_job_retries: int = 1
    output_filename: str = "tiles.gpkg"
    vacuum: bool = False
    qgis_process_skip_plugins: bool = True
    qgis_process_path: str = "auto"
    preserve_feature_order: bool = True

    def to_dict(self) -> Dict[str, Any]:
        """Plain dict (JSON-serialisable) of all settings."""
        d = asdict(self)
        d["zoom0_extent"] = list(self.zoom0_extent)
        return d

    def background_color_string(self) -> str:
        """Colour string understood by QgsProcessingParameterColor."""
        if self.background_color.strip().lower() == "transparent":
            return "rgba(0,0,0,0)"
        return self.background_color.strip()


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_COLOR_RE = re.compile(
    r"^(transparent|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{8}|"
    r"rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(,\s*[0-9.]+\s*)?\))$"
)


def validate_config(raw: Dict[str, Any]) -> TileConfig:
    """Validate a raw dict and return a :class:`TileConfig`.

    All problems are collected and reported together.
    """
    if not isinstance(raw, dict):
        raise ConfigError("defaults.json must contain a JSON object")
    errors: List[str] = []
    known = set(REQUIRED_KEYS) | set(ADVANCED_DEFAULTS)
    for k in raw:
        if k not in known:
            errors.append(f"unknown key '{k}' (allowed: {', '.join(sorted(known))})")
    for k in REQUIRED_KEYS:
        if k not in raw:
            errors.append(f"missing required key '{k}'")
    if errors:
        raise ConfigError("Invalid defaults.json:\n  - " + "\n  - ".join(errors))

    v = dict(ADVANCED_DEFAULTS)
    v.update(raw)

    def need(cond: bool, msg: str) -> None:
        if not cond:
            errors.append(msg)

    need(isinstance(v["background_color"], str) and bool(_COLOR_RE.match(v["background_color"].strip())),
         "background_color must be 'transparent', '#RRGGBB', '#RRGGBBAA', 'rgb(r,g,b)' or 'rgba(r,g,b,a)'")
    fmt = str(v["tile_format"]).upper()
    need(fmt in TILE_FORMATS, f"tile_format must be one of PNG, JPG, WEBP (got {v['tile_format']!r})")
    for k in ("tile_width", "tile_height"):
        need(_is_int(v[k]) and 1 <= v[k] <= 4096, f"{k} must be an integer 1..4096")
    need(_is_int(v["dpi"]) and 1 <= v["dpi"] <= 2400, "dpi must be an integer 1..2400")
    need(_is_int(v["quality"]) and 1 <= v["quality"] <= 100, "quality must be an integer 1..100")
    need(_is_int(v["metatile_size"]) and 1 <= v["metatile_size"] <= 20, "metatile_size must be an integer 1..20")
    for k in ("antialias", "skip_empty_tiles", "keep_temp_files", "vacuum", "qgis_process_skip_plugins",
              "preserve_feature_order"):
        need(isinstance(v[k], bool), f"{k} must be true or false")
    need(isinstance(v["target_crs"], str) and v["target_crs"].strip() != "", "target_crs must be a non-empty string such as 'EPSG:4326'")
    z0 = v["zoom0_extent"]
    if isinstance(z0, list) and len(z0) == 4 and all(_is_num(x) for x in z0):
        need(z0[0] < z0[1] and z0[2] < z0[3], "zoom0_extent must be [xmin, xmax, ymin, ymax] with xmin<xmax and ymin<ymax")
    else:
        errors.append("zoom0_extent must be a list of 4 numbers [xmin, xmax, ymin, ymax]")
    for k in ("zoom0_matrix_width", "zoom0_matrix_height"):
        need(_is_int(v[k]) and v[k] >= 1, f"{k} must be an integer >= 1")
    need(v["max_parallel_jobs"] == "auto" or (_is_int(v["max_parallel_jobs"]) and v["max_parallel_jobs"] >= 1),
         "max_parallel_jobs must be 'auto' or an integer >= 1")
    need(v["split_zoom"] == "auto" or (_is_int(v["split_zoom"]) and 0 <= v["split_zoom"] <= 25),
         "split_zoom must be 'auto' or an integer 0..25")
    need(_is_num(v["edge_margin_px"]) and v["edge_margin_px"] >= 0, "edge_margin_px must be a number >= 0")
    need(_is_int(v["split_tile_threshold"]) and v["split_tile_threshold"] >= 1, "split_tile_threshold must be an integer >= 1")
    for k in ("cost_per_metatile_s", "cost_per_feature_s"):
        need(_is_num(v[k]) and v[k] >= 0, f"{k} must be a number >= 0")
    need(v["job_startup_s"] == "auto" or (_is_num(v["job_startup_s"]) and v["job_startup_s"] >= 0),
         "job_startup_s must be 'auto' or a number >= 0")
    need(_is_num(v["ram_per_job_gb"]) and v["ram_per_job_gb"] > 0, "ram_per_job_gb must be a number > 0")
    need(_is_int(v["max_job_retries"]) and v["max_job_retries"] >= 0, "max_job_retries must be an integer >= 0")
    need(isinstance(v["output_filename"], str) and v["output_filename"].lower().endswith(".gpkg")
         and os.path.basename(v["output_filename"]) == v["output_filename"],
         "output_filename must be a plain file name ending in .gpkg")
    need(isinstance(v["qgis_process_path"], str), "qgis_process_path must be 'auto' or a path")
    if errors:
        raise ConfigError("Invalid defaults.json:\n  - " + "\n  - ".join(errors))

    v["tile_format"] = TILE_FORMATS[fmt]
    v["zoom0_extent"] = tuple(float(x) for x in z0)
    v["edge_margin_px"] = float(v["edge_margin_px"])
    return TileConfig(**v)


def load_config(path: Optional[Union[str, Path]] = None) -> TileConfig:
    """Read and validate defaults.json (the plugin's copy by default)."""
    p = Path(path) if path else DEFAULTS_PATH
    if not p.is_file():
        raise ConfigError(f"Configuration file not found: {p}")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except json.JSONDecodeError as e:
        raise ConfigError(f"defaults.json is not valid JSON ({p}): line {e.lineno} column {e.colno}: {e.msg}") from e
    return validate_config(raw)


def available_memory_gb() -> Optional[float]:
    """Available physical memory in GB, or None if it cannot be determined."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
                return stat.ullAvailPhys / 1024 ** 3
            return None
        if os.path.exists("/proc/meminfo"):
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemAvailable:"):
                        return int(line.split()[1]) / 1024 ** 2
        pages = os.sysconf("SC_AVPHYS_PAGES")
        return pages * os.sysconf("SC_PAGE_SIZE") / 1024 ** 3
    except Exception:  # pragma: no cover - platform specific
        return None


def resolve_max_parallel_jobs(cfg: TileConfig, cpu_count: Optional[int] = None,
                              mem_gb: Optional[float] = None, cpu_percent: int = 100) -> Tuple[int, str]:
    """Resolve the number of parallel qgis_process jobs; returns (jobs, explanation).

    ``cpu_percent`` (the algorithm's CPU usage limit) scales the usable cores:
    usable = max(1, cpu_count * cpu_percent / 100). "auto" then gives
    usable // 2 (each job renders with several threads itself), an explicit
    value is capped at ``usable``. Both are capped by available RAM.
    """
    cpus = cpu_count if cpu_count is not None else (os.cpu_count() or 2)
    pct = max(1, min(100, int(cpu_percent)))
    usable = max(1, int(cpus * pct / 100))
    if cfg.max_parallel_jobs == "auto":
        jobs = max(1, usable // 2)
        why = f"auto: {cpus} CPUs x {pct}% = {usable} usable cores // 2 = {jobs}"
    else:
        jobs = min(int(cfg.max_parallel_jobs), usable)
        why = f"defaults.json: {cfg.max_parallel_jobs}" + (
            f", capped to {jobs} by CPU limit {pct}% of {cpus} CPUs" if jobs < int(cfg.max_parallel_jobs) else "")
    mem = mem_gb if mem_gb is not None else available_memory_gb()
    if mem is not None:
        by_ram = max(1, int(mem // cfg.ram_per_job_gb))
        if by_ram < jobs:
            jobs = by_ram
            why += f", capped to {jobs} by {mem:.1f} GB available RAM / {cfg.ram_per_job_gb} GB per job"
    return jobs, why
