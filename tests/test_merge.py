"""Merge logic on small synthetic GeoPackages (no QGIS)."""

import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.merger import MergeError, merge_parts  # noqa: E402


def make_part(path, tiles, zooms, table="tiles", tms=(4326, -180.0, -90.0, 180.0, 90.0), unique=True,
              bounds=(1, 2, 3, 4)):
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
    con.execute("INSERT INTO gpkg_contents VALUES (?, 'tiles', ?, '', '2020', ?, ?, ?, ?, ?)",
                (table, table, *bounds, tms[0]))
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


class MergeToolTest(unittest.TestCase):
    """merge_tile_geopackages: the 'Merge XYZ tiles (GPKG)' tool."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def p(self, name):
        return os.path.join(self.d, name)

    def test_merge_zoom_ranges_with_gap(self):
        from src.merger import merge_tile_geopackages
        make_part(self.p("a.gpkg"), [(10, 1, 1), (11, 2, 2), (12, 4, 4)], [10, 11, 12], bounds=(34.0, 31.0, 35.0, 32.0))
        make_part(self.p("b.gpkg"), [(15, 32, 32), (15, 33, 32)], [15], bounds=(34.5, 30.0, 36.0, 31.5))
        with open(self.p("a.gpkg"), "rb") as fh:
            before = fh.read()
        rep = merge_tile_geopackages([self.p("a.gpkg"), self.p("b.gpkg")], self.p("out.gpkg"))
        self.assertEqual(rep.tiles_per_zoom, {10: 1, 11: 1, 12: 1, 13: 1, 14: 1, 15: 2})  # 13-14: filler tiles
        con = sqlite3.connect(self.p("out.gpkg"))
        rows = {r[0]: r[1:] for r in con.execute(
            "SELECT zoom_level, matrix_width, matrix_height, pixel_x_size FROM gpkg_tile_matrix")}
        self.assertEqual(sorted(rows), [10, 11, 12, 13, 14, 15])  # gap zooms 13-14 added
        for z in (13, 14):  # derived exactly like the existing rows
            self.assertEqual(rows[z][:2], (2 << z, 1 << z))
            self.assertAlmostEqual(rows[z][2], 180 / 256 / 2 ** z)
        self.assertEqual(con.execute("SELECT min_x, min_y, max_x, max_y FROM gpkg_contents").fetchone(),
                         (34.0, 30.0, 36.0, 32.0))  # union of the inputs' bounds
        con.close()
        with open(self.p("a.gpkg"), "rb") as fh:
            self.assertEqual(fh.read(), before)  # input untouched

    def test_first_input_wins_on_duplicates(self):
        from src.merger import merge_tile_geopackages
        make_part(self.p("a.gpkg"), [(12, 1, 1)], [12])
        make_part(self.p("b.gpkg"), [(12, 1, 1), (12, 2, 1)], [12])
        con = sqlite3.connect(self.p("b.gpkg"))
        con.execute("UPDATE tiles SET tile_data=? WHERE tile_column=1", (b"from-b",))
        con.commit()
        con.close()
        rep = merge_tile_geopackages([self.p("a.gpkg"), self.p("b.gpkg")], self.p("out.gpkg"))
        self.assertEqual(rep.ignored_duplicates, 1)
        con = sqlite3.connect(self.p("out.gpkg"))
        self.assertEqual(con.execute("SELECT tile_data FROM tiles WHERE tile_column=1").fetchone()[0], b"img12_1_1")
        con.close()

    def test_real_tile_replaces_filler(self):
        from src.merger import fill_empty_zooms, filler_tile, merge_tile_geopackages
        make_part(self.p("low.gpkg"), [(10, 1, 1)], [10, 11, 12], bounds=(-180, -90, 180, 90))
        self.assertEqual(fill_empty_zooms(self.p("low.gpkg"), 10, 12), [11, 12])
        con = sqlite3.connect(self.p("low.gpkg"))
        col, row = con.execute("SELECT tile_column, tile_row FROM tiles WHERE zoom_level=12").fetchone()
        con.close()
        self.assertEqual((col, row), (4096, 2048))  # centre of the extent
        make_part(self.p("high.gpkg"), [(12, col, row), (12, 5, 5)], [12])
        rep = merge_tile_geopackages([self.p("low.gpkg"), self.p("high.gpkg")], self.p("out.gpkg"))
        con = sqlite3.connect(self.p("out.gpkg"))
        tiles = {r[:3]: r[3] for r in con.execute("SELECT zoom_level, tile_column, tile_row, tile_data FROM tiles")}
        con.close()
        self.assertEqual(tiles[(12, col, row)], b"img12_%d_%d" % (col, row))  # the real tile, not the filler
        self.assertEqual(tiles[(11, 2048, 1024)], filler_tile())  # zoom 11 still has only its filler
        self.assertEqual(rep.tiles_per_zoom, {10: 1, 11: 1, 12: 2})

    def test_rejects_bad_inputs(self):
        from src.merger import merge_tile_geopackages
        make_part(self.p("a.gpkg"), [(12, 1, 1)], [12])
        make_part(self.p("b.gpkg"), [(12, 2, 1)], [12], tms=(3857, -1.0, -1.0, 1.0, 1.0))
        with self.assertRaisesRegex(MergeError, "must not be one of the inputs"):
            merge_tile_geopackages([self.p("a.gpkg")], self.p("a.gpkg"))
        with self.assertRaisesRegex(MergeError, "matrix set differs"):
            merge_tile_geopackages([self.p("a.gpkg"), self.p("b.gpkg")], self.p("out.gpkg"))
        with open(self.p("junk.gpkg"), "wb") as fh:
            fh.write(b"not a database")
        with self.assertRaisesRegex(MergeError, "not a tile GeoPackage"):
            merge_tile_geopackages([self.p("a.gpkg"), self.p("junk.gpkg")], self.p("out.gpkg"))


class FillEmptyZoomsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "t.gpkg")

    def tearDown(self):
        self.tmp.cleanup()

    def test_fills_only_empty_zooms_and_adds_matrix_rows(self):
        from src.merger import fill_empty_zooms, filler_tile
        make_part(self.path, [(17, 5, 5), (18, 10, 10)], [17, 18], bounds=(34.2, 29.45, 35.95, 33.35))
        self.assertEqual(fill_empty_zooms(self.path, 0, 18), list(range(0, 17)))
        con = sqlite3.connect(self.path)
        per = dict(con.execute("SELECT zoom_level, COUNT(*) FROM tiles GROUP BY 1"))
        self.assertEqual(per, {z: 1 for z in range(17)} | {17: 1, 18: 1})
        self.assertEqual([r[0] for r in con.execute("SELECT zoom_level FROM gpkg_tile_matrix ORDER BY 1")],
                         list(range(19)))
        n = 2 ** 13 / 360  # zoom 12: tiles per degree in the 2x1 scheme
        self.assertEqual(con.execute("SELECT tile_column, tile_row, tile_data FROM tiles WHERE zoom_level=12").fetchone(),
                         (int((35.075 + 180) * n), int((90 - 31.4) * n), filler_tile()))
        con.close()
        self.assertEqual(fill_empty_zooms(self.path, 0, 18), [])  # idempotent

    def test_filler_is_a_valid_transparent_png(self):
        import struct
        import zlib
        from src.merger import filler_tile
        png = filler_tile(256, 256)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">IIBB", png[16:26]), (256, 256, 8, 6))  # 8-bit RGBA
        idat_len = struct.unpack(">I", png[33:37])[0]
        raw = zlib.decompress(png[41:41 + idat_len])
        self.assertEqual(raw, (b"\x00" * (1 + 4 * 256)) * 256)
        self.assertEqual(filler_tile(), png)


if __name__ == "__main__":
    unittest.main()
