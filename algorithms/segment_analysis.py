# -*- coding: utf-8 -*-
import math

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsFeature, QgsFeatureSink, QgsField, QgsFields, QgsProcessing,
    QgsProcessingAlgorithm, QgsProcessingException,
    QgsProcessingParameterBoolean, QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber, QgsProcessingParameterVectorLayer,
    QgsWkbTypes,
)
from ..core.spacesyntax_engine import run_space_syntax, validate_result


def _quantile(sorted_values, p):
    if not sorted_values:
        return None
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    pos = (len(sorted_values) - 1) * p
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return float(sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac)


def _valid_metric_values(result, field_name):
    values = []
    for row in result.metrics.values():
        value = row.get(field_name)
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return sorted(values)


def _class_breaks(values):
    if not values:
        return None
    if abs(values[-1] - values[0]) <= 1.0e-12:
        return {"flat": True, "median": values[0]}
    return {
        "flat": False,
        "q20": _quantile(values, 0.20),
        "q40": _quantile(values, 0.40),
        "q60": _quantile(values, 0.60),
        "q80": _quantile(values, 0.80),
        "median": _quantile(values, 0.50),
    }


def _access_class(value, breaks):
    if value is None or breaks is None:
        return "Undefined"
    try:
        value = float(value)
    except (TypeError, ValueError):
        return "Undefined"
    if not math.isfinite(value):
        return "Undefined"
    if breaks.get("flat"):
        return "Moderate"
    if value <= breaks["q20"]:
        return "Very Low"
    if value <= breaks["q40"]:
        return "Low"
    if value <= breaks["q60"]:
        return "Moderate"
    if value <= breaks["q80"]:
        return "High"
    return "Very High"


def _percentile_rank(sorted_values, value):
    """Return a mid-rank empirical percentile in [0, 1]. Ties share the same rank."""
    if not sorted_values or value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    import bisect
    lo = bisect.bisect_left(sorted_values, value)
    hi = bisect.bisect_right(sorted_values, value)
    n = len(sorted_values)
    if n == 1:
        return 0.5
    # Mid-rank mapped so the sample endpoints approach 0 and 1.
    mid_index = (lo + hi - 1) / 2.0
    return max(0.0, min(1.0, mid_index / (n - 1.0)))


def _apply_composite_ucai(result):
    """Continuous UCAI composite using equal-weight percentile ranks.

    UCAI_COMP = 1 + 4 * mean(P(INT_RN), P(INT_R), P(CHOICE_RN)).
    This is network-relative and bounded to [1, 5]. It is a
    configurational index, not a validated measure of observed accessibility.
    """
    fields = ("INT_RN", "INT_R", "CHOICE_RN")
    distributions = {name: _valid_metric_values(result, name) for name in fields}
    if any(not distributions[name] for name in fields):
        raise ValueError("Composite UCAI requires Global Integration, Local Integration and Global Choice.")
    for row in result.metrics.values():
        pg = _percentile_rank(distributions["INT_RN"], row.get("INT_RN"))
        pl = _percentile_rank(distributions["INT_R"], row.get("INT_R"))
        pc = _percentile_rank(distributions["CHOICE_RN"], row.get("CHOICE_RN"))
        row["P_GINT"], row["P_LINT"], row["P_GCHO"] = pg, pl, pc
        if None in (pg, pl, pc):
            row["UCAI_COMP"] = None
            row["UC_CLASS"] = "Undefined"
            continue
        score = 1.0 + 4.0 * ((pg + pl + pc) / 3.0)
        score = max(1.0, min(5.0, score))
        row["UCAI_COMP"] = score
        if score < 1.8: label = "Very Low"
        elif score < 2.6: label = "Low"
        elif score < 3.4: label = "Moderate"
        elif score < 4.2: label = "High"
        else: label = "Very High"
        row["UC_CLASS"] = label


