# Reference expansion — 4 October 2026

The active bibliography increases from 17 to 28 entries: ten additional research/survey papers and one FHWA technical report. All 28 entries are cited in the manuscript; none was added using `nocite`. Existing anonymous self-references remain anonymous and now use the same numeric citation format as other entries.

## Added sources and use

| Key | Verified publication and primary source | Manuscript use |
|---|---|---|
| osorio2013simulation | Osorio & Bierlaire (2013), *A Simulation-Based Optimization Framework for Urban Transportation Problems*, Operations Research 61(6), 1333–1345. [Author-held published PDF](https://web.mit.edu/osorioc/www/papers/osoBie13SO.pdf), DOI 10.1287/opre.2013.1226. | §2: established metamodel-based traffic optimization under limited simulation budgets. |
| osorio2015large | Osorio & Chong (2015), *A Computationally Efficient Simulation-Based Optimization Algorithm for Large-Scale Urban Transportation Problems*, Transportation Science 49(3), 623–636. [Author-held published PDF](https://web.mit.edu/osorioc/www/papers/osoChoLgeScaleSO.pdf), DOI 10.1287/trsc.2014.0550. | §2: larger constrained traffic optimization; explicitly not an evaluated baseline. |
| yao2023react | Yao et al. (2023), *ReAct: Synergizing Reasoning and Acting in Language Models*, ICLR. [Camera-ready author record](https://arxiv.org/abs/2210.03629), [conference record](https://openreview.net/forum?id=WE_vluYUL-X). | §2: reasoning/actions informed by external observations. |
| shinn2023reflexion | Shinn et al. (2023), *Reflexion: Language Agents with Verbal Reinforcement Learning*, NeurIPS 36. [Official proceedings](https://proceedings.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html). | §2: linguistic feedback used in subsequent attempts. |
| wu2024autogen | Wu et al. (2024), *AutoGen: Enabling Next-Gen LLM Applications via Multi-Agent Conversations*, COLM. [Author-held conference PDF](https://www.microsoft.com/en-us/research/wp-content/uploads/2023/08/LLM_agent.pdf). | §2: configurable multi-agent conversation infrastructure, not proof of traffic optimization performance. |
| wang2024agentsurvey | Wang et al. (2024), *A Survey on Large Language Model Based Autonomous Agents*, Frontiers of Computer Science 18, 186345. [Publisher](https://link.springer.com/article/10.1007/s11704-024-40231-1). | §2: context for agent construction and evaluation. |
| liu2024agentbench | Liu et al. (2024), *AgentBench: Evaluating LLMs as Agents*, ICLR. [Official proceedings](https://proceedings.iclr.cc/paper_files/paper/2024/hash/e9df36b21ff4ee211a8b71ee8b7e9f57-Abstract-Conference.html). | §2: need to evaluate interactive outcomes and failure modes. |
| varaiya2013maxpressure | Varaiya (2013), *Max Pressure Control of a Network of Signalized Intersections*, Transportation Research Part C 36, 177–195. [Publisher](https://www.sciencedirect.com/science/article/abs/pii/S0968090X13001782), DOI 10.1016/j.trc.2013.08.014. | §2: established online feedback control with explicit model assumptions. |
| braess2005paradox | Braess, Nagurney & Wakolbinger (2005), *On a Paradox of Traffic Planning*, Transportation Science 39(4), 446–450. [Publisher](https://pubsonline.informs.org/doi/abs/10.1287/trsc.1050.0127). | §2: network changes can have adverse effects under equilibrium assignment; no claim that these experiments demonstrate the paradox. |
| hollander2008calibration | Hollander & Liu (2008), *The Principles of Calibrating Traffic Microsimulation Models*, Transportation 35, 347–362. [Publisher](https://link.springer.com/article/10.1007/s11116-007-9156-2). | §2 and §6.3: calibration, validation, and limits of synthetic-demand sensitivity. |
| wunderlich2019toolbox | Wunderlich, Vasudevan & Wang (2019), *Traffic Analysis Toolbox Volume III: Guidelines for Applying Traffic Microsimulation Modeling Software—2019 Update to the 2004 Version*, FHWA-HOP-18-036. [Official report](https://ops.fhwa.dot.gov/publications/fhwahop18036/), [author/date metadata](https://ops.fhwa.dot.gov/publications/fhwahop18036/form.htm). | §2 and §6.3: observed conditions, calibration, and alternatives analysis. |

Bibliographic details were checked against the sources above. The Osorio–Bierlaire paper is in **Operations Research**, not Transportation Science. AutoGen uses the formal COLM 2024 version: its PDF includes Jiale Liu and the plural “Conversations”, whereas the Microsoft landing page omits that author and uses a singular title. The conference PDF takes precedence. ReAct, Reflexion and AgentBench use formal conference years rather than initial preprint years.

## Changes and verification

- Expanded Related work into five paragraphs, connecting the new sources to this study's specific scope. Added calibration citations in §6.3. No experimental results or generated data tables changed.
- Preserved the previous manuscript/bibliography/PDF in `../multra_revision_history/pre_reference_expansion_20261004/`.
- Rebuilt PDF (22 pages), editable Word and existing anonymous archives. PDF contains 28 numbered entries; the Word export contains the same 28 references.
- BibTeX compilation succeeds without missing citations or duplicate entries; no overfull boxes remain. One pre-existing underfull paragraph warning remains in the logged example.
- PDF related-work and reference pages and a LibreOffice-rendered Word reference page were visually inspected. Word preview is a layout check, not confirmation that every journal formatting requirement has been met.
- `python3 experiment/audit_current_submission.py` now checks a one-to-one match between all 28 bibliography keys and actual citations, alongside the existing numerical and anonymous-package checks. All 31 checks passed.

The supplied journal-specific guide requires a Word source. `main.tex` remains the internal editable source and `main.pdf` the LaTeX preview; neither replaces the requested Word submission source. See `GUIDE_REQUIREMENTS_20261004.md` for the remaining submission checks.
