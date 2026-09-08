# Multi-Agent Role Cost Analysis

## Overall Role Token Share

| Role | Total Tokens | Calls | Share |
|---|---:|---:|---:|
| planner | 39294 | 30 | 0.243 |
| demand | 30200 | 28 | 0.187 |
| analyst | 29883 | 28 | 0.185 |
| runner_planner | 28689 | 28 | 0.177 |
| builder | 25158 | 30 | 0.155 |
| modifier | 8619 | 9 | 0.053 |

## summary_multi_complex.json

- runs=10, ok=9, error=1, mean_total_tokens_per_run=6028.7

| Role | Total Tokens | Calls | Mean Tokens/Run | Share |
|---|---:|---:|---:|---:|
| planner | 13996 | 10 | 1399.6 | 0.232 |
| demand | 10513 | 9 | 1168.1 | 0.174 |
| analyst | 9518 | 9 | 1057.6 | 0.158 |
| runner_planner | 9030 | 9 | 1003.3 | 0.150 |
| modifier | 8619 | 9 | 957.7 | 0.143 |
| builder | 8611 | 10 | 861.1 | 0.143 |

## summary_multi_medium.json

- runs=10, ok=10, error=0, mean_total_tokens_per_run=5310.1

| Role | Total Tokens | Calls | Mean Tokens/Run | Share |
|---|---:|---:|---:|---:|
| planner | 12922 | 10 | 1292.2 | 0.243 |
| demand | 10754 | 10 | 1075.4 | 0.203 |
| analyst | 10681 | 10 | 1068.1 | 0.201 |
| runner_planner | 10274 | 10 | 1027.4 | 0.193 |
| builder | 8470 | 10 | 847.0 | 0.160 |

## summary_multi_simple.json

- runs=10, ok=9, error=1, mean_total_tokens_per_run=4845.5

| Role | Total Tokens | Calls | Mean Tokens/Run | Share |
|---|---:|---:|---:|---:|
| planner | 12376 | 10 | 1237.6 | 0.255 |
| analyst | 9684 | 9 | 1076.0 | 0.200 |
| runner_planner | 9385 | 9 | 1042.8 | 0.194 |
| demand | 8933 | 9 | 992.6 | 0.184 |
| builder | 8077 | 10 | 807.7 | 0.167 |

## Scenario-Role Matrix (Total Tokens)

| Scenario | analyst | builder | demand | modifier | planner | runner_planner |
|---|---:|---:|---:|---:|---:|---:|
| complex | 9518 | 8611 | 10513 | 8619 | 13996 | 9030 |
| medium | 10681 | 8470 | 10754 | 0 | 12922 | 10274 |
| simple | 9684 | 8077 | 8933 | 0 | 12376 | 9385 |
