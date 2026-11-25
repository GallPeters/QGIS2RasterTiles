"""QGIS tests: field analysis, defaults.json, the project copy (steps 1-5).

Run with the QGIS Python, e.g. on Windows/OSGeo4W:
    cd tests
    python-qgis-dev.bat -m unittest test_qgis_components -v
"""

import json
import os
import sqlite3
import tempfile
import unittest

from qgis_fixtures import build_test_project, start_qgis  # noqa: E402

start_qgis()

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsRuleBasedLabeling,
    QgsRuleBasedRenderer,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)

from src.algorithm import check_feature_counts, prepare_project_copy, write_project_copy  # noqa: E402
from src.builtin_alg import ConfigError, find_xyz_gpkg_algorithm, load_defaults  # noqa: E402
from src.field_analysis import analyze_layer_fields  # noqa: E402
from src.qgis_process import last_percent, profile_arguments  # noqa: E402


def init_processing():
    import sys
    sys.path.append(os.path.join(QgsApplication.prefixPath(), "python", "plugins"))
    from processing.core.Processing import Processing
    Processing.initialize()


class Defaults(unittest.TestCase):
    def setUp(self):
        self.alg = find_xyz_gpkg_algorithm()

    def write(self, data):
        fd, path = tempfile.mkstemp(suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh)
        self.addCleanup(os.remove, path)
        return path

    def test_shipped_defaults(self):
        d = load_defaults(self.alg)
        self.assertEqual(self.alg.id(), "native:tilesxyzgpkg")
        self.assertEqual(d["TILE_FORMAT"], 0)  # "PNG" -> enum index
        self.assertEqual(d["METATILESIZE"], 8)
        self.assertEqual(d["TARGET_CRS"], "EPSG:4326")

    def test_errors(self):
        with self.assertRaisesRegex(ConfigError, "TYPO"):
            load_defaults(self.alg, self.write({"TYPO": 1}))
        with self.assertRaisesRegex(ConfigError, "ZOOM_MIN"):
            load_defaults(self.alg, self.write({"ZOOM_MIN": 3}))  # set by the plugin's own parameter
        with self.assertRaisesRegex(ConfigError, "TILE_FORMAT"):
            load_defaults(self.alg, self.write({"TILE_FORMAT": "TIFF"}))


