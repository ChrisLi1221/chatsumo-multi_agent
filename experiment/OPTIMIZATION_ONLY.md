# Optimization-only experiment runner

`run_optimization_only.py` starts from an existing SUMO `.sumocfg` and runs only:

```text
baseline simulation -> diagnosis/subgraph -> proposal generation/validation
-> one isolated SUMO run per valid proposal -> optional feedback-guided beam refinement
-> compatible pair expansion
-> incremental bundle validation/SUMO evaluation -> weighted-KPI ranking
```

It does not run scenario generation, the conversational planner, repair loops,
the analyst agent, or SQLite persistence.

## One scenario

```bash
python experiment/run_optimization_only.py \
  --cfg ablation/run_01/sim.sumocfg \
  --case-id albany \
  --goal "reduce traffic delay" \
  --profile delay \
  --output-dir experiment/optimization-only
```

To execute and evaluate only one specialist's proposal in SUMO, use
`--optimizer signal`, `--optimizer network`, or `--optimizer routing`. For
example:

```bash
python experiment/run_optimization_only.py \
  --cfg runs/scn_round_1_1784196587/sim.sumocfg \
  --case-id albany_routing_only \
  --goal "reduce traffic delay by jointly rerouting around up to three bottlenecks" \
  --profile delay \
  --optimizer routing \
  --output-dir experiment/optimization-only
```

The default is `--optimizer all`, which generates all three proposals and runs
each valid proposal independently. It then keeps the measured Top-K singles,
generates compatible two-action bundles, evaluates at most three bundles, and
selects the lowest measured KPI score across the baseline/no-op, singles, and
bundles. If no intervention strictly beats the baseline, the original scenario
is retained.
The LLM judge is retained as advisory metadata and no longer controls the
experimental winner. A single-optimizer run still performs capability and
safety validation; an invalid proposal is not forced into SUMO.

## Random initialization and two-round simulation feedback

The primary hybrid mode uses grounded random only for initialization.  With
three initial proposals it samples one validator-confirmed signal, network, and
routing candidate from the baseline-frozen objective-edge pools using the
recorded case seed. Invalid reverse-edge operations, unsafe closures,
non-optimizable TLS IDs, and malformed controls are removed before sampling. The
first three candidates are exactly the same prefix used by the grounded-random
baseline, making the feedback comparison paired.  The runner
then keeps a feasible-first, role-diverse beam and sends each parent's measured
KPIs, Top-10 result, and failed constraints back to its own specialist. The
orchestrator enumerates unseen, validator-confirmed one-field mutations; the LLM
selects one `mutation_id` from this finite catalog. A missing or invented ID gets
one LLM repair attempt, while duplicates never enter SUMO.

```bash
python experiment/run_optimization_only.py \
  --manifest experiment/proposal-source-primary-cases.jsonl \
  --optimizer all \
  --proposal-source random_initial_llm \
  --objective-scope top10 \
  --search-rounds 2 \
  --feedback-beam-width 2 \
  --sumo-budget 6 \
  --bundle-top-k 4 \
  --max-bundle-evals 1 \
  --output-dir experiment/feedback-beam-search
```

With this budget, three initial singles and up to two feedback refinements leave
one SUMO evaluation for a compatible bundle. If a legal role pool is exhausted,
a bundle is unavailable, or a candidate fails before consuming SUMO, the
corresponding candidate budget remains unused. Every feedback update is selected
by the responsible LLM specialist from the finite catalog. Feedback search applies to `--proposal-source llm` and
`--proposal-source random_initial_llm`; other proposal sources retain their
one-shot behavior.
`03_debate.json` records parents, feedback, mutations, and validation outcomes.
`04_candidates.json` additionally stores a `best_so_far_trace` indexed by
candidate SUMO evaluations.

For the budget-matched comparison, all three methods can now be run in one
invocation.  When `--proposal-attempt-budget` is omitted, the hybrid method
automatically receives three initial proposals while one-shot baselines receive
six; every method still has the same six-evaluation SUMO cap.

```bash
python experiment/run_proposal_source_comparison.py \
  --manifest experiment/equal-scale-baselines-seeded/optimization_cases.jsonl \
  --methods random_initial_llm,grounded_random,deterministic_heuristic \
  --optimizer all \
  --objective-scope top10 \
  --search-rounds 2 \
  --feedback-beam-width 2 \
  --sumo-budget 6 \
  --bundle-top-k 4 \
  --max-bundle-evals 1 \
  --output-dir experiment/random-initial-feedback-comparison
```

