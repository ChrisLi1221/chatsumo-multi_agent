# Equal-scale five-city baselines

These scenarios provide a controlled cross-city test set for the LLM optimizer.
All cities use the same demand-generation settings; only the network topology
changes.

## Controlled settings

- Cities: Albany, Troy, Ann Arbor, Los Angeles, and Manhattan
- Network size: approximately 218--234 lane-km after cropping and cleanup
- Network cleanup: retain the largest component and join junction fragments
  within 10 m
- Demand: 1,200 validated passenger OD trips
- Departure window: 0--3,600 s, one vehicle every 3 s
- Random seed: 42 for demand generation and SUMO
- Endpoint sampling: weighted by edge length multiplied by lane count
- Demand representation: adaptive `<trip from="..." to="...">` records
- Measurements: tripinfo, emissions, and 60 s edgeData
- Acceptance checks: all 1,200 trips complete and no vehicle teleports occur

## Regeneration

```bash
python experiment/generate_equal_scale_medium.py
python experiment/create_equal_scale_baselines.py
```

The generated scenarios are under `experiment/equal-scale-baselines/<city>/`.
The machine-readable summaries are `baseline_manifest.json` and
`optimization_cases.jsonl`.

## Multi-seed robustness runs

Generate independent OD demand and matching SUMO configurations for several
seeds in one command:

```bash
python experiment/create_equal_scale_baselines.py \
  --trips 1200 \
  --seeds 42,43,44 \
  --output-dir experiment/equal-scale-baselines-seeded
```

Each seed is isolated under `seed_<seed>/`. The combined root
`optimization_cases.jsonl` contains one uniquely named case per city and seed,
so the optimizer should use `--runs 1`; all candidates within a case reuse the
same demand and remain directly comparable.

## Current baseline results

| City | Completed | Teleports | Mean delay (s) | Mean duration (s) | Mean waiting (s) |
|---|---:|---:|---:|---:|---:|
| Albany | 1,200/1,200 | 0 | 108.68 | 249.37 | 76.01 |
| Troy | 1,200/1,200 | 0 | 97.22 | 270.80 | 64.49 |
| Ann Arbor | 1,200/1,200 | 0 | 130.53 | 240.59 | 91.60 |
| Los Angeles | 1,200/1,200 | 0 | 165.87 | 235.34 | 117.81 |
| Manhattan | 1,200/1,200 | 0 | 193.41 | 365.96 | 139.78 |
