# Urban Configurational Accessibility Index (UCAI) — QGIS Plugin v0.8.0

UCAI is a planner-focused QGIS plugin that uses a native, depthmapX-free Space Syntax Integration engine to analyse street-network configuration and translate the results into interpretable urban accessibility outputs.

The proven Integration calculations inherited from Space Syntax Engine v0.6.1 remain unchanged. UCAI does not require an external or built-in reference-threshold CSV.


## UCAI Composite Index — new in v0.8.0
UCAI now includes an **optional composite mode**. It does not replace the established Within-Network Profile. When enabled, the plugin calculates Global Integration (`INT_RN`), Local Integration (`INT_R`) at the user-selected R2–R50 radius, and Global Choice (`CHOICE_RN`). Each measure is converted to a mid-rank empirical percentile within the analysed network.

For equal weights, the implemented equation is:

`UCAI_COMP = 1 + 4 × [P(INT_RN) + P(INT_R) + P(CHOICE_RN)] / 3`

where `P(.)` is the network-relative percentile rank in the interval 0–1. The resulting `UCAI_COMP` is therefore bounded from **1 to 5**. The plugin also writes the component percentile fields `P_GINT`, `P_LINT`, and `P_GCHO`, plus `UC_CLASS` (Very Low–Very High) for transparent inspection.

**Interpretation:** the composite combines three configurational dimensions: global centrality/accessibility, local centrality/accessibility at the selected topological radius, and global through-movement potential (Choice). Equal weighting is the v0.8.0 default.

**Interpretation boundary:** this mode is network-relative and has not yet been externally validated against observed pedestrian counts, destination accessibility, travel behaviour, or other ground-truth measures. It should not be described as proven to improve predictive accuracy over the existing Integration-based profile until validation is completed.

## Core workflow
1. Select a projected ESRI Shapefile (`.shp`) containing line geometry.
2. Validate the input.
3. Check small geometric gaps and, when appropriate, create a new repaired shapefile.
4. By default, consolidate fragmented raw linework between true junctions. Consecutive line pieces meeting only at degree-2 nodes are merged so arbitrary raw-data feature breaks do not create artificial topological steps. True branching intersections are preserved.
5. For raw or inconsistently segmented source data, use **Build Analytical Network from Raw Data → New Shapefile** to create a reviewable intersection-to-intersection network before analysis. The tool splits at true geometric intersections, consolidates arbitrary degree-2 feature breaks between real junctions, preserves dead ends and branching junctions, and never overwrites the original dataset. The generated layer is automatically treated as an already-segmented analytical network.
5. Calculate Global Integration (Rn), Local Integration (R2–R50), or both.
6. Optionally create within-network configurational accessibility classes.
7. Optionally calculate a length-weighted single-city accessibility profile.
8. Optionally compare two analysed Local Integration result layers at the same topological radius using one shared classification scale.
9. Style results using continuous Integration or accessibility classes.

## Street-level Integration
- Global Integration (`INT_RN`)
- Local Integration (`INT_R`) with user-selected topological radius R2–R50
- Raw-line fragmentation consolidation between true junctions (recommended)
- Build Analytical Network from Raw Data: exports a clean intersection-to-intersection shapefile for review and reuse
- Already-segmented network mode
- QGIS Task Manager execution
- Analysis Log
- Flexible Space Syntax symbology

Topological radius counts network steps. It is not a fixed metric walking distance; for example, R5 does not mean 400 m.

## Within-network interpretation
The optional **Configurational Accessibility Interpretation** adds relative classes without altering Integration values:
- `G_CLASS` — Global class
- `L_CLASS` — Local class
- `LG_CLASS` / `LG_LABEL` — Local–Global type when both measures are calculated

Single-scale classes are based on the analysed network's own Integration distribution. They are useful for understanding spatial variation within one study area.

## Single-city accessibility profile
The optional **within-network city accessibility profile** summarizes the percentage of analysed street-network length in each class:
- Very Low
- Low
- Moderate
- High
- Very High

