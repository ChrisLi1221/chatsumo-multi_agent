# 🧠 ChatSUMO: A Conversational Traffic Simulation Agent

**ChatSUMO** is an intelligent conversational agent that can **generate, modify, run, and analyze SUMO traffic simulations** through natural language.  
It bridges the gap between **transportation researchers** and **complex microscopic simulators** by combining **LLM reasoning**, **automated SUMO pipelines**, and **interactive visualization**.

## 仓库导航

本仓库包含 ChatSUMO 多智能体编排、SimTIO 优化实验和演示材料。

| 目录 / 入口 | 内容 |
|---|---|
| `orchestrator.py` / `orchestrator_batch.py` | 单任务与批量多智能体编排 |
| `agents/` | 规划、候选选择、联合优化和交互反馈 |
| `providers/` | SUMO 网络、需求、修改、执行和指标工具 |
| [优化实验入口](experiment/OPTIMIZATION_ONLY.md) | 基于已有 SUMO 场景的实验命令 |
| [宽搜索协议](experiment/WIDE_FEEDBACK_V1.md) | 5 个共同初始方案、每父方案 2 个不同子代、15 次候选预算 |
| [交互反馈协议](experiment/INTERACTION_FEEDBACK_V1.md) | 组件单独与联合仿真、约束及路段副作用 |
| [完整反馈协议](experiment/FULL_FEEDBACK_V1.md) | 从共同初始动作生成反馈、mutation 和联合优化 |
| [联合反馈续跑](experiment/JOINT_FEEDBACK_CONTINUATION.md) | 联合反馈、单组件修改与完整重仿真 |
| [补充实验](experiment/SUPPLEMENT_EXPERIMENTS.md) | 稳定性、需求敏感性和复核命令 |
| [演示材料](presentation/itsc_draft/README.md) | 幻灯片、中文讲稿和生成脚本 |
| `tests/` | 回归测试：在仓库根目录运行 `python -m pytest -q` |

### Git 收录范围

提交源码、测试、配置示例、实验协议、演示材料及实验目录中的汇总证据。
`.env`、仿真原始输出、缓存、运行时数据库、LaTeX 中间文件、重复 ZIP 包、
整个 `paper/` 论文目录由
[`.gitignore`](.gitignore) 排除；本地文件仍保留。
实验输出和原始场景不随 Git 分发，部分重建脚本需要先生成或准备相应输入；
具体路径和命令见各实验协议。论文构建脚本还需要本地 `paper/` 资料。

---

## 🚗 Overview

ChatSUMO acts as an **LLM-powered interface** to the **Simulation of Urban Mobility (SUMO)** platform.  
It can:
- Build networks (from text, OSM data, or templates),
- Modify simulation configurations,
- Run and monitor SUMO/TraCI executions,
- Visualize results (network plots, flow analysis, KPIs),
- Explain simulation outcomes in natural language.

---

## 🧩 System Architecture

```bash
chat_sumo/
├── app_chat.py # Streamlit front-end: chat UI, visual outputs, agent loop
├── providers/
│ ├── provider_sumo_builder.py # Build SUMO networks (from OSM or text)
│ ├── provider_sumo_runner.py # Run SUMO or SUMO-GUI with clean MCP interface
│ ├── provider_sumo_analyzer.py # Parse tripinfo.xml, edgeData.xml, and compute KPIs
│ └── provider_sumo_modifier.py # Modify routes, edge attributes, traffic lights, etc.
├── runs/ # Session logs, network files, artifacts
│ └── session_chat/
│ ├── net.net.xml
│ ├── cfg.sumocfg
│ ├── tripinfo.xml
│ └── sumo.run.log
├── assets/ # UI images, icons
├── README.md # (this file)
└── requirements.txt
```

### 🧠 Core Components
| Module | Purpose |
|---------|----------|
| **Streamlit UI (`app_chat.py`)** | Provides a chat-based interface. Each LLM “turn” calls FastMCP tools and displays text, logs, and network previews. |
| **FastMCP Framework** | Handles multi-turn planning, tool invocation, and structured context management between LLM and simulation environment. |
| **SUMO Provider Suite** | Python MCP tools wrapping SUMO binaries (`sumo`, `sumo-gui`), OSM import, configuration handling, and result parsing. |
| **Visualization** | `matplotlib` + `sumolib` used to render networks and results inline in the chat. |
| **LLM Backend** | Supports DeepSeek-70B, Qwen, Llama-3, or any OpenAI-compatible endpoint. |
| **Workspace** | All runs and generated files (e.g., tripinfo, cfg, network) stored per session under `/runs/session_chat`. |

