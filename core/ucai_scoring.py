# -*- coding: utf-8 -*-
"""Planner-facing UCAI profile and two-city comparison helpers.

This module never changes INT_R or INT_RN. Single-city profiles summarize the
within-network accessibility classes already produced by the Integration
algorithm. Two-city comparison derives one shared, length-weighted classification
from both cities at the same topological radius, with each city contributing
50% of the reference weight.
"""

import math
from qgis.PyQt.QtCore import QVariant
from qgis.core import QgsField

CLASS_ORDER = ["Very Low", "Low", "Moderate", "High", "Very High"]
CLASS_SCORE = {name: i + 1 for i, name in enumerate(CLASS_ORDER)}


def _rating(score):
    if score < 1.8:
        return "Very Low"
    if score < 2.6:
        return "Low"
    if score < 3.4:
        return "Moderate"
    if score < 4.2:
        return "High"
    return "Very High"


def _field_name(layer, preferred):
    available = {field.name().upper(): field.name() for field in layer.fields()}
    if preferred.upper() in available:
        return available[preferred.upper()]
    for field in layer.fields():
        if field.name().upper().endswith(preferred.upper()):
            return field.name()
    return None


def _finalize_profile(lengths):
    total = sum(lengths.values())
    if total <= 0:
        return None
    percentages = {name: 100.0 * lengths[name] / total for name in CLASS_ORDER}
    score = sum(lengths[name] * CLASS_SCORE[name] for name in CLASS_ORDER) / total
    return {
        "total_length": total,
        "percentages": percentages,
        "score": score,
        "rating": _rating(score),
        "high_plus": percentages["High"] + percentages["Very High"],
        "low_plus": percentages["Very Low"] + percentages["Low"],
    }


def within_network_profiles(layer):
    """Summarize existing L_CLASS/G_CLASS fields by network length.

    These are descriptive, within-network profiles. Their 1–5 profile scores are
    not cross-city benchmark scores because each study area was classified from
    its own Integration distribution.
    """
    profiles = {}
    for scope, field_base in (("LOCAL", "L_CLASS"), ("GLOBAL", "G_CLASS")):
        field = _field_name(layer, field_base)
        if not field:
            continue
        lengths = {name: 0.0 for name in CLASS_ORDER}
        for feature in layer.getFeatures():
            label = str(feature[field]) if feature[field] is not None else ""
            if label not in lengths:
                continue
            geometry = feature.geometry()
            if geometry is None or geometry.isEmpty():
                continue
            length = float(geometry.length())
            if math.isfinite(length) and length > 0:
                lengths[label] += length
        summary = _finalize_profile(lengths)
        if summary is not None:
            profiles[scope] = summary
    return profiles


def _layer_observations(layer):
    value_field = _field_name(layer, "INT_R")
    radius_field = _field_name(layer, "RADIUS")
    if not value_field:
        raise ValueError(f"Layer '{layer.name()}' has no Local Integration field (INT_R).")
    if not radius_field:
        raise ValueError(f"Layer '{layer.name()}' has no RADIUS field; its local analysis radius cannot be verified.")

    radii = set()
    observations = []
    total_length = 0.0
    for feature in layer.getFeatures():
        try:
            radius = int(feature[radius_field])
            value = float(feature[value_field])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value):
            continue
        geometry = feature.geometry()
        if geometry is None or geometry.isEmpty():
            continue
        length = float(geometry.length())
        if not math.isfinite(length) or length <= 0:
            continue
        radii.add(radius)
        observations.append((feature.id(), value, length))
        total_length += length

    if not observations:
        raise ValueError(f"Layer '{layer.name()}' contains no usable INT_R values with positive line length.")
    if len(radii) != 1:
        raise ValueError(f"Layer '{layer.name()}' does not contain one consistent local topological radius.")
    return next(iter(radii)), observations, total_length