def _apply_configurational_interpretation(result, global_integration, local_integration):
    """Add planning-oriented, network-relative interpretation fields without changing Integration values."""
    g_breaks = _class_breaks(_valid_metric_values(result, "INT_RN")) if global_integration else None
    l_breaks = _class_breaks(_valid_metric_values(result, "INT_R")) if local_integration else None

    for row in result.metrics.values():
        if global_integration:
            row["G_CLASS"] = _access_class(row.get("INT_RN"), g_breaks)
        if local_integration:
            row["L_CLASS"] = _access_class(row.get("INT_R"), l_breaks)

        if global_integration and local_integration:
            gv = row.get("INT_RN")
            lv = row.get("INT_R")
            try:
                gv = float(gv)
                lv = float(lv)
                valid = math.isfinite(gv) and math.isfinite(lv)
            except (TypeError, ValueError):
                valid = False

            if not valid:
                row["LG_CLASS"] = "Undefined"
                row["LG_LABEL"] = "Undefined"
            elif g_breaks is None or l_breaks is None or g_breaks.get("flat") or l_breaks.get("flat"):
                row["LG_CLASS"] = "Undiff."
                row["LG_LABEL"] = "No variation"
            else:
                g_high = gv >= g_breaks["median"]
                l_high = lv >= l_breaks["median"]
                if g_high and l_high:
                    row["LG_CLASS"] = "High-High"
                    row["LG_LABEL"] = "Integrated Core"
                elif (not g_high) and l_high:
                    row["LG_CLASS"] = "Low-High"
                    row["LG_LABEL"] = "Local Pocket"
                elif g_high and (not l_high):
                    row["LG_CLASS"] = "High-Low"
                    row["LG_LABEL"] = "Strategic Corridor"
                else:
                    row["LG_CLASS"] = "Low-Low"
                    row["LG_LABEL"] = "Segregated Area"


class SegmentAnalysisAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    GLOBAL_INT = "GLOBAL_INT"
    LOCAL_INT = "LOCAL_INT"
    CONFIG_INTERP = "CONFIG_INTERP"
    COMPOSITE_UCAI = "COMPOSITE_UCAI"
    ALREADY_SEGMENTED = "ALREADY_SEGMENTED"
    CONSOLIDATE_RAW = "CONSOLIDATE_RAW"
    TOPO_RADIUS = "TOPO_RADIUS"
    SNAP_TOL = "SNAP_TOL"
    MIN_SEG_LEN = "MIN_SEG_LEN"
    OUTPUT = "OUTPUT"

    METRIC_FIELDS = [
        ("SEG_ID", QVariant.Int), ("SRC_FID", QVariant.LongLong),
        ("SRC_PART", QVariant.Int), ("LENGTH", QVariant.Double), ("CONN", QVariant.Int),
        ("COMP_ID", QVariant.Int), ("COMP_N", QVariant.Int), ("RADIUS", QVariant.Int),

        ("N_RN", QVariant.Int), ("TD_RN", QVariant.Double), ("MD_RN", QVariant.Double),
        ("RA_RN", QVariant.Double), ("RRA_RN", QVariant.Double), ("INT_RN", QVariant.Double),
        ("N_R", QVariant.Int), ("TD_R", QVariant.Double), ("MD_R", QVariant.Double),
        ("MAXD_R", QVariant.Int), ("RA_R", QVariant.Double), ("RRA_R", QVariant.Double),
        ("INT_R", QVariant.Double),

        ("G_CLASS", QVariant.String), ("L_CLASS", QVariant.String),
        ("LG_CLASS", QVariant.String), ("LG_LABEL", QVariant.String),
        ("CHOICE_RN", QVariant.Double), ("P_GINT", QVariant.Double),
        ("P_LINT", QVariant.Double), ("P_GCHO", QVariant.Double),
        ("UCAI_COMP", QVariant.Double), ("UC_CLASS", QVariant.String),
    ]

    def name(self): return "segment_analysis"
    def displayName(self): return "UCAI Integration Analysis"
    def group(self): return "Integration analysis"
    def groupId(self): return "network_analysis"
    def createInstance(self): return SegmentAnalysisAlgorithm()

    def shortHelpString(self):
        return (
            "UCAI street-level Integration analysis for projected ESRI Shapefile line networks, using the depthmapX-free Space Syntax engine. "
            "Calculate Global Integration (Rn), Local Integration at a user-selected topological radius R2–R50, "
            "or both. An optional Configurational Accessibility Interpretation classifies Integration values "
            "relative to the analysed network and, when both scales are present, adds a Local–Global type. "
            "The interpretation is configurational and must not be treated as a direct measurement of observed walkability. "
            "Raw street linework can optionally be consolidated so arbitrary degree-2 feature breaks between true junctions do not create extra topological steps. "
            "'Already segmented' skips intersection splitting and consolidation and should only be used for a prepared segment map."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(QgsProcessingParameterVectorLayer(
            self.INPUT, "Projected ESRI line shapefile", [QgsProcessing.TypeVectorLine]))
        self.addParameter(QgsProcessingParameterBoolean(
            self.GLOBAL_INT, "Global Integration (Rn)", defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.LOCAL_INT, "Local Integration", defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CONFIG_INTERP, "Configurational Accessibility Interpretation", defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.COMPOSITE_UCAI, "UCAI Composite Index", defaultValue=False))
        self.addParameter(QgsProcessingParameterBoolean(
            self.CONSOLIDATE_RAW, "Consolidate fragmented raw linework between true junctions",
            defaultValue=True))
        self.addParameter(QgsProcessingParameterBoolean(
            self.ALREADY_SEGMENTED, "Input network is already segmented (skip intersection splitting)",
            defaultValue=False))
        self.addParameter(QgsProcessingParameterNumber(
            self.TOPO_RADIUS, "Local topological radius (segment steps)",
            type=QgsProcessingParameterNumber.Integer, minValue=2, maxValue=50, defaultValue=3))
        self.addParameter(QgsProcessingParameterNumber(
            self.SNAP_TOL, "Endpoint snap tolerance (layer units)",
            type=QgsProcessingParameterNumber.Double, minValue=0.0, defaultValue=0.01))
        self.addParameter(QgsProcessingParameterNumber(
            self.MIN_SEG_LEN, "Minimum analytical segment length (layer units)",
            type=QgsProcessingParameterNumber.Double, minValue=0.0, defaultValue=0.000001))
        self.addParameter(QgsProcessingParameterFeatureSink(
            self.OUTPUT, "Space Syntax analytical segments", type=QgsProcessing.TypeVectorLine))

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        if layer is None or not layer.isValid():
            raise QgsProcessingException("A valid input line layer is required.")
        source = layer.source().split("|")[0]
        if not source.lower().endswith(".shp"):
            raise QgsProcessingException("Only ESRI Shapefile (.shp) input is accepted.")
        if not layer.crs().isValid() or layer.crs().isGeographic():
            raise QgsProcessingException("The input shapefile must use a valid projected CRS.")

        gi = self.parameterAsBoolean(parameters, self.GLOBAL_INT, context)
        li = self.parameterAsBoolean(parameters, self.LOCAL_INT, context)
        interpret = self.parameterAsBoolean(parameters, self.CONFIG_INTERP, context)
        composite = self.parameterAsBoolean(parameters, self.COMPOSITE_UCAI, context)
        already_segmented = self.parameterAsBoolean(parameters, self.ALREADY_SEGMENTED, context)
        consolidate_raw = self.parameterAsBoolean(parameters, self.CONSOLIDATE_RAW, context)
        if composite:
            gi = True
            li = True
        if not any((gi, li)):
            raise QgsProcessingException("Select Global Integration, Local Integration, or both.")

        topo_radius = self.parameterAsInt(parameters, self.TOPO_RADIUS, context)
        snap_tol = self.parameterAsDouble(parameters, self.SNAP_TOL, context)
        min_seg_len = self.parameterAsDouble(parameters, self.MIN_SEG_LEN, context)

        feedback.pushInfo(f"Input: {source}")
        feedback.pushInfo(f"Features: {layer.featureCount()} | CRS: {layer.crs().authid()}")
        feedback.pushInfo("Selected measures: " + ", ".join([
            n for enabled, n in [
                (gi, "Global Integration Rn"), (li, f"Local Integration R{topo_radius}"),
                (interpret, "Configurational Accessibility Interpretation"),
                (composite, "UCAI Composite Index") ]
            if enabled]))

        try:
            # Keep the proven v0.6.1 analytical engine unchanged; advanced metrics are simply not exposed or requested.
            result = run_space_syntax(
                layer=layer, topological_radius=topo_radius, angular_radius=90.0,
                snap_tolerance=snap_tol, min_segment_length=min_seg_len, feedback=feedback,
                global_integration=gi, local_integration=li, global_choice=composite,
                local_choice=False, angular_analysis=False, already_segmented=already_segmented,
                consolidate_raw_segments=(consolidate_raw and not already_segmented))
        except Exception as exc:
            raise QgsProcessingException(str(exc)) from exc

        issues = validate_result(result)
        if issues:
            raise QgsProcessingException("Internal result validation failed: " + " | ".join(issues[:10]))
        for warning in result.warnings:
            feedback.pushWarning(warning)

        if composite:
            _apply_composite_ucai(result)
            feedback.pushInfo("UCAI Composite added: UCAI_COMP = 1 + 4 × mean percentile rank of INT_RN, INT_R and CHOICE_RN; equal weights; bounded 1–5.")
            feedback.pushWarning("UCAI Composite is network-relative and has not yet been externally validated as a measure of observed accessibility or walkability.")

        if interpret:
            _apply_configurational_interpretation(result, gi, li)
            if gi and li:
                feedback.pushInfo(
                    "Configurational interpretation added: G_CLASS and L_CLASS use network-relative quintile "
                    "classes; LG_CLASS/LG_LABEL use median-based Local–Global types."
                )
            else:
                feedback.pushInfo(
                    "Configurational interpretation added: the selected Integration measure is classified "
                    "relative to this network using quintile classes."
                )
            feedback.pushInfo(
                "Interpretation describes configurational accessibility and is not a direct measure of observed walkability."
            )

        fields = QgsFields()
        used = set()
        for field in layer.fields():
            fields.append(field); used.add(field.name().upper())

        common = {"SEG_ID", "SRC_FID", "SRC_PART", "LENGTH", "CONN", "COMP_ID", "COMP_N", "RADIUS"}
        allowed = set(common)
        if gi: allowed.update({"N_RN", "TD_RN", "MD_RN", "RA_RN", "RRA_RN", "INT_RN"})
        if li: allowed.update({"N_R", "TD_R", "MD_R", "MAXD_R", "RA_R", "RRA_R", "INT_R"})
        if interpret and gi: allowed.add("G_CLASS")
        if interpret and li: allowed.add("L_CLASS")
        if interpret and gi and li: allowed.update({"LG_CLASS", "LG_LABEL"})
        if composite: allowed.update({"CHOICE_RN", "P_GINT", "P_LINT", "P_GCHO", "UCAI_COMP", "UC_CLASS"})

        output_metric_names = []
        for name, variant_type in self.METRIC_FIELDS:
            if name not in allowed: continue
            out_name = name; suffix = 1
            while out_name.upper() in used:
                out_name = f"SS_{name}" if suffix == 1 else f"SS{suffix}_{name}"
                suffix += 1
            fields.append(QgsField(out_name, variant_type)); used.add(out_name.upper())
            output_metric_names.append((name, out_name, variant_type))

        sink, dest_id = self.parameterAsSink(
            parameters, self.OUTPUT, context, fields, QgsWkbTypes.LineString, layer.sourceCrs())
        if sink is None:
            raise QgsProcessingException("Could not create output layer.")

        source_attributes = {int(f.id()): f.attributes() for f in layer.getFeatures()}
        feedback.pushInfo(f"Writing {len(result.segments)} analytical segment(s)…")
        total = max(1, len(result.segments))
        integer_metrics = {"SEG_ID","SRC_FID","SRC_PART","CONN","COMP_ID","COMP_N","RADIUS","N_RN","N_R","MAXD_R"}
        for i, segment in enumerate(result.segments):
            if feedback.isCanceled():
                feedback.pushInfo("Output writing canceled."); break
            row = result.metrics[segment.seg_id]
            feature = QgsFeature(fields); feature.setGeometry(segment.geometry)
            attrs = list(source_attributes.get(segment.source_fid, []))
            for metric_name, _, variant_type in output_metric_names:
                value = row.get(metric_name)
                if value is None:
                    attrs.append(None)
                elif variant_type == QVariant.String:
                    attrs.append(str(value))
                elif metric_name in integer_metrics:
                    attrs.append(int(round(value)))
                else:
                    attrs.append(float(value))
            feature.setAttributes(attrs); sink.addFeature(feature, QgsFeatureSink.FastInsert)
            feedback.setProgress(100.0 * (i + 1) / total)
        feedback.pushInfo("UCAI Integration analysis completed.")
        return {self.OUTPUT: dest_id}