class FieldAnalysis(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.project = QgsProject.instance()
        assert cls.project.read(build_test_project(os.path.join(cls.tmp.name, "src")))

    @classmethod
    def tearDownClass(cls):
        cls.project.clear()
        cls.tmp.cleanup()

    def layer(self, name):
        return self.project.mapLayersByName(name)[0]

    def test_rule_based_dd_and_virtual(self):
        a = analyze_layer_fields(self.layer("poi"))
        self.assertFalse(a.keep_all, a.warnings)
        self.assertEqual(set(a.required), {"kind", "size", "prio", "label_txt"})

    def test_categorized_and_simple_labels(self):
        self.assertEqual(set(analyze_layer_fields(self.layer("roads")).required), {"cls", "w"})
        self.assertEqual(set(analyze_layer_fields(self.layer("parks")).required), {"pname"})

    def test_else_rules(self):
        lyr = QgsVectorLayer("Point?crs=EPSG:4326&field=kind:string&field=junk:string&field=name:string", "t", "memory")
        root = QgsRuleBasedRenderer.Rule(None)
        root.appendChild(QgsRuleBasedRenderer.Rule(QgsMarkerSymbol.createSimple({}), 0, 0, "\"kind\" = 'a'", "a"))
        e = QgsRuleBasedRenderer.Rule(QgsMarkerSymbol.createSimple({}), 0, 0, "", "")
        e.setIsElse(True)
        root.appendChild(e)
        lyr.setRenderer(QgsRuleBasedRenderer(root))
        s = QgsPalLayerSettings()
        s.fieldName = "name"
        lroot = QgsRuleBasedLabeling.Rule(None)
        le = QgsRuleBasedLabeling.Rule(QgsPalLayerSettings(s), 0, 0, "", "")
        le.setIsElse(True)
        lroot.appendChild(le)
        lyr.setLabeling(QgsRuleBasedLabeling(lroot))
        lyr.setLabelsEnabled(True)
        a = analyze_layer_fields(lyr)
        self.assertEqual((a.keep_all, set(a.required), a.warnings), (False, {"kind", "name"}, []))

    def test_unparseable_keeps_all(self):
        lyr = self.layer("parks").clone()
        s = lyr.labeling().settings()
        s.fieldName = "concat(\"pname\", "
        s.isExpression = True
        lyr.setLabeling(QgsVectorLayerSimpleLabeling(s))
        self.assertTrue(analyze_layer_fields(lyr).keep_all)


class ProjectCopy(unittest.TestCase):
    """Steps 1-5 on the test project."""

    def test_package_and_copy(self):
        init_processing()
        import processing
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        project = QgsProject.instance()
        project.read(build_test_project(os.path.join(tmp.name, "src")))
        root = project.layerTreeRoot()
        visible = {n.layerId() for n in root.findLayers() if n.isVisible()}
        ids = [lyr.id() for lyr in root.layerOrder() if lyr.id() in visible]
        self.assertEqual([project.mapLayer(i).name() for i in ids], ["poi", "roads", "parks"])
        ctx = QgsProcessingContext()
        ctx.setProject(project)
        pkg = os.path.join(tmp.name, "data.gpkg")
        res = processing.run("native:package", {"LAYERS": ids, "OUTPUT": pkg, "OVERWRITE": True, "SAVE_STYLES": True,
                                                "CRS": "EPSG:4326"}, context=ctx, feedback=QgsProcessingFeedback())
        sources = dict(zip(ids, res["OUTPUT_LAYERS"]))
        self.assertEqual(check_feature_counts(project, sources), 342 + 12 + 2)  # poi filter applied

        copy = os.path.join(tmp.name, "copy.qgz")
        name = project.fileName()
        write_project_copy(project, copy)
        self.assertEqual(project.fileName(), name)  # the open project is unchanged
        kept, dropped = prepare_project_copy(copy, sources, ids)
        self.assertGreater(dropped, 0)
        project.clear()

        p = QgsProject()
        self.assertTrue(p.read(copy))
        self.assertEqual([lyr.name() for lyr in p.layerTreeRoot().layerOrder()], ["poi", "roads", "parks"])
        poi = p.mapLayersByName("poi")[0]
        self.assertIn("data.gpkg", poi.source())
        self.assertEqual(poi.subsetString(), "")
        self.assertEqual(poi.featureCount(), 342)
        self.assertEqual(poi.renderer().type(), "RuleRenderer")
        self.assertEqual(poi.labeling().type(), "rule-based")
        names = [f.name() for f in poi.fields()]
        self.assertEqual(names.count("label_txt"), 1)  # real column, no duplicate virtual field
        self.assertEqual(set(names) - {"fid"}, {"kind", "size", "prio", "label_txt"})
        self.assertEqual(p.scaleMethod(), Qgis.ScaleCalculationMethod.AtEquator)
        p.clear()
        con = sqlite3.connect(pkg)
        cols = [r[1] for r in con.execute('PRAGMA table_info("roads")')]
        con.close()
        self.assertEqual(set(cols) - {"fid", "geom"}, {"cls", "w"})  # unused columns dropped in the package
        tmp.cleanup()


class QgisProcessHelpers(unittest.TestCase):
    def test_progress_parsing(self):
        self.assertEqual(last_percent("0...10...20...30"), 20)
        self.assertEqual(last_percent("0...10...20...30..."), 30)
        self.assertIsNone(last_percent("starting"))

    def test_profile_arguments(self):
        a = profile_arguments("C:/x/QGIS/QGIS4/profiles/work/")
        self.assertEqual(a[:2], ["--profile", "work"])
        self.assertEqual(profile_arguments("/tmp/elsewhere/"), [])


if __name__ == "__main__":
    unittest.main()