The network specialist can propose a conservative `set_corridor_speed` action
for one evidenced road corridor, or fall back to the more disruptive
`remove_edge` action when a safe closure is supported by the diagnosis.

The routing specialist can jointly soft-avoid one to three distinct evidenced
bottleneck edges. Its selected fraction is sampled once from the union of
vehicles whose reference route uses any target edge, so a vehicle crossing two
selected edges is not double-counted. A routing result that changes zero routes
is rejected instead of being simulated as a routing no-op. Network, route, and
configuration paths are always rebound to runner-owned artifacts; model-supplied
paths are treated as untrusted text.

For a controlled one-edge versus three-edge ablation, generate the ordered edge
list once and evaluate nested prefixes in the same run:

```bash
python experiment/run_optimization_only.py \
  --manifest experiment/equal-scale-baselines/optimization_cases.jsonl \
  --optimizer routing \
  --routing-edge-counts 1,3 \
  --routing-reroute-fraction 0.5 \
  --max-bundle-evals 0 \
  --output-dir experiment/routing-edge-count-ablation
```

Both variants reuse the same LLM output and fixed reroute fraction. The
one-edge variant uses the first proposed edge; the three-edge variant uses the
first three proposed edges. The exact controls and expanded proposals are saved
in `00_metadata.json`, `03_debate.json`, and `04_candidates.json`.

Each run produces:

```text
00_metadata.json
01_baseline.json
02_diagnosis.json
03_debate.json
04_candidates.json
04_modification.json
05_optimized.json
06_comparison.json
```

Candidate-specific modification, simulation, and comparison files are stored
under `candidates/<proposal-id>/`. `04_modification.json`, `05_optimized.json`,
and `06_comparison.json` remain the compatibility view of the candidate with
the lowest final weighted KPI objective. Bundle steps and their incremental
validation records are stored under `bundles/<bundle-id>/step_*`. The original
network and route files are not modified.

The initial bundle search is deliberately conservative:

- bundle size is fixed at two actions;
- at most one topology, speed, routing, and signal action is allowed;
- actions run in topology -> speed -> routing -> signal order;
- deleted edges cannot also be speed-modified or redundantly avoided;
- every step is revalidated against artifacts produced by the previous step.

Use `--bundle-top-k 3` to control the single-action beam and
`--max-bundle-evals 3` to cap extra SUMO runs. Set
`--max-bundle-evals 0` to reproduce single-action-only selection.

Expected effects are normalized to the enums `improve`, `unchanged`, `worsen`,
and `unknown`; wording such as `reduced` and `decrease` therefore receives the
same proposal-stage interpretation. Final selection uses measured SUMO KPIs:

```text
J = w_delay * delay_norm + w_co2 * co2_norm
    - w_throughput * throughput_norm - w_speed * speed_norm
```

Lower is better. A route modification that changes the source demand or an
edge-level OD pair is rejected; throughput remains measured against the original
baseline demand.

## Repeated and batch experiments

Use `--runs 10` for ten independent LLM repetitions. For multiple scenarios,
create a JSONL manifest:

```jsonl
{"id":"albany_delay","cfg":"ablation/run_01/sim.sumocfg","goal":"reduce traffic delay","profile":"delay"}
{"id":"albany_emissions","cfg":"ablation/run_01/sim.sumocfg","goal":"reduce CO2 emissions","profile":"emissions"}
```

Then run:

```bash
python experiment/run_optimization_only.py \
  --manifest experiment/optimization_cases.jsonl \
  --runs 10 \
  --output-dir experiment/optimization-only
```

The root `run_manifest.json` is updated after every repetition, including failed
runs. This makes interrupted batches resumable at the data-analysis level and
preserves failure evidence.

## Main experimental controls

- `--top-n`: number of diagnostic edges retained.
- `--subgraph-seed-n`: number of seed bottlenecks.
- `--subgraph-hops`: local evidence radius.
- `--subgraph-max-edges`: context-size guard.
- `--profile`: objective weight profile.
- `--optimizer`: `all`, `signal`, `network`, or `routing`.
- `--search-rounds`: total proposal/evaluation rounds; use two to enable one feedback update.
- `--feedback-beam-width`: number of measured parents refined per feedback round.
- `--bundle-top-k`: number of measured single proposals eligible for pair expansion.
- `--max-bundle-evals`: maximum pair bundles simulated per run; zero disables composition.
- `--runs`: independent repetitions per case.

The Python version, platform, objective weights, case definition, and these
parameters are recorded in `00_metadata.json` for every run.
