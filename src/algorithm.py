"""The "Parallel XYZ tiles (GPKG)" Processing algorithm.

Threading model (the QGIS interface never blocks):

* ``prepareAlgorithm`` runs on the main thread and only does quick work:
  load defaults.json, find the built-in algorithm, resolve the extent and
  snapshot the project (layer clones detached from the main thread).
* ``processAlgorithm`` runs in Processing's worker thread (the algorithm is
  NOT flagged ``NoThreading``). It never touches ``QgsProject.instance()`` or
  live project layers; it works on the clones it adopted.
* Rendering happens in separate ``qgis_process`` child processes started at
  below-normal priority; the worker thread only waits on them.
"""

from __future__ import annotations

import logging
import os
import traceback
from typing import Any, Dict, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingOutputFile,
    QgsProcessingOutputNumber,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
)
from qgis.PyQt.QtCore import QCoreApplication

from .builtin_alg import BuiltinAlgorithmError, find_xyz_gpkg_algorithm, map_parameters
from .config import ConfigError, load_config
from .job_runner import JobFailed, JobsCanceled, QgisProcessNotFound
from .logging_utils import close_logging, setup_logging
from .pipeline import Pipeline, PipelineInputs
from .project_builder import snapshot_project
from .tile_grid import Rect


class ParallelXyzTilesAlgorithm(QgsProcessingAlgorithm):
    EXTENT = "EXTENT"
    OUTPUT_DIR = "OUTPUT_DIR"
    ZOOM_MIN = "ZOOM_MIN"
    ZOOM_MAX = "ZOOM_MAX"
    CPU_PERCENT = "CPU_PERCENT"
    OUTPUT_FILE = "OUTPUT_FILE"
    LOG_FILE = "LOG_FILE"
    TILE_COUNT = "TILE_COUNT"

    def __init__(self) -> None:
        super().__init__()
        self._inputs: Optional[PipelineInputs] = None

    # -- metadata ---------------------------------------------------------------
    def tr(self, s: str) -> str:
        return QCoreApplication.translate("ParallelXyzTiles", s)

    def createInstance(self):  # noqa: N802
        return ParallelXyzTilesAlgorithm()

    def name(self) -> str:
        return "parallelxyztilesgpkg"

    def displayName(self) -> str:  # noqa: N802
        return self.tr("Parallel XYZ tiles (GPKG)")

    def shortHelpString(self) -> str:  # noqa: N802
        return self.tr(
            "Same GeoPackage as the built-in 'Generate XYZ tiles (GeoPackage)', generated faster: the built-in "
            "tool runs in parallel processes, and high zoom levels are rendered only where there are features.\n\n"
            "Other settings (tile format and size, DPI, metatile size, CRS and tile matrix) are in defaults.json "
            "in the plugin folder. A log is written to the output directory. Run again with the same inputs to "
            "resume an interrupted run.")

    def flags(self):
        # Runs in the Processing worker thread (no NoThreading flag): the GUI stays responsive.
        return super().flags() | Qgis.ProcessingAlgorithmFlag.RequiresProject

    # -- parameters -------------------------------------------------------------
    def initAlgorithm(self, config: Optional[Dict[str, Any]] = None) -> None:  # noqa: N802
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, self.tr("Extent")))
        self.addParameter(QgsProcessingParameterNumber(
            self.ZOOM_MIN, self.tr("Minimum zoom"), Qgis.ProcessingNumberParameterType.Integer, 12, False, 0, 25))
        self.addParameter(QgsProcessingParameterNumber(
            self.ZOOM_MAX, self.tr("Maximum zoom"), Qgis.ProcessingNumberParameterType.Integer, 12, False, 0, 25))
        self.addParameter(QgsProcessingParameterNumber(
            self.CPU_PERCENT, self.tr("CPU usage limit (%)"), Qgis.ProcessingNumberParameterType.Integer,
            100, False, 1, 100))
        self.addParameter(QgsProcessingParameterFolderDestination(self.OUTPUT_DIR, self.tr("Output directory")))
        self.addOutput(QgsProcessingOutputFile(self.OUTPUT_FILE, self.tr("Tiles GeoPackage")))
        self.addOutput(QgsProcessingOutputFile(self.LOG_FILE, self.tr("Log file")))
        self.addOutput(QgsProcessingOutputNumber(self.TILE_COUNT, self.tr("Number of tiles")))

    def checkParameterValues(self, parameters, context):  # noqa: N802
        zmin = self.parameterAsInt(parameters, self.ZOOM_MIN, context)
        zmax = self.parameterAsInt(parameters, self.ZOOM_MAX, context)
        if zmin > zmax:
            return False, self.tr("Maximum zoom must be greater than or equal to minimum zoom")
        return super().checkParameterValues(parameters, context)

    # -- main thread ------------------------------------------------------------
    def prepareAlgorithm(self, parameters, context: QgsProcessingContext,  # noqa: N802
                         feedback: QgsProcessingFeedback) -> bool:
        try:
            cfg = load_config()
        except ConfigError as e:
            raise QgsProcessingException(str(e)) from e
        try:
            alg = find_xyz_gpkg_algorithm()
            mapping = map_parameters(alg)
        except BuiltinAlgorithmError as e:
            raise QgsProcessingException(str(e)) from e
        project = context.project()
        if project is None:
            raise QgsProcessingException(self.tr("This algorithm needs a project"))
        crs = QgsCoordinateReferenceSystem(cfg.target_crs)
        if not crs.isValid():
            raise QgsProcessingException(f"target_crs '{cfg.target_crs}' in defaults.json is not a valid CRS")
        ext = self.parameterAsExtent(parameters, self.EXTENT, context, crs)
        if ext.isNull() or ext.isEmpty():
            raise QgsProcessingException(self.tr("The extent is empty"))
        out_dir = self.parameterAsString(parameters, self.OUTPUT_DIR, context)
        if not out_dir:
            raise QgsProcessingException(self.tr("An output directory is required"))
        snapshot = snapshot_project(project)
        if not snapshot.render_order:
            raise QgsProcessingException(self.tr("The project has no visible layers"))
        self._inputs = PipelineInputs(
            cfg=cfg, mapping=mapping, snapshot=snapshot, target_crs=crs.authid() or cfg.target_crs,
            extent=Rect(ext.xMinimum(), ext.yMinimum(), ext.xMaximum(), ext.yMaximum()),
            zoom_min=self.parameterAsInt(parameters, self.ZOOM_MIN, context),
            zoom_max=self.parameterAsInt(parameters, self.ZOOM_MAX, context),
            output_dir=out_dir, cpu_percent=self.parameterAsInt(parameters, self.CPU_PERCENT, context),
            qgis_prefix=QgsApplication.prefixPath(), qgis_version=Qgis.version(),
            settings_dir=QgsApplication.qgisSettingsDirPath())
        return True

    # -- worker thread ----------------------------------------------------------
    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        inp = self._inputs
        if inp is None:
            raise QgsProcessingException("prepareAlgorithm did not run")
        logger, fb_handler, log_path = setup_logging(inp.output_dir, feedback)
        try:
            result = Pipeline(inp, feedback, fb_handler.drain).run()
        except JobsCanceled:
            logger.warning("Canceled. Re-run with the same inputs to resume.")
            fb_handler.drain()
            return {self.LOG_FILE: log_path}
        except (JobFailed, QgisProcessNotFound) as e:
            logger.error(str(e))
            raise QgsProcessingException(str(e)) from e
        except QgsProcessingException:
            raise
        except Exception as e:  # report full context, keep the log file
            logger.error("Failed: %s\n%s", e, traceback.format_exc())
            raise QgsProcessingException(f"{type(e).__name__}: {e}") from e
        finally:
            close_logging(logger)
            self._inputs = None
        return {self.OUTPUT_FILE: result["output"], self.LOG_FILE: log_path, self.TILE_COUNT: result["tiles"]}
