# 🧠 ChatSUMO: A Conversational Traffic Simulation Agent

**ChatSUMO** is an intelligent conversational agent that can **generate, modify, run, and analyze SUMO traffic simulations** through natural language.  
It bridges the gap between **transportation researchers** and **complex microscopic simulators** by combining **LLM reasoning**, **automated SUMO pipelines**, and **interactive visualization**.

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
