"""Checks of the render project before any rendering job starts.

* :func:`check_saved_project` re-reads the saved .qgz into a fresh project
  (exactly the file the qgis_process jobs load) and reports every layer's
  validity, feature count, renderer type and label state.
* :func:`python_functions_used` finds expression functions implemented in
  Python (registered by plugins or user scripts). Jobs run with
  ``--skip-loading-plugins`` would not know them, so the caller then lets the
  jobs load plugins.
* :func:`missing_label_fonts` lists label font families that are not
  installed (the built-in tool would fall back to another font as well).
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Set

from qgis.core import (
    QgsExpression,
    QgsFontUtils,
    QgsMapLayer,
    QgsProject,
    QgsRuleBasedLabeling,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)


def check_saved_project(path: str) -> List[Dict[str, object]]:
    """Layers of the saved project as a fresh QgsProject loads them (call from the worker thread)."""
    project = QgsProject()
    try:
        if not project.read(path):
            raise RuntimeError(f"cannot read render project {path}: {project.error()}")
        out = []
        for lyr in project.layerTreeRoot().layerOrder():
            info: Dict[str, object] = {"id": lyr.id(), "name": lyr.name(), "valid": lyr.isValid()}
            if isinstance(lyr, QgsVectorLayer):
                info["feature_count"] = lyr.featureCount()
                r = lyr.renderer()
                info["renderer"] = r.type() if r else None
                info["labels"] = lyr.labelsEnabled()
            out.append(info)
        return out
    finally:
        project.clear()  # deletes the layers in this (the owning) thread


def python_functions_used(expressions: Iterable[str]) -> Set[str]:
    """Names of Python-implemented expression functions referenced by ``expressions``."""
    py = {f.name().lower() for f in QgsExpression.Functions() if type(f).__module__ != "qgis._core"}
    used: Set[str] = set()
    for e in expressions:
        used |= {n.lower() for n in QgsExpression(e).referencedFunctions()}
    return used & py


def missing_label_fonts(layers: Iterable[QgsMapLayer]) -> List[str]:
    """Label font families used by visible labeling that are not installed."""
    fonts: Set[str] = set()
    for lyr in layers:
        if not isinstance(lyr, QgsVectorLayer) or not lyr.labelsEnabled():
            continue
        lab = lyr.labeling()
        if isinstance(lab, QgsVectorLayerSimpleLabeling):
            settings = [lab.settings()]
        elif isinstance(lab, QgsRuleBasedLabeling):
            settings = [rule.settings() for rule in lab.rootRule().descendants() if rule.settings()]
        else:
            settings = []
        for s in settings:
            fonts.add(s.format().font().family())
    return sorted(f for f in fonts if f and not QgsFontUtils.fontFamilyOnSystem(f))
