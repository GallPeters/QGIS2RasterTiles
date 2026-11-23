"""Merge logic on small synthetic GeoPackages (no QGIS)."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.merger import MergeError, merge_parts  # noqa: E402


def make_part(path, tiles, zooms, table="tiles", tms=(4326, -180.0, -90.0, 180.0, 90.0), unique=True):
    """Create a GeoPackage shaped like the one written by QGIS' tile writer."""
    con = sqlite3.connect(path)
    con.executescript(f"""
        CREATE TABLE gpkg_spatial_ref_sys (srs_name TEXT, srs_id INTEGER PRIMARY KEY, organization TEXT,
            organization_coordsys_id INTEGER, definition TEXT, description TEXT);
        CREATE TABLE gpkg_contents (table_name TEXT PRIMARY KEY, data_type TEXT NOT NULL, identifier TEXT UNIQUE,
            description TEXT DEFAULT '', last_change DATETIME, min_x DOUBLE, min_y DOUBLE, max_x DOUBLE,
            max_y DOUBLE, srs_id INTEGER);
        CREATE TABLE gpkg_tile_matrix_set (table_name TEXT PRIMARY KEY, srs_id INTEGER NOT NULL,
            min_x DOUBLE, min_y DOUBLE, max_x DOUBLE, max_y DOUBLE);
        CREATE TABLE gpkg_tile_matrix (table_name TEXT NOT NULL, zoom_level INTEGER NOT NULL,
            matrix_width INTEGER, matrix_height INTEGER, tile_width INTEGER, tile_height INTEGER,
            pixel_x_size DOUBLE, pixel_y_size DOUBLE, CONSTRAINT pk_ttm PRIMARY KEY (table_name, zoom_level));
        CREATE TABLE "{table}" (id INTEGER PRIMARY KEY AUTOINCREMENT, zoom_level INTEGER NOT NULL,
            tile_column INTEGER NOT NULL, tile_row INTEGER NOT NULL, tile_data BLOB NOT NULL);
    """)
    if unique:
        con.execute(f'CREATE UNIQUE INDEX uk_{table} ON "{table}" (zoom_level, tile_column, tile_row)')
    con.execute("INSERT INTO gpkg_contents VALUES (?, 'tiles', ?, '', '2020', 1, 2, 3, 4, ?)", (table, table, tms[0]))
    con.execute("INSERT INTO gpkg_tile_matrix_set VALUES (?,?,?,?,?,?)", (table, *tms))
    for z in zooms:
        con.execute("INSERT INTO gpkg_tile_matrix VALUES (?,?,?,?,256,256,?,?)",
                    (table, z, 2 << z, 1 << z, 180 / 256 / 2 ** z, 180 / 256 / 2 ** z))
    con.executemany(f'INSERT INTO "{table}" (zoom_level, tile_column, tile_row, tile_data) VALUES (?,?,?,?)',
                    [(z, c, r, b"img%d_%d_%d" % (z, c, r)) for z, c, r in tiles])
    con.commit()
    con.close()


def matrix_row(z):
    return (2 << z, 1 << z, 256, 256, 180 / 256 / 2 ** z, 180 / 256 / 2 ** z)


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, name):
        return os.path.join(self.d, name)

    def test_merge_three_parts(self):
        make_part(self.p("low.gpkg"), [(10, 1, 1), (11, 2, 2), (11, 3, 2)], [10, 11])
        make_part(self.p("a.gpkg"), [(12, 4, 4), (12, 5, 4)], [12])
        make_part(self.p("b.gpkg"), [(12, 8, 8), (13, 16, 16)], [12, 13])
        out = self.p("out.gpkg")
        rep = merge_parts([self.p("low.gpkg"), self.p("a.gpkg"), self.p("b.gpkg")], out,
                          (34.0, 31.0, 35.0, 32.0), 10, 14, matrix_row)
        self.assertEqual(rep.tiles_per_zoom, {10: 1, 11: 2, 12: 3, 13: 1})
        self.assertEqual(rep.integrity, "ok")
        con = sqlite3.connect(out)
        zooms = [r[0] for r in con.execute("SELECT zoom_level FROM gpkg_tile_matrix ORDER BY zoom_level")]
        self.assertEqual(zooms, [10, 11, 12, 13, 14])  # 14 added although no part has it
        bounds = con.execute("SELECT min_x, min_y, max_x, max_y FROM gpkg_contents").fetchone()
        self.assertEqual(bounds, (34.0, 31.0, 35.0, 32.0))
        blob = con.execute("SELECT tile_data FROM tiles WHERE zoom_level=13").fetchone()[0]
        self.assertEqual(blob, b"img13_16_16")
        con.close()

    def test_duplicates_ignored(self):
        make_part(self.p("a.gpkg"), [(12, 1, 1)], [12])
        make_part(self.p("b.gpkg"), [(12, 1, 1), (12, 2, 1)], [12])
        rep = merge_parts([self.p("a.gpkg"), self.p("b.gpkg")], self.p("o.gpkg"), (0, 0, 1, 1), 12, 12, matrix_row)
        self.assertEqual(rep.tiles_per_zoom, {12: 2})
        self.assertEqual(rep.ignored_duplicates, 1)

    def test_base_without_unique_index(self):
        make_part(self.p("a.gpkg"), [(12, 1, 1)], [12], unique=False)
        make_part(self.p("b.gpkg"), [(12, 1, 1)], [12])
        rep = merge_parts([self.p("a.gpkg"), self.p("b.gpkg")], self.p("o.gpkg"), (0, 0, 1, 1), 12, 12, matrix_row)
        self.assertEqual(rep.tiles_per_zoom, {12: 1})

    def test_different_table_name_fails(self):
        make_part(self.p("a.gpkg"), [(12, 1, 1)], [12])
        make_part(self.p("b.gpkg"), [(12, 2, 1)], [12], table="other")
        with self.assertRaisesRegex(MergeError, "table name differs"):
            merge_parts([self.p("a.gpkg"), self.p("b.gpkg")], self.p("o.gpkg"), (0, 0, 1, 1), 12, 12, matrix_row)

    def test_different_matrix_set_fails(self):
        make_part(self.p("a.gpkg"), [(12, 1, 1)], [12])
        make_part(self.p("b.gpkg"), [(12, 2, 1)], [12], tms=(4326, -180.0, -90.0, 180.0, 80.0))
        with self.assertRaisesRegex(MergeError, "matrix set differs"):
            merge_parts([self.p("a.gpkg"), self.p("b.gpkg")], self.p("o.gpkg"), (0, 0, 1, 1), 12, 12, matrix_row)

    def test_existing_output_replaced(self):
        make_part(self.p("a.gpkg"), [(12, 1, 1)], [12])
        with open(self.p("o.gpkg"), "wb") as fh:
            fh.write(b"junk")
        rep = merge_parts([self.p("a.gpkg")], self.p("o.gpkg"), (0, 0, 1, 1), 12, 12, matrix_row)
        self.assertEqual(rep.total_tiles, 1)


if __name__ == "__main__":
    unittest.main()