It also reports a descriptive 1–5 profile score calculated as the network-length-weighted mean of those classes. Because the class boundaries are derived separately inside each study area, this single-city score is **not** a direct cross-city benchmark.

Example statement:

> 30.0% of Malmö's analysed street-network length exhibits Very High local configurational accessibility at topological radius R3.

## Direct two-city comparison
UCAI v0.7.3 removes permanent reference thresholds. Instead, the **Compare Two Cities** function compares two UCAI Local Integration result layers directly.

Requirements:
- both layers must contain `INT_R`;
- both must contain one consistent `RADIUS` value;
- both must use the same topological radius, e.g. R3 vs R3;
- network preparation and Integration methodology should be equivalent.

UCAI derives one shared five-class scale from both cities. Each city contributes **50% of the reference weight**, while line segments within each city are weighted by analysed network length. Shared weighted 20th, 40th, 60th and 80th percentile breakpoints define the five comparison classes.

The comparison writes:
- `CMP_CLASS` — shared Very Low–Very High class
- `CMP_SCORE` — shared class score 1–5

Each city's comparison score is the network-length-weighted mean of `CMP_SCORE`. Because both cities use the same radius and the same shared breakpoints, their comparison scores are on the same two-city scale.

## Interpretation boundary
UCAI evaluates **configurational accessibility of the street network**. It does not directly measure observed pedestrian walkability, which also depends on sidewalks, crossings, destinations, land-use mix, slope, safety, comfort, traffic conditions and other environmental factors.

## v0.7.3
- Removes all reference-threshold CSV logic from the plugin and UI.
- Removes benchmark-threshold symbology options.
- Adds a within-network, network-length accessibility profile for a single study area.
- Adds direct two-city Local Integration comparison using one shared scale.
- Requires matching topological radius for comparison and blocks R3-vs-R5 comparisons.
- Gives each compared city equal statistical weight while weighting streets by network length.
- Adds `CMP_CLASS` and `CMP_SCORE` to compared result layers.
- Automatically styles compared layers by the shared comparison class.

See `docs/CITY_COMPARISON_METHOD.md` for the comparison method.


## v0.7.3

- UCAI now installs under its own plugin package (`UrbanConfigurationalAccessibilityIndex`) and uses the unique Processing provider ID `ucai`, so it can coexist with Space Syntax Engine v0.6.3.
- Completed analysis layers now include the planner-facing profile rating in the layer name, e.g. `UCAI Results — ESKILSTUNA-MODERATE`. Local profile rating is used when available; Global is used as fallback.


## v0.7.4
- Result layer names now mirror the planner-facing profile summary by including analysis scale, score, and rating, e.g. `UCAI Results — ESKILSTUNA-R3-2.65of5-MODERATE`.


## v0.7.5
- Refined single-city wording to **Within-Network Profile** to distinguish it from direct cross-city comparison.
- Completion popup now repeats the result identity, radius, 1–5 score and rating.
- If City / study area name is left blank, UCAI uses the input shapefile name in result naming and reporting.
- Updated UCAI plugin icon.


## v0.8.0
- Adds recommended raw-line consolidation: arbitrary degree-2 feature breaks between true junctions are merged before Space Syntax calculation, preventing artificial extra topological steps while preserving real branching intersections.
- Adds **Build Analytical Network from Raw Data**, a dedicated preprocessing/export workflow that reconstructs raw street linework into a clean, reviewable intersection-to-intersection analytical network while preserving genuine junctions and leaving the source data unchanged.
- Adds optional **UCAI Composite Index**.
- Computes Global Choice only when the composite mode is selected.
- Uses equal-weight mid-rank percentile normalization of `INT_RN`, `INT_R`, and `CHOICE_RN`.
- Writes transparent component percentiles (`P_GINT`, `P_LINT`, `P_GCHO`), continuous `UCAI_COMP` (1–5), and `UC_CLASS`.
- Preserves the v0.7.5 Within-Network Profile and direct two-city comparison as separate analytical approaches.
- Retains a clear interpretation boundary: the composite is network-relative and should not be treated as an externally validated measure of observed walkability or accessibility.
