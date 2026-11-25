<div align="center">

<img width="64" alt="icon" src="icon.png" />

# QGIS2RasterTiles

**XYZ raster tiles in a GeoPackage, using the whole CPU.**

</div>

## Why

QGIS's *Generate XYZ tiles (GeoPackage)* keeps only a few CPU cores busy and renders every metatile, even empty ones.
QGIS2RasterTiles runs the same tool on a lean local copy of your project, at normal priority, either once or as several parallel workers.
The tiles are the built-in tool's own output.

## How it works

1. **Package Layers:** the visible vector layers go into one GeoPackage, in the target CRS, with their filters applied.
2. **Copy** the project into a temp folder.
3. **Point** the copy's layers to the GeoPackage.
4. **Remove** the layer filters and the fields the styles don't use.
5. **Run** *Generate XYZ tiles (GeoPackage)* on the copy, at normal priority, with the chosen method:
   - **Built-in tool:** one process.
   - **Parallel workers:** several processes at once, each on its own part of the map, only where there are features.
     Before rendering, the plan is checked: every metatile is in exactly one job (no overlap, no gap).
     After rendering, each part is checked to hold only its own tiles, the parts are merged with zero duplicates allowed, and the result must open in GDAL with every zoom level.

## Requirements

QGIS 4.3 or later (the first version with *Generate XYZ tiles (GeoPackage)*).

## Usage

Processing Toolbox → **QGIS2RasterTiles**

**Generate XYZ tiles (GPKG) - full speed**

| Parameter | |
|---|---|
| Extent | area to tile |
| Minimum / maximum zoom | 0–25 |
| CPU usage limit (%) | share of the CPU cores to use |
| Tile generation method | *Built-in tool* (one process) or *Parallel workers* (faster, especially for sparse data) |
| Output directory | gets `tiles.gpkg` |

**Merge XYZ tiles (GPKG)** merges tile GeoPackages into one, e.g. a zooms 0–18 result and a zoom 19 result.
The inputs must share the same tile matrix; a tile present in several inputs is taken from the first one.

## Settings

[`defaults.json`](defaults.json) holds fixed values for the built-in tool's parameters, by their real names:
`TARGET_CRS`, `Z0_EXTENT`, `Z0_MATRIX_WIDTH`, `Z0_MATRIX_HEIGHT`, `TILE_WIDTH`, `TILE_HEIGHT`, `DPI`,
`BACKGROUND_COLOR`, `ANTIALIAS`, `TILE_FORMAT`, `QUALITY`, `METATILESIZE`, `SKIP_EMPTY_TILES`.
Parameters not listed keep the built-in default. `TARGET_CRS` is also the CRS of the packaged layers.

## License

[GPL-2.0](LICENSE)
