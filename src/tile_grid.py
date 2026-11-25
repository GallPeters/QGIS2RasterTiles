"""Tile-grid math matching QGIS' "Generate XYZ tiles" algorithms.

The grid is defined by a zoom-0 extent and a zoom-0 matrix size (columns x rows).
The origin is the top-left corner, rows grow downwards (GeoPackage convention),
and every zoom level halves the tile size. Web Mercator is *not* assumed.

The formulas mirror ``qgsalgorithmxyztiles.cpp`` (QGIS 4):

* zoom-0 extent is made square-pixelled by expanding it around its centre,
* tile range for an extent: ``floor`` on the min side, ``ceil - 1`` on the max
  side, both clamped to the matrix,
* metatiles are anchored at the first tile of the requested range (NOT at the
  global grid), i.e. metatile columns start at ``minX + k * metatile_size``.

No QGIS dependency.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterator, Optional, Tuple

#: Half-width of the EPSG:3857 world, as used by QGIS.
MERC_MAX = 20037508.342789244


@dataclass(frozen=True)
class Rect:
    """Axis-aligned rectangle (xmin, ymin, xmax, ymax) in map units."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float

    @property
    def width(self) -> float:
        return self.xmax - self.xmin

    @property
    def height(self) -> float:
        return self.ymax - self.ymin

    def is_empty(self) -> bool:
        return not (self.xmax > self.xmin and self.ymax > self.ymin)

    def buffered(self, d: float) -> "Rect":
        return Rect(self.xmin - d, self.ymin - d, self.xmax + d, self.ymax + d)

    def intersects(self, o: "Rect") -> bool:
        return not (o.xmin > self.xmax or o.xmax < self.xmin or o.ymin > self.ymax or o.ymax < self.ymin)

    def intersection(self, o: "Rect") -> Optional["Rect"]:
        r = Rect(max(self.xmin, o.xmin), max(self.ymin, o.ymin), min(self.xmax, o.xmax), min(self.ymax, o.ymax))
        return None if (r.xmax < r.xmin or r.ymax < r.ymin) else r

    def union(self, o: "Rect") -> "Rect":
        return Rect(min(self.xmin, o.xmin), min(self.ymin, o.ymin), max(self.xmax, o.xmax), max(self.ymax, o.ymax))

    def to_processing_string(self, crs_authid: Optional[str] = None) -> str:
        """``xmin,xmax,ymin,ymax [CRS]`` as accepted by extent parameters.

        ``repr`` keeps full double precision so tile boundaries are not shifted.
        """
        s = f"{self.xmin!r},{self.xmax!r},{self.ymin!r},{self.ymax!r}"
        return f"{s} [{crs_authid}]" if crs_authid else s


@dataclass(frozen=True)
class TileRange:
    """Inclusive tile index range at one zoom level."""

    zoom: int
    min_col: int
    max_col: int
    min_row: int
    max_row: int

    @property
    def cols(self) -> int:
        return self.max_col - self.min_col + 1

    @property
    def rows(self) -> int:
        return self.max_row - self.min_row + 1

    @property
    def count(self) -> int:
        return self.cols * self.rows


