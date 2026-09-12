# -*- coding: utf-8 -*-
import os
import math
from datetime import datetime

from qgis.PyQt.QtCore import Qt, QVariant
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox,
    QComboBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QTextEdit, QVBoxLayout, QWidget,
)
from qgis.core import (
    QgsApplication, QgsClassificationEqualInterval, QgsClassificationJenks,
    QgsClassificationQuantile, QgsClassificationStandardDeviation, QgsFeature,
    QgsCategorizedSymbolRenderer, QgsGraduatedSymbolRenderer, QgsLineSymbol, QgsProcessingAlgRunnerTask,
    QgsProcessingContext, QgsProcessingFeedback, QgsProcessingUtils, QgsProject,
    QgsRendererCategory, QgsRendererRange, QgsVectorFileWriter, QgsVectorLayer, QgsWkbTypes, QgsField,
)

from .core.network_integrity import inspect_network, connector_geometry
from .core.spacesyntax_engine import build_segments, consolidate_fragmented_segments
from .core.ucai_scoring import compare_local_layers, within_network_profiles


class SpaceSyntaxDialog(QDialog):
    """Guided, responsive UCAI workflow dialog built on the Space Syntax Integration engine."""

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.layer = None
        self.gaps = []
        self.analysis_task = None
        self.analysis_context = None
        self.analysis_feedback = None
        self._last_logged_progress = -10
        self._last_ucai_summaries = {}
        self._last_ucai_fields = {}
        self.setWindowTitle("Urban Configurational Accessibility Index (UCAI)")
        self.setMinimumSize(560, 420)
        self.resize(820, 760)
        self.setSizeGripEnabled(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._build_ui()
        self._log("Urban Configurational Accessibility Index (UCAI) v0.8.0 ready.")

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(6, 6, 6, 6)

        self.scroll_area = QScrollArea(self)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.scroll_area.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        content = QWidget()
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        root = QVBoxLayout(content)

        title = QLabel("<b>Urban Configurational Accessibility Index (UCAI)</b>")
        title.setStyleSheet("font-size: 16px;")
        root.addWidget(title)

        requirements = QLabel(
            "<b>Input requirements</b><br>"
            "• ESRI Shapefile (.shp) only<br>"
            "• Line vector data only<br>"
            "• The shapefile must use a projected coordinate reference system (CRS)"
        )
        requirements.setWordWrap(True)
        root.addWidget(requirements)

        # 1. Input
        input_group = QGroupBox("1. Input shapefile")
        input_layout = QVBoxLayout(input_group)
        row = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Select an ESRI line shapefile (.shp)")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_input)
        row.addWidget(self.path_edit, 1)
        row.addWidget(browse)
        input_layout.addLayout(row)

        self.validation_label = QLabel("Input not checked.")
        self.validation_label.setWordWrap(True)
        input_layout.addWidget(self.validation_label)
        validate_btn = QPushButton("Validate Input")
        validate_btn.clicked.connect(self.validate_input)
        input_layout.addWidget(validate_btn)
        root.addWidget(input_group)

        # 2. Integrity
        integrity_group = QGroupBox("2. Network integrity — check missing line pieces")
        integrity_layout = QFormLayout(integrity_group)

        self.connection_tol = QDoubleSpinBox()
        self.connection_tol.setDecimals(6)
        self.connection_tol.setRange(0.0, 1000000.0)
        self.connection_tol.setValue(0.01)
        self.connection_tol.setToolTip("Distance below which an endpoint is treated as already connected.")

        self.gap_tol = QDoubleSpinBox()
        self.gap_tol.setDecimals(3)
        self.gap_tol.setRange(0.000001, 1000000.0)
        self.gap_tol.setValue(1.0)
        self.gap_tol.setToolTip("Maximum distance used to flag/build a possible missing connector.")
        self.connection_tol.valueChanged.connect(self._sync_gap_minimum)

        integrity_layout.addRow("Connection tolerance (map units):", self.connection_tol)
        integrity_layout.addRow("Maximum missing-gap distance (map units):", self.gap_tol)

        check_btn = QPushButton("Check Network for Missing Line Pieces")
        check_btn.clicked.connect(self.check_network)
        integrity_layout.addRow(check_btn)

        self.integrity_label = QLabel("Network has not been checked.")
        self.integrity_label.setWordWrap(True)
        integrity_layout.addRow(self.integrity_label)

        repair_btn = QPushButton("Build Missing Line Pieces → New Shapefile")
        repair_btn.clicked.connect(self.repair_network)
        integrity_layout.addRow(repair_btn)

        analytical_btn = QPushButton("Build Analytical Network from Raw Data → New Shapefile")
        analytical_btn.setToolTip(
            "Creates a clean intersection-to-intersection analytical street network from the selected raw linework. "
            "Lines are split at true intersections, then arbitrary degree-2 raw-data feature breaks between real junctions "
            "are consolidated. The original source file is never overwritten."
        )
        analytical_btn.clicked.connect(self.build_analytical_network)
        integrity_layout.addRow(analytical_btn)
        root.addWidget(integrity_group)

        # 3. Analysis
        analysis_group = QGroupBox("3. Integration analysis")
        analysis_layout = QFormLayout(analysis_group)

        self.global_int_check = QCheckBox("Global Integration (Rn)")
        self.global_int_check.setChecked(True)
        self.local_int_check = QCheckBox("Local Integration (R3)")
        self.local_int_check.setChecked(True)

        self.local_radius = QSpinBox()
        self.local_radius.setRange(2, 50)
        self.local_radius.setValue(3)
        self.local_radius.setPrefix("R")
        self.local_radius.setToolTip(
            "Local topological radius: R2–R50 segment steps. For walkability, lower radii represent "
            "more local pedestrian-scale structure. R values are not fixed metric distances."
        )
        self.local_radius.valueChanged.connect(self._update_radius_labels)

        self.composite_ucai_check = QCheckBox("UCAI Composite Index (1–5)")
        self.composite_ucai_check.setChecked(False)
        self.composite_ucai_check.setToolTip(
            "Adds a continuous network-relative 1–5 index combining equal-weight percentile ranks of "
            "Global Integration, Local Integration at the selected radius, and Global Choice. "
            "Enabling this option automatically requires Global and Local Integration. The composite is network-relative "
            "and has not yet been externally validated against observed accessibility or walkability."
        )
        self.composite_ucai_check.toggled.connect(self._composite_toggled)

        self.config_interp_check = QCheckBox("Configurational Accessibility Interpretation")
        self.config_interp_check.setChecked(False)
        self.config_interp_check.setToolTip(
            "Optional planning-oriented interpretation of Integration values. "
            "Single-scale results are classified relative to the analysed network; when both Global and Local "
            "Integration are available, a Local–Global configurational type is also added. "
            "This is not a direct measure of observed walkability."
        )

        self.consolidate_raw_check = QCheckBox(
            "Consolidate fragmented raw linework between true junctions (recommended)"
        )
        self.consolidate_raw_check.setChecked(True)
        self.consolidate_raw_check.setToolTip(
            "Merges arbitrary degree-2 line-feature breaks that occur between actual network junctions, "
            "so raw-data fragmentation does not create artificial topological steps. True T-junctions, "
            "crossroads and other branching intersections are preserved."
        )

        self.already_segmented_check = QCheckBox(
            "Input network is already segmented — skip intersection splitting"
        )
        self.already_segmented_check.setChecked(False)
        self.already_segmented_check.setToolTip(
            "Use only for a prepared segment map where each analytical segment already exists as a line feature. "
            "This can substantially reduce preprocessing time."
        )
        self.already_segmented_check.toggled.connect(
            lambda checked: self.consolidate_raw_check.setEnabled(not checked)
        )

        self.display_metric_combo = QComboBox()
        self.display_metric_combo.addItems([
            "Auto — prefer Local Integration",
            "Local Integration",
            "Global Integration",
            "Local Accessibility Class — within network",
            "Global Accessibility Class — within network",
            "UCAI Composite Index",
            "UCAI Composite Class",
        ])
        self.display_metric_combo.setToolTip(
            "Choose continuous Integration symbology or within-network accessibility classes. "
            "Two-city comparison classes are styled automatically when a comparison is run. "
            "If the selected field is unavailable, the plugin falls back to an available Integration metric."
        )

        analysis_layout.addRow(self.global_int_check)
        analysis_layout.addRow(self.local_int_check)
        analysis_layout.addRow("Local topological radius:", self.local_radius)
        analysis_layout.addRow(self.config_interp_check)
        analysis_layout.addRow(self.composite_ucai_check)

        self.walkability_note = QLabel()
        self.walkability_note.setWordWrap(True)
        self.walkability_note.setStyleSheet("color: #555; font-style: italic;")
        self.walkability_note.setToolTip(
            "Interpretive guidance only. Topological radius counts network steps and cannot be converted "
            "universally to 400 m, 800 m, or another fixed walking distance."
        )
        analysis_layout.addRow("Walkability interpretation:", self.walkability_note)

        analysis_layout.addRow("Colour result by:", self.display_metric_combo)
        analysis_layout.addRow(self.consolidate_raw_check)
        analysis_layout.addRow(self.already_segmented_check)

        button_row = QHBoxLayout()
        self.run_btn = QPushButton("Run UCAI Analysis")
        self.run_btn.clicked.connect(self.run_analysis)
        self.cancel_btn = QPushButton("Cancel Analysis")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self.cancel_analysis)
        button_row.addWidget(self.run_btn)
        button_row.addWidget(self.cancel_btn)
        analysis_layout.addRow(button_row)
        root.addWidget(analysis_group)

        # 4. City profile and comparison
        city_group = QGroupBox("4. City accessibility profile and comparison")
        city_layout = QFormLayout(city_group)

        self.city_profile_check = QCheckBox("Calculate within-network city accessibility profile")
        self.city_profile_check.setChecked(False)
        self.city_profile_check.setToolTip(
            "Summarizes the analysed network by length across Very Low–Very High accessibility classes and reports "
            "a descriptive 1–5 profile score. This single-city score is relative to that city's own distribution and "
            "is not a cross-city benchmark."
        )
        self.city_profile_check.toggled.connect(self._city_profile_toggled)
        city_layout.addRow(self.city_profile_check)

        self.city_name_edit = QLineEdit()
        self.city_name_edit.setPlaceholderText("e.g., Malmö")
        city_layout.addRow("City / study area name:", self.city_name_edit)

        profile_note = QLabel(
            "Single-city percentages are based on analysed street-network length. For a direct city-to-city comparison, "
            "use the comparison controls below so both cities receive one shared classification scale."
        )
        profile_note.setWordWrap(True)
        profile_note.setStyleSheet("color: #555; font-style: italic;")
        city_layout.addRow(profile_note)

        compare_heading = QLabel("<b>Compare two analysed UCAI Local Integration layers</b>")
        city_layout.addRow(compare_heading)

        self.compare_layer_a_combo = QComboBox()
        self.compare_layer_b_combo = QComboBox()
        city_layout.addRow("City A result layer:", self.compare_layer_a_combo)
        city_layout.addRow("City B result layer:", self.compare_layer_b_combo)

        self.compare_name_a_edit = QLineEdit()
        self.compare_name_a_edit.setPlaceholderText("Optional display name; defaults to layer name")
        self.compare_name_b_edit = QLineEdit()
        self.compare_name_b_edit.setPlaceholderText("Optional display name; defaults to layer name")
        city_layout.addRow("City A name:", self.compare_name_a_edit)
        city_layout.addRow("City B name:", self.compare_name_b_edit)

        compare_buttons = QHBoxLayout()
        refresh_compare_btn = QPushButton("Refresh Result Layers")
        refresh_compare_btn.clicked.connect(self._refresh_compare_layers)
        compare_btn = QPushButton("Compare Two Cities")
        compare_btn.clicked.connect(self._compare_two_cities)
        compare_buttons.addWidget(refresh_compare_btn)
        compare_buttons.addWidget(compare_btn)
        city_layout.addRow(compare_buttons)

        compare_note = QLabel(
            "Direct comparison requires Local Integration results calculated at the same topological radius (for example, R3 vs R3) "
            "with equivalent network preparation. UCAI derives one shared five-class scale from the two cities; each city contributes "
            "equal reference weight and streets are weighted by network length."
        )
        compare_note.setWordWrap(True)
        compare_note.setStyleSheet("color: #555; font-style: italic;")
        city_layout.addRow(compare_note)
        root.addWidget(city_group)

        # 5. Result symbology
        style_group = QGroupBox("5. Result symbology")
        style_layout = QFormLayout(style_group)

        spectrum_note = QLabel(
            "Default Space Syntax spectrum: low values = blue/cool; high values = red/warm. "
            "Class count and classification are user controlled."
        )
        spectrum_note.setWordWrap(True)
        style_layout.addRow(spectrum_note)

        self.class_count_spin = QSpinBox()
        self.class_count_spin.setRange(3, 50)
        self.class_count_spin.setValue(15)
        self.class_count_spin.setToolTip(
            "Number of legend classes. The plugin does not impose a fixed 15-class scale."
        )

        self.class_method_combo = QComboBox()
        self.class_method_combo.addItems([
            "Equal Interval",
            "Quantile",
            "Natural Breaks (Jenks)",
            "Standard Deviation",
        ])
        self.class_method_combo.setCurrentText("Equal Interval")

        self.line_width_spin = QDoubleSpinBox()
        self.line_width_spin.setDecimals(2)
        self.line_width_spin.setRange(0.10, 10.0)
        self.line_width_spin.setSingleStep(0.10)
        self.line_width_spin.setValue(1.20)
        self.line_width_spin.setSuffix(" mm")

        self.legend_precision_spin = QSpinBox()
        self.legend_precision_spin.setRange(0, 8)
        self.legend_precision_spin.setValue(2)

        self.reverse_ramp_check = QCheckBox("Reverse colour ramp")
        self.reverse_ramp_check.setChecked(False)

        self.manual_range_check = QCheckBox("Use manual minimum / maximum")
        self.manual_range_check.setChecked(False)
        self.manual_range_check.toggled.connect(self._toggle_manual_range)

        self.manual_min_spin = QDoubleSpinBox()
        self.manual_min_spin.setDecimals(6)
        self.manual_min_spin.setRange(-1.0e12, 1.0e12)
        self.manual_min_spin.setValue(0.0)
        self.manual_min_spin.setEnabled(False)

        self.manual_max_spin = QDoubleSpinBox()
        self.manual_max_spin.setDecimals(6)
        self.manual_max_spin.setRange(-1.0e12, 1.0e12)
        self.manual_max_spin.setValue(1.0)
        self.manual_max_spin.setEnabled(False)

        style_layout.addRow("Number of classes:", self.class_count_spin)
        style_layout.addRow("Classification method:", self.class_method_combo)
        style_layout.addRow("Line width:", self.line_width_spin)
        style_layout.addRow("Legend decimal places:", self.legend_precision_spin)
        style_layout.addRow(self.reverse_ramp_check)
        style_layout.addRow(self.manual_range_check)
        style_layout.addRow("Manual minimum:", self.manual_min_spin)
        style_layout.addRow("Manual maximum:", self.manual_max_spin)

        root.addWidget(style_group)

        # 6. Log
        log_group = QGroupBox("6. Analysis Log")
        log_layout = QVBoxLayout(log_group)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        log_layout.addWidget(self.progress)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(90)
        self.log_text.setPlaceholderText("Validation, network checks, analysis progress, warnings and errors appear here.")
        log_layout.addWidget(self.log_text)

        log_buttons = QHBoxLayout()
        clear_log = QPushButton("Clear Log")
        clear_log.clicked.connect(self.log_text.clear)
        log_buttons.addStretch(1)
        log_buttons.addWidget(clear_log)
        log_layout.addLayout(log_buttons)
        root.addWidget(log_group)

        note = QLabel(
            "UCAI retains the Space Syntax Integration engine for street-level analysis. By default, fragmented raw linework "
            "is consolidated through degree-2 intermediate nodes so arbitrary feature breaks between true junctions do not create "
            "artificial topological steps; real branching intersections are preserved. Within-network interpretation and city-profile "
            "summaries do not alter INT_R or INT_RN. Direct two-city comparison uses one shared scale and requires the same local "
            "topological radius and equivalent network preparation. The repair tool never overwrites the source shapefile."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #666;")
        root.addWidget(note)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.close)
        root.addWidget(close_btn, alignment=Qt.AlignRight)
        root.addStretch(1)

        self._update_radius_labels(self.local_radius.value())
        self._refresh_compare_layers()

        self.scroll_area.setWidget(content)
        outer.addWidget(self.scroll_area)

    def _update_radius_labels(self, value):
        self.local_int_check.setText(f"Local Integration (R{value})")

        if value <= 3:
            scale = "Immediate / micro-scale pedestrian context"
            use = "very local access, frontage and immediate surroundings"
        elif value <= 6:
            scale = "Neighbourhood-scale walkability"
            use = "local centres, schools, daily services and short walking trips"
        elif value <= 10:
            scale = "Extended neighbourhood walkability"
            use = "broader pedestrian accessibility and neighbourhood movement structure"
        elif value <= 20:
            scale = "District-scale accessibility"
            use = "larger urban catchments; less representative of short everyday walks"
        else:
            scale = "Sub-city / strategic accessibility"
            use = "strategic spatial structure rather than conventional pedestrian catchments"

        if hasattr(self, "walkability_note"):
            self.walkability_note.setText(
                f"R{value}: {scale} — {use}. "
                "Topological radius is not a fixed metric catchment (for example, R5 ≠ 400 m)."
            )

    def _sync_gap_minimum(self, connection_value):
        step = max(0.000001, 10 ** (-self.gap_tol.decimals()))
        minimum = float(connection_value) + step
        self.gap_tol.setMinimum(minimum)
        if self.gap_tol.value() <= connection_value:
            self.gap_tol.setValue(minimum)

    def _toggle_manual_range(self, enabled):
        self.manual_min_spin.setEnabled(enabled)
        self.manual_max_spin.setEnabled(enabled)

    @staticmethod
    def _interpolate_channel(a, b, t):
        return int(round(a + (b - a) * t))

    def _spectrum_color(self, t):
        """
        Continuous Space Syntax-style spectrum:
        blue -> cyan -> green -> yellow -> orange -> red.
        """
        t = max(0.0, min(1.0, float(t)))
        if self.reverse_ramp_check.isChecked():
            t = 1.0 - t

        stops = [
            (0.00, QColor("#2C3EDE")),
            (0.20, QColor("#22B8F0")),
            (0.40, QColor("#28C76F")),
            (0.60, QColor("#D8E219")),
            (0.80, QColor("#FF8C22")),
            (1.00, QColor("#E32620")),
        ]

        for i in range(1, len(stops)):
            p0, c0 = stops[i - 1]
            p1, c1 = stops[i]
            if t <= p1:
                local = 0.0 if p1 == p0 else (t - p0) / (p1 - p0)
                return QColor(
                    self._interpolate_channel(c0.red(), c1.red(), local),
                    self._interpolate_channel(c0.green(), c1.green(), local),
                    self._interpolate_channel(c0.blue(), c1.blue(), local),
                )
        return QColor(stops[-1][1])

    def _classification_ranges(self, values, class_count):
        method_name = self.class_method_combo.currentText()
        method_map = {
            "Equal Interval": QgsClassificationEqualInterval,
            "Quantile": QgsClassificationQuantile,
            "Natural Breaks (Jenks)": QgsClassificationJenks,
            "Standard Deviation": QgsClassificationStandardDeviation,
        }
        method_cls = method_map.get(method_name, QgsClassificationEqualInterval)
        method = method_cls()

        # QGIS 3.44 supports classification directly from a list of values.
        ranges = method.classes(values, class_count)
        return ranges, method_name

    def _log(self, message, level="INFO"):
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.append(f"[{stamp}] {level}: {message}")
        sb = self.log_text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _composite_toggled(self, checked):
        if checked:
            self.global_int_check.setChecked(True)
            self.local_int_check.setChecked(True)
            self._log("UCAI Composite enabled: Global Integration, Local Integration and Global Choice will be calculated.")

    def _city_profile_toggled(self, checked):
        if checked and not self.config_interp_check.isChecked():
            self.config_interp_check.setChecked(True)
            self._log("Within-network city profile enabled: Configurational Accessibility Interpretation was enabled automatically.")

    @staticmethod
    def _has_field(layer, name):
        target = name.upper()
        return any(field.name().upper() == target or field.name().upper().endswith(target) for field in layer.fields())

    def _refresh_compare_layers(self):
        if not hasattr(self, "compare_layer_a_combo"):
            return
        current_a = self.compare_layer_a_combo.currentData()
        current_b = self.compare_layer_b_combo.currentData()
        self.compare_layer_a_combo.clear()
        self.compare_layer_b_combo.clear()
        candidates = []
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer) or not layer.isValid():
                continue
            if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.LineGeometry:
                continue
            if self._has_field(layer, "INT_R") and self._has_field(layer, "RADIUS"):
                candidates.append(layer)
        candidates.sort(key=lambda lyr: lyr.name().lower())
        for layer in candidates:
            label = f"{layer.name()}  [{layer.featureCount()} features]"
            self.compare_layer_a_combo.addItem(label, layer.id())
            self.compare_layer_b_combo.addItem(label, layer.id())
        for combo, previous in ((self.compare_layer_a_combo, current_a), (self.compare_layer_b_combo, current_b)):
            if previous:
                idx = combo.findData(previous)
                if idx >= 0:
                    combo.setCurrentIndex(idx)
        if self.compare_layer_b_combo.count() > 1 and self.compare_layer_b_combo.currentIndex() == self.compare_layer_a_combo.currentIndex():
            self.compare_layer_b_combo.setCurrentIndex(1)

    def _layer_from_combo(self, combo):
        layer_id = combo.currentData()
        return QgsProject.instance().mapLayer(layer_id) if layer_id else None

    def _style_named_class_field(self, layer, field_name, label):
        if not field_name:
            return False
        observed = set()
        for feature in layer.getFeatures():
            value = feature[field_name]
            if value is not None:
                observed.add(str(value))
        categories = []
        width = float(self.line_width_spin.value())
        class_order = ["Very Low", "Low", "Moderate", "High", "Very High"]
        for i, class_name in enumerate(class_order):
            if class_name not in observed:
                continue
            color = self._spectrum_color((i + 0.5) / len(class_order))
            symbol = QgsLineSymbol.createSimple({"color": color.name(), "width": f"{width:.2f}"})
            categories.append(QgsRendererCategory(class_name, symbol, class_name))
        if not categories:
            return False
        layer.setRenderer(QgsCategorizedSymbolRenderer(field_name, categories))
        layer.triggerRepaint()
        self._log(f"{layer.name()} styled by {label} ({field_name}) using the shared Very Low–Very High scale.")
        return True

    def _log_profile_summary(self, scope, summary, city, cross_city=False, radius=None):
        if scope == "LOCAL":
            metric = f"Local R{radius if radius is not None else self.local_radius.value()}"
            phrase = f"local configurational accessibility at topological radius R{radius if radius is not None else self.local_radius.value()}"
        else:
            metric = "Global Rn"
            phrase = "global configurational accessibility"
        p = summary["percentages"]
        score_label = "comparison score" if cross_city else "Within-Network Profile"
        self._log(f"UCAI {metric} {score_label} — {city}: {summary['score']:.2f}/5 ({summary['rating']}).")
        self._log(
            f"Accessibility profile by analysed street-network length — Very Low {p['Very Low']:.1f}%, "
            f"Low {p['Low']:.1f}%, Moderate {p['Moderate']:.1f}%, High {p['High']:.1f}%, Very High {p['Very High']:.1f}%."
        )
        self._log(f"{p['Very High']:.1f}% of {city}'s analysed street-network length exhibits Very High {phrase}.")
        self._log(f"High or Very High: {summary['high_plus']:.1f}% | Low or Very Low: {summary['low_plus']:.1f}%.")
        if not cross_city:
            self._log(
                "Within-Network Profile is relative to this network's own Integration distribution; use Compare Two Cities at the same radius for a direct cross-city comparison.",
                "WARNING",
            )

    def _study_area_name(self):
        """Return the user-supplied city/study-area name, otherwise use the input filename."""
        typed = self.city_name_edit.text().strip()
        if typed:
            return typed
        path = self.path_edit.text().strip()
        if path:
            base = os.path.splitext(os.path.basename(path))[0].strip()
            if base:
                return base
        return "Study area"

    def _apply_city_profile(self, layer):
        if not self.city_profile_check.isChecked():
            self._last_ucai_summaries = {}
            return
        summaries = within_network_profiles(layer)
        if not summaries:
            raise RuntimeError(
                "City profile could not be calculated because no within-network accessibility class fields were found. "
                "Enable Configurational Accessibility Interpretation."
            )
        self._last_ucai_summaries = summaries
        city = self._study_area_name()
        for scope, summary in summaries.items():
            self._log_profile_summary(scope, summary, city, cross_city=False)

    def _compare_two_cities(self):
        try:
            layer_a = self._layer_from_combo(self.compare_layer_a_combo)
            layer_b = self._layer_from_combo(self.compare_layer_b_combo)
            if layer_a is None or layer_b is None:
                raise ValueError("Select two analysed UCAI Local Integration result layers.")
            result = compare_local_layers(layer_a, layer_b)
            name_a = self.compare_name_a_edit.text().strip() or layer_a.name()
            name_b = self.compare_name_b_edit.text().strip() or layer_b.name()
            radius = result["radius"]
            b = result["breaks"]
            self._log(
                f"Two-city comparison completed at Local R{radius}. Shared INT_R breakpoints: "
                f"{b[0]:.6g}, {b[1]:.6g}, {b[2]:.6g}, {b[3]:.6g}."
            )
            self._log(
                "Shared comparison thresholds use equal city weighting (50/50); streets within each city are weighted by analysed network length."
            )
            if not result["distinct_breaks"]:
                self._log("Some shared class breakpoints are identical because the pooled Integration distribution has limited variation.", "WARNING")
            self._log_profile_summary("LOCAL", result["a"], name_a, cross_city=True, radius=radius)
            self._log_profile_summary("LOCAL", result["b"], name_b, cross_city=True, radius=radius)
            self._style_named_class_field(layer_a, result["fields_a"]["class"], "Shared Comparison Class")
            self._style_named_class_field(layer_b, result["fields_b"]["class"], "Shared Comparison Class")
            layer_a.triggerRepaint()
            layer_b.triggerRepaint()
            QMessageBox.information(
                self, "UCAI City Comparison",
                f"Local R{radius} comparison completed.\n\n"
                f"{name_a}: {result['a']['score']:.2f}/5 — {result['a']['rating']}\n"
                f"{name_b}: {result['b']['score']:.2f}/5 — {result['b']['rating']}\n\n"
                "Both scores use the same shared classification scale."
            )
        except Exception as exc:
            self._log(str(exc), "ERROR")
            QMessageBox.critical(self, "UCAI City Comparison", str(exc))

    def _browse_input(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select ESRI line shapefile", "", "ESRI Shapefile (*.shp)")
        if path:
            self.path_edit.setText(path)
            self.layer = None
            self.gaps = []
            self.validation_label.setText("Input selected; validation required.")
            self.integrity_label.setText("Network has not been checked.")
            self._log(f"Input selected: {path}")

    def _load_and_validate(self, show_success=True):
        path = self.path_edit.text().strip()
        if not path:
            raise ValueError("Select an ESRI Shapefile before continuing.")
        if not path.lower().endswith(".shp"):
            raise ValueError("Only ESRI Shapefile (.shp) input is accepted by this plugin.")
        if not os.path.isfile(path):
            raise ValueError("The selected shapefile does not exist.")

        layer = QgsVectorLayer(path, os.path.splitext(os.path.basename(path))[0], "ogr")
        if not layer.isValid():
            raise ValueError("QGIS could not open the selected shapefile.")
        if QgsWkbTypes.geometryType(layer.wkbType()) != QgsWkbTypes.LineGeometry:
            raise ValueError("The shapefile must contain line geometry.")
        if not layer.crs().isValid():
            raise ValueError("The shapefile has no valid CRS definition.")
        if layer.crs().isGeographic():
            raise ValueError(
                "The vector data is not projected. Reproject it to an appropriate projected CRS before analysis."
            )

        self.layer = layer
        if show_success:
            text = (
                f"✓ Valid ESRI line shapefile | CRS: {layer.crs().authid()} — "
                f"{layer.crs().description()} | Features: {layer.featureCount()}"
            )
            self.validation_label.setText(text)
        return layer

    def validate_input(self):
        try:
            layer = self._load_and_validate(show_success=True)
            self._log(
                f"Validation passed: {layer.featureCount()} line feature(s), "
                f"projected CRS {layer.crs().authid()}."
            )
        except Exception as exc:
            self.layer = None
            self.validation_label.setText(f"✗ {exc}")
            self._log(str(exc), "ERROR")
            QMessageBox.warning(self, "Input validation", str(exc))
            return False
        return True

    def check_network(self):
        try:
            layer = self._load_and_validate(show_success=True)
            self._log(
                f"Network integrity check started: connection tolerance={self.connection_tol.value():g}; "
                f"maximum gap={self.gap_tol.value():g} map units."
            )
            gaps, dangling = inspect_network(
                layer,
                connection_tolerance=self.connection_tol.value(),
                gap_tolerance=self.gap_tol.value(),
            )
            self.gaps = gaps
            if gaps:
                msg = (
                    f"{dangling} dangling endpoint(s); {len(gaps)} probable small missing line piece(s) "
                    f"within {self.gap_tol.value():g} map units."
                )
                self.integrity_label.setText("⚠ " + msg)
                self._log(msg, "WARNING")
            else:
                msg = (
                    f"Check complete. {dangling} dangling endpoint(s), but no probable missing line pieces "
                    f"within {self.gap_tol.value():g} map units."
                )
                self.integrity_label.setText("✓ " + msg)
                self._log(msg)
        except Exception as exc:
            self._log(str(exc), "ERROR")
            QMessageBox.warning(self, "Network integrity check", str(exc))

    def repair_network(self):
        try:
            layer = self._load_and_validate(show_success=True)
            if not self.gaps:
                self._log("No cached gap candidates; running integrity check before repair.")
                gaps, _ = inspect_network(
                    layer,
                    connection_tolerance=self.connection_tol.value(),
                    gap_tolerance=self.gap_tol.value(),
                )
                self.gaps = gaps
            if not self.gaps:
                self._log("Repair skipped: no probable small missing line pieces found.")
                QMessageBox.information(self, "Repair network", "No probable small missing line pieces were found.")
                return

            src_path = self.path_edit.text().strip()
            default_path = os.path.splitext(src_path)[0] + "_repaired.shp"
            output_path, _ = QFileDialog.getSaveFileName(
                self, "Save repaired ESRI Shapefile", default_path, "ESRI Shapefile (*.shp)")
            if not output_path:
                self._log("Repair canceled before output was created.")
                return
            if not output_path.lower().endswith(".shp"):
                output_path += ".shp"

            self._log(f"Creating repaired shapefile: {output_path}")
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "ESRI Shapefile"
            options.fileEncoding = "UTF-8"
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
            error, new_file, new_layer, message = QgsVectorFileWriter.writeAsVectorFormatV3(
                layer, output_path, QgsProject.instance().transformContext(), options)
            if error != QgsVectorFileWriter.NoError:
                raise RuntimeError(message or "Could not create repaired shapefile.")

            repaired = QgsVectorLayer(output_path, os.path.splitext(os.path.basename(output_path))[0], "ogr")
            if not repaired.isValid():
                raise RuntimeError("The repaired shapefile was created but could not be reopened.")

            provider = repaired.dataProvider()
            attr_count = len(repaired.fields())
            multipart = QgsWkbTypes.isMultiType(repaired.wkbType())
            additions = []
            for gap in self.gaps:
                f = QgsFeature(repaired.fields())
                f.setGeometry(connector_geometry(gap, multipart=multipart))
                f.setAttributes([None] * attr_count)
                additions.append(f)
            ok, _ = provider.addFeatures(additions)
            if not ok:
                raise RuntimeError("QGIS could not append the generated missing line pieces.")
            repaired.updateExtents()

            QgsProject.instance().addMapLayer(repaired)
            self.path_edit.setText(output_path)
            self.layer = repaired
            self.gaps = []
            self.validation_label.setText(f"✓ Repaired shapefile loaded | Added {len(additions)} connector segment(s).")
            self.integrity_label.setText("Repaired network loaded. Run the integrity check again before analysis.")
            self._log(f"Repair complete: {len(additions)} connector segment(s) added to a new shapefile.")
            QMessageBox.information(
                self, "Repair complete",
                f"Created a new ESRI Shapefile and added {len(additions)} probable missing line piece(s).\n\n"
                "Review the generated connectors before running final analysis.")
        except Exception as exc:
            self._log(str(exc), "ERROR")
            QMessageBox.critical(self, "Repair network", str(exc))

    def build_analytical_network(self):
        """Create a clean intersection-to-intersection network from raw linework.

        This is a preprocessing/export tool. It does not calculate Space Syntax
        measures. Source lines are split at genuine geometric intersections and
        consecutive pieces are then merged through degree-2 intermediate nodes.
        The resulting shapefile can be reviewed, edited if needed, and used as an
        already-segmented network for subsequent UCAI analysis.
        """
        try:
            layer = self._load_and_validate(show_success=True)
            src_path = self.path_edit.text().strip()
            default_path = os.path.splitext(src_path)[0] + "_analytical_network.shp"
            output_path, _ = QFileDialog.getSaveFileName(
                self, "Save analytical street network", default_path, "ESRI Shapefile (*.shp)")
            if not output_path:
                self._log("Analytical-network creation canceled before output was created.")
                return
            if not output_path.lower().endswith(".shp"):
                output_path += ".shp"

            self._log("Building analytical network from raw linework.")
            self._log(
                "Analytical-network rule: split at true intersections, then merge arbitrary degree-2 "
                "feature breaks between junctions; preserve dead ends, T-junctions, crossroads and other branches."
            )

            feedback = QgsProcessingFeedback()
            segments, warnings = build_segments(
                layer,
                snap_tolerance=self.connection_tol.value(),
                min_segment_length=0.000001,
                feedback=feedback,
            )
            split_count = len(segments)
            segments, merged_count = consolidate_fragmented_segments(
                segments, snap_tolerance=self.connection_tol.value(), feedback=feedback
            )

            memory = QgsVectorLayer(
                f"LineString?crs={layer.crs().authid()}",
                "UCAI analytical network",
                "memory",
            )
            if not memory.isValid():
                raise RuntimeError("Could not create the temporary analytical-network layer.")
            provider = memory.dataProvider()
            provider.addAttributes([
                QgsField("SEG_ID", QVariant.Int),
                QgsField("SRC_FID", QVariant.Int),
                QgsField("SRC_PART", QVariant.Int),
                QgsField("LENGTH", QVariant.Double),
            ])
            memory.updateFields()

            features = []
            for seg in segments:
                feat = QgsFeature(memory.fields())
                feat.setGeometry(seg.geometry)
                feat.setAttributes([int(seg.seg_id), int(seg.source_fid), int(seg.source_part), float(seg.length)])
                features.append(feat)
            ok, _ = provider.addFeatures(features)
            if not ok:
                raise RuntimeError("Could not populate the analytical-network layer.")
            memory.updateExtents()

            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = "ESRI Shapefile"
            options.fileEncoding = "UTF-8"
            options.actionOnExistingFile = QgsVectorFileWriter.CreateOrOverwriteFile
            error, new_file, new_layer, message = QgsVectorFileWriter.writeAsVectorFormatV3(
                memory, output_path, QgsProject.instance().transformContext(), options
            )
            if error != QgsVectorFileWriter.NoError:
                raise RuntimeError(message or "Could not create analytical-network shapefile.")

            analytical = QgsVectorLayer(
                output_path, os.path.splitext(os.path.basename(output_path))[0], "ogr"
            )
            if not analytical.isValid():
                raise RuntimeError("The analytical network was created but could not be reopened.")

            QgsProject.instance().addMapLayer(analytical)
            self.path_edit.setText(output_path)
            self.layer = analytical
            self.gaps = []
            self.already_segmented_check.setChecked(True)
            self.validation_label.setText(
                f"✓ Analytical network loaded | {len(segments)} intersection-to-intersection segment(s)."
            )
            self.integrity_label.setText(
                "Analytical network created. Review the geometry, then run the integrity check before final analysis."
            )
            self._log(
                f"Analytical network complete: {split_count} post-intersection piece(s) were reduced to "
                f"{len(segments)} analytical segment(s); {merged_count} artificial intermediate break(s) consolidated."
            )
            for warning in warnings[:10]:
                self._log(warning, "WARNING")
            if len(warnings) > 10:
                self._log(f"{len(warnings) - 10} additional preprocessing warning(s) omitted from the log.", "WARNING")
            self._log(
                "The new shapefile is now selected and marked as already segmented. "
                "The original raw dataset remains unchanged."
            )
            QMessageBox.information(
                self,
                "Analytical network created",
                f"Created a new intersection-to-intersection analytical network with {len(segments)} segment(s).\n\n"
                f"Consolidated {merged_count} artificial raw-data break(s).\n\n"
                "Review the new layer and run the network-integrity check before final UCAI analysis."
            )
        except Exception as exc:
            self._log(str(exc), "ERROR")
            QMessageBox.critical(self, "Build analytical network", str(exc))

    def _selected_measure_names(self):
        r = self.local_radius.value()
        names = []
        if self.global_int_check.isChecked(): names.append("Global Integration (Rn)")
        if self.local_int_check.isChecked(): names.append(f"Local Integration (R{r})")
        if self.config_interp_check.isChecked() or self.city_profile_check.isChecked(): names.append("Configurational Accessibility Interpretation")
        if self.city_profile_check.isChecked(): names.append("Within-network City Accessibility Profile")
        if self.composite_ucai_check.isChecked(): names.append("UCAI Composite Index")
        return names

    def run_analysis(self):
        if self.analysis_task is not None:
            QMessageBox.information(self, "UCAI analysis", "An analysis is already running.")
            return
        try:
            self._last_ucai_summaries = {}
            self._last_ucai_fields = {}
            layer = self._load_and_validate(show_success=True)
            primary = [
                self.global_int_check.isChecked(), self.local_int_check.isChecked(),
            ]
            if not any(primary):
                raise ValueError("Select Global Integration, Local Integration, or both.")

            alg = QgsApplication.processingRegistry().algorithmById("ucai:segment_analysis")
            if alg is None:
                raise RuntimeError("UCAI Integration Processing algorithm is not registered. Restart QGIS and try again.")

            params = {
                "INPUT": layer,
                "GLOBAL_INT": self.global_int_check.isChecked(),
                "LOCAL_INT": self.local_int_check.isChecked(),
                "CONFIG_INTERP": self.config_interp_check.isChecked() or self.city_profile_check.isChecked(),
                "COMPOSITE_UCAI": self.composite_ucai_check.isChecked(),
                "ALREADY_SEGMENTED": self.already_segmented_check.isChecked(),
                "CONSOLIDATE_RAW": self.consolidate_raw_check.isChecked(),
                "TOPO_RADIUS": self.local_radius.value(),
                "SNAP_TOL": self.connection_tol.value(),
                "MIN_SEG_LEN": 0.000001,
                "OUTPUT": "TEMPORARY_OUTPUT",
            }

            self.analysis_context = QgsProcessingContext()
            self.analysis_context.setProject(QgsProject.instance())
            self.analysis_feedback = QgsProcessingFeedback()
            self.analysis_task = QgsProcessingAlgRunnerTask(
                alg, params, self.analysis_context, self.analysis_feedback)
            self.analysis_task.executed.connect(self._analysis_finished)
            self.analysis_task.progressChanged.connect(self._analysis_progress)

            self.progress.setValue(0)
            self._last_logged_progress = -10
            self.run_btn.setEnabled(False)
            self.cancel_btn.setEnabled(True)

            self._log("UCAI analysis queued in QGIS Task Manager.")
            self._log("Selected measures: " + ", ".join(self._selected_measure_names()))
            if self.already_segmented_check.isChecked():
                self._log("Network preparation: already segmented; intersection splitting and raw-line consolidation will be skipped.")
            else:
                prep = "street lines; point intersections will be detected and split"
                if self.consolidate_raw_check.isChecked():
                    prep += "; degree-2 raw-data breaks between true junctions will be consolidated"
                self._log("Network preparation: " + prep + ".")
            if self.composite_ucai_check.isChecked():
                self._log(
                    "UCAI Composite: UCAI_COMP = 1 + 4 × mean[P(INT_RN), P(INT_R), P(CHOICE_RN)]. "
                    "Equal weights; percentile ranks are network-relative; score is bounded 1–5."
                )
                self._log(
                    "Interpretation boundary: do not interpret UCAI_COMP as an externally validated measure of observed walkability or accessibility."
                , "WARNING")
            if self.config_interp_check.isChecked() or self.city_profile_check.isChecked():
                if self.global_int_check.isChecked() and self.local_int_check.isChecked():
                    self._log(
                        "Configurational interpretation enabled: Global/Local accessibility classes plus "
                        "Local–Global type will be derived relative to this network."
                    )
                else:
                    self._log(
                        "Configurational interpretation enabled: the selected Integration measure will be "
                        "classified relative to this network."
                    )
                self._log(
                    "Interpretation describes configurational accessibility; it is not a direct measurement "
                    "of observed pedestrian walkability."
                )
            QgsApplication.taskManager().addTask(self.analysis_task)
        except Exception as exc:
            self._log(str(exc), "ERROR")
            QMessageBox.critical(self, "UCAI analysis", str(exc))

    def _analysis_progress(self, value):
        value = max(0, min(100, int(round(value))))
        self.progress.setValue(value)
        bucket = (value // 10) * 10
        if bucket >= self._last_logged_progress + 10 and bucket < 100:
            self._last_logged_progress = bucket
            self._log(f"Analysis progress: {bucket}%")

    def cancel_analysis(self):
        if self.analysis_task is None:
            return
        self._log("Cancellation requested by user.", "WARNING")
        try:
            if self.analysis_feedback is not None:
                self.analysis_feedback.cancel()
            self.analysis_task.cancel()
        except Exception as exc:
            self._log(f"Could not cancel task cleanly: {exc}", "ERROR")

    @staticmethod
    def _quantile(sorted_values, p):
        if not sorted_values:
            return 0.0
        if len(sorted_values) == 1:
            return float(sorted_values[0])
        pos = (len(sorted_values) - 1) * p
        lo = int(pos)
        hi = min(lo + 1, len(sorted_values) - 1)
        frac = pos - lo
        return float(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac)

    def _preferred_style_field(self, layer):
        available = {f.name().upper(): f.name() for f in layer.fields()}

        candidates = {
            "Local Integration": ["INT_R", "SS_INT_R"],
            "Global Integration": ["INT_RN", "SS_INT_RN"],
            "UCAI Composite Index": ["UCAI_COMP", "SS_UCAI_COMP"],
            "UCAI Composite Class": ["UC_CLASS", "SS_UC_CLASS"],
        }

        selected = self.display_metric_combo.currentText()
        ordered = []
        if selected.startswith("Auto"):
            ordered = ["Local Integration", "Global Integration"]
        else:
            ordered = [selected, "Local Integration", "Global Integration"]

        seen = set()
        for label in ordered:
            if label in seen:
                continue
            seen.add(label)
            for candidate in candidates.get(label, []):
                if candidate.upper() in available:
                    return available[candidate.upper()], label

        # Defensive fallback for prefixed/truncated collision-safe names.
        for field in layer.fields():
            name = field.name()
            upper = name.upper()
            if upper.endswith("INT_R"):
                return name, "Local Integration"
            if upper.endswith("INT_RN"):
                return name, "Global Integration"
        return None, None


    def _preferred_class_field(self, layer):
        available = {f.name().upper(): f.name() for f in layer.fields()}
        selected = self.display_metric_combo.currentText()

        mapping = {
            "Local Accessibility Class — within network": (["L_CLASS", "SS_L_CLASS"], "Local Accessibility Class"),
            "Global Accessibility Class — within network": (["G_CLASS", "SS_G_CLASS"], "Global Accessibility Class"),
        }
        if selected not in mapping:
            return None, None
        candidates, label = mapping[selected]
        for candidate in candidates:
            if candidate.upper() in available:
                return available[candidate.upper()], label

        suffixes = [c.upper() for c in candidates]
        for field in layer.fields():
            upper = field.name().upper()
            if any(upper.endswith(suffix) for suffix in suffixes):
                return field.name(), label
        return None, label

    def _apply_accessibility_class_style(self, layer):
        field_name, class_label = self._preferred_class_field(layer)
        if not class_label:
            return None
        if not field_name:
            self._log(
                f"Requested {class_label} symbology is unavailable. Enable the corresponding interpretation or "
                "interpretation option and calculate the required Integration measure; falling back to Integration symbology.",
                "WARNING",
            )
            return False

        class_order = ["Very Low", "Low", "Moderate", "High", "Very High"]
        observed = set()
        for feature in layer.getFeatures():
            value = feature[field_name]
            if value is not None:
                observed.add(str(value))

        categories = []
        width = float(self.line_width_spin.value())
        for i, label in enumerate(class_order):
            if label not in observed:
                continue
            t = (i + 0.5) / len(class_order)
            color = self._spectrum_color(t)
            symbol = QgsLineSymbol.createSimple({
                "color": color.name(),
                "width": f"{width:.2f}",
            })
            categories.append(QgsRendererCategory(label, symbol, label))

        if not categories:
            self._log(
                f"Requested {class_label} symbology skipped: field {field_name} contains no recognized accessibility classes.",
                "WARNING",
            )
            return False

        renderer = QgsCategorizedSymbolRenderer(field_name, categories)
        layer.setRenderer(renderer)
        layer.triggerRepaint()
        self._log(
            f"Result styled by {class_label} ({field_name}) using {len(categories)} categorical class(es): "
            "Very Low–Very High; Space Syntax spectrum low-blue/high-red."
        )
        return True

    def _apply_integration_style(self, layer):
        field_name, metric_label = self._preferred_style_field(layer)
        if not field_name:
            self._log(
                "Automatic integration styling skipped: no integration field was found.",
                "WARNING",
            )
            return False

        values = []
        for feature in layer.getFeatures():
            value = feature[field_name]
            try:
                value = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values.append(value)

        if not values:
            self._log(
                f"Automatic styling skipped: field {field_name} has no numeric values.",
                "WARNING",
            )
            return False

        data_min = min(values)
        data_max = max(values)

        if self.manual_range_check.isChecked():
            vmin = float(self.manual_min_spin.value())
            vmax = float(self.manual_max_spin.value())
            if vmax <= vmin:
                raise ValueError("Manual symbology maximum must be greater than the minimum.")

            # Classification methods which depend on the value distribution use
            # values inside the requested display range.
            class_values = [v for v in values if vmin <= v <= vmax]
            if not class_values:
                raise ValueError(
                    "No integration values fall inside the manual symbology range."
                )
        else:
            vmin = data_min
            vmax = data_max
            class_values = values

            # Populate controls with the observed range for convenient later reuse.
            self.manual_min_spin.setValue(vmin)
            self.manual_max_spin.setValue(vmax)

        if abs(vmax - vmin) <= 1.0e-12:
            color = self._spectrum_color(0.5)
            symbol = QgsLineSymbol.createSimple({
                "color": color.name(),
                "width": f"{self.line_width_spin.value():.2f}",
            })
            precision = self.legend_precision_spin.value()
            label = f"{vmin:.{precision}f}"
            layer.setRenderer(
                QgsGraduatedSymbolRenderer(
                    field_name,
                    [QgsRendererRange(vmin, vmax, symbol, label)],
                )
            )
            layer.triggerRepaint()
            self._log(
                f"Result styled by {metric_label} ({field_name}); all values are identical."
            )
            return True

        requested_classes = int(self.class_count_spin.value())
        classification_ranges, method_name = self._classification_ranges(
            class_values, requested_classes
        )

        if not classification_ranges:
            raise RuntimeError(
                f"QGIS could not generate {method_name} classes for field {field_name}."
            )

        # Respect explicit/manual endpoints. QGIS-generated class limits are
        # otherwise retained exactly.
        bounds = []
        for i, class_range in enumerate(classification_ranges):
            lower = float(class_range.lowerBound())
            upper = float(class_range.upperBound())
            if i == 0:
                lower = vmin
            if i == len(classification_ranges) - 1:
                upper = vmax
            lower = max(vmin, lower)
            upper = min(vmax, upper)
            if upper >= lower:
                bounds.append((lower, upper))

        if not bounds:
            raise RuntimeError("No usable graduated ranges were generated.")

        precision = int(self.legend_precision_spin.value())
        width = float(self.line_width_spin.value())
        ranges = []

        for i, (lower, upper) in enumerate(bounds):
            # Use class midpoint position across the spectrum, independent of
            # whether the classes themselves have equal widths.
            t = (i + 0.5) / len(bounds)
            color = self._spectrum_color(t)
            symbol = QgsLineSymbol.createSimple({
                "color": color.name(),
                "width": f"{width:.2f}",
            })
            label = f"{lower:.{precision}f} – {upper:.{precision}f}"
            ranges.append(QgsRendererRange(lower, upper, symbol, label))

        renderer = QgsGraduatedSymbolRenderer(field_name, ranges)
        layer.setRenderer(renderer)
        layer.triggerRepaint()

        self._log(
            f"Result styled by {metric_label} ({field_name}) using "
            f"{len(ranges)} {method_name} class(es); "
            f"range {vmin:.{precision}f}–{vmax:.{precision}f}; "
            f"Space Syntax spectrum {'reversed' if self.reverse_ramp_check.isChecked() else 'low-blue/high-red'}."
        )
        return True

    def _analysis_finished(self, successful, results):
        try:
            if not successful:
                self.progress.setValue(0)
                self._log("Analysis did not complete successfully or was canceled.", "ERROR")
                QMessageBox.warning(
                    self, "UCAI analysis",
                    "Analysis was canceled or failed. Review the Analysis Log and QGIS Processing log for details.")
                return

            output = results.get("OUTPUT") if results else None
            if not output:
                raise RuntimeError("Analysis completed without an output layer reference.")

            out_layer = None
            if isinstance(output, QgsVectorLayer):
                out_layer = output
            else:
                out_layer = QgsProcessingUtils.mapLayerFromString(str(output), self.analysis_context)

            if out_layer is not None and out_layer.isValid():
                city = self._study_area_name()
                store = self.analysis_context.temporaryLayerStore() if self.analysis_context else None
                if store is not None and store.mapLayer(out_layer.id()) is not None:
                    taken = store.takeMapLayer(out_layer)
                    if taken is not None:
                        out_layer = taken
                self._apply_city_profile(out_layer)

                # Planner-facing layer name: append the resulting city/profile rating.
                # Prefer the Local rating when available because local configurational
                # accessibility is the primary city-profile measure; otherwise use Global.
                summaries = getattr(self, "_last_ucai_summaries", {})
                preferred = summaries.get("LOCAL") or summaries.get("GLOBAL")
                rating = preferred.get("rating") if preferred else None
                score = preferred.get("score") if preferred else None
                if preferred and summaries.get("LOCAL") is preferred:
                    scale_label = f"R{self.local_radius.value()}"
                elif preferred:
                    scale_label = "Rn"
                else:
                    scale_label = None

                # Keep the QGIS layer name consistent with the completion popup,
                # including analysis scale, 1–5 profile score, and rating.
                profile_bits = []
                if scale_label:
                    profile_bits.append(scale_label)
                if score is not None:
                    profile_bits.append(f"{score:.2f}of5")
                if rating:
                    profile_bits.append(rating.upper())
                profile_suffix = "-".join(profile_bits)

                if city and profile_suffix:
                    out_layer.setName(f"UCAI Results — {city}-{profile_suffix}")
                elif city:
                    out_layer.setName(f"UCAI Results — {city}")
                elif profile_suffix:
                    out_layer.setName(f"UCAI Results — {profile_suffix}")
                else:
                    out_layer.setName("UCAI Results")

                class_style_result = self._apply_accessibility_class_style(out_layer)
                if class_style_result is not True:
                    self._apply_integration_style(out_layer)
                QgsProject.instance().addMapLayer(out_layer)
                self._refresh_compare_layers()
            else:
                self.iface.addVectorLayer(str(output), "UCAI Results", "ogr")

            self.progress.setValue(100)
            self._log("UCAI analysis completed successfully. Output added to the QGIS project with the selected symbology where available.")
            summary_lines = []
            city = self._study_area_name()
            for scope, summary in getattr(self, "_last_ucai_summaries", {}).items():
                label = f"Local R{self.local_radius.value()}" if scope == "LOCAL" else "Global Rn"
                summary_lines.append(f"{label} profile: {summary['score']:.2f}/5 — {summary['rating']}")
            message = "UCAI analysis completed successfully."
            if summary_lines:
                preferred = getattr(self, "_last_ucai_summaries", {}).get("LOCAL") or getattr(self, "_last_ucai_summaries", {}).get("GLOBAL")
                if preferred:
                    scale = f"R{self.local_radius.value()}" if "LOCAL" in getattr(self, "_last_ucai_summaries", {}) else "Rn"
                    message += (
                        f"\n\nUCAI Results — {city}"
                        f"\n{scale} {preferred['score']:.2f}/5 — {preferred['rating'].upper()}"
                        f"\n\nWithin-Network Profile"
                    )
                    for scope, summary in getattr(self, "_last_ucai_summaries", {}).items():
                        label = f"Local R{self.local_radius.value()}" if scope == "LOCAL" else "Global Rn"
                        message += f"\n{label}: {summary['score']:.2f}/5 — {summary['rating']}"
                    message += (
                        "\n\nThis is a within-network profile relative to this study area's own Integration distribution. "
                        "Use Compare Two Cities at the same radius for direct cross-city comparison."
                    )
            QMessageBox.information(self, "Urban Configurational Accessibility Index (UCAI)", message)
        except Exception as exc:
            self._log(str(exc), "ERROR")
            QMessageBox.critical(self, "UCAI analysis", str(exc))
        finally:
            self.run_btn.setEnabled(True)
            self.cancel_btn.setEnabled(False)
            self.analysis_task = None
            self.analysis_feedback = None
            # Retain context only long enough to resolve temporary outputs.
            self.analysis_context = None
