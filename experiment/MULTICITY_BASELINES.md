# Multi-city baselines

Generate clean baseline scenarios and run SUMO:

```bash
python experiment/create_multicity_baselines.py
```

The generator defaults to `/usr/bin/sumo` so baseline and optimizer runs use the
same SUMO build. Override it only when the whole experiment uses another binary:

```bash
python experiment/create_multicity_baselines.py --sumo-binary /path/to/sumo
```

Outputs are written under `experiment/multicity-baselines/<city>/`. The aggregate
KPI and artifact index is `baseline_manifest.json`. The JSONL file
`optimization_cases.jsonl` can be passed directly to the optimization-only runner:

```bash
python experiment/run_optimization_only.py \
  --manifest experiment/multicity-baselines/optimization_cases.jsonl \
  --optimizer all \
  --bundle-top-k 3 \
  --max-bundle-evals 3 \
  --output-dir experiment/multicity-optimization
```

The source city list is maintained in `experiment/multicity_sources.json`.
