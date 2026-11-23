"""Snapshot of the current project and construction of the lean render project.

Two phases, because Processing runs ``processAlgorithm`` in a worker thread:

1. :func:`snapshot_project` (main thread, from ``prepareAlgorithm``): records
   the layer tree (visible nodes only, exact order, groups with their group-layer
   rendering settings), custom layer order, project settings, and *clones* of
   the visible layers which are detached from the main thread (like the
   built-in algorithm does).
2. :func:`build_render_project` (worker thread): creates a new ``QgsProject``
   with the exported FlatGeobuf layers in the same tree, original layer ids
   (so masks / expressions referring to ids keep working), styles copied via
   QML (all categories except *Fields*, see below), absolute resource paths,
   project-level rendering settings, then verifies and saves it as .qgz.

The *Fields* style category is excluded on purpose: it would re-create virtual
fields on top of the real fields that already hold their evaluated values.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransformContext,
    QgsExpression,
    QgsFeatureRequest,
    QgsGroupLayer,
    QgsLabelingEngineSettings,
    QgsLayerTree,
    QgsLayerTreeGroup,
    QgsLayerTreeLayer,
    QgsMapLayer,
    QgsPathResolver,
    QgsProject,
    QgsRasterFillSymbolLayer,
    QgsRasterMarkerSymbolLayer,
    QgsReadWriteContext,
    QgsRuleBasedLabeling,
    QgsSVGFillSymbolLayer,
    QgsSvgMarkerSymbolLayer,
    QgsSymbol,
    QgsSymbolLayerUtils,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt import sip
from qgis.PyQt.QtCore import QThread
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtXml import QDomDocument

SC = QgsMapLayer.StyleCategory
#: Every style category except Fields (virtual fields are exported as real fields).
#: PyQt6: ``&``/``~`` on the IntEnum give a plain int, which the bindings reject;
#: the enum constructor turns the combined value back into a StyleCategory.
STYLE_CATEGORIES = SC(int(SC.AllStyleCategories) & ~int(SC.Fields))


class ProjectBuildError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Snapshot (main thread)
# ---------------------------------------------------------------------------
@dataclass
class TreeEntry:
    kind: str                      # "group" | "layer"
    name: str
    layer_id: Optional[str] = None
    children: List["TreeEntry"] = field(default_factory=list)
    group_style_xml: Optional[str] = None  # style of a "render as group" group layer


@dataclass
class ProjectSnapshot:
    root: TreeEntry
    layers: Dict[str, QgsMapLayer]          # id -> detached clone
    render_order: List[str]                  # ids in drawing order (top first), visible only
    custom_order: Optional[List[str]]
    file_name: str
    home_path: str
    ellipsoid: str
    transform_context: QgsCoordinateTransformContext
    scale_method: Qgis.ScaleCalculationMethod
    variables: Dict[str, object]
    palette: Tuple[List[str], List[str]]
    labeling_settings: QgsLabelingEngineSettings
    background: QColor
    style_settings_xml: Optional[str]
    warnings: List[str] = field(default_factory=list)

    def vector_ids(self) -> List[str]:
        return [i for i in self.render_order if isinstance(self.layers[i], QgsVectorLayer)]

    def adopt_layers(self) -> None:
        """Move the detached clones to the calling (worker) thread."""
        for lyr in self.layers.values():
            lyr.moveToThread(QThread.currentThread())

    def dispose(self) -> None:
        """Delete the clones in the thread that owns them (call when done)."""
        for lyr in self.layers.values():
            if not sip.isdeleted(lyr):
                sip.delete(lyr)
        self.layers = {}

    def fingerprint(self) -> List[list]:
        """Data used to decide whether a previous run can be resumed."""
        out = []
        for lid in self.render_order:
            lyr = self.layers[lid]
            doc = QDomDocument()
            lyr.exportNamedStyle(doc, QgsReadWriteContext(), STYLE_CATEGORIES)
            src = lyr.source()
            mtime = os.path.getmtime(src.split("|")[0]) if os.path.isfile(src.split("|")[0]) else None
            subset = lyr.subsetString() if isinstance(lyr, QgsVectorLayer) else ""
            style_hash = hashlib.sha1(doc.toString().encode("utf-8")).hexdigest()
            out.append([lid, src, mtime, subset, style_hash])
        return out


def snapshot_project(project: QgsProject) -> ProjectSnapshot:
    """Capture everything needed from ``project`` (call from the main thread)."""
    root = project.layerTreeRoot()
    visible_ids = {n.layerId() for n in root.findLayers() if n.isVisible() and n.layer() is not None}
    warnings: List[str] = []
    # same selection as the built-in: layerOrder() filtered by visible layer-tree nodes
    order = [lyr.id() for lyr in root.layerOrder() if lyr.id() in visible_ids]
    in_order = set(order)

    def walk(group: QgsLayerTreeGroup) -> TreeEntry:
        entry = TreeEntry("group", group.name())
        gl = group.groupLayer() if hasattr(group, "groupLayer") else None
        if gl is not None:
            doc = QDomDocument()
            gl.exportNamedStyle(doc, QgsReadWriteContext(), SC.AllStyleCategories)
            entry.group_style_xml = doc.toString()
        for child in group.children():
            if isinstance(child, QgsLayerTreeGroup):
                if child.isVisible():
                    sub = walk(child)
                    if sub.children:
                        entry.children.append(sub)
            elif isinstance(child, QgsLayerTreeLayer) and child.layerId() in in_order:
                entry.children.append(TreeEntry("layer", child.name(), child.layerId()))
        return entry

    tree = walk(root)
    layers: Dict[str, QgsMapLayer] = {}
    for lid in order:
        src = project.mapLayer(lid)
        clone = src.clone()
        if clone is None:
            raise ProjectBuildError(f"layer '{src.name()}' cannot be cloned")
        clone.moveToThread(None)
        layers[lid] = clone
        if not isinstance(src, QgsVectorLayer):
            warnings.append(f"layer '{src.name()}' ({src.type().name}) is not a vector layer: it is kept as-is "
                            f"(original source) and treated as occupying its whole extent")

    custom = [lyr.id() for lyr in root.customLayerOrder() if lyr.id() in visible_ids] \
        if root.hasCustomLayerOrder() else None

    style_xml = None
    try:
        doc = QDomDocument()
        el = project.styleSettings().writeXml(doc, QgsReadWriteContext())
        doc.appendChild(el)
        style_xml = doc.toString()
    except Exception:  # pragma: no cover - optional
        pass

    colors, _ = project.readListEntry("Palette", "/Colors")
    labels, _ = project.readListEntry("Palette", "/Labels")
    return ProjectSnapshot(
        root=tree, layers=layers, render_order=order, custom_order=custom,
        file_name=project.fileName(), home_path=project.homePath(), ellipsoid=project.ellipsoid(),
        transform_context=project.transformContext(), scale_method=project.scaleMethod(),
        variables=dict(project.customVariables()), palette=(list(colors), list(labels)),
        labeling_settings=QgsLabelingEngineSettings(project.labelingEngineSettings()),
        background=QColor(project.backgroundColor()), style_settings_xml=style_xml, warnings=warnings)


# ---------------------------------------------------------------------------
# Path fixing
# ---------------------------------------------------------------------------
def _is_resource_absolute(p: str) -> bool:
    return (not p or os.path.isabs(p) or p.startswith(("base64:", "http://", "https://", "data:", ":/"))
            or "://" in p)


def _resolve(p: str, resolver: QgsPathResolver) -> Optional[str]:
    if _is_resource_absolute(p):
        return None
    cand = QgsSymbolLayerUtils.svgSymbolNameToPath(p, resolver)
    if cand and os.path.isabs(cand) and os.path.exists(cand):
        return cand
    cand = resolver.readPath(p)
    return cand if cand and os.path.isabs(cand) and os.path.exists(cand) else None


def absolutize_symbol_paths(sym: Optional[QgsSymbol], resolver: QgsPathResolver, changed: List[str]) -> None:
    """Make SVG / raster image paths of a symbol absolute (recursively)."""
    if sym is None:
        return
    for sl in sym.symbolLayers():
        getter = setter = None
        if isinstance(sl, (QgsSvgMarkerSymbolLayer, QgsRasterMarkerSymbolLayer)):
            getter, setter = sl.path, sl.setPath
        elif isinstance(sl, QgsSVGFillSymbolLayer):
            getter, setter = sl.svgFilePath, sl.setSvgFilePath
        elif isinstance(sl, QgsRasterFillSymbolLayer):
            getter, setter = sl.imageFilePath, sl.setImageFilePath
        if getter is not None:
            new = _resolve(getter(), resolver)
            if new:
                changed.append(f"{getter()} -> {new}")
                setter(new)
        absolutize_symbol_paths(sl.subSymbol(), resolver, changed)


def absolutize_layer_paths(layer: QgsVectorLayer, resolver: QgsPathResolver) -> List[str]:
    """Fix relative resource paths in renderer symbols and label backgrounds."""
    changed: List[str] = []
    r = layer.renderer()
    if r is not None:
        from qgis.core import QgsRenderContext
        for s in r.symbols(QgsRenderContext()):
            absolutize_symbol_paths(s, resolver, changed)
    lab = layer.labeling()
    settings_list = []
    if isinstance(lab, QgsVectorLayerSimpleLabeling):
        settings_list = [lab.settings()]
    elif isinstance(lab, QgsRuleBasedLabeling):
        settings_list = [rule.settings() for rule in lab.rootRule().descendants() if rule.settings()]
    for s in settings_list:
        fmt = s.format()
        bg = fmt.background()
        new = _resolve(bg.svgFile(), resolver)
        if new:
            changed.append(f"label background {bg.svgFile()} -> {new}")
            bg.setSvgFile(new)
            fmt.setBackground(bg)
            s.setFormat(fmt)
            absolutize_symbol_paths(bg.markerSymbol(), resolver, changed)
    if changed:
        layer.triggerRepaint()
    return changed


# ---------------------------------------------------------------------------
# Build (worker thread)
# ---------------------------------------------------------------------------
@dataclass
class BuildReport:
    project_path: str
    checks: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)


def _copy_style(src: QgsMapLayer, dst: QgsMapLayer, categories=STYLE_CATEGORIES) -> None:
    doc = QDomDocument()
    err = src.exportNamedStyle(doc, QgsReadWriteContext(), categories)
    if err:
        raise ProjectBuildError(f"cannot export style of '{src.name()}': {err}")
    ok, err = dst.importNamedStyle(doc, categories)
    if not ok:
        raise ProjectBuildError(f"cannot import style into '{dst.name()}': {err}")


def build_render_project(snap: ProjectSnapshot, exported: Dict[str, Tuple[str, int]],
                         target_crs: QgsCoordinateReferenceSystem, out_path: str,
                         order_fields: Optional[Dict[str, str]] = None) -> BuildReport:
    """Create and save the render project.

    :param exported: layer id -> (FlatGeobuf path, exported feature count)
    :param order_fields: layer id -> exported field holding the source
        feature order; such layers get it as "control feature rendering order"
        so the spatially sorted FlatGeobuf draws in the original order.
    """
    order_fields = order_fields or {}
    rep = BuildReport(out_path)
    project = QgsProject()
    project.setFilePathStorage(Qgis.FilePathType.Absolute)
    project.setCrs(target_crs)
    project.setEllipsoid(snap.ellipsoid)
    project.setTransformContext(snap.transform_context)
    project.setScaleMethod(snap.scale_method)
    project.setCustomVariables(snap.variables)
    project.setLabelingEngineSettings(snap.labeling_settings)
    project.setBackgroundColor(snap.background)
    if snap.home_path:
        project.setPresetHomePath(snap.home_path)  # keeps @project_home based expressions working
    colors, labels = snap.palette
    if colors:
        project.writeEntry("Palette", "/Colors", colors)
        project.writeEntry("Palette", "/Labels", labels)
    if snap.style_settings_xml:
        doc = QDomDocument()
        if doc.setContent(snap.style_settings_xml):
            project.styleSettings().readXml(doc.documentElement(), QgsReadWriteContext())
    resolver = QgsPathResolver(snap.file_name)

    new_layers: Dict[str, QgsMapLayer] = {}
    for lid in snap.render_order:
        src = snap.layers[lid]
        if isinstance(src, QgsVectorLayer):
            path, count = exported[lid]
            lyr = QgsVectorLayer(path, src.name(), "ogr")
            if not lyr.isValid():
                raise ProjectBuildError(f"exported layer '{src.name()}' is not valid: {path}")
            _copy_style(src, lyr)
            r = lyr.renderer()
            if lid in order_fields and r is not None and not r.orderByEnabled():
                r.setOrderBy(QgsFeatureRequest.OrderBy(
                    [QgsFeatureRequest.OrderByClause(QgsExpression.quotedColumnRef(order_fields[lid]), True)]))
                r.setOrderByEnabled(True)
                rep.checks.append(f"{src.name()}: drawing order pinned to source order ({order_fields[lid]})")
            fixed = absolutize_layer_paths(lyr, resolver)
            if fixed:
                rep.checks.append(f"{src.name()}: made {len(fixed)} resource path(s) absolute")
        else:
            lyr = src.clone()
        if not lyr.setId(lid):
            rep.warnings.append(f"could not keep layer id {lid} for '{src.name()}'")
        new_layers[lid] = lyr

    for lyr in new_layers.values():
        project.addMapLayer(lyr, False)

    def build(entry: TreeEntry, group: QgsLayerTreeGroup) -> None:
        for child in entry.children:
            if child.kind == "layer":
                node = group.addLayer(new_layers[child.layer_id])
                node.setName(child.name)
                node.setItemVisibilityChecked(True)
            else:
                g = group.addGroup(child.name)
                g.setItemVisibilityChecked(True)
                build(child, g)
                if child.group_style_xml:
                    gl = g.convertToGroupLayer(QgsGroupLayer.LayerOptions(snap.transform_context))
                    if gl is not None:
                        doc = QDomDocument()
                        doc.setContent(child.group_style_xml)
                        gl.importNamedStyle(doc, SC.AllStyleCategories)
                        project.addMapLayer(gl, False)

    root: QgsLayerTree = project.layerTreeRoot()
    build(snap.root, root)
    if snap.custom_order:
        root.setCustomLayerOrder([new_layers[i] for i in snap.custom_order])
        root.setHasCustomLayerOrder(True)

    # --- verification -----------------------------------------------------------
    order_new = [lyr.id() for lyr in root.layerOrder() if lyr.id() in new_layers]
    order_old = list(snap.render_order)
    if order_new != order_old:
        raise ProjectBuildError(f"drawing order differs after rebuild: {order_new} vs {order_old}")
    for lid, lyr in new_layers.items():
        src = snap.layers[lid]
        if not isinstance(src, QgsVectorLayer):
            continue
        path, count = exported[lid]
        if lyr.featureCount() != count:
            raise ProjectBuildError(f"{src.name()}: feature count {lyr.featureCount()} != exported {count}")
        rs, rn = src.renderer(), lyr.renderer()
        if (rs.type() if rs else None) != (rn.type() if rn else None):
            raise ProjectBuildError(f"{src.name()}: renderer type differs ({rs and rs.type()} vs {rn and rn.type()})")
        if src.labelsEnabled() != lyr.labelsEnabled():
            raise ProjectBuildError(f"{src.name()}: labeling enabled state differs")
        ls, ln = src.labeling(), lyr.labeling()
        if (ls.type() if ls else None) != (ln.type() if ln else None):
            raise ProjectBuildError(f"{src.name()}: labeling type differs")
        if lyr.subsetString():
            raise ProjectBuildError(f"{src.name()}: exported layer unexpectedly has a filter")
        if abs(lyr.opacity() - src.opacity()) > 1e-9 or lyr.blendMode() != src.blendMode() or \
                lyr.hasScaleBasedVisibility() != src.hasScaleBasedVisibility():
            raise ProjectBuildError(f"{src.name()}: opacity/blend mode/scale visibility not preserved")
        rep.checks.append(f"{src.name()}: {count} features, renderer '{rn.type() if rn else None}', "
                          f"labels {'on (' + ln.type() + ')' if lyr.labelsEnabled() and ln else 'off'} - OK")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    if not project.write(out_path):
        raise ProjectBuildError(f"cannot write render project {out_path}: {project.error()}")
    project.clear()
    return rep