---

## 🖥️ User Interaction Flow

1. **User prompt →** “Generate a roundabout network with two entries.”  
2. **LLM planner →** Chooses `sumo_builder` tool, creates `.net.xml` and `.sumocfg`.  
3. **System →** Displays the generated network preview.  
4. **User →** “Run for 1800 seconds and report total emissions.”  
5. **Agent →** Executes `sumo_runner`, collects `tripinfo.xml`.  
6. **Analyzer →** Parses metrics (mean speed, travel time, CO₂, delay).  
7. **Chat →** Displays results summary and log tail.  
8. **Agent →** Asks: “What would you like to do next?”  

---

## 🧱 Dependencies

The experiment scripts and LLM agents automatically read `OPENAI_API_KEY` from
`.env` in the project root. Copy `.env.example` to `.env` and set your key once;
subsequent runs need no manual export. An existing nonempty `OPENAI_API_KEY`
environment variable takes priority. The local `.env` file is ignored by Git.

| Library | Purpose |
|----------|----------|
| `sumolib`, `traci` | SUMO network and runtime interaction |
| `matplotlib`, `pandas`, `xml.etree` | Visualization and XML parsing |
| `streamlit` | Chat-based user interface |
| `fastmcp` | Multi-tool conversational agent framework |
| `anyio`, `exceptiongroup` | Async orchestration and error handling |
| `deepseek`, `llama`, `mistral`, etc. | Supported LLMs via API endpoints |
| `geopy` | Reverse geocoding for OSM-based networks |

To install:
```bash
pip install -r requirements.txt
```
---

## 📊 Current Features

✅ Text-to-Simulation generation via SUMO

✅ Configurable network generation from OSM or templates

✅ Automated simulation runs (headless or GUI)

✅ Inline network visualization in Streamlit

✅ Tripinfo parsing and KPI summarization

✅ Log inspection (sumo.run.log)

✅ Post-run conversation (“What would you like to do next?”)

---
## 🚧 Work in Progress / Future Additions

🔹 Core Enhancements

 - LLM routing planner: interpret traffic-control commands (“add signal at intersection A”).

 - Network diff visualization: highlight changes between iterations.

 - Error recovery for failed builds (missing OSM, geocode failure).

 - aching / memoization to reuse generated networks.

🔹 CARLA Integration (Next Phase)

 - Create a provider_carla_runner.py for CARLA simulation (spawn, tick, record).

 - Shared API schema: unify SUMO ↔ CARLA tools under sim_provider interface.

 - Capture RGB camera frames or top-down views as chat images.

 - Integrate synchronous step-loop between LLM planner and CARLA BehaviorAgent.

🔹 LLM Optimization

 - Introduce lightweight planner (e.g., Mistral-7B) for deterministic steps.

 - Use DeepSeek-70B or GPT-5 only for high-level reasoning.

 - Add structured memory (state.last_cfg_path, last_net_path) to persist context.

🔹 UI / Streamlit

 - Live simulation progress bar / tick count.

 - Interactive map (zoomable via Folium or OpenStreetMap).

 - Command buttons for common actions (Build / Run / Analyze / Modify).

## 🛠️ Example Usage
Start the Streamlit interface:
```bash
streamlit run app_chat.py
```
Example prompts:

> “Generate a four-lane arterial with one intersection.”
> 
> “Run for 900 seconds and report mean speed.”
> 
> “Increase flow on route A by 20%.”
> 
> “Show me the tripinfo summary.”
> 
> “What should I do next?”

---

## 📂 Output Artifacts

| File | Description |
|-------|--------------|
| `net.net.xml` | SUMO network definition |
| `cfg.sumocfg` | Simulation configuration file |
| `tripinfo.xml` | Vehicle-level travel statistics |
| `sumo.run.log` | Log of each SUMO execution |
| `net_preview.png` | Rendered visualization of network topology |

---

## 🧩 Future Integration Targets

| Direction | Goal |
|------------|------|
| **CARLA** | Integrate LLM planner into CARLA environment for AV agents |
| **OSMnx / Overpass API** | Smarter city-scale network extraction |
| **MCP-based Multi-Agent Planner** | Multiple LLM sub-agents (Planner / Runner / Analyst) |
| **LoRA / Domain fine-tuning** | Train small models on SUMO command schemas |
| **RL-based Decision Layer** | Reinforcement feedback from simulation results |

---

## 🧰 Maintainers
- **Chris Li** – Transportation Systems Engineering, RPI  
- **Research Advisor:** Dr. Ruimin Ke  
- Contact: *[your email]*
