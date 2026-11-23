"""Export of lean, filtered, reprojected FlatGeobuf files (one per vector layer).

``QgsVectorFileWriter.writeAsVectorFormatV3`` is used with:

* only the required attribute indices (virtual and joined fields included:
  the writer evaluates them, so they become real fields with the same name),
* the layer's subset string implicitly applied (the writer iterates the layer),
* ``filterExtent`` in the *destination* CRS (the writer maps it back to the
  layer CRS), i.e. features intersecting the covered extent + margin,
* ``ct`` reprojecting to the target CRS, geometries otherwise untouched,
* ``SPATIAL_INDEX=YES``.

If the driver rejects geometries that do not match the declared layer type
(e.g. multi-part features in a layer declared single-part), the export is
retried once with an "Unknown" geometry type, which keeps every geometry
exactly as it is instead of converting it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsCoordinateTransformContext,
    QgsCsException,
    QgsFeatureRequest,
    QgsFeedback,
    QgsRectangle,
    QgsVectorFileWriter,
    QgsVectorLayer,
    QgsWkbTypes,
)
from qgis.PyQt import sip

from .field_analysis import FieldAnalysis


class ExportError(RuntimeError):
    pass


@dataclass
class ExportResult:
    layer_id: str
    layer_name: str
    path: str
    fields: List[str]
    exported_count: int
    expected_count: int          # source features (filter applied) inside the render area
    wkb_type: str
    notes: List[str] = field(default_factory=list)
    source_count: int = -1       # source features with the layer filter applied (whole layer)
    null_geometry: int = 0       # exported features without geometry (never drawn)
    missing_ids: List[int] = field(default_factory=list)   # in the area but not exported
    extra_ids: List[int] = field(default_factory=list)     # exported but not expected

    @property
    def outside_area(self) -> int:
        """Features entirely outside the rendered area + margin (no tile shows them, in any tool)."""
        return self.source_count - self.expected_count if self.source_count >= 0 else 0

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def source_filter_rect(layer: QgsVectorLayer, target_crs: QgsCoordinateReferenceSystem, rect: Optional[QgsRectangle],
                       transform_context: QgsCoordinateTransformContext) -> Optional[QgsRectangle]:
    """``rect`` (target CRS) mapped to the layer CRS, or None for "no filter"."""
    if rect is None:
        return None
    if layer.crs() == target_crs:
        return QgsRectangle(rect)
    ct = QgsCoordinateTransform(layer.crs(), target_crs, transform_context)
    ct.setBallparkTransformsAreAppropriate(True)
    try:
        return ct.transformBoundingBox(rect, Qgis.TransformDirection.Reverse)
    except QgsCsException:
        return None


def count_source_features(layer: QgsVectorLayer, rect: Optional[QgsRectangle], feedback: Optional[QgsFeedback]) -> int:
    """Number of features the export is expected to write (subset string applies automatically)."""
    return len(source_feature_ids(layer, rect, feedback))


def source_feature_ids(layer: QgsVectorLayer, rect: Optional[QgsRectangle],
                       feedback: Optional[QgsFeedback]) -> List[int]:
    """Ids of the source features (layer filter applied) intersecting ``rect`` (layer CRS; None = all)."""
    req = QgsFeatureRequest().setNoAttributes()
    if rect is not None:
        req.setFilterRect(rect)
    else:
        req.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    ids: List[int] = []
    for f in layer.getFeatures(req):
        ids.append(f.id())
        if feedback is not None and len(ids) % 10000 == 0 and feedback.isCanceled():
            break
    return ids


def export_layer(layer: QgsVectorLayer, analysis: FieldAnalysis, out_path: str,
                 target_crs: QgsCoordinateReferenceSystem, filter_rect: Optional[QgsRectangle],
                 transform_context: QgsCoordinateTransformContext,
                 feedback: Optional[QgsFeedback] = None, id_field: Optional[str] = None) -> ExportResult:
    """Write ``layer`` to ``out_path`` (.fgb). ``filter_rect`` is in the target CRS (None = everything).

    Feature accounting: the source ids inside the area are compared with the
    exported ones when ``id_field`` (an exported ``$id`` field) is given,
    otherwise only the counts are compared by the caller.
    """
    notes: List[str] = []
    fields = layer.fields()
    attrs = [fields.lookupField(n) for n in analysis.required]
    if any(i < 0 for i in attrs):
        raise ExportError(f"{layer.name()}: required field missing from layer: {analysis.required}")

    def attempt(override_unknown: bool):
        for suffix in ("", ".tmp"):
            if os.path.exists(out_path + suffix):
                os.remove(out_path + suffix)
        opts = QgsVectorFileWriter.SaveVectorOptions()
        opts.driverName = "FlatGeobuf"
        opts.layerName = os.path.splitext(os.path.basename(out_path))[0]
        opts.fileEncoding = "UTF-8"
        opts.layerOptions = ["SPATIAL_INDEX=YES"]
        opts.attributes = attrs
        opts.skipAttributeCreation = False
        opts.includeZ = True
        opts.forceMulti = False
        opts.onlySelectedFeatures = False
        opts.symbologyExport = Qgis.FeatureSymbologyExport.NoSymbology
        opts.saveMetadata = False
        opts.actionOnExistingFile = QgsVectorFileWriter.ActionOnExistingFile.CreateOrOverwriteFile
        if layer.crs() != target_crs:
            opts.ct = QgsCoordinateTransform(layer.crs(), target_crs, transform_context)
        if filter_rect is not None:
            opts.filterExtent = QgsRectangle(filter_rect)
        if override_unknown:
            opts.overrideGeometryType = Qgis.WkbType.Unknown
        if feedback is not None:
            opts.feedback = feedback
        return QgsVectorFileWriter.writeAsVectorFormatV3(layer, out_path, transform_context, opts)

    err, msg, new_file, _new_layer = attempt(False)
    if err != QgsVectorFileWriter.WriterError.NoError:
        if feedback is not None and feedback.isCanceled():
            raise ExportError("canceled")
        notes.append(f"first export attempt failed ({msg}); retried with geometry type 'Unknown'")
        err, msg, new_file, _new_layer = attempt(True)
        if err != QgsVectorFileWriter.WriterError.NoError:
            raise ExportError(f"{layer.name()}: export to FlatGeobuf failed: {msg}")
    path = new_file or out_path

    # --- read back the export: count, ids, features without geometry ---------
    out = QgsVectorLayer(path, "check", "ogr")
    exported_ids: List[int] = []
    null_geom = 0
    try:
        if not out.isValid():
            raise ExportError(f"{layer.name()}: exported file is not readable: {path}")
        field_names = [f.name() for f in out.fields()]
        wkb = QgsWkbTypes.displayString(out.wkbType())
        id_idx = out.fields().lookupField(id_field) if id_field else -1
        req = QgsFeatureRequest().setSubsetOfAttributes([id_idx] if id_idx >= 0 else [])
        exported = 0
        for f in out.getFeatures(req):
            exported += 1
            g = f.geometry()
            if g.isNull() or g.isEmpty():
                null_geom += 1
            if id_idx >= 0:
                exported_ids.append(int(f.attribute(id_idx)))
    finally:
        dispose_layer(out)

    # --- the source side --------------------------------------------------------
    area_ids = source_feature_ids(layer, source_filter_rect(layer, target_crs, filter_rect, transform_context),
                                  feedback)
    source_count = len(area_ids) if filter_rect is None else count_source_features(layer, None, feedback)
    missing: List[int] = []
    extra: List[int] = []
    if id_field and id_idx >= 0:
        a, e = set(area_ids), set(exported_ids)
        missing = sorted(a - e)
        extra = sorted(e - a)
    return ExportResult(layer.id(), layer.name(), path, field_names, exported, len(area_ids), wkb, notes,
                        source_count, null_geom, missing, extra)


def dispose_layer(layer) -> None:
    """Delete a layer now, in the thread that owns it.

    Left to the garbage collector, the wrapper may be released from another
    thread; the deferred deletion then runs while the worker thread exits,
    which can corrupt the heap and crash QGIS.
    """
    if layer is not None and not sip.isdeleted(layer):
        sip.delete(layer)
