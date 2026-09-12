# -*- coding: utf-8 -*-
"""Network integrity checks and conservative small-gap repair helpers."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from qgis.core import (
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)


@dataclass
class GapCandidate:
    source_fid: int
    endpoint_name: str
    start: QgsPointXY
    target_fid: int
    end: QgsPointXY
    distance: float


def _distance(a: QgsPointXY, b: QgsPointXY) -> float:
    return math.hypot(a.x() - b.x(), a.y() - b.y())


def _line_endpoints(feature: QgsFeature) -> List[Tuple[str, QgsPointXY]]:
    geom = feature.geometry()
    if geom is None or geom.isNull() or geom.isEmpty():
        return []
    if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
        return []

    out = []
    if geom.isMultipart():
        for i, line in enumerate(geom.asMultiPolyline()):
            if len(line) >= 2:
                out.append((f"part_{i}_start", QgsPointXY(line[0])))
                out.append((f"part_{i}_end", QgsPointXY(line[-1])))
    else:
        line = geom.asPolyline()
        if len(line) >= 2:
            out.append(("start", QgsPointXY(line[0])))
            out.append(("end", QgsPointXY(line[-1])))
    return out


def _bbox_around(point: QgsPointXY, radius: float) -> QgsRectangle:
    return QgsRectangle(
        point.x() - radius,
        point.y() - radius,
        point.x() + radius,
        point.y() + radius,
    )


def inspect_network(
    layer: QgsVectorLayer,
    connection_tolerance: float = 0.01,
    gap_tolerance: float = 1.0,
    feedback=None,
) -> Tuple[List[GapCandidate], int]:
    """
    Find small gaps beginning at dangling endpoints.

    An endpoint is considered connected when it lies within connection_tolerance
    of another line geometry. A repair candidate is the nearest other line
    geometry within gap_tolerance. The target may be another endpoint or an
    interior point on a line.

    Returns (unique_gap_candidates, dangling_endpoint_count).
    """
    if layer is None or not layer.isValid():
        raise ValueError("Input layer is invalid.")
    if layer.crs().isGeographic():
        raise ValueError("A projected CRS is required for network gap checking.")
    if gap_tolerance <= connection_tolerance:
        raise ValueError("Gap tolerance must be greater than connection tolerance.")

    features = {int(f.id()): f for f in layer.getFeatures()}
    index = QgsSpatialIndex()
    for f in features.values():
        index.addFeature(f)

    candidates: List[GapCandidate] = []
    dangling_count = 0
    endpoints = []
    for fid, feature in features.items():
        for endpoint_name, p in _line_endpoints(feature):
            endpoints.append((fid, endpoint_name, p))

    total = max(1, len(endpoints))
    for pos, (fid, endpoint_name, p) in enumerate(endpoints):
        if feedback and feedback.isCanceled():
            break

        nearby_ids = index.intersects(_bbox_around(p, gap_tolerance))
        nearest_connected = False
        best = None

        pgeom = QgsGeometry.fromPointXY(p)
        for other_fid in nearby_ids:
            if int(other_fid) == fid:
                continue
            other = features.get(int(other_fid))
            if other is None:
                continue

            other_geom = other.geometry()
            d = pgeom.distance(other_geom)
            if d <= connection_tolerance:
                nearest_connected = True
                break

            if d <= gap_tolerance:
                target_geom = other_geom.nearestPoint(pgeom)
                if target_geom is None or target_geom.isNull() or target_geom.isEmpty():
                    continue
                q = QgsPointXY(target_geom.asPoint())
                exact_d = _distance(p, q)
                if best is None or exact_d < best[0]:
                    best = (exact_d, int(other_fid), q)

        if not nearest_connected:
            dangling_count += 1
            if best is not None and best[0] > connection_tolerance:
                candidates.append(
                    GapCandidate(
                        source_fid=fid,
                        endpoint_name=endpoint_name,
                        start=QgsPointXY(p),
                        target_fid=best[1],
                        end=QgsPointXY(best[2]),
                        distance=float(best[0]),
                    )
                )

        if feedback:
            feedback.setProgress(100.0 * (pos + 1) / total)

    # De-duplicate mutual endpoint-to-endpoint suggestions using coordinate pairs.
    unique: Dict[Tuple[Tuple[int, int], Tuple[int, int]], GapCandidate] = {}
    precision = max(connection_tolerance, 1.0e-9)

    def key_point(pt: QgsPointXY):
        return (round(pt.x() / precision), round(pt.y() / precision))

    for gap in candidates:
        a = key_point(gap.start)
        b = key_point(gap.end)
        key = tuple(sorted((a, b)))
        old = unique.get(key)
        if old is None or gap.distance < old.distance:
            unique[key] = gap

    return list(unique.values()), dangling_count


def connector_geometry(gap: GapCandidate, multipart: bool = False) -> QgsGeometry:
    line = [QgsPointXY(gap.start), QgsPointXY(gap.end)]
    if multipart:
        return QgsGeometry.fromMultiPolylineXY([line])
    return QgsGeometry.fromPolylineXY(line)
