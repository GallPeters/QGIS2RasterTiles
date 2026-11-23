# Development

## Layout

| Path | Contents |
|---|---|
| `__init__.py`, `metadata.txt`, `defaults.json`, `icon.*` | plugin root (the repository root is the plugin folder) |
| `src/` | plugin code |
| `tests/` | unit, QGIS and integration tests |
| `tools/build_release.py` | builds `releases/QGIS2RasterTiles-<version>.zip` |

## Pipeline (`src/pipeline.py`)

1. **Snapshot** (main thread): visible layers in drawing order, cloned and detached from the main thread; tree, groups and project settings.
2. **Fields** (`field_analysis.py`): fields used by renderer, labels, data-defined properties, diagrams; all fields if an expression does not parse.
3. **Export** (`exporter.py`): FlatGeobuf per layer: used fields + `$id`, layer filter applied, render area + margin, reprojected.
4. **Render project** (`project_builder.py`): new `.qgz`, same tree, layer ids and styles (QML), absolute resource paths.
5. **Plan** (`partition.py`, `occupancy_scan.py`): zooms below `split_zoom` in one job; from `split_zoom` on, blocks of the single run's own metatiles that contain features.
6. **Jobs** (`job_runner.py`): `qgis_process run native:tilesxyzgpkg -` in parallel, below-normal priority, same user profile, retry, resume manifest. With `max_parallel_jobs: "auto"` the scheduler starts half the usable cores and samples system CPU (`sysload.py`) every 5 s: below the target it adds processes (up to one per usable core, if RAM allows), above it lets the count shrink as jobs finish.
7. **Merge** (`merger.py`): `ATTACH` + `INSERT OR IGNORE`, tile-matrix check, integrity check.

The built-in algorithm anchors metatiles at the first tile of the requested range at every zoom. Partitioned jobs therefore cover one zoom each and start on the single run's metatile origins, which keeps tiles identical.

## Feature accounting

The run stops if any count does not match:

- source ids (layer filter applied) inside the render area equal the exported ids;
- the saved render project, reloaded from disk, has the exported counts;
- every exported feature with a geometry is scanned for occupancy;
- every occupied metatile is inside a job.

Features entirely outside the render area are reported; no tile shows them, in the built-in tool either.

## Tests

Plain Python, no QGIS needed:

    python -m unittest discover -s tests -p "test_*.py"

QGIS, on Windows with OSGeo4W:

    cd tests
    python-qgis-dev.bat -m unittest test_qgis_components -v

Built-in vs plugin (tile keys, pixels, timing):

    python-qgis-dev.bat tests/integration_compare.py --zmin 12 --zmax 18 --extent 34.70,31.98,34.90,32.18

## Release

Bump `version` and `changelog` in `metadata.txt`, then run:

    python tools/build_release.py

Upload the zip from `releases/` to plugins.qgis.org.
