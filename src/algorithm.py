"""Generate XYZ tiles (GeoPackage) at full speed.

A plain Processing algorithm that chains built-in tools, like a Model Designer model:

1. Package Layers: the visible vector layers -> one GeoPackage, in the target CRS,
   layer filters applied, styles saved.
2. Copy the project into the temp folder (.qgz).
3. Point the copy's layers at the GeoPackage.
4. Remove the layer filters (the packaged data is already filtered).
5. Remove the fields the styles do not use.
6. Run the built-in "Generate XYZ tiles (GeoPackage)" on the copy with the fixed
   parameters from defaults.json, in its own qgis_process at normal priority
   (inside QGIS its threads may run at low priority and use only a few cores).
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import uuid
from typing import Any, Dict, List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingMultiStepFeedback,
    QgsProcessingOutputFile,
    QgsProcessingOutputNumber,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingUtils,
    QgsProject,
    QgsVectorLayer,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QCoreApplication

from . import qgis_process
from .builtin_alg import ConfigError, as_argument, find_xyz_gpkg_algorithm, load_defaults
from .field_analysis import analyze_layer_fields
from .job_runner import JobsCanceled
from .merger import fill_empty_zooms
from .parallel import check_output, run_parallel
from .tile_grid import Rect


def format_zooms(zooms: List[int]) -> str:
    """[0, 1, 2, 5, 7, 8] -> '0-2, 5, 7-8'."""
    runs: List[List[int]] = []
    for z in sorted(zooms):
        if runs and z == runs[-1][1] + 1:
            runs[-1][1] = z
        else:
            runs.append([z, z])
    return ", ".join(f"{a}-{b}" if a != b else f"{a}" for a, b in runs)

OUTPUT_NAME = "tiles.gpkg"


class XyzTilesAlgorithm(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
    ZOOM_MIN = "ZOOM_MIN"
    ZOOM_MAX = "ZOOM_MAX"
    CPU_PERCENT = "CPU_PERCENT"
    METHOD = "METHOD"
    OUTPUT_DIR = "OUTPUT_DIR"
    METHODS = ["Built-in tool (one process)", "Parallel workers (several processes, empty areas skipped)"]
    OUTPUT_FILE = "OUTPUT_FILE"
    TILE_COUNT = "TILE_COUNT"

    def tr(self, s: str) -> str:
        return QCoreApplication.translate("QGIS2RasterTiles", s)

    def createInstance(self):  # noqa: N802
        return XyzTilesAlgorithm()

    def name(self) -> str:
        return "xyztilesgpkg"

    def displayName(self) -> str:  # noqa: N802
        return self.tr("Generate XYZ tiles (GPKG) - full speed")

    def shortHelpString(self) -> str:  # noqa: N802
        return self.tr(
            "Runs the built-in 'Generate XYZ tiles (GeoPackage)' on a lean local copy of the project, "
            "at normal priority.\n\n"
            "Steps: Package Layers (visible vector layers, filtered, in the target CRS) - copy the project - "
            "point its layers to the package - remove filters and unused fields - generate the tiles.\n\n"
            "Tile generation method: 'Built-in tool' runs it once. 'Parallel workers' runs several copies "
            "at once, each on its own part of the map, and only where there are features; the parts are "
            "verified (no overlap, no gap) and merged into one GeoPackage.\n\n"
            "Fixed tile settings are in defaults.json in the plugin folder. The result is "
            f"'{OUTPUT_NAME}' in the output directory.")

    def initAlgorithm(self, config: Optional[Dict[str, Any]] = None) -> None:  # noqa: N802
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, self.tr("Extent")))
        self.addParameter(QgsProcessingParameterNumber(
            self.ZOOM_MIN, self.tr("Minimum zoom"), Qgis.ProcessingNumberParameterType.Integer, 12, False, 0, 25))
        self.addParameter(QgsProcessingParameterNumber(
            self.ZOOM_MAX, self.tr("Maximum zoom"), Qgis.ProcessingNumberParameterType.Integer, 12, False, 0, 25))
        self.addParameter(QgsProcessingParameterNumber(
            self.CPU_PERCENT, self.tr("CPU usage limit (%)"), Qgis.ProcessingNumberParameterType.Integer,
            100, False, 1, 100))
        self.addParameter(QgsProcessingParameterEnum(
            self.METHOD, self.tr("Tile generation method"), [self.tr(m) for m in self.METHODS], defaultValue=0))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_DIR, self.tr("Output directory")))
        self.addOutput(QgsProcessingOutputFile(self.OUTPUT_FILE, self.tr("Tiles GeoPackage")))
        self.addOutput(QgsProcessingOutputNumber(self.TILE_COUNT, self.tr("Number of tiles")))

    def checkParameterValues(self, parameters, context):  # noqa: N802
        if self.parameterAsInt(parameters, self.ZOOM_MIN, context) > self.parameterAsInt(parameters, self.ZOOM_MAX,
                                                                                         context):
            return False, self.tr("Maximum zoom must be greater than or equal to minimum zoom")
        return super().checkParameterValues(parameters, context)

    # -- main thread: everything that needs the open project ---------------------------
    def prepareAlgorithm(self, parameters, context, feedback) -> bool:  # noqa: N802
        try:
            self.xyz = find_xyz_gpkg_algorithm()
            self.defaults = load_defaults(self.xyz)
        except ConfigError as e:
            raise QgsProcessingException(str(e)) from e
        project = context.project()
        if project is None:
            raise QgsProcessingException(self.tr("This algorithm needs a project"))
        crs_value = self.defaults.get("TARGET_CRS", self.xyz.parameterDefinition("TARGET_CRS").defaultValue())
        self.crs = QgsCoordinateReferenceSystem(str(crs_value))
        if not self.crs.isValid():
            raise QgsProcessingException(f"TARGET_CRS '{crs_value}' in defaults.json is not a valid CRS")

        # the layers the built-in renders: render order, visible layer-tree nodes only
        root = project.layerTreeRoot()
        visible = {n.layerId() for n in root.findLayers() if n.isVisible() and n.layer() is not None}
        self.visible_ids = [lyr.id() for lyr in root.layerOrder() if lyr.id() in visible]
        self.vector_ids = [i for i in self.visible_ids if isinstance(project.mapLayer(i), QgsVectorLayer)]
        if not self.visible_ids:
            raise QgsProcessingException(self.tr("The project has no visible layers"))
        for i in self.visible_ids:
            if i not in self.vector_ids:
                feedback.pushWarning(f"'{project.mapLayer(i).name()}' is not a vector layer: it is used as is "
                                     f"(from its original source)")

        rect = self.parameterAsExtent(parameters, self.EXTENT, context, self.crs)
        self.extent = f"{rect.xMinimum()!r},{rect.xMaximum()!r},{rect.yMinimum()!r},{rect.yMaximum()!r} " \
                      f"[{self.crs.authid()}]"
        self.zoom_min = self.parameterAsInt(parameters, self.ZOOM_MIN, context)
        self.zoom_max = self.parameterAsInt(parameters, self.ZOOM_MAX, context)
        self.cpu_percent = self.parameterAsInt(parameters, self.CPU_PERCENT, context)
        self.method = self.parameterAsEnum(parameters, self.METHOD, context)
        self.extent_rect = Rect(rect.xMinimum(), rect.yMinimum(), rect.xMaximum(), rect.yMaximum())
        self.non_vector = len(self.visible_ids) != len(self.vector_ids)
        self.out_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)
        self.ellipsoid = project.ellipsoid()
        self.prefix = QgsApplication.prefixPath()
        self.settings_dir = QgsApplication.qgisSettingsDirPath()

        self.temp = os.path.join(QgsProcessingUtils.tempFolder(context), f"qgis2rastertiles_{uuid.uuid4().hex[:8]}")
        os.makedirs(self.temp)
        self.project_copy = os.path.join(self.temp, "project.qgz")
        write_project_copy(project, self.project_copy)  # step 2 (needs the live project)
        return True

    # -- worker thread ----------------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        import processing

        steps = QgsProcessingMultiStepFeedback(4, feedback)
        package = os.path.join(self.temp, "data.gpkg")

        # 1. Package Layers
        steps.setCurrentStep(0)
        feedback.pushInfo(f"1/4 Package Layers: {len(self.vector_ids)} layer(s) -> {self.crs.authid()}")
        res = processing.run("native:package", {
            "LAYERS": self.vector_ids, "OUTPUT": package, "OVERWRITE": True, "SAVE_STYLES": True,
            "SAVE_METADATA": False, "SELECTED_FEATURES_ONLY": False, "EXPORT_RELATED_LAYERS": False,
            "CRS": self.crs}, context=context, feedback=steps, is_child_algorithm=True)
        uris = res["OUTPUT_LAYERS"]
        if len(uris) != len(self.vector_ids) or len(set(uris)) != len(uris):
            raise QgsProcessingException("Package Layers did not return one table per layer "
                                         "(do two layers have the same name?)")
        sources = dict(zip(self.vector_ids, uris))
        total, counts = check_feature_counts(context.project(), sources)
        feedback.pushInfo(f"   packaged {total} features, none lost")
        if steps.isCanceled():
            return {}

        # 2. (project copied in prepareAlgorithm)  3.-5. edit the copy
        steps.setCurrentStep(1)
        feedback.pushInfo("2/4 Project copied")
        steps.setCurrentStep(2)
        kept, dropped, renderers = prepare_project_copy(self.project_copy, sources, self.visible_ids)
        feedback.pushInfo(f"3/4 Project copy uses the GeoPackage; filters removed; "
                          f"{dropped} unused field(s) removed, {kept} kept")

        # 6. Generate XYZ tiles (GeoPackage): one process or parallel workers, normal priority
        steps.setCurrentStep(3)
        os.makedirs(self.out_dir, exist_ok=True)
        output = os.path.join(self.out_dir, OUTPUT_NAME)
        base = [qgis_process.find_qgis_process(self.prefix)] + qgis_process.profile_arguments(self.settings_dir)
        base += ["run", self.xyz.id(), f"--PROJECT_PATH={self.project_copy}"]
        if self.ellipsoid and self.ellipsoid.upper() != "NONE":
            base.append(f"--ELLIPSOID={self.ellipsoid}")
        fixed = [f"--{k}={as_argument(v)}" for k, v in self.defaults.items()]

        def make_args(rect: Rect, zmin: int, zmax: int, out: str) -> List[str]:
            ext = f"{rect.xmin!r},{rect.xmax!r},{rect.ymin!r},{rect.ymax!r} [{self.crs.authid()}]"
            return base + [f"--EXTENT={ext}", f"--ZOOM_MIN={zmin}", f"--ZOOM_MAX={zmax}", f"--OUTPUT_FILE={out}"] + fixed

        try:
            if self.method == 0:
                feedback.pushInfo(f"4/4 {self.xyz.displayName()}: zooms {self.zoom_min}-{self.zoom_max}, "
                                  f"one process, normal priority")
                qgis_process.run(make_args(self.extent_rect, self.zoom_min, self.zoom_max, output),
                                 os.path.join(self.temp, "qgis_process.log"), steps, self.cpu_percent)
            else:
                feedback.pushInfo(f"4/4 {self.xyz.displayName()}: zooms {self.zoom_min}-{self.zoom_max}, "
                                  f"parallel workers, normal priority")
                run_parallel(xyz=self.xyz, defaults=self.defaults, crs_authid=self.crs.authid(),
                             extent=self.extent_rect, zmin=self.zoom_min, zmax=self.zoom_max, make_args=make_args,
                             sources=sources, counts=counts, renderers=renderers, has_non_vector=self.non_vector,
                             output=output, work=self.temp, cpu_percent=self.cpu_percent, feedback=steps,
                             info=feedback.pushInfo)
        except (RuntimeError, JobsCanceled) as e:
            if feedback.isCanceled():
                return {}
            raise QgsProcessingException(str(e)) from e
        try:
            filled = fill_empty_zooms(output, self.zoom_min, self.zoom_max)
            if filled:
                feedback.pushInfo(f"   zooms {format_zooms(filled)}: nothing drawn; one transparent tile each, "
                                  f"so viewers can show the whole area quickly")
            check_output(output, self.zoom_min, self.zoom_max)
        except (RuntimeError, sqlite3.Error) as e:
            raise QgsProcessingException(str(e)) from e
        con = sqlite3.connect(output)
        tiles = con.execute("SELECT COUNT(*) FROM tiles").fetchone()[0]
        con.close()
        shutil.rmtree(self.temp, ignore_errors=True)
        feedback.pushInfo(f"Done: {tiles} tiles -> {output}")
        return {self.OUTPUT_FILE: output, self.TILE_COUNT: tiles}


def write_project_copy(project: QgsProject, path: str) -> None:
    """Save the open project to ``path`` (absolute paths) without changing the open project."""
    name, dirty, storage = project.fileName(), project.isDirty(), project.filePathStorage()
    try:
        project.setFilePathStorage(Qgis.FilePathType.Absolute)
        if not project.write(path):
            raise QgsProcessingException(f"Cannot copy the project: {project.error()}")
    finally:
        project.setFileName(name)
        project.setFilePathStorage(storage)
        project.setDirty(dirty)


def check_feature_counts(project: QgsProject, sources: Dict[str, str]) -> tuple:
    """Packaged features per layer must equal the layer's features (its filter applied).

    Returns (total, {layer id: packaged count}).
    """
    total, counts = 0, {}
    for lid, uri in sources.items():
        src = project.mapLayer(lid)
        out = QgsVectorLayer(uri, "check", "ogr")
        try:
            n = out.featureCount()
        finally:
            sip.delete(out)
        expected = src.featureCount()
        if expected >= 0 and n != expected:
            raise QgsProcessingException(f"'{src.name()}': {expected} features in the project, {n} packaged")
        total += n
        counts[lid] = n
    return total, counts


def prepare_project_copy(path: str, sources: Dict[str, str], visible_ids: List[str]) -> tuple:
    """Steps 3-5 on the saved copy: GeoPackage sources, no filters, only used fields.

    The copy is read without resolving its original data sources, so the original
    data is not opened again. Returns (fields kept, fields removed, {layer id: renderer type}).
    """
    p = QgsProject()
    kept = dropped = 0
    renderers: Dict[str, str] = {}
    try:
        if not p.read(path, Qgis.ProjectReadFlag.DontResolveLayers):
            raise QgsProcessingException(f"Cannot read the project copy: {p.error()}")
        for lid in [i for i in p.mapLayers() if i not in visible_ids]:
            p.removeMapLayer(lid)  # never rendered by the built-in tool
        for lid, uri in sources.items():
            lyr = p.mapLayer(lid)
            lyr.setDataSource(uri, lyr.name(), "ogr", False)  # False: keep the layer's own style
            if not lyr.isValid():
                raise QgsProcessingException(f"'{lyr.name()}' cannot use the packaged table {uri}")
            lyr.setSubsetString("")
            for join in list(lyr.vectorJoins()):  # joined values are packaged as real columns
                lyr.removeJoin(join.joinLayerId())
            provider_names = set(lyr.dataProvider().fields().names())
            for i in reversed(range(lyr.fields().count())):  # virtual fields are packaged as real columns
                if lyr.fields().fieldOrigin(i) == Qgis.FieldOrigin.Expression and \
                        lyr.fields().at(i).name() in provider_names:
                    lyr.removeExpressionField(i)
            needed = set(analyze_layer_fields(lyr).required)
            fields = lyr.dataProvider().fields()
            unused = [i for i in range(fields.count()) if fields.at(i).name() not in needed
                      and fields.at(i).name().lower() != "fid"]
            if unused:
                lyr.dataProvider().deleteAttributes(unused)
                lyr.updateFields()
            dropped += len(unused)
            kept += fields.count() - len(unused)
            renderers[lid] = lyr.renderer().type() if lyr.renderer() else ""
        p.setFilePathStorage(Qgis.FilePathType.Absolute)
        if not p.write(path):
            raise QgsProcessingException(f"Cannot save the project copy: {p.error()}")
    finally:
        p.clear()
    return kept, dropped, renderers
