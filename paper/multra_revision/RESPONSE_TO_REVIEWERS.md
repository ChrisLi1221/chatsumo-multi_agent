# Response to the TRB reviews: current revision for Multimodal Transportation

This response concerns the current manuscript, not the earlier 500-token draft. The main text now distinguishes (A) a seven-evaluation frozen-input selector endpoint extracted from the corrected three-initial experiment, (B) the current fifteen-evaluation adaptive search, (C) fixed-demand sampling repeats, and (D) synthetic-demand intensity tests. Reused cases are identified and are not counted as independent additions to the primary sample. Section and table numbers refer to the rebuilt `main.pdf`.

## Reviewer 1, major comment 1 — Mutation catalog construction and a real example

**Addressed through implementation detail and a complete logged trace.** Sections 3.2–3.5 describe initialization, role ownership, legal enumeration, executable validation, semantic deduplication, sibling exclusion, composition, and conditional joint refinement. Table 1 gives the discrete cycle, green-share, speed-factor, rerouting-fraction, and target choices for every intervention family. It distinguishes the enumerated search space from the validator's broader permitted ranges. A catalog-valid action is not represented as proof of successful rerouting or acceptable traffic performance.

Appendix A uses Albany seed 42, the first case in the predetermined primary case order, without outcome filtering. It lists both measured parents, all entries in each initial legal catalog, filter dispositions, four selected mutation IDs, post-execution KPIs, both bundles, both joint rounds, and the retained plan. The real trace retains the initial plan rather than illustrating a fabricated optimization success. Display labels map to original proposal IDs in `supplementary_current/strict_prefix/worked_current_case.json`, which includes full requests, complete conditional catalogs, filter records, and candidate outcomes.

## Reviewer 1, major comment 2 — Equal synthetic demand, variability, and weak statistical evidence

**Addressed in experimental scope and additional sensitivity analysis; calibration remains unresolved.** Sections 4.1 and 6.3 explicitly describe controlled algorithm tests on network extracts. Network inventory and baseline losses are regenerated from frozen inputs (Table 2). Primary mean Top-10 reductions are 11.53% ± 10.57%, 12.10% ± 11.07%, and 14.81% ± 10.55% for LLM, rule, and random search. All three succeed in 13/15 cases. Section 5.1 and Tables 3–4 report paired differences and exact case/city sign-flip and Holm-adjusted Wilcoxon tests; no LLM advantage is asserted.

Sections 4.4 and 5.3 add Albany and Manhattan at 1,200/2,400/3,600 trips for seeds 42–44, with the same departure window and search protocol. Twelve new baselines and 36 searches were executed; 2,400-trip results are reused. Table 6 and Figure 6 report demand-dependent performance and baseline congestion. LLM is highest on average at low demand, while random is highest at the other levels, without significant paired evidence of LLM superiority. Each intensity has its own frozen Top-10 set; this and the single model sample per new case limit cross-level interpretation. The two-city experiment is not represented as broad external validation.

The high-demand Manhattan baselines have 84, 93, and 100 teleports. These stress-test outcomes are disclosed rather than discarded; accepted plans need only avoid increasing the corresponding baseline teleport count. Apparent delay gains under recovery behavior are qualified accordingly.

## Reviewer 1, major comment 3 — Spatially concentrated deterioration outside one hop

**Addressed with full-network diagnostics, while retaining the original acceptance rule.** Sections 4.5 and 5.4 report edge-level signed changes, worsening fractions, tail values, remote maxima, and fixed 500-m-cell net increases. Figure 5 and Table 8 summarize current adaptive outcomes. Each method has 13/15 results with at least one remote edge worsening by over 60 accumulated vehicle-seconds. The LLM's worst remote-edge increase is 5,478.7 vehicle-seconds and its worst remote-cell increase is 9,081.1.

`spatial_selected.csv` and `worst_remote_edges.csv` are supplied separately for adaptive, sampling, and demand cohorts. Remote means outside the topological monitored neighborhood, not necessarily geographically distant. Absolute loss is distinguished from per-traveler delay, and edges are not independent scenario replicates. Sections 6.3 and 7 expressly state that aggregate acceptance and the no-op guard do not certify spatial safety or equity. New spatial constraints have not been retroactively imposed.

## Reviewer 2, comment 1 — Isolating the LLM contribution

**Addressed with a strictly frozen mutation-selection endpoint, separated from adaptive search.** Section 4.2 and Tables 3–4 report the saved pre-joint endpoint of the corrected three-initial experiment: three common initials, two fixed parents, two complete fixed catalogs, the same initial-only feedback/history, simulator seeds, seven-evaluation cap, and subsequent bundle rules. Both parent request contents are audited identical across all selectors. This is a retrospective extraction of already executed stages, not a newly independent experiment. Later joint candidates cannot influence the extracted endpoint.

