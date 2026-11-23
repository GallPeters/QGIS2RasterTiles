"""QGIS-side component tests: grid vs QgsTileMatrix, field analysis, export, render project.

Run with the QGIS Python, e.g. on Windows/OSGeo4W:
    python-qgis-dev.bat -m unittest tests.test_qgis_components -v
"""

import os
import sqlite3
import tempfile
import unittest

from qgis_fixtures import EXTENT_4326, build_test_project, start_qgis  # noqa: E402

start_qgis()

from qgis.core import (  # noqa: E402
    QgsCoordinateReferenceSystem,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsTileMatrix,
    QgsTileXYZ,
    QgsVectorLayer,
)
from qgis.PyQt.QtCore import QThread  # noqa: E402

from src.exporter import export_layer  # noqa: E402
from src.field_analysis import analyze_layer_fields  # noqa: E402
from src.project_builder import build_render_project, snapshot_project  # noqa: E402
from src.tile_grid import Rect, TileGrid  # noqa: E402


class GridMatchesQgsTileMatrix(unittest.TestCase):
    def test_4326_2x1(self):
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        z0 = QgsTileMatrix.fromCustomDef(0, crs, QgsPointXY(-180, 90), 180.0, 2, 1)
        g = TileGrid.for_target("EPSG:4326", (-180, 180, -90, 90), 2, 1, 256, 256)
        for z in (0, 3, 10, 16, 19):
            m = QgsTileMatrix.fromTileMatrix(z, z0)
            self.assertEqual((m.matrixWidth(), m.matrixHeight()), g.matrix_size(z))
            for col, row in ((0, 0), (5, 3), (m.matrixWidth() - 1, m.matrixHeight() - 1)):
                te = m.tileExtent(QgsTileXYZ(col, row, z))
                b = g.tile_bounds(col, row, z)
                for a, c in ((te.xMinimum(), b.xmin), (te.yMinimum(), b.ymin),
                             (te.xMaximum(), b.xmax), (te.yMaximum(), b.ymax)):
                    self.assertAlmostEqual(a, c, places=9)


class OccupancyScanMatchesBruteForce(unittest.TestCase):
    """Large features use the exact GEOS quadtree descent; it must mark exactly
    the metatiles a brute-force intersection test marks."""

    def test_lines_and_polygon(self):
        from qgis.core import QgsCoordinateTransformContext, QgsFeature, QgsGeometry, QgsVectorFileWriter
        from src.occupancy_scan import scan_vector_file
        from src.partition import Occupancy
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        wkts = ["LineString (34.702 32.000, 34.760 32.090, 34.898 32.170)",
                "LineString (34.71 32.10, 34.89 32.10)",
                "Polygon ((34.80 32.00, 34.88 32.00, 34.88 32.06, 34.80 32.06, 34.80 32.00),"
                " (34.82 32.02, 34.86 32.02, 34.86 32.04, 34.82 32.04, 34.82 32.02))"]
        geoms = [QgsGeometry.fromWkt(w) for w in wkts]
        for k, w in enumerate(wkts):
            mem = QgsVectorLayer(("LineString" if k < 2 else "Polygon") + "?crs=EPSG:4326", "l", "memory")
            f = QgsFeature()
            f.setGeometry(geoms[k])
            mem.dataProvider().addFeatures([f])
            opts = QgsVectorFileWriter.SaveVectorOptions()
            opts.driverName = "FlatGeobuf"
            QgsVectorFileWriter.writeAsVectorFormatV3(mem, os.path.join(tmp.name, f"g{k}.fgb"),
                                                      QgsCoordinateTransformContext(), opts)
        g = TileGrid.for_target("EPSG:4326", (-180, 180, -90, 90), 2, 1, 256, 256)
        ext = Rect(34.70, 31.98, 34.90, 32.18)
        occ = Occupancy(g, ext, [16, 18], 8, 50)
        for k in range(len(wkts)):
            scan_vector_file(os.path.join(tmp.name, f"g{k}.fgb"), occ, lambda: False)
        for z, zo in occ.zooms.items():
            expected = set()
            for i in range(zo.mgrid.meta_cols):
                for j in range(zo.mgrid.meta_rows):
                    r = zo.metatile_rect(i, i, j, j).buffered(zo.margin)
                    rg = QgsGeometry.fromRect(QgsRectangle(r.xmin, r.ymin, r.xmax, r.ymax))
                    if any(gm.intersects(rg) for gm in geoms):
                        expected.add((i, j))
            self.assertEqual(set(zo.cells), expected, f"zoom {z}")
            self.assertLess(len(expected), zo.mgrid.count)  # the hole and empty areas are skipped
        tmp.cleanup()


