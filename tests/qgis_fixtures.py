"""Shared helpers for tests that need QGIS (run with python-qgis*.bat / a QGIS Python).

Builds a small synthetic project (source data in EPSG:3857, a few km² near
34.78E 32.07N) with:

* ``poi`` points: rule-based renderer with data-defined size and colour,
  rule-based labeling on a virtual field, a subset filter, unused fields;
* ``roads`` lines (in group "Base"): categorized renderer, data-defined width;
* ``parks`` polygons (in group "Base"): simple fill, simple labeling;
* ``hidden`` points: unchecked, must not be exported.
"""

from __future__ import annotations

import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from qgis.core import (  # noqa: E402
    Qgis,
    QgsApplication,
    QgsCategorizedSymbolRenderer,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsFeature,
    QgsField,
    QgsFillSymbol,
    QgsGeometry,
    QgsLineSymbol,
    QgsMarkerSymbol,
    QgsPalLayerSettings,
    QgsPointXY,
    QgsProject,
    QgsProperty,
    QgsRendererCategory,
    QgsRuleBasedLabeling,
    QgsRuleBasedRenderer,
    QgsSingleSymbolRenderer,
    QgsSymbolLayer,
    QgsTextFormat,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QMetaType  # noqa: E402
from qgis.PyQt.QtGui import QColor  # noqa: E402

_APP = None

#: test extent in EPSG:4326 (xmin, ymin, xmax, ymax) - about 4 x 4.5 km
EXTENT_4326 = (34.760, 32.050, 34.800, 32.090)


def start_qgis():
    """Initialise a headless QgsApplication with the native algorithms (once)."""
    global _APP
    if _APP is None:
        _APP = QgsApplication([], False)
        _APP.initQgis()
        from qgis.analysis import QgsNativeAlgorithms
        reg = QgsApplication.processingRegistry()
        if reg.providerById("native") is None:
            reg.addProvider(QgsNativeAlgorithms())
    return _APP


def _to3857(lon: float, lat: float) -> QgsPointXY:
    ct = QgsCoordinateTransform(QgsCoordinateReferenceSystem("EPSG:4326"), QgsCoordinateReferenceSystem("EPSG:3857"),
                                QgsCoordinateTransformContext())
    return ct.transform(QgsPointXY(lon, lat))


def _write(mem: QgsVectorLayer, path: str) -> QgsVectorLayer:
    opts = QgsVectorFileWriter.SaveVectorOptions()
    opts.driverName = "GPKG"
    opts.layerName = mem.name()
    opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteLayer \
        if os.path.exists(path) else QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
    err = QgsVectorFileWriter.writeAsVectorFormatV3(mem, path, QgsCoordinateTransformContext(), opts)
    assert err[0] == QgsVectorFileWriter.WriterError.NoError, err
    lyr = QgsVectorLayer(f"{path}|layername={mem.name()}", mem.name(), "ogr")
    assert lyr.isValid()
    return lyr


def build_test_project(folder: str, n_points: int = 400, seed: int = 3) -> str:
    """Create data + project in ``folder``; returns the .qgz path."""
    os.makedirs(folder, exist_ok=True)
    rnd = random.Random(seed)
    gpkg = os.path.join(folder, "data.gpkg")
    if os.path.exists(gpkg):
        os.remove(gpkg)
    x0, y0, x1, y1 = EXTENT_4326

    # ---- points: two clusters + sparse scatter -------------------------------
    pts = QgsVectorLayer("Point?crs=EPSG:3857", "poi", "memory")
    pts.dataProvider().addAttributes([
        QgsField("id", QMetaType.Type.Int), QgsField("kind", QMetaType.Type.QString),
        QgsField("size", QMetaType.Type.Int), QgsField("prio", QMetaType.Type.Int),
        QgsField("name", QMetaType.Type.QString), QgsField("junk1", QMetaType.Type.QString),
        QgsField("junk2", QMetaType.Type.Double), QgsField("junk3", QMetaType.Type.QString)])
    pts.updateFields()
    feats = []
    clusters = [(34.768, 32.058), (34.792, 32.083)]
    for i in range(n_points):
        if i % 10 == 0:
            lon, lat = rnd.uniform(x0, x1), rnd.uniform(y0, y1)
        else:
            cx, cy = clusters[i % 2]
            lon, lat = cx + rnd.gauss(0, 0.002), cy + rnd.gauss(0, 0.002)
        f = QgsFeature(pts.fields())
        f.setAttributes([i, "a" if i % 2 else "b", rnd.randint(1, 10), rnd.randint(1, 4), f"P{i}",
                         "x" * 50, rnd.random(), "unused"])
        f.setGeometry(QgsGeometry.fromPointXY(_to3857(lon, lat)))
        feats.append(f)
    pts.dataProvider().addFeatures(feats)
    poi = _write(pts, os.path.join(folder, "poi.gpkg"))

    # ---- lines ----------------------------------------------------------------
    ln = QgsVectorLayer("LineString?crs=EPSG:3857", "roads", "memory")
    ln.dataProvider().addAttributes([QgsField("cls", QMetaType.Type.QString), QgsField("w", QMetaType.Type.Double),
                                     QgsField("junk", QMetaType.Type.QString)])
    ln.updateFields()
    lf = []
    for k in range(12):
        lat = y0 + (y1 - y0) * (k + 0.5) / 12
        f = QgsFeature(ln.fields())
        f.setAttributes(["major" if k % 3 == 0 else "minor", 0.3 + 0.2 * (k % 4), "zzz"])
        f.setGeometry(QgsGeometry.fromPolylineXY([_to3857(x0 + 0.002, lat), _to3857(34.770 + 0.001 * k, lat + 0.004),
                                                  _to3857(x1 - 0.002, lat - 0.002)]))
        lf.append(f)
    ln.dataProvider().addFeatures(lf)
    roads = _write(ln, os.path.join(folder, "roads.gpkg"))

    # ---- polygons -----------------------------------------------------------------
    pg = QgsVectorLayer("MultiPolygon?crs=EPSG:3857", "parks", "memory")
    pg.dataProvider().addAttributes([QgsField("pname", QMetaType.Type.QString), QgsField("area_x", QMetaType.Type.Double)])
    pg.updateFields()
    pf = []
    for k, (cx, cy, r) in enumerate([(34.775, 32.070, 0.004), (34.788, 32.060, 0.002)]):
        ring = [_to3857(cx + r * math.cos(a / 20 * 2 * math.pi), cy + r * math.sin(a / 20 * 2 * math.pi))
                for a in range(20)]
        hole = [_to3857(cx + r / 3 * math.cos(a / 10 * 2 * math.pi), cy + r / 3 * math.sin(a / 10 * 2 * math.pi))
                for a in range(10)]
        f = QgsFeature(pg.fields())
        f.setAttributes([f"Park {k}", 1.0])
        f.setGeometry(QgsGeometry.fromMultiPolygonXY([[ring, hole]]))
        pf.append(f)
    pg.dataProvider().addFeatures(pf)
    parks = _write(pg, os.path.join(folder, "parks.gpkg"))

    hidden = _write(QgsVectorLayer("Point?crs=EPSG:3857&field=a:integer", "hidden", "memory"), os.path.join(folder, "hidden.gpkg"))

    # ---- styles -------------------------------------------------------------------
    poi.setSubsetString('"id" % 7 <> 0')
    poi.addExpressionField('upper("name")', QgsField("label_txt", QMetaType.Type.QString))
    root_rule = QgsRuleBasedRenderer.Rule(None)
    for flt, color in (("\"kind\" = 'a'", "220,30,30"), ("\"kind\" = 'b'", "30,30,220")):
        sym = QgsMarkerSymbol.createSimple({"name": "circle", "color": color, "size": "3"})
        sym.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.Property.Size, QgsProperty.fromField("size"))
        sym.symbolLayer(0).setDataDefinedProperty(
            QgsSymbolLayer.Property.FillColor,
            QgsProperty.fromExpression("case when \"prio\" > 2 then '255,140,0' else '0,160,0' end"))
        root_rule.appendChild(QgsRuleBasedRenderer.Rule(sym, 0, 0, flt, flt))
    poi.setRenderer(QgsRuleBasedRenderer(root_rule))
    lab_root = QgsRuleBasedLabeling.Rule(None)
    s = QgsPalLayerSettings()
    s.fieldName = "label_txt"
    s.isExpression = False
    fmt = QgsTextFormat()
    fmt.setSize(9)
    s.setFormat(fmt)
    lab_root.appendChild(QgsRuleBasedLabeling.Rule(s, 0, 0, '"size" > 6', "big"))
    poi.setLabeling(QgsRuleBasedLabeling(lab_root))
    poi.setLabelsEnabled(True)

    cats = []
    for v, col, wd in (("major", "80,80,80", "1.2"), ("minor", "150,150,150", "0.6")):
        sym = QgsLineSymbol.createSimple({"color": col, "width": wd})
        sym.symbolLayer(0).setDataDefinedProperty(QgsSymbolLayer.Property.StrokeWidth, QgsProperty.fromField("w"))
        cats.append(QgsRendererCategory(v, sym, v))
    roads.setRenderer(QgsCategorizedSymbolRenderer("cls", cats))

    parks.setRenderer(QgsSingleSymbolRenderer(QgsFillSymbol.createSimple(
        {"color": "120,200,120,180", "outline_color": "20,100,20", "outline_width": "0.4"})))
    ps = QgsPalLayerSettings()
    ps.fieldName = "pname"
    ps.setFormat(fmt)
    parks.setLabeling(QgsVectorLayerSimpleLabeling(ps))
    parks.setLabelsEnabled(True)
    parks.setOpacity(0.8)

    # ---- project ------------------------------------------------------------------
    project = QgsProject()
    project.setCrs(QgsCoordinateReferenceSystem("EPSG:3857"))
    # "Always calculate at the equator" (relevant for EPSG:4326 scale-based visibility)
    project.setScaleMethod(Qgis.ScaleCalculationMethod.AtEquator)
    for lyr in (poi, roads, parks, hidden):
        project.addMapLayer(lyr, False)
    root = project.layerTreeRoot()
    root.addLayer(poi)
    grp = root.addGroup("Base")
    grp.addLayer(roads)
    grp.addLayer(parks)
    root.addLayer(hidden).setItemVisibilityChecked(False)
    path = os.path.join(folder, "test_project.qgz")
    assert project.write(path), project.error()
    project.clear()
    return path


def qgis_process_path() -> str:
    """qgis_process launcher of the running QGIS (for tests)."""
    from qgis.core import Qgis as _Q
    from src.job_runner import find_qgis_process
    return str(find_qgis_process(QgsApplication.prefixPath(), _Q.version()))
