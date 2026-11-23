"""Determine the attribute fields a vector layer needs for rendering.

Sources of field references that are collected:

* renderer ``usedAttributes(renderContext)`` (includes rule filters, symbol and
  symbol-layer data-defined properties, geometry generators, classification
  attributes) plus an explicit walk of rule-based rules, symbols and order-by,
* labeling: ``QgsPalLayerSettings.referencedFields`` for simple labeling and for
  every rule (plus rule filters) of rule-based labeling,
* diagrams: ``QgsDiagramRenderer.referencedFields`` and diagram layer settings'
  data-defined properties.

Every expression string found is parsed; if any fails to parse, or something
requires *all* attributes, the layer keeps all fields (correct rendering beats
speed). Virtual (expression) and joined fields that are needed are reported so
they are exported as real fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Set, Tuple

from qgis.core import (
    Qgis,
    QgsCategorizedSymbolRenderer,
    QgsExpression,
    QgsExpressionContext,
    QgsExpressionContextUtils,
    QgsFeatureRequest,
    QgsGeometryGeneratorSymbolLayer,
    QgsGraduatedSymbolRenderer,
    QgsMapSettings,
    QgsPalLayerSettings,
    QgsProject,
    QgsPropertyCollection,
    QgsRenderContext,
    QgsRuleBasedLabeling,
    QgsRuleBasedRenderer,
    QgsSymbol,
    QgsVectorLayer,
    QgsVectorLayerSimpleLabeling,
)
from qgis.PyQt.QtCore import QSize

ALL = QgsFeatureRequest.ALL_ATTRIBUTES

#: Expression functions that read *other* layers (their data is not exported
#: unless those layers are part of the render project themselves).
CROSS_LAYER_FUNCTIONS = ("aggregate", "relation_aggregate", "get_feature", "get_feature_by_id",
                         "overlay_", "represent_value", "layer_property")


@dataclass
class FieldAnalysis:
    """Result for one layer."""

    layer_name: str
    required: List[str]               # field names in layer order
    keep_all: bool
    virtual_fields: List[str] = field(default_factory=list)
    joined_fields: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    expressions_checked: int = 0
    #: every expression string the style evaluates (re-parsed later inside qgis_process)
    expressions: List[str] = field(default_factory=list)

    def summary(self, total_fields: int) -> str:
        s = (f"{self.layer_name}: keeping {'ALL ' if self.keep_all else ''}{len(self.required)}/{total_fields} "
             f"fields: {', '.join(self.required) or '(none)'}")
        if self.virtual_fields:
            s += f"; virtual→real: {', '.join(self.virtual_fields)}"
        if self.joined_fields:
            s += f"; joined→real: {', '.join(self.joined_fields)}"
        return s


def make_render_context(layer: QgsVectorLayer, project: Optional[QgsProject] = None) -> QgsRenderContext:
    """A valid render context (from map settings) with global/project/layer scopes."""
    ms = QgsMapSettings()
    ms.setLayers([layer])
    ms.setDestinationCrs(layer.crs())
    ext = layer.extent()
    if ext.isEmpty():
        ext = ext.buffered(1.0)
    ms.setExtent(ext)
    ms.setOutputSize(QSize(1024, 1024))
    rc = QgsRenderContext.fromMapSettings(ms)
    ctx = QgsExpressionContext()
    ctx.appendScope(QgsExpressionContextUtils.globalScope())
    if project is not None:
        ctx.appendScope(QgsExpressionContextUtils.projectScope(project))
    ctx.appendScope(QgsExpressionContextUtils.mapSettingsScope(ms))
    ctx.appendScope(QgsExpressionContextUtils.layerScope(layer))
    rc.setExpressionContext(ctx)
    return rc


class _Collector:
    def __init__(self, rc: QgsRenderContext, layer_fields) -> None:
        self.rc = rc
        self.layer_fields = layer_fields
        self.fields: Set[str] = set()
        self.expressions: List[Tuple[str, str]] = []  # (where, expression)
        self.keep_all_reasons: List[str] = []

    def add_fields(self, names: Iterable[str], where: str) -> None:
        for n in names:
            if n == ALL:
                self.keep_all_reasons.append(f"{where} requires all attributes")
            elif n:
                self.fields.add(n)

    def add_expression(self, expr: str, where: str) -> None:
        if expr and expr.strip():
            self.expressions.append((where, expr))

    def add_properties(self, props: Optional[QgsPropertyCollection], where: str) -> None:
        if props is None:
            return
        self.add_fields(props.referencedFields(self.rc.expressionContext()), where)
        for key in props.propertyKeys():
            p = props.property(key)
            if p.isActive() and p.propertyType() == Qgis.PropertyType.Expression:
                self.add_expression(p.expressionString(), f"{where} property {key}")

    def add_symbol(self, sym: Optional[QgsSymbol], where: str) -> None:
        if sym is None:
            return
        self.add_fields(sym.usedAttributes(self.rc), where)
        self.add_properties(sym.dataDefinedProperties(), f"{where} symbol")
        for i, sl in enumerate(sym.symbolLayers()):
            w = f"{where} symbol layer {i} ({sl.layerType()})"
            self.add_fields(sl.usedAttributes(self.rc), w)
            self.add_properties(sl.dataDefinedProperties(), w)
            if isinstance(sl, QgsGeometryGeneratorSymbolLayer):
                self.add_expression(sl.geometryExpression(), f"{w} geometry generator")
            self.add_symbol(sl.subSymbol(), f"{w} sub-symbol")

    def add_pal(self, s: Optional[QgsPalLayerSettings], where: str) -> None:
        if s is None:
            return
        self.add_fields(s.referencedFields(self.rc), where)
        self.add_properties(s.dataDefinedProperties(), where)
        if s.isExpression:
            self.add_expression(s.fieldName, f"{where} label expression")


def analyze_layer_fields(layer: QgsVectorLayer, project: Optional[QgsProject] = None) -> FieldAnalysis:
    """Collect the fields of ``layer`` needed to render it like the original."""
    rc = make_render_context(layer, project)
    c = _Collector(rc, layer.fields())

    renderer = layer.renderer()
    if renderer is not None:
        _collect_renderer(c, renderer, "renderer")

    if layer.labelsEnabled() and layer.labeling() is not None:
        lab = layer.labeling()
        if isinstance(lab, QgsRuleBasedLabeling):
            for rule in lab.rootRule().descendants():
                w = f"label rule '{rule.description()}'"
                if not _is_else(rule):
                    c.add_expression(rule.filterExpression(), f"{w} filter")
                c.add_pal(rule.settings(), w)
        elif isinstance(lab, QgsVectorLayerSimpleLabeling):
            c.add_pal(lab.settings(), "labeling")
        else:
            for pid in lab.subProviders():
                c.add_pal(lab.settings(pid), f"labeling provider {pid}")

    dr = layer.diagramRenderer()
    if dr is not None:
        c.add_fields(dr.referencedFields(rc.expressionContext()), "diagrams")
        dls = layer.diagramLayerSettings()
        if dls is not None:
            c.add_properties(dls.dataDefinedProperties(), "diagram settings")

    # --- parse check ----------------------------------------------------------
    warnings: List[str] = []
    for where, expr in c.expressions:
        e = QgsExpression(expr)
        if e.hasParserError():
            c.keep_all_reasons.append(f"cannot parse {where}: {e.parserErrorString()} [{expr}]")
            continue
        c.add_fields(e.referencedColumns(), where)
        low = expr.lower()
        if any(fn + ("" if fn.endswith("_") else "(") in low.replace(" (", "(") for fn in CROSS_LAYER_FUNCTIONS):
            warnings.append(f"{where} reads other layers ({expr}); make sure those layers are available "
                            f"to the render project")

    fields = layer.fields()
    keep_all = bool(c.keep_all_reasons)
    warnings.extend(c.keep_all_reasons)
    if keep_all:
        idxs = list(range(fields.count()))
    else:
        idxs_set = set()
        for name in c.fields:
            i = fields.lookupField(name)  # case-insensitive like the expression engine
            if i >= 0:
                idxs_set.add(i)
        idxs = sorted(idxs_set)
    required = [fields.at(i).name() for i in idxs]
    virtual = [fields.at(i).name() for i in idxs if fields.fieldOrigin(i) == Qgis.FieldOrigin.Expression]
    joined = [fields.at(i).name() for i in idxs if fields.fieldOrigin(i) == Qgis.FieldOrigin.Join]
    return FieldAnalysis(layer.name(), required, keep_all, virtual, joined, warnings, len(c.expressions),
                         sorted({e for _, e in c.expressions}))


def _is_else(rule) -> bool:
    """ELSE rules ("all other values") store the literal filter 'ELSE', which is not an
    expression; they reference no fields of their own (their siblings' filters are analysed)."""
    return rule.isElse() or rule.filterExpression().strip().upper() == "ELSE"


def _collect_renderer(c: _Collector, renderer, where: str) -> None:
    c.add_fields(renderer.usedAttributes(c.rc), where)
    if renderer.orderByEnabled():
        for clause in renderer.orderBy():
            c.add_expression(clause.expression().expression(), f"{where} order by")
    if isinstance(renderer, QgsRuleBasedRenderer):
        for rule in renderer.rootRule().descendants():
            w = f"{where} rule '{rule.label()}'"
            if rule.filterExpression() and not _is_else(rule):
                c.add_expression(rule.filterExpression(), f"{w} filter")
            c.add_symbol(rule.symbol(), w)
    else:
        if isinstance(renderer, (QgsCategorizedSymbolRenderer, QgsGraduatedSymbolRenderer)):
            attr = renderer.classAttribute()
            if attr and c.layer_fields.lookupField(attr) < 0:  # an expression, not a plain field name
                c.add_expression(attr, f"{where} class attribute")
        for sym in renderer.symbols(c.rc):
            c.add_symbol(sym, where)
    # point displacement / cluster / inverted polygon / 2.5D wrap another renderer
    emb = getattr(renderer, "embeddedRenderer", None)
    if callable(emb) and emb() is not None:
        _collect_renderer(c, emb(), f"{where} embedded")
