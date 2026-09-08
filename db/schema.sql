PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  goal TEXT NOT NULL,
  context_json TEXT NOT NULL,
  constraints_json TEXT,
  created_ts REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS scenarios (
  scenario_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  parent_scenario_id TEXT,
  workspace TEXT NOT NULL,
  artifacts_json TEXT NOT NULL,
  meta_json TEXT,
  created_ts REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(task_id)
);

CREATE TABLE IF NOT EXISTS proposals (
  proposal_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  scenario_id TEXT,
  agent_id TEXT NOT NULL,
  summary TEXT NOT NULL,
  changes_json TEXT NOT NULL,
  expected_effect_json TEXT,
  risks_json TEXT,
  validation_plan_json TEXT,
  status TEXT NOT NULL,              -- proposed/accepted/rejected/merged
  created_ts REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(task_id),
  FOREIGN KEY(scenario_id) REFERENCES scenarios(scenario_id)
);

CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  scenario_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  config_json TEXT NOT NULL,
  seed INTEGER,
  status TEXT NOT NULL,              -- queued/running/done/failed
  output_dir TEXT,
  started_ts REAL,
  ended_ts REAL,
  FOREIGN KEY(task_id) REFERENCES tasks(task_id),
  FOREIGN KEY(scenario_id) REFERENCES scenarios(scenario_id)
);

CREATE TABLE IF NOT EXISTS kpi_records (
  kpi_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  kpi_json TEXT NOT NULL,
  created_ts REAL NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE TABLE IF NOT EXISTS run_results (
  result_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  scenario_id TEXT NOT NULL,
  artifacts_json TEXT NOT NULL,
  kpi_json TEXT,
  diagnosis_json TEXT,
  report TEXT,
  created_ts REAL NOT NULL,
  FOREIGN KEY(run_id) REFERENCES runs(run_id),
  FOREIGN KEY(task_id) REFERENCES tasks(task_id),
  FOREIGN KEY(scenario_id) REFERENCES scenarios(scenario_id)
);

CREATE TABLE IF NOT EXISTS steps (
  step_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  scenario_id TEXT,
  run_id TEXT,
  agent_id TEXT NOT NULL,
  idx INTEGER NOT NULL,
  thought TEXT,
  action TEXT,
  params_json TEXT,
  observation_json TEXT,
  ts REAL NOT NULL,
  FOREIGN KEY(task_id) REFERENCES tasks(task_id),
  FOREIGN KEY(scenario_id) REFERENCES scenarios(scenario_id),
  FOREIGN KEY(run_id) REFERENCES runs(run_id)
);

CREATE INDEX IF NOT EXISTS idx_steps_task_ts ON steps(task_id, ts);
CREATE INDEX IF NOT EXISTS idx_runs_task_scenario ON runs(task_id, scenario_id);
CREATE INDEX IF NOT EXISTS idx_run_results_task_ts ON run_results(task_id, created_ts);