Mean Top-10 reductions are 8.50%/9.36%/9.55%, with success 12/15, 14/15, and 13/15 and 103 logical evaluations per selector. All 30 LLM mutation requests succeed. Paired LLM-minus-rule and LLM-minus-random differences are −0.86 and −1.05 percentage points; exact case sign-flip p values are 0.438 and 0.386, and Holm-adjusted Wilcoxon p values are both 0.569. Thus the clean selector test does not establish an LLM benefit.

The fifteen-budget five-initial adaptive cohort is reported separately. Its first parent requests are common, but second-child exclusions and subsequent joint paths can diverge following prior choices. We do not present its higher absolute reductions relative to the smaller protocol as causal evidence for LLM reasoning, search width, or the joint loop. Equal-budget width/depth and joint/independent-exploration controls remain unperformed and are explicitly identified in Section 6.1.

Evidence: `supplementary_current/strict_prefix/strict_prefix_results.csv`, `strict_prefix_provenance.json`, `strict_prefix_paired.json`, and the separate adaptive results.

## Reviewer 2, comment 2 — LLM stochasticity

**Addressed for the current repaired protocol.** Sections 4.3 and 5.2 and Table 5 report five LLM and five random searches in each of five fixed seed-42 cases, with the deterministic rule once. Existing repeat zero is reused; 40 searches are added. LLM mean within-city SD is 3.09 percentage points versus 2.55 for random. Ann Arbor's LLM range is 8.07–22.31%, and Los Angeles' is 8.27–19.74%. Seven of ten fixed first-parent catalogs receive different LLM choices across repeats.

All shared initial sets already contain a feasible plan, so 25/25 final successes are not interpreted as evidence of better generation or universal stability. All 160 newly added LLM requests succeed; this replaces the older short-output-budget diagnostic. Sampling frequencies use only identical first-child requests; later divergent catalogs are not pooled as identical inputs. City means, not individual repeats, enter paired inference. Five repeats, one fixed demand per city, and a model alias without an immutable version remain limitations.

## Reviewer 2, comment 3 — Synthetic demand versus observed counts/calibrated OD

**Partially addressed; empirical calibration remains unresolved.** The repository lacks a usable observed-count dataset or calibrated OD matrix for these instances. We do not claim to have added a calibration case. Instead, Sections 4.4 and 5.3 add paired synthetic intensity sensitivity and disclose its exact construction: seeded one-per-adjacent-pair thinning and matched OD duplication, with unchanged vehicle attributes and departure window. The full initial action set is frozen anew for each scenario before evaluation.

Sections 1, 4.1, 6.3, 7, and the abstract restrict the conclusion to synthetic passenger-demand algorithm behavior. The revision does not claim actual improvements in Manhattan, Los Angeles, or other cities, nor multimodal effectiveness. Observed calibration, behavioral validation, and transport accessibility remain outstanding requirements for practical claims.

## Reviewer 2, comment 4 — Acceptance-threshold sensitivity

**Addressed by candidate-pool reselection over 100 combinations.** Sections 4.5 and 5.4 and Table 7 report alternative Top-10, global-delay, and neighbor tolerances while retaining demand, throughput, and teleport checks. Random has the highest mean Top-10 outcome across all 100 settings in the adaptive cohort; success-rate ordering changes. For example, allowing 10% neighboring loss yields 14/15 successes for LLM and rule versus 13/15 for random. Tightening neighboring spillover to zero yields mean reductions 10.13%/11.20%/12.98%.

Full grids for the adaptive, stability, and demand cohorts are provided separately. This is retrospective reselection from measured candidates, not a rerun of the entire search under each threshold. The manuscript states that distinction and does not choose thresholds to favor LLM.

## Reviewer 2, comment 5 — Different implementation horizons

**Addressed in scope and discussion.** Section 6.2 distinguishes operational/periodic signal and routing decisions, infrastructure-dependent or regulatory speed changes, and planning/design/closure decisions for edge removal and one-way conversion. Controller constraints, pedestrian clearance, compliance, access, emergency routes, and demand adaptation are discussed as practical requirements. SimTIO is described as offline simulation scenario screening, not a unified real-time controller. Time-loss changes under modified speed limits are also qualified because the desired-speed reference changes; travel time, accessibility, welfare, and safety are not established by that metric alone.

## Reproduction and author-side items

`experiment/build_current_manuscript.py` regenerates current evidence and the frozen-prefix analysis without new API calls or SUMO runs. Full experiment commands and seeds are in `experiment/SUPPLEMENT_EXPERIMENTS.md` and the protocol documents. The old main text and submission bundle are preserved locally under `paper/multra_revision_history/pre_current_20261004/`; only `generated_current`, `figures_current`, and `supplementary_current` enter the rebuilt anonymous bundle.

The author has confirmed correspondence, funding, no competing interests, CRediT, author approval, unpublished status, and Codex assistance. Code is not currently public. Journal-specific upload and self-citation anonymization requirements remain to be checked. The publisher guide again returned HTTP 403 on 2026-10-04; full guide compliance is not asserted. No external submission has been made.