@dataclass(frozen=True)
class MetatileGrid:
    """Metatile layout of one single built-in run at one zoom level.

    Metatile ``(i, j)`` covers columns ``range.min_col + i*size ...`` and rows
    ``range.min_row + j*size ...``, clipped to the range.
    """

    range: TileRange
    size: int

    @property
    def zoom(self) -> int:
        return self.range.zoom

    @property
    def meta_cols(self) -> int:
        return -(-self.range.cols // self.size)

    @property
    def meta_rows(self) -> int:
        return -(-self.range.rows // self.size)

    @property
    def count(self) -> int:
        return self.meta_cols * self.meta_rows

    def tile_span(self, i0: int, i1: int, j0: int, j1: int) -> TileRange:
        """Tile range covered by metatiles i0..i1 x j0..j1 (inclusive)."""
        r = self.range
        return TileRange(
            r.zoom,
            r.min_col + i0 * self.size,
            min(r.min_col + (i1 + 1) * self.size - 1, r.max_col),
            r.min_row + j0 * self.size,
            min(r.min_row + (j1 + 1) * self.size - 1, r.max_row),
        )

    def meta_index_range(self, cols: Tuple[int, int], rows: Tuple[int, int]) -> Optional[Tuple[int, int, int, int]]:
        """Metatile indices (i0, i1, j0, j1) touching tile cols/rows, or None if outside."""
        r = self.range
        c0, c1 = max(cols[0], r.min_col), min(cols[1], r.max_col)
        r0, r1 = max(rows[0], r.min_row), min(rows[1], r.max_row)
        if c0 > c1 or r0 > r1:
            return None
        return ((c0 - r.min_col) // self.size, (c1 - r.min_col) // self.size,
                (r0 - r.min_row) // self.size, (r1 - r.min_row) // self.size)


@dataclass(frozen=True)
class TileGrid:
    """A tile matrix set: top-left origin, zoom-0 tile size and matrix size."""

    origin_x: float
    origin_y: float
    z0_tile_size: float  # map units, square tiles (QgsTileMatrix::fromCustomDef)
    z0_cols: int
    z0_rows: int
    tile_width_px: int
    tile_height_px: int

    @classmethod
    def from_zoom0_extent(cls, xmin: float, xmax: float, ymin: float, ymax: float,
                          matrix_width: int, matrix_height: int,
                          tile_width_px: int, tile_height_px: int) -> "TileGrid":
        """Build the grid like QGIS does from Z0_EXTENT and the zoom-0 matrix size.

        A non-square-pixelled extent is expanded around its centre so that pixels
        are square (QGIS does the same and logs a warning).
        """
        w, h = xmax - xmin, ymax - ymin
        px = w / (matrix_width * tile_width_px)
        py = h / (matrix_height * tile_height_px)
        if not math.isclose(px, py, rel_tol=1e-12):
            p = max(px, py)
            cx, cy = (xmin + xmax) / 2.0, (ymin + ymax) / 2.0
            w = p * matrix_width * tile_width_px
            h = p * matrix_height * tile_height_px
            xmin, xmax, ymin, ymax = cx - w / 2, cx + w / 2, cy - h / 2, cy + h / 2
        return cls(xmin, ymax, w / matrix_width, matrix_width, matrix_height, tile_width_px, tile_height_px)

    @classmethod
    def for_target(cls, crs_authid: str, zoom0_extent: Tuple[float, float, float, float],
                   matrix_width: int, matrix_height: int, tile_width_px: int, tile_height_px: int) -> "TileGrid":
        """Grid exactly as the built-in derives it for a target CRS.

        For EPSG:3857 QGIS ignores Z0_EXTENT and uses the Web Mercator square
        (the zoom-0 matrix size is still applied).
        """
        if crs_authid.upper() == "EPSG:3857":
            zoom0_extent = (-MERC_MAX, MERC_MAX, -MERC_MAX, MERC_MAX)
        return cls.from_zoom0_extent(*zoom0_extent, matrix_width, matrix_height, tile_width_px, tile_height_px)

    # --- per-zoom properties -------------------------------------------------
    def matrix_size(self, zoom: int) -> Tuple[int, int]:
        """(columns, rows) of the tile matrix at ``zoom``."""
        f = 1 << zoom
        return self.z0_cols * f, self.z0_rows * f

    def tile_size(self, zoom: int) -> float:
        """Tile edge length in map units at ``zoom``."""
        return self.z0_tile_size / (1 << zoom)

    def matrix_extent(self, zoom: int = 0) -> Rect:
        """Extent of the full tile matrix (identical at every zoom)."""
        cols, rows = self.matrix_size(zoom)
        s = self.tile_size(zoom)
        return Rect(self.origin_x, self.origin_y - rows * s, self.origin_x + cols * s, self.origin_y)

    def tile_units(self, zoom: int) -> Tuple[float, float]:
        """Tile width/height in map units, computed like QGIS (extent / matrix size)."""
        e = self.matrix_extent(zoom)
        cols, rows = self.matrix_size(zoom)
        return e.width / cols, e.height / rows

    def pixel_size(self, zoom: int) -> Tuple[float, float]:
        """Map units per pixel (x, y) at ``zoom`` (as stored in gpkg_tile_matrix)."""
        e = self.matrix_extent(zoom)
        cols, rows = self.matrix_size(zoom)
        return e.width / (cols * self.tile_width_px), e.height / (rows * self.tile_height_px)

    # --- extent <-> tiles ----------------------------------------------------
    def tile_range(self, extent: Rect, zoom: int) -> TileRange:
        """Tiles touched by ``extent`` at ``zoom`` exactly as the built-in computes them."""
        tw, th = self.tile_units(zoom)
        e = self.matrix_extent(zoom)
        cols, rows = self.matrix_size(zoom)
        min_x = math.floor((extent.xmin - e.xmin) / tw)
        max_x = math.ceil((extent.xmax - e.xmin) / tw) - 1
        min_y = math.floor((e.ymax - extent.ymax) / th)
        max_y = math.ceil((e.ymax - extent.ymin) / th) - 1
        clamp = lambda v, hi: max(0, min(v, hi))  # noqa: E731
        return TileRange(zoom, clamp(min_x, cols - 1), clamp(max_x, cols - 1),
                         clamp(min_y, rows - 1), clamp(max_y, rows - 1))

    def tile_bounds(self, col: int, row: int, zoom: int) -> Rect:
        """Map extent of one tile."""
        tw, th = self.tile_units(zoom)
        e = self.matrix_extent(zoom)
        return Rect(e.xmin + col * tw, e.ymax - (row + 1) * th, e.xmin + (col + 1) * tw, e.ymax - row * th)

    def range_bounds(self, r: TileRange) -> Rect:
        """Map extent covered by a tile range."""
        a = self.tile_bounds(r.min_col, r.min_row, r.zoom)
        b = self.tile_bounds(r.max_col, r.max_row, r.zoom)
        return Rect(a.xmin, b.ymin, b.xmax, a.ymax)

    def job_extent(self, r: TileRange, epsilon_fraction: float = 1e-6) -> Rect:
        """Extent that makes the built-in select exactly the tiles of ``r``.

        The tile-aligned bounds are shrunk by a tiny fraction of a tile so the
        shared edge with neighbouring blocks is not included twice
        (``floor``/``ceil - 1`` would otherwise add one extra row/column).
        """
        b = self.range_bounds(r)
        tw, th = self.tile_units(r.zoom)
        ex, ey = tw * epsilon_fraction, th * epsilon_fraction
        return Rect(b.xmin + ex, b.ymin + ey, b.xmax - ex, b.ymax - ey)

    def tile_cols_rows_for_rect(self, rect: Rect, zoom: int) -> Tuple[Tuple[int, int], Tuple[int, int]]:
        """Unclamped (col0, col1), (row0, row1) touched by ``rect`` (closed intervals)."""
        tw, th = self.tile_units(zoom)
        e = self.matrix_extent(zoom)
        c0 = math.floor((rect.xmin - e.xmin) / tw)
        c1 = math.floor((rect.xmax - e.xmin) / tw)
        r0 = math.floor((e.ymax - rect.ymax) / th)
        r1 = math.floor((e.ymax - rect.ymin) / th)
        return (c0, c1), (r0, r1)

    def metatile_grid(self, extent: Rect, zoom: int, metatile_size: int) -> MetatileGrid:
        """Metatile layout of a single built-in run over ``extent`` at ``zoom``."""
        return MetatileGrid(self.tile_range(extent, zoom), metatile_size)

    def iter_tiles(self, r: TileRange) -> Iterator[Tuple[int, int, int]]:
        for col in range(r.min_col, r.max_col + 1):
            for row in range(r.min_row, r.max_row + 1):
                yield r.zoom, col, row