class FieldsExportProject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.project_path = build_test_project(os.path.join(cls.tmp.name, "src"))
        cls.project = QgsProject.instance()
        assert cls.project.read(cls.project_path)

    @classmethod
    def tearDownClass(cls):
        cls.project.clear()
        cls.tmp.cleanup()

    def layer(self, name) -> QgsVectorLayer:
        return self.project.mapLayersByName(name)[0]

    def test_required_fields_rule_based(self):
        a = analyze_layer_fields(self.layer("poi"))
        self.assertFalse(a.keep_all, a.warnings)
        # rule filters (kind), DD size (size), DD colour expression (prio),
        # label rule filter (size) and label text on a virtual field (label_txt)
        self.assertEqual(set(a.required), {"kind", "size", "prio", "label_txt"})
        self.assertEqual(a.virtual_fields, ["label_txt"])

    def test_required_fields_categorized_and_simple_labels(self):
        self.assertEqual(set(analyze_layer_fields(self.layer("roads")).required), {"cls", "w"})
        self.assertEqual(set(analyze_layer_fields(self.layer("parks")).required), {"pname"})

    def test_else_rules_are_not_parse_errors(self):
        """ELSE rules (filter text 'ELSE') must not trigger the keep-all-fields fallback."""
        from qgis.core import QgsMarkerSymbol, QgsPalLayerSettings, QgsRuleBasedLabeling, QgsRuleBasedRenderer
        lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=kind:string&field=junk:string&field=name:string", "t", "memory")
        root = QgsRuleBasedRenderer.Rule(None)
        root.appendChild(QgsRuleBasedRenderer.Rule(QgsMarkerSymbol.createSimple({}), 0, 0, "\"kind\" = 'a'", "a"))
        else_rule = QgsRuleBasedRenderer.Rule(QgsMarkerSymbol.createSimple({}), 0, 0, "", "")
        else_rule.setIsElse(True)
        root.appendChild(else_rule)
        lyr.setRenderer(QgsRuleBasedRenderer(root))
        s = QgsPalLayerSettings()
        s.fieldName = "name"
        lroot = QgsRuleBasedLabeling.Rule(None)
        lab_else = QgsRuleBasedLabeling.Rule(QgsPalLayerSettings(s), 0, 0, "", "")
        lab_else.setIsElse(True)
        lroot.appendChild(lab_else)
        lyr.setLabeling(QgsRuleBasedLabeling(lroot))
        lyr.setLabelsEnabled(True)
        a = analyze_layer_fields(lyr)
        self.assertFalse(a.keep_all, a.warnings)
        self.assertEqual(set(a.required), {"kind", "name"})
        self.assertEqual(a.warnings, [])

    def test_unparseable_expression_keeps_all(self):
        lyr = self.layer("parks").clone()
        s = lyr.labeling().settings()
        s.fieldName = "concat(\"pname\", "  # broken
        s.isExpression = True
        from qgis.core import QgsVectorLayerSimpleLabeling
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
        a = analyze_layer_fields(lyr)
        self.assertTrue(a.keep_all)
        self.assertEqual(len(a.required), lyr.fields().count())

    def test_export_filter_virtual_and_reprojection(self):
        poi = self.layer("poi")
        a = analyze_layer_fields(poi)
        out = os.path.join(self.tmp.name, "poi.fgb")
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        x0, y0, x1, y1 = EXTENT_4326
        rect = QgsRectangle(x0 + 0.01, y0 + 0.01, x1 - 0.01, y1 - 0.01)
        res = export_layer(poi, a, out, crs, rect, self.project.transformContext())
        self.assertEqual(res.exported_count, res.expected_count)
        self.assertGreater(res.exported_count, 0)
        lyr = QgsVectorLayer(out, "x", "ogr")
        self.assertEqual(lyr.crs().authid(), "EPSG:4326")
        self.assertEqual(lyr.subsetString(), "")
        self.assertEqual(set(f.name() for f in lyr.fields()) - {"fid"}, set(a.required))
        names = [f["label_txt"] for f in lyr.getFeatures()]
        self.assertTrue(all(n == n.upper() and n.startswith("P") for n in names))
        # the subset filter (id % 7 <> 0) was applied: no exported label is P0, P7, P14, ...
        ids = {int(n[1:]) for n in names}
        self.assertFalse(any(i % 7 == 0 for i in ids))
        # filterExtent is in the target CRS
        for f in lyr.getFeatures():
            p = f.geometry().asPoint()
            self.assertTrue(rect.contains(p), p)
        del lyr

    def test_export_feature_accounting(self):
        """Ids are compared source vs. export; features outside the area are counted, not lost."""
        from qgis.core import QgsField
        from qgis.PyQt.QtCore import QMetaType
        poi = self.layer("poi").clone()
        poi.addExpressionField("$id", QgsField("_pxyz_fid", QMetaType.Type.LongLong))
        a = analyze_layer_fields(poi)
        a.required.append("_pxyz_fid")
        x0, y0, x1, y1 = EXTENT_4326
        rect = QgsRectangle(x0, y0, (x0 + x1) / 2, y1)  # west half only
        res = export_layer(poi, a, os.path.join(self.tmp.name, "acc.fgb"), QgsCoordinateReferenceSystem("EPSG:4326"),
                           rect, self.project.transformContext(), id_field="_pxyz_fid")
        self.assertEqual(res.missing_ids, [])
        self.assertEqual(res.extra_ids, [])
        self.assertEqual(res.exported_count, res.expected_count)
        self.assertEqual(res.source_count, poi.featureCount())   # layer filter applied
        self.assertGreater(res.outside_area, 0)                   # east half counted as outside
        self.assertEqual(res.source_count, res.expected_count + res.outside_area)

    def test_python_expression_functions_detected(self):
        from qgis.core import qgsfunction
        from src.render_check import python_functions_used

        @qgsfunction(group="Custom")
        def pxyz_test_func(value, feature, parent):
            return value

        self.assertEqual(python_functions_used(['upper("name")', 'pxyz_test_func(1) + 2']), {"pxyz_test_func"})
        self.assertEqual(python_functions_used(['upper("name")']), set())

    def test_render_project(self):
        snap = snapshot_project(self.project)
        names = [snap.layers[i].name() for i in snap.render_order]
        self.assertEqual(names, ["poi", "roads", "parks"])  # hidden layer excluded, drawing order kept
        snap.adopt_layers()
        crs = QgsCoordinateReferenceSystem("EPSG:4326")
        exported = {}
        for n, lid in enumerate(snap.vector_ids()):
            lyr = snap.layers[lid]
            out = os.path.join(self.tmp.name, f"l{n}.fgb")
            r = export_layer(lyr, analyze_layer_fields(lyr), out, crs, None, snap.transform_context)
            exported[lid] = (r.path, r.exported_count)
        qgz = os.path.join(self.tmp.name, "render.qgz")
        rep = build_render_project(snap, exported, crs, qgz, {snap.render_order[0]: "label_txt"})
        self.assertTrue(os.path.isfile(qgz))
        p = QgsProject()
        self.assertTrue(p.read(qgz))
        self.assertEqual(p.crs().authid(), "EPSG:4326")
        from qgis.core import Qgis
        self.assertEqual(p.scaleMethod(), Qgis.ScaleCalculationMethod.AtEquator)  # project scale method kept
        order = [l.name() for l in p.layerTreeRoot().layerOrder()]
        self.assertEqual(order, ["poi", "roads", "parks"])
        self.assertIsNotNone(p.layerTreeRoot().findGroup("Base"))
        poi = p.mapLayersByName("poi")[0]
        self.assertEqual(poi.id(), snap.render_order[0])  # original id kept
        self.assertEqual(poi.renderer().type(), "RuleRenderer")
        self.assertEqual(poi.labeling().type(), "rule-based")
        self.assertTrue(poi.labelsEnabled())
        self.assertTrue(poi.renderer().orderByEnabled())
        parks = p.mapLayersByName("parks")[0]
        self.assertAlmostEqual(parks.opacity(), 0.8)
        self.assertEqual(p.mapLayersByName("roads")[0].renderer().type(), "categorizedSymbol")
        self.assertTrue(any("OK" in c for c in rep.checks))
        p.clear()
        snap.dispose()


if __name__ == "__main__":
    unittest.main()
