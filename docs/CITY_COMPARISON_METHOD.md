# UCAI two-city comparison method

UCAI does not require a built-in or user-supplied threshold CSV.

For a single study area, the optional Configurational Accessibility Interpretation describes each analysed network relative to its own Integration distribution. The resulting network-length percentages and profile score are descriptive within that study area and are not a cross-city benchmark.

For direct comparison, select two UCAI Local Integration result layers calculated at the **same topological radius** (for example, R3 versus R3). UCAI verifies the `RADIUS` field before comparing them.

The comparison derives one shared five-class scale from the two `INT_R` distributions. Each city contributes 50% of the threshold reference weight; within each city, street segments are weighted by analysed network length. Shared 20th, 40th, 60th and 80th weighted-percentile breakpoints define Very Low, Low, Moderate, High and Very High classes for both cities.

UCAI then writes `CMP_CLASS` and `CMP_SCORE` (1–5) to both selected result layers and reports network-length percentages and a length-weighted comparison score for each city.

The comparison is valid only when network preparation, Integration definition, and local topological radius are equivalent. Topological radius is not a fixed metric distance.
