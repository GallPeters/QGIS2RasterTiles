"""Merging partial GeoPackage tile files into one.

Only the standard ``sqlite3`` module is used. The first part (normally the
full-extent low-zoom part) is copied as the base; every other part is
ATTACHed and its tiles and tile-matrix rows are copied with
``INSERT OR IGNORE``.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import sqlite3
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger("qgis2rastertiles." + __name__.rsplit(".", 1)[-1])

#: (matrix_width, matrix_height, tile_width, tile_height, pixel_x_size, pixel_y_size)
MatrixRow = Tuple[int, int, int, int, float, float]


class MergeError(RuntimeError):
    """Raised when parts cannot be merged consistently."""


@dataclass
class MergeReport:
    output: str
    tile_table: str
    tiles_per_zoom: Dict[int, int] = field(default_factory=dict)
    expected_per_zoom: Dict[int, int] = field(default_factory=dict)
    ignored_duplicates: int = 0
    integrity: str = ""

    @property
    def total_tiles(self) -> int:
        return sum(self.tiles_per_zoom.values())


def _qi(name: str) -> str:
    """Quote an SQL identifier."""
    return '"' + name.replace('"', '""') + '"'


def checkpoint(path: str) -> None:
    """Fold a pending WAL file back into the database so the file can be copied safely.

    Does nothing (and never writes to ``path``) when there is no WAL file.
    """
    if not os.path.exists(path + "-wal"):
        return
    con = sqlite3.connect(path)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.execute("PRAGMA journal_mode=DELETE")
    finally:
        con.close()


def tile_table_name(con: sqlite3.Connection, schema: str = "main") -> str:
    rows = con.execute(f"SELECT table_name FROM {schema}.gpkg_contents WHERE data_type='tiles'").fetchall()
    if len(rows) != 1:
        raise MergeError(f"expected exactly one tiles table in gpkg_contents ({schema}), found {len(rows)}")
    return rows[0][0]


def _matrix_set(con: sqlite3.Connection, schema: str, table: str) -> Tuple:
    row = con.execute(
        f"SELECT srs_id, min_x, min_y, max_x, max_y FROM {schema}.gpkg_tile_matrix_set WHERE table_name=?",
        (table,)).fetchone()
    if row is None:
        raise MergeError(f"no gpkg_tile_matrix_set row for '{table}' in {schema}")
    return row


def _matrices(con: sqlite3.Connection, schema: str, table: str) -> Dict[int, Tuple]:
    return {r[0]: tuple(r[1:]) for r in con.execute(
        f"SELECT zoom_level, matrix_width, matrix_height, tile_width, tile_height, pixel_x_size, pixel_y_size "
        f"FROM {schema}.gpkg_tile_matrix WHERE table_name=?", (table,))}


def _close(a: Sequence, b: Sequence) -> bool:
    for x, y in zip(a, b):
        if isinstance(x, float) or isinstance(y, float):
            if not math.isclose(float(x), float(y), rel_tol=1e-9, abs_tol=1e-12):
                return False
        elif x != y:
            return False
    return len(a) == len(b)


def tiles_per_zoom(path: str) -> Dict[int, int]:
    con = sqlite3.connect(path)
    try:
        t = tile_table_name(con)
        return {z: n for z, n in con.execute(f"SELECT zoom_level, COUNT(*) FROM {_qi(t)} GROUP BY zoom_level")}
    finally:
        con.close()


def validate_part(path: str) -> bool:
    """True if ``path`` is a readable GeoPackage with one tiles table."""
    if not os.path.isfile(path):
        return False
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tile_table_name(con)
            return True
        finally:
            con.close()
    except (sqlite3.Error, MergeError):
        return False


def merge_parts(parts: Sequence[str], output: str, full_extent: Tuple[float, float, float, float],
                zoom_min: int, zoom_max: int, matrix_row: Callable[[int], MatrixRow],
                vacuum: bool = False, progress: Optional[Callable[[float], None]] = None,
                expect_duplicates: bool = False,
                is_canceled: Optional[Callable[[], bool]] = None) -> MergeReport:
    """Merge ``parts`` into ``output``.

    :param parts: part GeoPackages; the first one becomes the base.
    :param full_extent: (xmin, ymin, xmax, ymax) of the requested output extent
        in the target CRS, written to gpkg_contents.
    :param matrix_row: returns the gpkg_tile_matrix values for a zoom, used to
        add rows for zooms that no part contains.
    """
    if not parts:
        raise MergeError("no parts to merge")
    for p in parts:
        if not os.path.isfile(p):
            raise MergeError(f"part not found: {p}")
        checkpoint(p)

    expected: Dict[int, int] = {}
    for p in parts:
        for z, n in tiles_per_zoom(p).items():
            expected[z] = expected.get(z, 0) + n

    if os.path.exists(output):
        log.warning("Removing existing output file %s", output)
        os.remove(output)
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.exists(output + suffix):
            os.remove(output + suffix)
    shutil.copyfile(parts[0], output)

    con = sqlite3.connect(output, isolation_level=None)
    ignored = 0
    try:
        con.execute("PRAGMA journal_mode=DELETE")
        con.execute("PRAGMA synchronous=OFF")
        table = tile_table_name(con)
        base_set = _matrix_set(con, "main", table)
        con.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_qi('uk_' + table)} ON {_qi(table)} "
                    f"(zoom_level, tile_column, tile_row)")
        base_matrices = _matrices(con, "main", table)

        for n, p in enumerate(parts[1:], start=1):
            if is_canceled and is_canceled():
                raise MergeError("merge canceled")
            con.execute("ATTACH DATABASE ? AS part", (p,))
            try:
                ptable = tile_table_name(con, "part")
                if ptable != table:
                    raise MergeError(f"tile table name differs: '{ptable}' in {p} vs '{table}' in base")
                pset = _matrix_set(con, "part", ptable)
                if not _close(pset, base_set):
                    raise MergeError(f"tile matrix set differs in {p}: {pset} vs {base_set}")
                pm = _matrices(con, "part", ptable)
                for z, row in pm.items():
                    if z in base_matrices and not _close(row, base_matrices[z]):
                        raise MergeError(f"gpkg_tile_matrix for zoom {z} differs in {p}: {row} vs {base_matrices[z]}")
                part_count = con.execute(f"SELECT COUNT(*) FROM part.{_qi(ptable)}").fetchone()[0]
                con.execute("BEGIN")
                cur = con.execute(
                    f"INSERT OR IGNORE INTO main.{_qi(table)} (zoom_level, tile_column, tile_row, tile_data) "
                    f"SELECT zoom_level, tile_column, tile_row, tile_data FROM part.{_qi(ptable)}")
                inserted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else part_count
                ignored += part_count - inserted
                con.execute(
                    "INSERT OR IGNORE INTO main.gpkg_tile_matrix (table_name, zoom_level, matrix_width, matrix_height, "
                    "tile_width, tile_height, pixel_x_size, pixel_y_size) SELECT ?, zoom_level, matrix_width, "
                    "matrix_height, tile_width, tile_height, pixel_x_size, pixel_y_size FROM part.gpkg_tile_matrix "
                    "WHERE table_name=?", (table, ptable))
                con.execute("COMMIT")
                for z, row in pm.items():
                    base_matrices.setdefault(z, row)
            except Exception:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            finally:
                con.execute("DETACH DATABASE part")
            if progress:
                progress(n / max(1, len(parts) - 1))

        con.execute("BEGIN")
        for z in range(zoom_min, zoom_max + 1):
            if z not in base_matrices:
                con.execute("INSERT OR IGNORE INTO gpkg_tile_matrix (table_name, zoom_level, matrix_width, "
                            "matrix_height, tile_width, tile_height, pixel_x_size, pixel_y_size) "
                            "VALUES (?,?,?,?,?,?,?,?)", (table, z, *matrix_row(z)))
        xmin, ymin, xmax, ymax = full_extent
        con.execute("UPDATE gpkg_contents SET min_x=?, min_y=?, max_x=?, max_y=?, "
                    "last_change=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE table_name=?",
                    (xmin, ymin, xmax, ymax, table))
        con.execute("COMMIT")

        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        per_zoom = {z: c for z, c in con.execute(
            f"SELECT zoom_level, COUNT(*) FROM {_qi(table)} GROUP BY zoom_level ORDER BY zoom_level")}
        if vacuum:
            con.execute("VACUUM")
    finally:
        con.close()

    if integrity != "ok":
        raise MergeError(f"PRAGMA integrity_check failed on {output}: {integrity}")
    report = MergeReport(output, table, per_zoom, expected, ignored, integrity)
    if ignored == 0:
        for z in sorted(set(expected) | set(per_zoom)):
            if per_zoom.get(z, 0) != expected.get(z, 0):
                raise MergeError(f"tile count mismatch at zoom {z}: merged {per_zoom.get(z, 0)}, "
                                 f"parts {expected.get(z, 0)}")
    else:
        (log.debug if expect_duplicates else log.warning)(
            "%d duplicate tiles were ignored while merging (parts overlapped)", ignored)
    return report


def merge_tile_geopackages(inputs: Sequence[str], output: str, vacuum: bool = False,
                           progress: Optional[Callable[[float], None]] = None,
                           is_canceled: Optional[Callable[[], bool]] = None) -> MergeReport:
    """Merge any tile GeoPackages that share one tile matrix set (e.g. zooms 0-18 + zoom 19).

    * the tile table name and the tile matrix set must be identical in every input;
    * the output's gpkg_contents bounds are the union of the inputs' bounds;
    * gpkg_tile_matrix covers every zoom from the lowest to the highest input zoom;
      rows for zooms no input has are derived from the nearest zoom (factor 2 per zoom);
    * a tile present in several inputs is taken from the first input listed.
    Inputs are not modified (only a pending WAL file is folded back first).
    """
    inputs = [os.path.abspath(p) for p in inputs]
    out_abs = os.path.abspath(output)
    if not inputs:
        raise MergeError("no input GeoPackages")
    if out_abs in inputs:
        raise MergeError("the output file must not be one of the inputs")
    bounds = None
    matrices: Dict[int, Tuple] = {}
    for p in inputs:
        if not validate_part(p):
            raise MergeError(f"not a tile GeoPackage (one tiles table expected): {p}")
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        try:
            table = tile_table_name(con)
            b = con.execute("SELECT min_x, min_y, max_x, max_y FROM gpkg_contents WHERE table_name=?",
                            (table,)).fetchone()
            if b and None not in b:
                bounds = b if bounds is None else (min(bounds[0], b[0]), min(bounds[1], b[1]),
                                                   max(bounds[2], b[2]), max(bounds[3], b[3]))
            for z, row in _matrices(con, "main", table).items():
                matrices.setdefault(z, row)
        finally:
            con.close()
    if not matrices:
        raise MergeError("the inputs have no gpkg_tile_matrix rows")
    if bounds is None:
        raise MergeError("the inputs have no extent in gpkg_contents")

    def matrix_row(z: int) -> MatrixRow:
        k = min(matrices, key=lambda m: abs(m - z))
        w, h, tw, th, px, py = matrices[k]
        f = 2.0 ** (z - k)
        return int(round(w * f)), int(round(h * f)), tw, th, px / f, py / f

    return merge_parts(inputs, out_abs, tuple(bounds), min(matrices), max(matrices), matrix_row,
                       vacuum=vacuum, progress=progress, expect_duplicates=True, is_canceled=is_canceled)