<div align="center">

<img width="64" alt="icon" src="icon.png" />

# QGIS2RasterTiles

**XYZ raster tiles in a GeoPackage, much faster, with the same result as QGIS's built-in tool.**

</div>

## How it works

The built-in *Generate XYZ tiles (GeoPackage)* spends most of its time on empty metatiles and uses only a few cores.
QGIS2RasterTiles:

1. exports the visible layers to lean files (only the fields the style uses, filters applied, reprojected);
2. renders high zoom levels only where there are features;
3. runs the built-in tool in parallel processes;
4. merges the parts into one GeoPackage.

Each part uses the same metatiles as a single run, so the tiles are identical to the built-in tool's output.
Features are counted at every step; the run stops if any visible feature would be lost.

## Requirements

QGIS 4.3 or later (the first version with *Generate XYZ tiles (GeoPackage)*).

## Usage

Processing Toolbox → **QGIS2RasterTiles** → **Parallel XYZ tiles (GPKG)**

| Parameter | |
|---|---|
| Extent | area to tile |
| Minimum / maximum zoom | 0–25 |
| CPU usage limit (%) | how much of the CPU to use (100 = keep it about 95% busy) |
| Output directory | gets `tiles.gpkg` and a log file |

Run it again with the same inputs to resume an interrupted run.

## Settings

All other settings are in [`defaults.json`](defaults.json) in the plugin folder:
tile format and size, DPI, antialiasing, quality, metatile size, skip empty tiles, target CRS, zoom-0 extent and matrix size,
plus:

| Key | Default | Meaning |
|---|---|---|
| `max_parallel_jobs` | `auto` | parallel processes; `auto` starts with half the cores and adds more while the CPU is below the *CPU usage limit* (up to one per core, limited by free RAM); a number = fixed |
| `split_zoom` | `auto` | zooms below it run as one job; zooms from it up are split into areas |
| `edge_margin_px` | 100 | how far symbols and labels reach beyond their feature, in pixels |
| `keep_temp_files` | false | keep the intermediate files |

Tuning:

- **CPU too busy for other work:** lower the *CPU usage limit*; the number of processes follows it.
- **Tiles missing at the edge of features:** raise `edge_margin_px`.
- **Many tiny jobs at one zoom:** raise `split_zoom`.

## QGIS settings

The parallel processes use the same QGIS user profile as your session, so global settings (GPU/OpenCL, fonts, SVG paths) are the same.
Project settings that affect rendering, such as the scale calculation method, are copied.
Labeling engine and screen DPI settings don't apply: the built-in tool uses its own (DPI parameter, no truncated labels at map edges), and so does every process.

## License

[GPL-2.0](LICENSE)
