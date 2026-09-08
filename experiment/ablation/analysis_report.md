# Experiment Analysis

## Per-Experiment Metrics

| file | agent | scenario | runs | success_rate | tti_mean_s | token_total_mean |
|---|---|---:|---:|---:|---:|---:|
| summary_multi_complex.json | multi | complex | 10 | 0.900 | 8.31659951210022 | 6028.7 |
| summary_single_complex.json | single | complex | 10 | 0.700 | 41.42531502246857 | 11404.7 |
| summary_multi_medium.json | multi | medium | 10 | 1.000 | 7.251017045974732 | 5310.1 |
| summary_single_medium.json | single | medium | 10 | 1.000 | 22.740278363227844 | 9690.3 |
| summary_multi_simple.json | multi | simple | 10 | 0.900 | 6.151262664794922 | 4845.5 |
| summary_single_simple.json | single | simple | 10 | 1.000 | 19.758131504058838 | 8529.2 |

## Single vs Multi (By Scenario)

| scenario | single_success | multi_success | delta_success(m-s) | single_tti | multi_tti | delta_tti | single_tok | multi_tok | delta_tok |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| complex | 0.7 | 0.9 | 0.20000000000000007 | 41.42531502246857 | 8.31659951210022 | -33.10871551036835 | 11404.7 | 6028.7 | -5376.000000000001 |
| medium | 1.0 | 1.0 | 0.0 | 22.740278363227844 | 7.251017045974732 | -15.489261317253114 | 9690.3 | 5310.1 | -4380.199999999999 |
| simple | 1.0 | 0.9 | -0.09999999999999998 | 19.758131504058838 | 6.151262664794922 | -13.606868839263916 | 8529.2 | 4845.5 | -3683.7000000000007 |

## Top Failure Reasons

### summary_multi_complex.json
- multi_error:builder:Expecting value: line 1 column 1 (char 0): 1

### summary_single_complex.json
- single_error:Missing required param: mode: 2
- single_error:Missing required param: city_name: 1

### summary_multi_medium.json
- no failures

### summary_single_medium.json
- no failures

### summary_multi_simple.json
- multi_error:builder:Expecting value: line 1 column 1 (char 0): 1

### summary_single_simple.json
- no failures