def _weighted_quantile(items, probability):
    """Weighted quantile for (value, weight) pairs using cumulative weight."""
    if not items:
        raise ValueError("Cannot calculate a shared classification from an empty comparison set.")
    items = sorted((float(v), float(w)) for v, w in items if w > 0 and math.isfinite(v) and math.isfinite(w))
    total = sum(w for _, w in items)
    if total <= 0:
        raise ValueError("Comparison weights sum to zero.")
    target = max(0.0, min(1.0, float(probability))) * total
    cumulative = 0.0
    for value, weight in items:
        cumulative += weight
        if cumulative >= target:
            return value
    return items[-1][0]


def _classify(value, breaks):
    if value <= breaks[0]:
        return "Very Low"
    if value <= breaks[1]:
        return "Low"
    if value <= breaks[2]:
        return "Moderate"
    if value <= breaks[3]:
        return "High"
    return "Very High"


def _ensure_comparison_fields(layer):
    provider = layer.dataProvider()
    class_field = _field_name(layer, "CMP_CLASS")
    score_field = _field_name(layer, "CMP_SCORE")
    additions = []
    if not class_field:
        additions.append(QgsField("CMP_CLASS", QVariant.String, len=10))
    if not score_field:
        additions.append(QgsField("CMP_SCORE", QVariant.Int))
    if additions:
        if not provider.addAttributes(additions):
            raise RuntimeError(f"QGIS could not add comparison fields to '{layer.name()}'.")
        layer.updateFields()
    return _field_name(layer, "CMP_CLASS"), _field_name(layer, "CMP_SCORE")


def _apply_shared_classes(layer, observations, breaks):
    class_field, score_field = _ensure_comparison_fields(layer)
    class_idx = layer.fields().indexFromName(class_field)
    score_idx = layer.fields().indexFromName(score_field)
    lengths = {name: 0.0 for name in CLASS_ORDER}
    changes = {}
    for fid, value, length in observations:
        label = _classify(value, breaks)
        changes[fid] = {class_idx: label, score_idx: CLASS_SCORE[label]}
        lengths[label] += length
    if changes and not layer.dataProvider().changeAttributeValues(changes):
        raise RuntimeError(f"QGIS could not write shared comparison classes to '{layer.name()}'.")
    layer.updateFields()
    return _finalize_profile(lengths), {"class": class_field, "score": score_field}


def compare_local_layers(layer_a, layer_b):
    """Compare two UCAI result layers using one shared Local Integration scale.

    Both layers must contain INT_R calculated at exactly the same topological radius.
    Each city receives equal (50/50) weight in threshold derivation; within each city,
    observations are weighted by analysed street-network length. This avoids the larger
    city dominating the shared reference solely because it has more street length.
    """
    if layer_a is None or layer_b is None or layer_a.id() == layer_b.id():
        raise ValueError("Select two different analysed UCAI result layers for comparison.")

    radius_a, obs_a, total_a = _layer_observations(layer_a)
    radius_b, obs_b, total_b = _layer_observations(layer_b)
    if radius_a != radius_b:
        raise ValueError(
            f"Local-radius mismatch: '{layer_a.name()}' is R{radius_a}, while '{layer_b.name()}' is R{radius_b}. "
            "Direct comparison requires the same topological radius."
        )

    weighted = []
    for _, value, length in obs_a:
        weighted.append((value, 0.5 * length / total_a))
    for _, value, length in obs_b:
        weighted.append((value, 0.5 * length / total_b))

    breaks = tuple(_weighted_quantile(weighted, p) for p in (0.20, 0.40, 0.60, 0.80))
    # Degenerate distributions can create duplicate breakpoints; classification still
    # remains deterministic, but warn the caller through the returned flag.
    distinct = len(set(round(v, 12) for v in breaks)) == 4

    summary_a, fields_a = _apply_shared_classes(layer_a, obs_a, breaks)
    summary_b, fields_b = _apply_shared_classes(layer_b, obs_b, breaks)
    return {
        "radius": radius_a,
        "breaks": breaks,
        "distinct_breaks": distinct,
        "a": summary_a,
        "b": summary_b,
        "fields_a": fields_a,
        "fields_b": fields_b,
    }
