# -*- coding: utf-8 -*-
"""
SpaceSyntaxEngine — native PyQGIS analytical core
Version 0.5.0

Purpose
-------
Segment-based space-syntax analysis directly inside QGIS, without depthmapX
and without GeoPandas / NetworkX / Shapely.

Core model
----------
* One analytical segment = one graph node.
* Segments are split at point intersections.
* Segment adjacency is created at common/snap-tolerant endpoints.
* Topological distance = number of segment-to-segment transitions.
* Angular distance = cumulative junction deflection angle in degrees.
* Angular shortest paths use a lexicographic (angle, hops) cost. This preserves
  exact 0-degree straight continuations without zero-cost-cycle ambiguity.
* HH integration (RA/RRA) is calculated from topological depths only.
* Angular integration is reported separately from angular mean depth.
* Choice is calculated for both topological and angular shortest paths.

Important
---------
Input must use a projected CRS. Distances and snapping tolerances are interpreted
in the layer CRS map units.

This engine intentionally does NOT attempt axial-map generation. It analyses a
line/segment network supplied by the user.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsWkbTypes,
)


EPS = 1.0e-9
INF = float("inf")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class SourcePart:
    part_uid: int
    source_fid: int
    part_id: int
    points: List[QgsPointXY]
    geometry: QgsGeometry


@dataclass
class Segment:
    seg_id: int
    source_fid: int
    source_part: int
    geometry: QgsGeometry
    points: List[QgsPointXY]
    length: float
    start_junction: Optional[int] = None
    end_junction: Optional[int] = None


@dataclass
class Edge:
    neighbor: int
    angle: float


@dataclass
class EngineResult:
    segments: List[Segment]
    metrics: Dict[int, Dict[str, object]]
    warnings: List[str]
    graph: Dict[int, List[Edge]]


# ---------------------------------------------------------------------------
# Basic geometry helpers
# ---------------------------------------------------------------------------

def _distance(a: QgsPointXY, b: QgsPointXY) -> float:
    return math.hypot(b.x() - a.x(), b.y() - a.y())


def _same_point(a: QgsPointXY, b: QgsPointXY, tol: float = EPS) -> bool:
    return _distance(a, b) <= tol


def _polyline_length(points: Sequence[QgsPointXY]) -> float:
    return sum(_distance(points[i - 1], points[i]) for i in range(1, len(points)))


def _clean_polyline(points: Sequence[QgsPointXY], tol: float = EPS) -> List[QgsPointXY]:
    out: List[QgsPointXY] = []
    for p in points:
        q = QgsPointXY(p)
        if not out or not _same_point(out[-1], q, tol):
            out.append(q)
    return out


def _geometry_to_parts(feature: QgsFeature, next_part_uid: int) -> Tuple[List[SourcePart], int]:
    geom = feature.geometry()
    if geom is None or geom.isNull() or geom.isEmpty():
        return [], next_part_uid

    if QgsWkbTypes.geometryType(geom.wkbType()) != QgsWkbTypes.LineGeometry:
        return [], next_part_uid

    parts: List[List[QgsPointXY]]
    if geom.isMultipart():
        parts = [list(line) for line in geom.asMultiPolyline()]
    else:
        parts = [list(geom.asPolyline())]

    result: List[SourcePart] = []
    for part_id, pts in enumerate(parts):
        pts = _clean_polyline(pts)
        if len(pts) < 2 or _polyline_length(pts) <= EPS:
            continue
        g = QgsGeometry.fromPolylineXY(pts)
        result.append(
            SourcePart(
                part_uid=next_part_uid,
                source_fid=int(feature.id()),
                part_id=part_id,
                points=pts,
                geometry=g,
            )
        )
        next_part_uid += 1

    return result, next_part_uid


def _extract_intersection_points(geom: QgsGeometry) -> List[QgsPointXY]:
    """Returns point intersections only. Linear overlaps are handled separately."""
    if geom is None or geom.isNull() or geom.isEmpty():
        return []

    flat = QgsWkbTypes.flatType(geom.wkbType())

    if flat == QgsWkbTypes.Point:
        return [QgsPointXY(geom.asPoint())]

    if flat == QgsWkbTypes.MultiPoint:
        return [QgsPointXY(p) for p in geom.asMultiPoint()]

    if flat == QgsWkbTypes.GeometryCollection:
        out: List[QgsPointXY] = []
        for part in geom.asGeometryCollection():
            out.extend(_extract_intersection_points(part))
        return out

    # LineString/MultiLineString means overlapping collinear geometry,
    # which is ambiguous for a segment graph. We flag and skip it.
    return []


def _has_linear_component(geom: QgsGeometry) -> bool:
    if geom is None or geom.isNull() or geom.isEmpty():
        return False

    gt = QgsWkbTypes.geometryType(geom.wkbType())
    if gt == QgsWkbTypes.LineGeometry:
        return True

    if QgsWkbTypes.flatType(geom.wkbType()) == QgsWkbTypes.GeometryCollection:
        return any(_has_linear_component(g) for g in geom.asGeometryCollection())

    return False


def _point_chainage(points: Sequence[QgsPointXY], p: QgsPointXY) -> float:
    """
    Returns chainage of the closest position on a polyline to p.
    Works in planar/projected coordinates.
    """
    best_dist2 = INF
    best_chain = 0.0
    chain = 0.0

    for i in range(1, len(points)):
        a = points[i - 1]
        b = points[i]
        dx = b.x() - a.x()
        dy = b.y() - a.y()
        seg_len2 = dx * dx + dy * dy

        if seg_len2 <= EPS * EPS:
            continue

        t = ((p.x() - a.x()) * dx + (p.y() - a.y()) * dy) / seg_len2
        t = max(0.0, min(1.0, t))

        px = a.x() + t * dx
        py = a.y() + t * dy
        d2 = (p.x() - px) ** 2 + (p.y() - py) ** 2

        seg_len = math.sqrt(seg_len2)
        if d2 < best_dist2:
            best_dist2 = d2
            best_chain = chain + t * seg_len

        chain += seg_len

    return best_chain


def _interpolate_on_polyline(points: Sequence[QgsPointXY], chainage: float) -> QgsPointXY:
    total = _polyline_length(points)
    if chainage <= 0.0:
        return QgsPointXY(points[0])
    if chainage >= total:
        return QgsPointXY(points[-1])

    chain = 0.0
    for i in range(1, len(points)):
        a = points[i - 1]
        b = points[i]
        seg_len = _distance(a, b)
        if seg_len <= EPS:
            continue

        if chain + seg_len >= chainage - EPS:
            t = (chainage - chain) / seg_len
            return QgsPointXY(
                a.x() + t * (b.x() - a.x()),
                a.y() + t * (b.y() - a.y()),
            )
        chain += seg_len

    return QgsPointXY(points[-1])


def _substring_polyline(
    points: Sequence[QgsPointXY], start: float, end: float
) -> List[QgsPointXY]:
    """Extracts the planar polyline between two chainages."""
    if end - start <= EPS:
        return []

    total = _polyline_length(points)
    start = max(0.0, min(total, start))
    end = max(0.0, min(total, end))

    if end - start <= EPS:
        return []

    out = [_interpolate_on_polyline(points, start)]

    chain = 0.0
    for i in range(1, len(points)):
        seg_len = _distance(points[i - 1], points[i])
        chain += seg_len

        # Preserve original interior vertices.
        if start + EPS < chain < end - EPS:
            out.append(QgsPointXY(points[i]))

    out.append(_interpolate_on_polyline(points, end))
    return _clean_polyline(out)


def _unique_sorted(values: Iterable[float], tolerance: float) -> List[float]:
    vals = sorted(values)
    if not vals:
        return []

    out = [vals[0]]
    for value in vals[1:]:
        if abs(value - out[-1]) > tolerance:
            out.append(value)
    return out


# ---------------------------------------------------------------------------
# Network preparation
# ---------------------------------------------------------------------------

def build_segments(
    layer: QgsVectorLayer,
    snap_tolerance: float = 0.01,
    min_segment_length: float = 1.0e-6,
    feedback=None,
) -> Tuple[List[Segment], List[str]]:
    """
    Splits source line parts at all point intersections.

    snap_tolerance is used for endpoint junction clustering after splitting.
    """
    if layer is None or not layer.isValid():
        raise ValueError("Input layer is invalid.")

    if layer.crs().isGeographic():
        raise ValueError(
            "Space Syntax Engine requires a projected CRS. "
            "Reproject the street network before analysis."
        )

    source_parts: List[SourcePart] = []
    next_uid = 0

    for feature in layer.getFeatures():
        parts, next_uid = _geometry_to_parts(feature, next_uid)
        source_parts.extend(parts)

    if not source_parts:
        raise ValueError("No valid line geometries were found in the input layer.")

    # Temporary features let QgsSpatialIndex prune intersection tests.
    index_features: List[QgsFeature] = []
    by_uid: Dict[int, SourcePart] = {}
    for part in source_parts:
        f = QgsFeature()
        f.setId(part.part_uid)
        f.setGeometry(part.geometry)
        index_features.append(f)
        by_uid[part.part_uid] = part

    # QGIS 3.44 does not accept a plain Python list in the
    # QgsSpatialIndex constructor. Build an empty R-tree and add the
    # temporary features explicitly for broad QGIS 3.x compatibility.
    spatial_index = QgsSpatialIndex()
    for index_feature in index_features:
        if not spatial_index.addFeature(index_feature):
            raise RuntimeError(
                f"Could not add temporary feature {index_feature.id()} "
                "to the spatial index."
            )

    # Break chainages always include line endpoints.
    breakpoints: Dict[int, List[float]] = {}
    for part in source_parts:
        breakpoints[part.part_uid] = [0.0, _polyline_length(part.points)]

    warnings: List[str] = []
    warned_overlap_pairs: Set[Tuple[int, int]] = set()

    for pos, part in enumerate(source_parts):
        if feedback and feedback.isCanceled():
            raise RuntimeError("Analysis canceled by user.")

        candidates = spatial_index.intersects(part.geometry.boundingBox())
        for other_uid in candidates:
            if other_uid <= part.part_uid:
                continue

            other = by_uid.get(other_uid)
            if other is None:
                continue

            if not part.geometry.intersects(other.geometry):
                continue

            intersection = part.geometry.intersection(other.geometry)
            points = _extract_intersection_points(intersection)

            if _has_linear_component(intersection):
                pair = (part.part_uid, other.part_uid)
                if pair not in warned_overlap_pairs:
                    warnings.append(
                        "Overlapping collinear linework detected between "
                        f"source features {part.source_fid} and {other.source_fid}. "
                        "The overlap itself was not converted into additional junctions."
                    )
                    warned_overlap_pairs.add(pair)

            for p in points:
                breakpoints[part.part_uid].append(_point_chainage(part.points, p))
                breakpoints[other.part_uid].append(_point_chainage(other.points, p))

        if feedback:
            feedback.setProgress(20.0 * (pos + 1) / max(1, len(source_parts)))

    # Generate unique analytical segments.
    segments: List[Segment] = []
    next_seg_id = 0

    for pos, part in enumerate(source_parts):
        total = _polyline_length(part.points)
        # Chainage merge tolerance is deliberately much smaller than the
        # junction snap tolerance, because it only de-duplicates split locations.
        chain_tol = max(EPS, min(snap_tolerance * 0.01, total * 1.0e-9))
        cuts = _unique_sorted(breakpoints[part.part_uid], chain_tol)

        for a, b in zip(cuts[:-1], cuts[1:]):
            pts = _substring_polyline(part.points, a, b)
            if len(pts) < 2:
                continue

            length = _polyline_length(pts)
            if length < min_segment_length:
                continue

            geom = QgsGeometry.fromPolylineXY(pts)
            segments.append(
                Segment(
                    seg_id=next_seg_id,
                    source_fid=part.source_fid,
                    source_part=part.part_id,
                    geometry=geom,
                    points=pts,
                    length=length,
                )
            )
            next_seg_id += 1

        if feedback:
            feedback.setProgress(20.0 + 15.0 * (pos + 1) / max(1, len(source_parts)))

    if not segments:
        raise ValueError("Intersection splitting produced no analytical segments.")

    return segments, warnings



def consolidate_fragmented_segments(
    segments: List[Segment],
    snap_tolerance: float = 0.01,
    feedback=None,
) -> Tuple[List[Segment], int]:
    """
    Merge chains of analytical line pieces through degree-2 junctions.

    Raw street datasets are often arbitrarily divided into two or more line
    features between actual intersections. For topological Space Syntax this
    can create artificial extra segment steps. This routine removes those
    artificial breaks after intersection splitting by merging through nodes
    where exactly two segment endpoints meet. True branch junctions (degree
    1, 3, 4, ...) are preserved.

    The routine is deliberately topology-led rather than angle-led: a curved
    street between two real junctions may still be one analytical segment.
    Closed components made entirely of degree-2 nodes are left unchanged.
    """
    if len(segments) < 2:
        return segments, 0

    junctions = _cluster_segment_endpoints(segments, snap_tolerance)
    by_id = {s.seg_id: s for s in segments}
    degree = {jid: len(entries) for jid, entries in junctions.items()}
    visited: Set[int] = set()
    consolidated: List[Segment] = []
    merges = 0

    def other_junction(seg: Segment, jid: int) -> int:
        if seg.start_junction == jid:
            return seg.end_junction
        if seg.end_junction == jid:
            return seg.start_junction
        raise RuntimeError("Segment does not touch the expected junction.")

    def oriented_points(seg: Segment, entry_jid: int) -> List[QgsPointXY]:
        if seg.start_junction == entry_jid:
            return list(seg.points)
        if seg.end_junction == entry_jid:
            return list(reversed(seg.points))
        raise RuntimeError("Segment does not touch the expected junction.")

    # Start chains from a non-degree-2 endpoint. This walks each ordinary
    # intersection-to-intersection chain exactly once.
    for seg in segments:
        if seg.seg_id in visited:
            continue
        endpoints = [seg.start_junction, seg.end_junction]
        starts = [jid for jid in endpoints if degree.get(jid, 0) != 2]
        if not starts:
            continue

        entry_jid = starts[0]
        cur = seg
        chain_ids: List[int] = []
        merged_pts: List[QgsPointXY] = []
        first_seg = seg

        while True:
            if cur.seg_id in visited:
                break
            visited.add(cur.seg_id)
            chain_ids.append(cur.seg_id)
            pts = oriented_points(cur, entry_jid)
            if not merged_pts:
                merged_pts.extend(pts)
            else:
                merged_pts.extend(pts[1:])

            exit_jid = other_junction(cur, entry_jid)
            if degree.get(exit_jid, 0) != 2:
                break

            entries = junctions.get(exit_jid, [])
            next_ids = [sid for sid, _ in entries if sid != cur.seg_id and sid not in visited]
            if not next_ids:
                break
            next_seg = by_id[next_ids[0]]
            entry_jid = exit_jid
            cur = next_seg

        if len(chain_ids) == 1:
            original = by_id[chain_ids[0]]
            consolidated.append(Segment(
                seg_id=original.seg_id, source_fid=original.source_fid,
                source_part=original.source_part, geometry=original.geometry,
                points=list(original.points), length=original.length))
        else:
            merges += len(chain_ids) - 1
            consolidated.append(Segment(
                seg_id=first_seg.seg_id, source_fid=first_seg.source_fid,
                source_part=first_seg.source_part,
                geometry=QgsGeometry.fromPolylineXY(merged_pts),
                points=merged_pts, length=_polyline_length(merged_pts)))

    # Preserve closed rings / components whose every node has degree 2.
    for seg in segments:
        if seg.seg_id not in visited:
            consolidated.append(Segment(
                seg_id=seg.seg_id, source_fid=seg.source_fid,
                source_part=seg.source_part, geometry=seg.geometry,
                points=list(seg.points), length=seg.length))

    consolidated.sort(key=lambda s: s.seg_id)
    for new_id, seg in enumerate(consolidated):
        seg.seg_id = new_id
        seg.start_junction = None
        seg.end_junction = None

    if feedback and merges:
        feedback.pushInfo(f"Consolidated {merges} artificial intermediate segment break(s).")
    return consolidated, merges

def build_existing_segments(
    layer: QgsVectorLayer,
    min_segment_length: float = 1.0e-6,
    feedback=None,
) -> Tuple[List[Segment], List[str]]:
    """Build analytical segments directly from existing input line parts."""
    if layer is None or not layer.isValid():
        raise ValueError("Input layer is invalid.")
    if layer.crs().isGeographic():
        raise ValueError(
            "Space Syntax Engine requires a projected CRS. "
            "Reproject the street network before analysis."
        )

    segments: List[Segment] = []
    warnings: List[str] = []
    next_seg_id = 0
    next_part_uid = 0
    features = list(layer.getFeatures())
    total = max(1, len(features))

    for pos, feature in enumerate(features):
        if feedback and feedback.isCanceled():
            raise RuntimeError("Analysis canceled by user.")
        parts, next_part_uid = _geometry_to_parts(feature, next_part_uid)
        for part in parts:
            length = _polyline_length(part.points)
            if length < min_segment_length:
                continue
            segments.append(Segment(
                seg_id=next_seg_id,
                source_fid=part.source_fid,
                source_part=part.part_id,
                geometry=part.geometry,
                points=part.points,
                length=length,
            ))
            next_seg_id += 1
        if feedback:
            feedback.setProgress(20.0 * (pos + 1) / total)

    if not segments:
        raise ValueError("No valid analytical line segments were found.")
    return segments, warnings


# ---------------------------------------------------------------------------
# Junction clustering and angular graph
# ---------------------------------------------------------------------------

def _cluster_segment_endpoints(
    segments: List[Segment], tolerance: float
) -> Dict[int, List[Tuple[int, str]]]:
    """
    Clusters start/end points into junctions using a metric tolerance.

    Returns:
        junction_id -> [(seg_id, 'start'|'end'), ...]
    """
    if tolerance <= 0:
        tolerance = 1.0e-9

    buckets: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    representatives: Dict[int, QgsPointXY] = {}
    members: Dict[int, List[Tuple[int, str]]] = defaultdict(list)
    next_junction = 0

    def assign(p: QgsPointXY) -> int:
        nonlocal next_junction
        gx = math.floor(p.x() / tolerance)
        gy = math.floor(p.y() / tolerance)

        best_id = None
        best_distance = INF

        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for jid in buckets.get((gx + dx, gy + dy), []):
                    d = _distance(p, representatives[jid])
                    if d <= tolerance and d < best_distance:
                        best_distance = d
                        best_id = jid

        if best_id is not None:
            return best_id

        jid = next_junction
        next_junction += 1
        representatives[jid] = QgsPointXY(p)
        buckets[(gx, gy)].append(jid)
        return jid

    for seg in segments:
        start_id = assign(seg.points[0])
        end_id = assign(seg.points[-1])

        seg.start_junction = start_id
        seg.end_junction = end_id

        members[start_id].append((seg.seg_id, "start"))
        members[end_id].append((seg.seg_id, "end"))

    return members


def _outward_vector(seg: Segment, endpoint: str) -> Tuple[float, float]:
    pts = seg.points

    if endpoint == "start":
        origin = pts[0]
        for p in pts[1:]:
            dx = p.x() - origin.x()
            dy = p.y() - origin.y()
            if math.hypot(dx, dy) > EPS:
                return dx, dy
    else:
        origin = pts[-1]
        for p in reversed(pts[:-1]):
            dx = p.x() - origin.x()
            dy = p.y() - origin.y()
            if math.hypot(dx, dy) > EPS:
                return dx, dy

    return 0.0, 0.0


def _deflection_angle(seg_a: Segment, end_a: str, seg_b: Segment, end_b: str) -> float:
    """
    Junction turn angle in degrees:
      0   = straight continuation
      90  = right-angle turn
      180 = reversal
    """
    ax, ay = _outward_vector(seg_a, end_a)
    bx, by = _outward_vector(seg_b, end_b)

    na = math.hypot(ax, ay)
    nb = math.hypot(bx, by)
    if na <= EPS or nb <= EPS:
        return 180.0

    cos_theta = (ax * bx + ay * by) / (na * nb)
    cos_theta = max(-1.0, min(1.0, cos_theta))
    ray_angle = math.degrees(math.acos(cos_theta))

    return max(0.0, min(180.0, 180.0 - ray_angle))


def build_segment_graph(
    segments: List[Segment], snap_tolerance: float = 0.01
) -> Dict[int, List[Edge]]:
    junctions = _cluster_segment_endpoints(segments, snap_tolerance)
    by_id = {s.seg_id: s for s in segments}

    # Pair map avoids duplicate edges and retains the smallest turn angle if
    # malformed geometry causes the same pair to meet more than once.
    pair_angle: Dict[Tuple[int, int], float] = {}

    for _, members in junctions.items():
        if len(members) < 2:
            continue

        for i in range(len(members)):
            sid_a, end_a = members[i]
            for j in range(i + 1, len(members)):
                sid_b, end_b = members[j]

                if sid_a == sid_b:
                    continue

                angle = _deflection_angle(
                    by_id[sid_a], end_a, by_id[sid_b], end_b
                )
                key = (min(sid_a, sid_b), max(sid_a, sid_b))
                pair_angle[key] = min(angle, pair_angle.get(key, 180.0))

    graph: Dict[int, List[Edge]] = {s.seg_id: [] for s in segments}
    for (a, b), angle in pair_angle.items():
        graph[a].append(Edge(b, angle))
        graph[b].append(Edge(a, angle))

    return graph


# ---------------------------------------------------------------------------
# Shortest paths
# ---------------------------------------------------------------------------

def _bfs_distances(
    graph: Dict[int, List[Edge]],
    source: int,
    radius: Optional[int] = None,
) -> Dict[int, int]:
    dist = {source: 0}
    queue = deque([source])

    while queue:
        v = queue.popleft()
        dv = dist[v]

        if radius is not None and dv >= radius:
            continue

        for edge in graph[v]:
            w = edge.neighbor
            if w not in dist:
                dist[w] = dv + 1
                queue.append(w)

    return dist


def _angular_dijkstra(
    graph: Dict[int, List[Edge]],
    source: int,
    angular_radius: Optional[float] = None,
) -> Dict[int, Tuple[float, int]]:
    """
    Lexicographic shortest path:
       primary   = cumulative angular deflection
       secondary = hops

    The secondary term removes zero-cost-cycle ambiguity while leaving angular
    distance itself unchanged.
    """
    dist: Dict[int, Tuple[float, int]] = {source: (0.0, 0)}
    heap: List[Tuple[float, int, int]] = [(0.0, 0, source)]

    while heap:
        angle_d, hops, v = heapq.heappop(heap)
        if dist.get(v) != (angle_d, hops):
            continue

        if angular_radius is not None and angle_d > angular_radius + EPS:
            continue

        for edge in graph[v]:
            w = edge.neighbor
            candidate = (angle_d + edge.angle, hops + 1)

            if angular_radius is not None and candidate[0] > angular_radius + EPS:
                continue

            current = dist.get(w)
            if current is None or candidate < current:
                dist[w] = candidate
                heapq.heappush(heap, (candidate[0], candidate[1], w))

    return dist


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def _depth_statistics(depths: Dict[int, int]) -> Tuple[int, float, float, int]:
    """Return reachable node count (excluding root), total depth, mean depth, max depth."""
    positive = [int(d) for d in depths.values() if d > 0]
    if not positive:
        return 0, 0.0, 0.0, 0
    total_depth = float(sum(positive))
    return len(positive), total_depth, total_depth / len(positive), max(positive)


def _hillier_integration(depths: Dict[int, int]) -> Tuple[int, float, float, int, Optional[float], Optional[float], Optional[float]]:
    """
    Classic Hillier/Hanson topological integration.

    Returns:
        node_count_excluding_root, total_depth, mean_depth, max_depth,
        RA, RRA, HH integration (1/RRA)

    Important: a root whose entire reachable system is at depth 1 has RA=0 and
    therefore mathematically unbounded/undefined HH integration. Earlier versions
    incorrectly converted this case to 0.0, which falsely represented a maximally
    shallow root as maximally segregated. We now return None for undefined values.
    """
    node_count, total_depth, md, max_depth = _depth_statistics(depths)
    k = node_count + 1  # include root, as in the Hillier/Hanson system size N

    if node_count == 0:
        return 0, 0.0, 0.0, 0, None, None, None
    if k <= 2:
        return node_count, total_depth, md, max_depth, None, None, None

    ra = (2.0 * (md - 1.0)) / (k - 2.0)

    # D-value normalizer used for RRA.
    dk_num = 2.0 * (k * (math.log((k + 2.0) / 3.0) - 1.0) + 1.0)
    dk_den = (k - 1.0) * (k - 2.0)
    if abs(dk_den) <= EPS:
        return node_count, total_depth, md, max_depth, ra, None, None
    dk = dk_num / dk_den
    if abs(dk) <= EPS:
        return node_count, total_depth, md, max_depth, ra, None, None

    rra = ra / dk
    if rra <= EPS:
        # Complete/depth-1 neighbourhood: integration tends to infinity.
        return node_count, total_depth, md, max_depth, ra, rra, None

    return node_count, total_depth, md, max_depth, ra, rra, 1.0 / rra


def _angular_statistics(
    distances: Dict[int, Tuple[float, int]]
) -> Tuple[int, float, float, int, float]:
    """
    Return angular node count, total angular depth, mean angular depth,
    maximum hop count and NAIN.

    NAIN follows the commonly used Hillier-Yang-Turner normalisation:
        node_count^1.2 / (angular_total_depth + 2)
    """
    vals = [(float(angle), int(hops)) for angle, hops in distances.values() if hops > 0]
    if not vals:
        return 0, 0.0, 0.0, 0, 0.0
    node_count = len(vals)
    total_depth = sum(v[0] for v in vals)
    mean_depth = total_depth / node_count
    max_hops = max(v[1] for v in vals)
    nain = (node_count ** 1.2) / (total_depth + 2.0)
    return node_count, total_depth, mean_depth, max_hops, nain


def _nach(choice: float, angular_total_depth: float) -> float:
    """Normalised angular choice: log(choice+1) / log(total angular depth+3)."""
    denominator = math.log(angular_total_depth + 3.0)
    if denominator <= EPS:
        return 0.0
    return math.log(max(0.0, choice) + 1.0) / denominator


# ---------------------------------------------------------------------------
# Choice / betweenness
# ---------------------------------------------------------------------------

def _brandes_topological(
    graph: Dict[int, List[Edge]],
    radius: Optional[int] = None,
) -> Dict[int, float]:
    nodes = list(graph.keys())
    cb = {v: 0.0 for v in nodes}

    for s in nodes:
        stack: List[int] = []
        pred: Dict[int, List[int]] = {w: [] for w in nodes}
        sigma = {w: 0.0 for w in nodes}
        sigma[s] = 1.0

        dist = {s: 0}
        queue = deque([s])

        while queue:
            v = queue.popleft()
            stack.append(v)

            if radius is not None and dist[v] >= radius:
                continue

            for edge in graph[v]:
                w = edge.neighbor
                nd = dist[v] + 1

                if radius is not None and nd > radius:
                    continue

                if w not in dist:
                    dist[w] = nd
                    queue.append(w)

                if dist.get(w) == nd:
                    sigma[w] += sigma[v]
                    pred[w].append(v)

        delta = {w: 0.0 for w in nodes}
        while stack:
            w = stack.pop()
            if sigma[w] > 0:
                coeff = (1.0 + delta[w]) / sigma[w]
                for v in pred[w]:
                    delta[v] += sigma[v] * coeff
            if w != s:
                cb[w] += delta[w]

    # Undirected graph: each pair is encountered from both endpoints.
    for v in cb:
        cb[v] *= 0.5

    return cb


def _brandes_angular(
    graph: Dict[int, List[Edge]],
    angular_radius: Optional[float] = None,
) -> Dict[int, float]:
    nodes = list(graph.keys())
    cb = {v: 0.0 for v in nodes}

    for s in nodes:
        stack: List[int] = []
        pred: Dict[int, List[int]] = {w: [] for w in nodes}
        sigma = {w: 0.0 for w in nodes}
        sigma[s] = 1.0

        dist: Dict[int, Tuple[float, int]] = {s: (0.0, 0)}
        settled: Set[int] = set()
        heap: List[Tuple[float, int, int]] = [(0.0, 0, s)]

        while heap:
            angle_d, hops, v = heapq.heappop(heap)
            dv = (angle_d, hops)

            if v in settled or dist.get(v) != dv:
                continue

            if angular_radius is not None and angle_d > angular_radius + EPS:
                continue

            settled.add(v)
            stack.append(v)

            for edge in graph[v]:
                w = edge.neighbor
                nd = (angle_d + edge.angle, hops + 1)

                if angular_radius is not None and nd[0] > angular_radius + EPS:
                    continue

                old = dist.get(w)

                if old is None or nd < old:
                    dist[w] = nd
                    heapq.heappush(heap, (nd[0], nd[1], w))
                    sigma[w] = sigma[v]
                    pred[w] = [v]

                elif nd == old:
                    sigma[w] += sigma[v]
                    pred[w].append(v)

        delta = {w: 0.0 for w in nodes}
        while stack:
            w = stack.pop()
            if sigma[w] > 0:
                coeff = (1.0 + delta[w]) / sigma[w]
                for v in pred[w]:
                    delta[v] += sigma[v] * coeff
            if w != s:
                cb[w] += delta[w]

    for v in cb:
        cb[v] *= 0.5

    return cb


def _connected_components(graph: Dict[int, List[Edge]]) -> Tuple[Dict[int, int], Dict[int, int]]:
    """Return node->component id and component id->size."""
    comp_of: Dict[int, int] = {}
    sizes: Dict[int, int] = {}
    cid = 0
    for start in graph:
        if start in comp_of:
            continue
        queue = deque([start])
        comp_of[start] = cid
        size = 0
        while queue:
            v = queue.popleft()
            size += 1
            for edge in graph[v]:
                w = edge.neighbor
                if w not in comp_of:
                    comp_of[w] = cid
                    queue.append(w)
        sizes[cid] = size
        cid += 1
    return comp_of, sizes


def _normalize_choice_by_component(
    choice: Dict[int, float], comp_of: Dict[int, int], comp_sizes: Dict[int, int]
) -> Dict[int, float]:
    """Standard undirected betweenness normalization within each connected component."""
    out: Dict[int, float] = {}
    for node, raw in choice.items():
        n = comp_sizes.get(comp_of.get(node, -1), 0)
        if n <= 2:
            out[node] = 0.0
        else:
            out[node] = raw * (2.0 / ((n - 1.0) * (n - 2.0)))
    return out


# ---------------------------------------------------------------------------
# Main analysis
# ---------------------------------------------------------------------------

def run_space_syntax(
    layer: QgsVectorLayer,
    topological_radius: int = 3,
    angular_radius: float = 90.0,
    snap_tolerance: float = 0.01,
    min_segment_length: float = 1.0e-6,
    feedback=None,
    global_integration: bool = True,
    local_integration: bool = True,
    global_choice: bool = False,
    local_choice: bool = False,
    angular_analysis: bool = False,
    already_segmented: bool = False,
    consolidate_raw_segments: bool = True,
) -> EngineResult:
    """Run selected Space Syntax measures without calculating unrequested metrics."""
    if topological_radius < 2 or topological_radius > 50:
        raise ValueError("Local topological radius must be between R2 and R50. Use Global Integration/Choice (Rn) for network-wide analysis.")
    if angular_radius < 0:
        raise ValueError("Angular radius cannot be negative.")
    if snap_tolerance < 0:
        raise ValueError("Snap tolerance cannot be negative.")
    if not any((global_integration, local_integration, global_choice, local_choice, angular_analysis)):
        raise ValueError("Select at least one integration, choice, or angular measure.")

    if feedback:
        feedback.pushInfo(
            "Network preparation: " +
            ("already segmented - intersection splitting skipped" if already_segmented
             else "street lines - detecting and splitting point intersections")
        )

    if already_segmented:
        segments, warnings = build_existing_segments(
            layer=layer, min_segment_length=min_segment_length, feedback=feedback)
    else:
        segments, warnings = build_segments(
            layer=layer, snap_tolerance=snap_tolerance,
            min_segment_length=min_segment_length, feedback=feedback)
        if consolidate_raw_segments:
            before = len(segments)
            segments, merged_count = consolidate_fragmented_segments(
                segments, snap_tolerance=snap_tolerance, feedback=feedback)
            if feedback:
                feedback.pushInfo(
                    f"Raw-line consolidation: {merged_count} artificial intermediate break(s) merged; "
                    f"analytical segments {before} -> {len(segments)}.")
            if merged_count:
                warnings.append(
                    f"Raw street fragmentation normalized: {merged_count} degree-2 intermediate break(s) "
                    "were merged between true junctions before Space Syntax calculation.")

    if feedback and feedback.isCanceled():
        raise RuntimeError("Analysis canceled by user.")

    graph = build_segment_graph(segments, snap_tolerance=snap_tolerance)
    comp_of, comp_sizes = _connected_components(graph)
    if feedback:
        feedback.pushInfo(
            f"Analytical graph: {len(segments)} segment(s); "
            f"{sum(len(v) for v in graph.values()) // 2} adjacency edge(s); "
            f"{len(comp_sizes)} connected component(s).")
        feedback.setProgress(25.0)

    choice_t_g = {sid: 0.0 for sid in graph}
    choice_t_l = {sid: 0.0 for sid in graph}
    choice_a_g = {sid: 0.0 for sid in graph}
    choice_a_l = {sid: 0.0 for sid in graph}

    selected_choice_jobs = []
    if global_choice:
        selected_choice_jobs.append("global topological choice")
    if local_choice:
        selected_choice_jobs.append(f"local topological choice R{topological_radius}")
    if angular_analysis and global_choice:
        selected_choice_jobs.append("global angular choice")
    if angular_analysis and local_choice:
        selected_choice_jobs.append(f"local angular choice <= {angular_radius:g} degrees")

    choice_step = 20.0 / max(1, len(selected_choice_jobs))
    choice_progress = 25.0

    if global_choice:
        if feedback: feedback.pushInfo("Calculating global topological choice...")
        choice_t_g = _brandes_topological(graph, radius=None)
        choice_progress += choice_step
        if feedback: feedback.setProgress(choice_progress)
        if feedback and feedback.isCanceled(): raise RuntimeError("Analysis canceled by user.")

    if local_choice:
        if feedback: feedback.pushInfo(f"Calculating local topological choice R{topological_radius}...")
        choice_t_l = _brandes_topological(graph, radius=topological_radius)
        choice_progress += choice_step
        if feedback: feedback.setProgress(choice_progress)
        if feedback and feedback.isCanceled(): raise RuntimeError("Analysis canceled by user.")

    if angular_analysis and global_choice:
        if feedback: feedback.pushInfo("Calculating global angular choice...")
        choice_a_g = _brandes_angular(graph, angular_radius=None)
        choice_progress += choice_step
        if feedback: feedback.setProgress(choice_progress)
        if feedback and feedback.isCanceled(): raise RuntimeError("Analysis canceled by user.")

    if angular_analysis and local_choice:
        if feedback: feedback.pushInfo(f"Calculating local angular choice <= {angular_radius:g} degrees...")
        choice_a_l = _brandes_angular(graph, angular_radius=angular_radius)
        choice_progress += choice_step
        if feedback: feedback.setProgress(choice_progress)
        if feedback and feedback.isCanceled(): raise RuntimeError("Analysis canceled by user.")

    nch_t_g = _normalize_choice_by_component(choice_t_g, comp_of, comp_sizes) if global_choice else {sid: 0.0 for sid in graph}

    metrics: Dict[int, Dict[str, object]] = {}
    total = max(1, len(segments))
    undefined_local_integration = 0
    shallow_local_roots = 0

    for pos, seg in enumerate(segments):
        if feedback and feedback.isCanceled():
            raise RuntimeError("Analysis canceled by user.")
        sid = seg.seg_id
        cid = comp_of.get(sid, -1)
        csize = comp_sizes.get(cid, 1)

        row = {
            "SEG_ID": int(sid), "SRC_FID": int(seg.source_fid),
            "SRC_PART": int(seg.source_part), "LENGTH": float(seg.length),
            "CONN": int(len(graph[sid])), "COMP_ID": int(cid), "COMP_N": int(csize),
            "RADIUS": int(topological_radius),

            "N_RN": 0, "TD_RN": 0.0, "MD_RN": 0.0,
            "RA_RN": None, "RRA_RN": None, "INT_RN": None,
            "N_R": 0, "TD_R": 0.0, "MD_R": 0.0, "MAXD_R": 0,
            "RA_R": None, "RRA_R": None, "INT_R": None,

            "CHOICE_RN": float(choice_t_g.get(sid, 0.0)),
            "NCHOICE_RN": float(nch_t_g.get(sid, 0.0)),
            "CHOICE_R": float(choice_t_l.get(sid, 0.0)),

            "ANG_N_RN": 0, "ANG_TD_RN": 0.0, "ANG_MD_RN": 0.0,
            "NAIN_RN": 0.0, "ACH_RN": float(choice_a_g.get(sid, 0.0)), "NACH_RN": 0.0,
            "ANG_N_R": 0, "ANG_TD_R": 0.0, "ANG_MD_R": 0.0,
            "NAIN_R": 0.0, "ACH_R": float(choice_a_l.get(sid, 0.0)), "NACH_R": 0.0,
        }

        if global_integration:
            topo_g = _bfs_distances(graph, sid, radius=None)
            n, td, md, maxd, ra, rra, integ = _hillier_integration(topo_g)
            row.update({"N_RN": n, "TD_RN": td, "MD_RN": md,
                        "RA_RN": ra, "RRA_RN": rra, "INT_RN": integ})

        if local_integration:
            topo_l = _bfs_distances(graph, sid, radius=topological_radius)
            n, td, md, maxd, ra, rra, integ = _hillier_integration(topo_l)
            row.update({"N_R": n, "TD_R": td, "MD_R": md, "MAXD_R": maxd,
                        "RA_R": ra, "RRA_R": rra, "INT_R": integ})
            if maxd <= 1 and n > 0:
                shallow_local_roots += 1
            if integ is None:
                undefined_local_integration += 1

        # Angular statistics are computed whenever angular analysis is selected,
        # independently of whether topological integration is selected.
        if angular_analysis:
            ang_g = _angular_dijkstra(graph, sid, angular_radius=None)
            an, atd, amd, _, nain = _angular_statistics(ang_g)
            row.update({"ANG_N_RN": an, "ANG_TD_RN": atd,
                        "ANG_MD_RN": amd, "NAIN_RN": nain})
            if global_choice:
                row["NACH_RN"] = _nach(float(row["ACH_RN"]), atd)

            ang_l = _angular_dijkstra(graph, sid, angular_radius=angular_radius)
            an, atd, amd, _, nain = _angular_statistics(ang_l)
            row.update({"ANG_N_R": an, "ANG_TD_R": atd,
                        "ANG_MD_R": amd, "NAIN_R": nain})
            if local_choice:
                row["NACH_R"] = _nach(float(row["ACH_R"]), atd)

        metrics[sid] = row
        if feedback:
            feedback.setProgress(45.0 + 55.0 * (pos + 1) / total)

    if local_integration and undefined_local_integration:
        warnings.append(
            f"Local integration is undefined for {undefined_local_integration} segment(s) "
            "because their reachable local neighbourhood has insufficient depth or RA=0. "
            "These values are written as NULL rather than incorrectly as zero."
        )
    if local_integration and shallow_local_roots > len(segments) * 0.25:
        warnings.append(
            f"{shallow_local_roots} segment(s) reach only depth 1 within R{topological_radius}. "
            "This suggests strong network fragmentation or endpoint/junction connectivity issues; "
            "review COMP_ID, COMP_N, N_R and MAXD_R before interpreting local integration."
        )
    if len(comp_sizes) > 1:
        largest = max(comp_sizes.values()) if comp_sizes else 0
        warnings.append(
            f"The analytical graph contains {len(comp_sizes)} connected components "
            f"(largest={largest} segments). Global Rn measures are computed within each "
            "reachable component; compare components cautiously."
        )

    isolated = sum(1 for sid in graph if not graph[sid])
    if isolated:
        warnings.append(
            f"{isolated} analytical segment(s) are isolated. "
            "Check network gaps, grade-separated crossings, or snap tolerance.")
    return EngineResult(segments=segments, metrics=metrics, warnings=warnings, graph=graph)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def validate_result(result: EngineResult) -> List[str]:
    """Runs structural consistency checks on an EngineResult."""
    issues: List[str] = []

    segment_ids = [s.seg_id for s in result.segments]
    if len(segment_ids) != len(set(segment_ids)):
        issues.append("Duplicate analytical SEG_ID values detected.")

    if set(segment_ids) != set(result.graph.keys()):
        issues.append("Segment IDs and graph node IDs do not match.")

    if set(segment_ids) != set(result.metrics.keys()):
        issues.append("Segment IDs and metric IDs do not match.")

    for a, edges in result.graph.items():
        for edge in edges:
            if not (0.0 <= edge.angle <= 180.0):
                issues.append(
                    f"Invalid angular cost {edge.angle} on edge {a}->{edge.neighbor}."
                )

            reverse = [
                e for e in result.graph.get(edge.neighbor, [])
                if e.neighbor == a and abs(e.angle - edge.angle) <= EPS
            ]
            if not reverse:
                issues.append(
                    f"Graph edge {a}->{edge.neighbor} has no symmetric reverse edge."
                )

    return issues
