# Development

## Layout

| Path | Contents |
|---|---|
| `__init__.py`, `metadata.txt`, `defaults.json`, `icon.*` | plugin root (the repository root is the plugin folder) |
| `src/algorithm.py` | *Generate XYZ tiles (GPKG) - full speed* |
| `src/merge_algorithm.py`, `src/merger.py` | *Merge XYZ tiles (GPKG)* |
| `src/field_analysis.py` | fields a layer's style uses |
| `src/builtin_alg.py` | finds `native:tilesxyzgpkg`, reads and checks `defaults.json` |
| `src/qgis_process.py` | runs one `qgis_process` at normal priority (progress, cancel, CPU limit) |
| `tests/` | QGIS and plain-Python tests, integration comparisons |
| `tools/build_release.py` | builds `releases/QGIS2RasterTiles-<version>.zip` |

## Steps (`src/algorithm.py`)

1. **Package Layers** (`native:package`): visible vector layers, in render order, to one GeoPackage in `TARGET_CRS`; layer filters applied; styles saved. Feature counts are checked against the layers.
2. **Copy the project** to the temp folder (absolute paths) without changing the open project.
3. **Point the copy's layers** to the packaged tables (`setDataSource`, style kept). The copy is read without resolving its original sources, so the original data is not opened again.
4. **Remove filters**, joins and virtual fields: their values are real columns in the package.
5. **Remove unused fields** from the package (`field_analysis.py`).
6. **Run the built-in tool** on the copy in one `qgis_process` at normal priority, with the `defaults.json` values and the user's extent and zooms.

Hidden layers are removed from the copy; visible non-vector layers keep their original source.

## Tests

Plain Python:

    python -m unittest discover -s tests -p "test_merge.py"

QGIS, on Windows with OSGeo4W:

    cd tests
    python-qgis-dev.bat -m unittest test_qgis_components -v

Plugin vs built-in (tile bytes, timing, priority of the rendering process):

    python-qgis-dev.bat tests/integration_compare.py --zmin 12 --zmax 19
    python-qgis-dev.bat tests/integration_merge.py

## Release

Bump `version` and `changelog` in `metadata.txt`, then run:

    python tools/build_release.py

Upload the zip from `releases/` to plugins.qgis.org.
