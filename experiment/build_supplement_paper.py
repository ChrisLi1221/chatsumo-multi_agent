#!/usr/bin/env python3
"""Build a separate anonymous, editable supplement from audited actual results."""
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
PAPER=ROOT/'paper/multra_revision/new_experiments'

def read(path):return json.loads(path.read_text())

def main():
    stability=ROOT/'experiment/supplement_stability_v1'
    demand=ROOT/'experiment/supplement_demand_v1'
    ss=read(stability/'analysis/summary.json');ds=read(demand/'analysis/summary.json')
    sp=read(stability/'analysis/paired_statistics.json')
    sa=read(stability/'execution_accounting.json');da=read(demand/'execution_accounting.json')
    PAPER.mkdir(parents=True,exist_ok=True)
    text=r'''\documentclass[11pt]{article}
\usepackage[margin=0.85in]{geometry}
\usepackage{graphicx,booktabs,amsmath}
\usepackage[hidelinks]{hyperref}
\setlength{\emergencystretch}{2em}
\title{SimTIO: Additional Sampling-Stability and Synthetic-Demand Experiments}
\author{}
\date{}
\begin{document}
\maketitle
\noindent This standalone supplement reports new experiments using the frozen wide-search protocol. It supersedes the older output-limited sampling diagnostic for this protocol. The earlier manuscript must be reconciled with the final experimental cohorts before submission.

\section{Common protocol and reproducibility}
Each search begins with five shared, measured initial interventions. Two parents are selected by the existing feedback rule; each receives two distinct children drawn from its finite legal mutation catalog. Both children directly modify the same parent and use initial-only history; the second catalog excludes the first selected semantic action. Up to two compatible bundles are constructed from the four highest-ranked measured single interventions. Two subsequent joint-feedback rounds retain a beam of at most two bundle parents, and revise one component field while holding its partner fixed. The best feasible measured plan and the no-operation guard are retained.

All methods use a candidate-evaluation cap of 15, with the public baseline counted separately. Logical evaluations include cache reuse; actual simulation counts can be lower because of duplicate actions, catalog exhaustion, validation failures, or incompatible top-four candidates. We retained this compatibility limitation unchanged. Baseline demand, initial interventions, parent feedback, base catalogs, SUMO seeds, acceptance constraints, and bundle rules are common across methods within each scenario. The first request for each initial parent is identical across methods and repeats. Sibling catalogs and subsequent adaptive paths can diverge; we do not describe every later decision as a fully frozen-catalog comparison.

The LLM is configured as \texttt{gpt-5.6-luna}, with a 4,096-completion-token cap and default sampling; no model sampling seed is specified. The deterministic policy uses the first entry of the existing feedback-ordered legal catalog. Random selection uses seed $20260927+s+1000r+j$, where $s$ is the SUMO seed, $r$ the repeat index, and $j$ the request index. SUMO version 1.27.1 and executed seeds are checked against simulation outputs. Exact requests, responses, legal catalogs, sibling exclusions, input hashes, and evaluation ledgers are retained. Independent model requests need not yield independent outputs under undisclosed provider behavior. Existing repeat zero was obtained earlier; although the configured and returned model identifiers match, unreported provider-side model changes cannot be excluded.

\section{Sampling stability at fixed demand}
We preselected all five networks at demand/SUMO seed 42. LLM and random searches were repeated five times per network; the deterministic rule was evaluated once. Existing repeat zero was reused, adding 40 full searches. All five cases already contained at least one feasible shared initial plan. Consequently, final success rates have a ceiling under best-plan retention and are less informative than variation in final improvement.

\begin{table}[htbp]\centering\small
\input{../../../experiment/supplement_stability_v1/analysis/results_table.tex}
\caption{Fixed-demand stability. SD concerns repeated search sampling within a city. The deterministic row has one evaluation; zero SD is not an uncertainty estimate over traffic demand.}
\end{table}

Across the five cities, the LLM, rule, and random mean Top-10 reductions were STAB_LLM\%, STAB_RULE\%, and STAB_RANDOM\%, respectively. LLM and random searches were feasible in 25/25 runs and the rule in 5/5 cases, conditional on the feasible initial sets. Mean within-city SD was STAB_LLM_SD and STAB_RANDOM_SD percentage points for LLM and random search. LLM choices varied across repeats for STAB_VARIABLE of ten fixed initial-parent catalogs; only each parent's first child is used for this strictly identical-input choice comparison.

Paired analysis first averages repeats within each city ($n=5$ cities). LLM minus rule and LLM minus random differences were DIFF_RULE and DIFF_RANDOM percentage points; two-sided exact city sign-flip $p$ values were P_RULE and P_RANDOM. These results do not establish LLM superiority or equivalence. Ann Arbor and Los Angeles exhibited particularly large LLM outcome ranges; full ranges and choice-frequency distributions are supplied in the machine-readable results.

\begin{figure}[htbp]\centering
\includegraphics[width=\linewidth]{../../../experiment/supplement_stability_v1/analysis/stability.pdf}
\caption{All fixed-demand final outcomes. Points are repeated searches; black segments mark means. Repeats are nested within cities and are not independent city replicates.}
\end{figure}

\section{Synthetic demand-intensity sensitivity}
Albany and Manhattan were preselected to provide contrasting network scales. Each uses demand/SUMO seeds 42, 43, and 44 at 1,200, 2,400, and 3,600 trips, with a fixed one-hour departure window. The archived 2,400-trip cases are reused. For each original demand, one trip is selected by seeded sampling from every pair of adjacent departures. These 1,200 trips define the low-demand case. The high-demand case retains the original 2,400 trips and adds those same 1,200 selected OD pairs, assigning unique vehicle IDs and shifting the copied departure times by 0.75 seconds. Vehicle attributes are retained. This paired construction reduces OD-regeneration confounding but does not exactly preserve empirical OD shares between levels.

Each of the twelve new scenarios receives a newly simulated baseline, a newly frozen Top-10 road set, and five initial interventions generated from its own baseline diagnosis. The five actions are frozen before their measurements and shared across methods. Failed interventions are retained without replacement. Each selector is then run once, adding 36 complete searches. Simulation continues after the departure window until all trips are recorded as completed; this can include teleport recovery. Percent improvements refer to each demand scenario's own frozen target roads, which can differ between levels.

The Manhattan baselines at 3,600 trips exhibit 84, 93, and 100 teleports for seeds 42, 43, and 44, respectively. These high-demand cases are congested simulation stress tests, not uninterrupted physical-traffic simulations. The unchanged acceptance rule limits teleports to each scenario's baseline count; it does not require zero teleports. Absolute baseline and selected-plan teleport counts are supplied separately and qualify the interpretation of time-loss changes.

\begin{table}[htbp]\centering\small
\input{../../../experiment/supplement_demand_v1/analysis/results_table.tex}
\caption{Demand sensitivity across six city--seed cases per level. SD describes variation across those cases, rather than repeated LLM sampling. The 2,400-trip rows reuse the corresponding primary results.}
\end{table}

DEMAND_DESCRIPTION

\begin{figure}[htbp]\centering
\includegraphics[width=\linewidth]{../../../experiment/supplement_demand_v1/analysis/demand.pdf}
\caption{Demand sensitivity by city and baseline congestion. Error bars are sample SD across three demand seeds, not confidence intervals. Targets are re-frozen separately at each demand level.}
\end{figure}

\section{Accounting, statistics, and boundaries}
The added stability runs used STAB_LOGICAL logical candidate evaluations: STAB_NEW new SUMO evaluations and STAB_CACHE cached measurements, with STAB_API completed HTTP requests. New demand searches used DEMAND_LOGICAL logical candidate evaluations, including DEMAND_NEW actual new candidate simulations (shared initialization counted once) and DEMAND_CACHE cache uses, plus twelve new baseline simulations. They made DEMAND_API HTTP requests. Infrastructure transport faults are logged separately from model-output failures and retried after connectivity is available. Baseline and cached work are not hidden inside nominal budgets.

Paired differences are reported in percentage points. We provide two-sided exact sign-flip tests, Wilcoxon tests with Holm adjustment within each pair of LLM comparisons, and descriptive city-cluster bootstrap intervals. Stability inference uses five city means; demand analyses have only two city clusters and are exploratory. A single search sample per method at each new demand scenario cannot completely separate demand effects from LLM sampling effects. Five stability repeats also provide limited information about rare failures.

Both cohorts include 100 acceptance-threshold combinations re-evaluated on saved candidate KPIs, and distributions and worst-edge metrics for spatial degradation outside the target neighborhood. These analyses reselect existing candidates; they do not reproduce how a search would behave under changed thresholds. A global average constraint and one-hop spillover test cannot guarantee the absence of distant concentrated harm.

These remain uncalibrated synthetic passenger-demand algorithm tests. No observed traffic counts or calibrated OD matrix were available in the repository. The additional demand levels address sensitivity to demand intensity, not real-world traffic effectiveness or multimodal benefits. SimTIO is a simulation-based scenario-screening framework across operational and planning horizons; this supplement does not validate field deployment.
\end{document}
'''
    values={
        'STAB_LLM':f"{ss['llm']['top10_mean']:.2f}",'STAB_RULE':f"{ss['catalog_first']['top10_mean']:.2f}",
        'STAB_RANDOM':f"{ss['catalog_random']['top10_mean']:.2f}",
        'STAB_LLM_SD':f"{ss['llm']['mean_within_case_sd']:.2f}",'STAB_RANDOM_SD':f"{ss['catalog_random']['mean_within_case_sd']:.2f}",
        'STAB_VARIABLE':str(ss['llm']['variable_fixed_catalog_parents']),
        'DIFF_RULE':f"{sp['catalog_first']['mean_difference_pp']:.2f}",'DIFF_RANDOM':f"{sp['catalog_random']['mean_difference_pp']:.2f}",
        'P_RULE':f"{sp['catalog_first']['exact_city_signflip_p']:.3f}",'P_RANDOM':f"{sp['catalog_random']['exact_city_signflip_p']:.3f}",
        'STAB_LOGICAL':str(sa['logical']),'STAB_NEW':str(sa['new_sumo']),'STAB_CACHE':str(sa['reused']),'STAB_API':str(sa['api_requests']),
        'DEMAND_LOGICAL':str(da['logical']),'DEMAND_NEW':str(da['new_sumo_excluding_baselines']),
        'DEMAND_CACHE':str(da['reused']),'DEMAND_API':str(da['api_requests'])}
    sentences=[]
    for n in [1200,2400,3600]:
        rows={r['arm']:r for r in ds if r['trips']==n}
        sentences.append(f"At {n:,} trips, mean Top-10 reductions for LLM, rule, and random search were "
            +', '.join(f"{rows[arm]['top10_mean']:.2f}\\%" for arm in ['llm','catalog_first','catalog_random'])
            +', with '+', '.join(f"{rows[arm]['successes']}/6" for arm in ['llm','catalog_first','catalog_random'])+' feasible results, respectively.')
    values['DEMAND_DESCRIPTION']=' '.join(sentences)
    for key in sorted(values,key=len,reverse=True):text=text.replace(key,values[key])
    (PAPER/'supplement.tex').write_text(text)
    print(PAPER/'supplement.tex')

if __name__=='__main__':main()
