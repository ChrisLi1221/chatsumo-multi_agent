import os
import json
import time
import uuid
import sqlite3
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field, asdict
from agents.contracts import Proposal

# ============================================================
# Multi-agent friendly state + optional local SQLite database
# ============================================================

def _uid(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"

# -----------------------------
# Core payloads (in-memory)
# -----------------------------

@dataclass
class KPI:
    mean_speed: Optional[float] = None
    mean_delay: Optional[float] = None
    # Fraction of departed vehicles that completed their trips (0..1).
    throughput: Optional[float] = None
    co2: Optional[float] = None
    completed_trips: Optional[int] = None
    departed_trips: Optional[int] = None
    unfinished_trips: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "KPI":
        return KPI(**(d or {}))


@dataclass
class Artifacts:
    """Scenario artifacts for SUMO (paths can be relative to workspace or absolute)."""
    workspace: str
    net: Optional[str] = None
    routes: Optional[str] = None
    cfg: Optional[str] = None
    tripinfo: Optional[str] = None
    add: List[str] = field(default_factory=list)

    def set(self, **kwargs) -> None:
        for k, v in kwargs.items():
            setattr(self, k, v)

    def ensure_workspace(self) -> str:
        os.makedirs(self.workspace, exist_ok=True)
        return self.workspace

    def to_dict(self) -> Dict[str, Any]:
        return {
            "workspace": self.workspace,
            "net": self.net,
            "routes": self.routes,
            "cfg": self.cfg,
            "tripinfo": self.tripinfo,
            "add": list(self.add or []),
        }

    @staticmethod
    def from_dict(d: Optional[Dict[str, Any]]) -> "Artifacts":
        d = d or {}
        return Artifacts(
            workspace=d.get("workspace", ""),
            net=d.get("net"),
            routes=d.get("routes"),
            cfg=d.get("cfg"),
            tripinfo=d.get("tripinfo"),
            add=list(d.get("add") or []),
        )


@dataclass
class StepRecord:
    """One step of an agent (thought/action/observation), suitable for DB logging."""
    thought: str
    action: str
    params: Dict[str, Any]
    observation: Any
    kpi: Optional[KPI] = None
    ts: float = field(default_factory=lambda: time.time())
    idx: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["kpi"] = self.kpi.to_dict() if self.kpi is not None else None
        return d

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "StepRecord":
        return StepRecord(
            thought=d.get("thought", ""),
            action=d.get("action", ""),
            params=d.get("params") or {},
            observation=d.get("observation"),
            kpi=KPI.from_dict(d.get("kpi")) if d.get("kpi") else None,
            ts=float(d.get("ts", time.time())),
            idx=int(d.get("idx", 0)),
        )


# -----------------------------
# Optional SQLite shared store
# -----------------------------

_SCHEMA_SQL = r"""
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
  status TEXT NOT NULL,
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
  status TEXT NOT NULL,
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
"""

class SQLiteStateStore:
    """Local shared DB (single file) for multi-agent collaboration."""

    def __init__(self, db_path: str):
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA foreign_keys=ON;")
        self.conn.row_factory = sqlite3.Row
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    # ---- create core entities ----

    def create_task(self, goal: str, context: Dict[str, Any], constraints: Optional[Dict[str, Any]] = None) -> str:
        task_id = _uid("task")
        self.conn.execute(
            "INSERT INTO tasks(task_id, goal, context_json, constraints_json, created_ts) VALUES (?,?,?,?,?)",
            (task_id, goal, json.dumps(context or {}), json.dumps(constraints or {}), time.time())
        )
        self.conn.commit()
        return task_id

    def create_scenario(
        self,
        task_id: str,
        workspace: str,
        artifacts: Dict[str, Any],
        parent_scenario_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        scenario_id = _uid("scn")
        self.conn.execute(
            "INSERT INTO scenarios(scenario_id, task_id, parent_scenario_id, workspace, artifacts_json, meta_json, created_ts) "
            "VALUES (?,?,?,?,?,?,?)",
            (scenario_id, task_id, parent_scenario_id, workspace,
             json.dumps(artifacts or {}), json.dumps(meta or {}), time.time())
        )
        self.conn.commit()
        return scenario_id

    def update_scenario_artifacts(self, scenario_id: str, artifacts: Dict[str, Any]) -> None:
        self.conn.execute(
            "UPDATE scenarios SET artifacts_json=? WHERE scenario_id=?",
            (json.dumps(artifacts or {}), scenario_id)
        )
        self.conn.commit()

    # ---- steps/events ----

    def append_step(
        self,
        task_id: str,
        agent_id: str,
        idx: int,
        thought: str,
        action: str,
        params: Dict[str, Any],
        observation: Any,
        ts: Optional[float] = None,
        scenario_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> str:
        step_id = _uid("step")
        self.conn.execute(
            "INSERT INTO steps(step_id, task_id, scenario_id, run_id, agent_id, idx, thought, action, params_json, observation_json, ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (step_id, task_id, scenario_id, run_id, agent_id, idx,
             thought, action, json.dumps(params or {}), json.dumps(observation), float(ts or time.time()))
        )
        self.conn.commit()
        return step_id

    # ---- proposals ----

    def create_proposal(
        self,
        task_id: str,
        agent_id: str,
        summary: str,
        changes: List[Dict[str, Any]],
        scenario_id: Optional[str] = None,
        expected_effect: Optional[Dict[str, Any]] = None,
        risks: Optional[List[str]] = None,
        validation_plan: Optional[Dict[str, Any]] = None,
        status: str = "proposed",
    ) -> str:
        proposal_id = _uid("prop")
        self.conn.execute(
            "INSERT INTO proposals(proposal_id, task_id, scenario_id, agent_id, summary, changes_json, expected_effect_json, risks_json, validation_plan_json, status, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (proposal_id, task_id, scenario_id, agent_id, summary,
             json.dumps(changes or []),
             json.dumps(expected_effect or {}),
             json.dumps(risks or []),
             json.dumps(validation_plan or {}),
             status,
             time.time())
        )
        self.conn.commit()
        return proposal_id

    # ---- runs + KPI ----

    def create_run(
        self,
        task_id: str,
        scenario_id: str,
        agent_id: str,
        config: Dict[str, Any],
        seed: Optional[int] = None,
        status: str = "queued",
        output_dir: Optional[str] = None,
    ) -> str:
        run_id = _uid("run")
        started_ts = time.time() if status == "running" else None
        self.conn.execute(
            "INSERT INTO runs(run_id, task_id, scenario_id, agent_id, config_json, seed, status, output_dir, started_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (run_id, task_id, scenario_id, agent_id, json.dumps(config or {}),
             seed, status, output_dir, started_ts)
        )
        self.conn.commit()
        return run_id

    def finish_run(self, run_id: str, status: str, output_dir: Optional[str] = None) -> None:
        self.conn.execute(
            "UPDATE runs SET status=?, output_dir=COALESCE(?, output_dir), ended_ts=? WHERE run_id=?",
            (status, output_dir, time.time(), run_id)
        )
        self.conn.commit()

    def write_kpi(self, run_id: str, kpi: Dict[str, Any]) -> str:
        kpi_id = _uid("kpi")
        self.conn.execute(
            "INSERT INTO kpi_records(kpi_id, run_id, kpi_json, created_ts) VALUES (?,?,?,?)",
            (kpi_id, run_id, json.dumps(kpi or {}), time.time())
        )
        self.conn.commit()
        return kpi_id

    def fetch_latest_kpi_for_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT k.kpi_json FROM kpi_records k JOIN runs r ON k.run_id=r.run_id WHERE r.task_id=? ORDER BY k.created_ts DESC LIMIT 1",
            (task_id,)
        ).fetchone()
        if not row:
            return None
        return json.loads(row[0])

    def write_run_result(
        self,
        run_id: str,
        task_id: str,
        scenario_id: str,
        artifacts: Dict[str, Any],
        kpi: Optional[Dict[str, Any]] = None,
        diagnosis: Optional[Dict[str, Any]] = None,
        report: Optional[str] = None,
    ) -> str:
        result_id = _uid("res")
        self.conn.execute(
            "INSERT INTO run_results(result_id, run_id, task_id, scenario_id, artifacts_json, kpi_json, diagnosis_json, report, created_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                result_id,
                run_id,
                task_id,
                scenario_id,
                json.dumps(artifacts or {}),
                json.dumps(kpi or {}),
                json.dumps(diagnosis or {}),
                report,
                time.time(),
            ),
        )
        self.conn.commit()
        return result_id

    def update_latest_run_result_report(self, task_id: str, report: str) -> None:
        row = self.conn.execute(
            "SELECT result_id FROM run_results WHERE task_id=? ORDER BY created_ts DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if not row:
            return
        self.conn.execute(
            "UPDATE run_results SET report=? WHERE result_id=?",
            (report, row["result_id"]),
        )
        self.conn.commit()

    def fetch_recent_run_results(self, task_id: str, limit: int = 2) -> List[Dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT rr.*, r.config_json, r.status, r.output_dir, r.started_ts, r.ended_ts "
            "FROM run_results rr JOIN runs r ON rr.run_id=r.run_id "
            "WHERE rr.task_id=? ORDER BY rr.created_ts DESC LIMIT ?",
            (task_id, int(limit)),
        ).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            out.append({
                "result_id": row["result_id"],
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "scenario_id": row["scenario_id"],
                "artifacts": json.loads(row["artifacts_json"] or "{}"),
                "kpi": json.loads(row["kpi_json"] or "{}"),
                "diagnosis": json.loads(row["diagnosis_json"] or "{}"),
                "report": row["report"],
                "config": json.loads(row["config_json"] or "{}"),
                "status": row["status"],
                "output_dir": row["output_dir"],
                "started_ts": row["started_ts"],
                "ended_ts": row["ended_ts"],
                "created_ts": row["created_ts"],
            })
        return out

# -----------------------------
# Agent runtime state (backward compatible)
# -----------------------------

@dataclass
class AgentState:
    """Runtime state with optional DB persistence for multi-agent.

    Backward compatible with your original API:
      - AgentState.create(goal, workspace)
      - state.add_record(...)
      - state.set_artifact(...)
      - state.get_last_kpi()

    Multi-agent additions:
      - attach_store(db_path, agent_id, task_id?, scenario_id?)
      - fork_scenario(new_workspace, meta?)
      - record_proposal(...)
      - record_run_start/finish + record_kpi(...)
    """

    goal: str
    workspace: str
    artifacts: Artifacts

    history: List[StepRecord] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)      # osm_path / targets etc.
    constraints: Dict[str, Any] = field(default_factory=dict)  # invariants / bounds etc.

    step_counter: int = 0
    last_kpi: Optional[KPI] = None

    # runtime-control flags
    pending_modification: bool = False
    last_mod_op: Optional[str] = None
    last_mod_target: Optional[str] = None

    # shared/multi-agent IDs
    agent_id: str = "single_agent"
    task_id: Optional[str] = None
    scenario_id: Optional[str] = None
    run_id: Optional[str] = None
    parent_scenario_id: Optional[str] = None

    # optional persistence
    store: Optional[SQLiteStateStore] = None
    persist_history: bool = True

    @staticmethod
    def create(goal: str, workspace: str) -> "AgentState":
        artifacts = Artifacts(workspace=os.path.abspath(workspace))
        artifacts.ensure_workspace()
        return AgentState(goal=goal, workspace=artifacts.workspace, artifacts=artifacts)

    # bookkeeping
    def next_step_id(self) -> int:
        self.step_counter += 1
        return self.step_counter

    # ---- DB attach / scenario versioning ----

    def attach_store(
        self,
        db_path: str,
        agent_id: str = "orchestrator",
        task_id: Optional[str] = None,
        scenario_id: Optional[str] = None,
        constraints: Optional[Dict[str, Any]] = None,
        scenario_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, str]:
        """Attach SQLite store; create task/scenario if absent. Returns (task_id, scenario_id)."""
        self.store = SQLiteStateStore(db_path)
        self.agent_id = agent_id
        if constraints is not None:
            self.constraints = constraints

        if task_id is not None:
            self.task_id = task_id
        if self.task_id is None:
            self.task_id = self.store.create_task(self.goal, self.context, self.constraints)

        if scenario_id is not None:
            self.scenario_id = scenario_id
        if self.scenario_id is None:
            self.scenario_id = self.store.create_scenario(
                task_id=self.task_id,
                workspace=self.workspace,
                artifacts=self.artifacts.to_dict(),
                parent_scenario_id=self.parent_scenario_id,
                meta=scenario_meta or {"note": "init from AgentState"},
            )

        return self.task_id, self.scenario_id

    def fork_scenario(self, new_workspace: str, meta: Optional[Dict[str, Any]] = None) -> str:
        """Create a new scenario version (child of current scenario) and switch state to it."""
        if self.store is None or self.task_id is None or self.scenario_id is None:
            raise RuntimeError("fork_scenario requires an attached store with task_id and scenario_id")

        new_workspace = os.path.abspath(new_workspace)
        os.makedirs(new_workspace, exist_ok=True)

        # inherit current artifacts but swap workspace
        new_art = self.artifacts.to_dict()
        new_art["workspace"] = new_workspace

        child_id = self.store.create_scenario(
            task_id=self.task_id,
            workspace=new_workspace,
            artifacts=new_art,
            parent_scenario_id=self.scenario_id,
            meta=meta or {"note": "forked scenario"},
        )
        self.parent_scenario_id = self.scenario_id
        self.scenario_id = child_id
        self.workspace = new_workspace
        self.artifacts = Artifacts.from_dict(new_art)

        # record a step for auditability
        self.add_record(
            thought="Forked scenario version",
            action="FORK_SCENARIO",
            params={"parent_scenario_id": self.parent_scenario_id, "child_scenario_id": child_id, "workspace": new_workspace},
            observation={"ok": True},
        )
        return child_id

    # ---- core APIs (compatible) ----

    def add_record(
        self,
        thought: str,
        action: str,
        params: Dict[str, Any],
        observation: Any,
        kpi: Optional[KPI] = None,
        scenario_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> StepRecord:
        """
        Add a step record to in-memory history AND (optionally) persist to SQLite.

        Compatible with orchestrator._log_step(...).
        """
        self.step_counter += 1
        rec = StepRecord(
            thought=thought,
            action=action,
            params=params or {},
            observation=observation,
            kpi=kpi,
            ts=time.time(),
            idx=self.step_counter,
        )
        self.history.append(rec)

        # update last KPI
        if kpi is not None:
            self.last_kpi = kpi

        # persist to DB if attached
        if self.persist_history and self.store is not None and self.task_id is not None:
            self.store.append_step(
                task_id=self.task_id,
                agent_id=self.agent_id,
                idx=rec.idx,
                thought=rec.thought,
                action=rec.action,
                params=rec.params,
                observation={
                    "observation": rec.observation,
                    "kpi": rec.kpi.to_dict() if rec.kpi else None,
                },
                ts=rec.ts,
                scenario_id=scenario_id or self.scenario_id,
                run_id=run_id or self.run_id,
            )

        return rec


    def set_artifact(self, **kwargs) -> None:
        self.artifacts.set(**kwargs)
        # keep scenario artifacts up-to-date in DB
        if self.store is not None and self.scenario_id is not None:
            self.store.update_scenario_artifacts(self.scenario_id, self.artifacts.to_dict())

    def get_last_kpi(self) -> Optional[KPI]:
        # if DB attached, you can optionally pull latest KPI from DB for robustness
        if self.last_kpi is not None:
            return self.last_kpi
        if self.store is not None and self.task_id is not None:
            d = self.store.fetch_latest_kpi_for_task(self.task_id)
            if d:
                self.last_kpi = KPI.from_dict(d)
        return self.last_kpi

    # ---- multi-agent convenience ----

    def record_proposal(self, proposal: Proposal, status: str = "proposed") -> Optional[str]:
        if self.store is None or self.task_id is None:
            return None
        prop_id = self.store.create_proposal(
            task_id=self.task_id,
            scenario_id=self.scenario_id,
            agent_id=proposal.agent_id,
            summary=proposal.summary,
            changes=proposal.changes,
            expected_effect=proposal.expected_effect,
            risks=proposal.risks,
            validation_plan=proposal.validation_plan,
            status=status,
        )
        self.add_record(
            thought=f"Recorded proposal {prop_id}",
            action="PROPOSAL",
            params={"proposal_id": prop_id, "status": status},
            observation=proposal.to_dict(),
        )
        return prop_id

    def record_run_start(self, config: Dict[str, Any], seed: Optional[int] = None, output_dir: Optional[str] = None) -> Optional[str]:
        if self.store is None or self.task_id is None or self.scenario_id is None:
            return None
        self.run_id = self.store.create_run(
            task_id=self.task_id,
            scenario_id=self.scenario_id,
            agent_id=self.agent_id,
            config=config,
            seed=seed,
            status="running",
            output_dir=output_dir,
        )
        self.add_record(
            thought="Started simulation run",
            action="RUN_START",
            params={"run_id": self.run_id, "seed": seed, "output_dir": output_dir},
            observation={"config": config},
        )
        return self.run_id

    def record_run_finish(self, status: str, output_dir: Optional[str] = None, error: Optional[str] = None) -> None:
        if self.store is not None and self.run_id is not None:
            self.store.finish_run(self.run_id, status=status, output_dir=output_dir)
        self.add_record(
            thought="Finished simulation run",
            action="RUN_FINISH",
            params={"run_id": self.run_id, "status": status, "output_dir": output_dir},
            observation={"error": error} if error else {"ok": True},
        )
        if status != "running":
            self.run_id = None

    def record_kpi(self, kpi: KPI) -> None:
        self.last_kpi = kpi
        if self.store is not None and self.run_id is not None:
            self.store.write_kpi(self.run_id, kpi.to_dict())
        self.add_record(
            thought="Recorded KPI",
            action="KPI",
            params={"run_id": self.run_id},
            observation=kpi.to_dict(),
            kpi=kpi,
        )
