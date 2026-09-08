import asyncio
import os
import json
import time
from agents.state_refactored import AgentState
from orchestrator import MCPBridge
# 导入你之前的 planner 逻辑
from agents.llm_planner import decide_next_action

class AblationBaseline:
    def __init__(self, workspace: str):
        self.workspace = workspace
        self.bridge = MCPBridge()

    @staticmethod
    def _as_dict(res):
        if isinstance(res, dict):
            return res
        if isinstance(res, str):
            return json.loads(res)
        raise ValueError(f"Unsupported tool response type: {type(res)}")

    @staticmethod
    def _require(params, key: str):
        if key not in params:
            raise RuntimeError(f"Missing required param: {key}")
        return params[key]
        
    async def run(self, user_query: str):
        print(f"[*] [Baseline] 启动单 Agent 消融实验任务: {user_query}")
        await self.bridge.start()
        status = "ok"
        stop_reason = "unknown"
        error_message = None
        last_action = None
        try:
            # 创建状态
            state = AgentState.create(goal=user_query, workspace=self.workspace)

            # 记录 TTI (Time-to-Insight)
            start_time = time.time()

            # 模拟 batch_tti_agent.py 里的最大步数循环
            for step in range(12):
                print(f"\n[Step {step}] 决策中...")

                print(f"[Current State]: {state}")  # 这里你可以自定义状态的文本渲染方式，或者直接输出关键字段
                # decide_next_action 签名是 (llm, state)
                decision = await decide_next_action(None, state)
                usage = decision.get("token_usage") or {}
                usage_total = (decision.get("token_usage_cumulative") or {}).get("total_tokens")

                action = decision.get("action")
                params = decision.get("params")
                if not isinstance(params, dict):
                    raise RuntimeError("Planner must return object field 'params'")
                if not action:
                    raise RuntimeError("Planner must return non-empty 'action'")
                last_action = action
                state.add_record(
                    thought=str(decision.get("reason", "")),
                    action="PLAN",
                    params={"action": action, "params": params},
                    observation={"summary": (decision.get("decision") or {}).get("summary")},
                )

                print(f"[Thought]: {decision.get('decision', {}).get('summary')}")
                print(f"[Action]: {action} with {params}")
                print(
                    f"[Tokens] step={usage.get('total_tokens', 0)} "
                    f"(prompt={usage.get('prompt_tokens', 0)}, completion={usage.get('completion_tokens', 0)}), "
                    f"cumulative={usage_total if usage_total is not None else 0}"
                )

                if action == "STOP":
                    print("Agent 认为任务已完成或无法继续。")
                    stop_reason = "planner_stop"
                    break

                # 模拟执行 (这里映射到你当前的 MCP Provider)
                try:
                    if action == "BUILD_NET":
                        city_name = self._require(params, "city_name")
                        distance_miles = self._require(params, "distance_miles")
                        volume = self._require(params, "volume")
                        res = await self.bridge.acall("builder", "build_from_realworld", {
                            "city_name": city_name,
                            "distance_miles": distance_miles,
                            "volume": volume,
                            "out_dir": self.workspace
                        })
                        resj = self._as_dict(res)
                        state.artifacts.net = resj.get("net")
                        if not state.artifacts.net:
                            raise RuntimeError(f"BUILD_NET missing net artifact: {resj}")
                        print(f"[*] net ready: {state.artifacts.net}")
                        state.add_record(
                            thought="Built network from realworld",
                            action="BUILD_NET",
                            params={"city_name": city_name, "distance_miles": distance_miles, "volume": volume},
                            observation={"net": state.artifacts.net},
                        )

                    elif action == "BUILD_ROUTES":
                        if not state.artifacts.net:
                            raise RuntimeError("BUILD_ROUTES requires state.artifacts.net")

                        mode = str(self._require(params, "mode")).lower()
                        if mode == "flow":
                            from_edge = self._require(params, "from_edge")
                            to_edge = self._require(params, "to_edge")
                            vph = self._require(params, "vph")
                            res = await self.bridge.acall("demand", "generate_flow_route", {
                                "net_path": state.artifacts.net,
                                "from_edge": from_edge,
                                "to_edge": to_edge,
                                "vph": vph,
                                "route_output_path": self.workspace,
                            })
                        elif mode == "random":
                            flows = self._require(params, "flows")
                            period = self._require(params, "period")
                            res = await self.bridge.acall("demand", "build_routes_random", {
                                "net": state.artifacts.net,
                                "flows": flows,
                                "period": period,
                                "out_dir": self.workspace
                            })
                        else:
                            raise RuntimeError(f"Unsupported BUILD_ROUTES mode: {mode}")
                        resj = self._as_dict(res)
                        state.artifacts.routes = resj.get("route_path")
                        if not state.artifacts.routes:
                            raise RuntimeError(f"BUILD_ROUTES missing routes artifact: {resj}")
                        print(f"[*] routes ready: {state.artifacts.routes}")

                        # Chain-complete: always write cfg after routes.
                        cfg_res = await self.bridge.acall("demand", "write_sumocfg", {
                            "net": state.artifacts.net,
                            "routes": state.artifacts.routes,
                        })
                        cfgj = self._as_dict(cfg_res)
                        state.artifacts.cfg = cfgj.get("cfg")
                        if not state.artifacts.cfg:
                            raise RuntimeError(f"write_sumocfg missing cfg artifact: {cfgj}")
                        state.artifacts.tripinfo = os.path.join(
                            os.path.dirname(state.artifacts.cfg), "tripinfo.xml"
                        )
                        print(f"[*] cfg ready: {state.artifacts.cfg}")
                        state.add_record(
                            thought="Built routes and sumocfg",
                            action="BUILD_ROUTES",
                            params=params,
                            observation={"routes": state.artifacts.routes, "cfg": state.artifacts.cfg},
                        )

                    elif action == "MODIFY":
                        edge_id = self._require(params, "edge_id")
                        volume = params.get("volume")
                        res = await self.bridge.acall("modifier", "remove_edge", {
                            "net_path": state.artifacts.net,
                            "edge_id": edge_id,
                            "volume": volume,
                            "route_path": state.artifacts.routes,
                            "cfg_path": state.artifacts.cfg,
                            "out_dir": self.workspace
                        })
                        resj = self._as_dict(res)
                        if resj.get("status") == "error":
                            raise RuntimeError(resj.get("message") or resj)
                        arts = resj.get("artifacts") if isinstance(resj.get("artifacts"), dict) else {}
                        state.artifacts.net = arts.get("net", state.artifacts.net)
                        if arts.get("routes"):
                            state.artifacts.routes = arts.get("routes")
                        if arts.get("cfg"):
                            state.artifacts.cfg = arts.get("cfg")
                        if arts.get("tripinfo"):
                            state.artifacts.tripinfo = arts.get("tripinfo")
                        print(f"[*] modify done: net={state.artifacts.net}, routes={state.artifacts.routes}")
                        state.add_record(
                            thought="Modified network",
                            action="MODIFY",
                            params=params,
                            observation={"net": state.artifacts.net, "routes": state.artifacts.routes, "cfg": state.artifacts.cfg},
                        )

                    elif action == "RUN":
                        if not state.artifacts.cfg:
                            raise RuntimeError("RUN requires cfg artifact in strict mode")
                        gui = self._require(params, "gui")
                        run_args = {"cfg_path": state.artifacts.cfg, "gui": gui}
                        if "steps" in params:
                            run_args["step_limit"] = params["steps"]

                        run_res = await self.bridge.acall("runner", "run", run_args)
                        runj = self._as_dict(run_res)
                        state.artifacts.tripinfo = runj.get("tripinfo")
                        if not state.artifacts.tripinfo:
                            raise RuntimeError(f"RUN missing tripinfo artifact: {runj}")
                        print(f"[*] run done: tripinfo={state.artifacts.tripinfo}")
                        state.add_record(
                            thought="Simulation run completed",
                            action="RUN",
                            params=run_args,
                            observation={"tripinfo": state.artifacts.tripinfo},
                        )

                    elif action == "ANALYZE":
                        if not state.artifacts.tripinfo:
                            raise RuntimeError("ANALYZE requires tripinfo artifact in strict mode")
                        res = await self.bridge.acall("metrics", "tripinfo_mean_speed", {
                            "path": state.artifacts.tripinfo,
                            "unit": "km/h",
                        })
                        resj = self._as_dict(res)
                        print(f"[Analysis Result]: mean_speed={resj.get('value')} km/h, count={resj.get('count')}")
                        state.add_record(
                            thought="Analyzed tripinfo mean speed",
                            action="ANALYZE",
                            params={"metric": "tripinfo_mean_speed", "unit": "km/h"},
                            observation=resj,
                        )
                        print("[*] ANALYZE completed, stopping baseline loop.")
                        stop_reason = "analyze_completed"
                        break

                except Exception as e:
                    print(f"[!] 执行出错 (消融实验证据): {e}")
                    status = "error"
                    stop_reason = "step_error"
                    error_message = str(e)
                    state.add_record(
                        thought="Step execution failed",
                        action="ERROR",
                        params={"action": action, "params": params},
                        observation={"error": str(e)},
                    )
                    break

            total_time = time.time() - start_time
            print(f"[*] 实验结束。TTI: {total_time:.2f}s")
            final_usage = (state.context or {}).get("token_usage") or {}
            print(
                "[*] Token usage summary: "
                f"calls={final_usage.get('calls', 0)}, "
                f"prompt={final_usage.get('prompt_tokens', 0)}, "
                f"completion={final_usage.get('completion_tokens', 0)}, "
                f"total={final_usage.get('total_tokens', 0)}"
            )
            if stop_reason == "unknown":
                stop_reason = "max_steps_reached"
            return {
                "status": status,
                "stop_reason": stop_reason,
                "error": error_message,
                "tti_seconds": total_time,
                "last_action": last_action,
                "workspace": state.workspace,
                "artifacts": {
                    "net": state.artifacts.net,
                    "routes": state.artifacts.routes,
                    "cfg": state.artifacts.cfg,
                    "tripinfo": state.artifacts.tripinfo,
                },
                "token_usage": final_usage,
                "steps": [r.to_dict() for r in state.history],
            }
        finally:
            await self.bridge.close()

if __name__ == "__main__":
    query = "Build a 1 km network in Albany with 800 veh/hour from \"Colonie Street\" to \"Lark Street\", removing Eagle Street, then run a simulation and report the average speed."
    runs = 10
    ablation_root = os.path.abspath("./ablation")
    os.makedirs(ablation_root, exist_ok=True)

    all_results = []
    for i in range(1, runs + 1):
        run_dir = os.path.join(ablation_root, f"run_{i:02d}")
        os.makedirs(run_dir, exist_ok=True)
        print(f"\n{'=' * 20} Baseline Run {i}/{runs} {'=' * 20}")
        baseline = AblationBaseline(workspace=run_dir)
        result = asyncio.run(baseline.run(query))
        result["run_index"] = i
        all_results.append(result)

        with open(os.path.join(run_dir, "result.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    summary = {
        "query": query,
        "runs": runs,
        "ok_runs": sum(1 for r in all_results if r.get("status") == "ok"),
        "error_runs": sum(1 for r in all_results if r.get("status") == "error"),
        "results": all_results,
    }
    summary_path = os.path.join(ablation_root, "summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"\n[*] Ablation finished. Summary saved to: {summary_path}")
