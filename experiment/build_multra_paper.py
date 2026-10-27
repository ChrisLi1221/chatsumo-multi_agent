#!/usr/bin/env python3
"""Render manuscript result fragments and anonymized supplements from real logs."""
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from collections import Counter

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment.analyze_multra_revision import read,write,csvwrite,load_all,eligible
OUT=ROOT/'experiment/multra_revision';PAPER=ROOT/'paper/multra_revision';GEN=PAPER/'generated'

def tex(s):
    return str(s).replace('\\',r'\textbackslash{}').replace('_',r'\_').replace('#',r'\#').replace('%',r'\%').replace('&',r'\&')

def anonymous(text):
    return text.replace(str(ROOT),'PROJECT_ROOT').replace('/home/chris','USER_HOME')

def main():
    s=read(OUT/'analysis/summary.json');p=read(OUT/'analysis/paired_statistics.json');stable=read(OUT/'analysis/llm_stability.json')
    assert all(s['fresh'][a]['n']==15 for a in ['llm','catalog_first','catalog_random'])
    assert len(stable)==3 and all(x['n']==5 for x in stable)
    accounting=read(OUT/'analysis/execution_accounting.json')
    fresh_llm=[x for x in accounting if x['arm']=='llm' and x['repeat']==0]
    model_fail=sum(x['failed_decisions'] for x in fresh_llm)
    macros=[]
    for a,name in [('llm','Llm'),('catalog_first','Rule'),('catalog_random','Random')]:
        macros.append(r'\newcommand{\Fresh'+name+'Top}{'+f"{s['fresh'][a]['top10_mean']:.2f}"+'}')
    (GEN/'result_macros.tex').write_text('\n'.join(macros))
    ll=s['fresh']['llm'];rule=s['fresh']['catalog_first'];rnd=s['fresh']['catalog_random'];pr=p['fresh']['catalog_first'];pa=p['fresh']['catalog_random']
    (GEN/'fresh_narrative.tex').write_text(
        f"The fresh LLM mean is {ll['top10_mean']:.2f}\\%, versus {rule['top10_mean']:.2f}\\% for the rule and {rnd['top10_mean']:.2f}\\% for random selection. "
        f"The paired LLM-minus-rule difference is {pr['mean_difference_pp']:.2f} percentage points (pp), and LLM-minus-random is {pa['mean_difference_pp']:.2f} pp. "
        f"Exact case-level sign-flip $p$ values are {pr['exact_case_signflip_p']:.3f} and {pa['exact_case_signflip_p']:.3f}; city-level values are {pr['exact_city_signflip_p']:.3f} and {pa['exact_city_signflip_p']:.3f}. "
        f"Holm-adjusted Wilcoxon values are {pr['holm_wilcoxon_p']:.3f} and {pa['holm_wilcoxon_p']:.3f}. "
        f"The city-bootstrap intervals are [{pr['city_cluster_bootstrap_ci95_pp'][0]:.2f}, {pr['city_cluster_bootstrap_ci95_pp'][1]:.2f}] pp and [{pa['city_cluster_bootstrap_ci95_pp'][0]:.2f}, {pa['city_cluster_bootstrap_ci95_pp'][1]:.2f}] pp. "
        "The first descriptive bootstrap interval narrowly excludes zero, whereas the discrete city sign-flip test does not reject; with five cities these should not be read as precise confirmatory bounds. "
        f"No comparison supports an LLM advantage. The LLM wins/ties/loses {pr['wins']}/{pr['ties']}/{pr['losses']} cases against the rule and {pa['wins']}/{pa['ties']}/{pa['losses']} against random.\n\n"
        f"The LLM produces {model_fail} failed refinement decisions out of 30, compared with none for the non-LLM selectors. Its {ll['evaluations']} candidate SUMO invocations yield {ll['successful_simulations']} successful simulations; the other arms each use 103 invocations with 102 successful simulations. "
        f"Pooled feasible-improving yields are {100*ll['feasible_improving']/ll['evaluations']:.1f}\\%, {100*rule['feasible_improving']/rule['evaluations']:.1f}\\%, and {100*rnd['feasible_improving']/rnd['evaluations']:.1f}\\%, respectively. "
        "Thus a shared budget cap does not imply equal realized traffic evaluations. We report this reliability cost, rather than replacing missing LLM choices with a stronger policy.\n")
    lines=[r'\begin{tabular}{lrrrr}',r'\toprule',r'Case & Mean $\pm$ SD (\%) & Range (\%) & Success & Failed picks \\',r'\midrule']
    for x in stable:
        city=x['case'].split('_equal_scale')[0].replace('_',' ').title();fail=sum(c['counts'].get('FAILED',0) for c in x['choices'])
        lines.append(f"{city} & {x['mean_top10']:.2f} $\\pm$ {x['sd_top10']:.2f} & {x['min_top10']:.2f}--{x['max_top10']:.2f} & {x['successes']}/5 & {fail}/10 \\\\")
    lines += [r'\bottomrule',r'\end{tabular}'];(GEN/'stability.tex').write_text('\n'.join(lines))
    (GEN/'stability_narrative.tex').write_text(
        'Albany varies between 2.98\\% and 4.20\\% Top-10 reduction; Ann Arbor varies between 5.90\\% and 7.73\\%; Manhattan remains at 33.89\\%. '
        'All 15 repeated searches retain an accepted plan, but 16 of their 30 refinement decisions fail. For Albany, the signal selection fails in all five repeats; the network specialist chooses mutation 002 three times, mutation 001 once, and fails once. '
        'In Ann Arbor, valid signal and network choices occur in one and three repeats, respectively. Manhattan produces the same valid network mutation in every repeat, but only one valid signal choice. '
        'The supplement provides full ID frequencies and entropy with failure treated as a separate outcome, avoiding the interpretation of repeated failure as successful decision stability.\n')
    sp=read(OUT/'analysis/fresh_spatial_summary.json')
    (GEN/'spatial_narrative.tex').write_text(
        f"In the fresh matched cohort, {sp['llm']['cases_with_remote_over60s']} LLM outcomes, {sp['catalog_first']['cases_with_remote_over60s']} rule outcomes, and {sp['catalog_random']['cases_with_remote_over60s']} random outcomes contain at least one edge outside the monitored neighborhood with more than 60~s additional accumulated time loss. "
        f"For the LLM, the median case-level maximum remote-edge increase is {sp['llm']['median_max_remote_s']:.2f}~s (including no-ops), and the largest is {sp['llm']['maximum_remote_s']:.2f}~s. "
        f"The largest LLM remote 500~m-cell net increase is {sp['llm']['maximum_cell_s']:.2f}~s. "
        "These increases coexist with satisfaction of the default global and one-hop constraints. Complete summary and worst-edge records for both historical and fresh cohorts accompany the paper.\n")
    # Select a real complete case deterministically: first alphabetically with two valid fresh LLM decisions.
    files=sorted((OUT/'runs').glob('*/llm_rep00/result.json'))
    path=next(x for x in files if all(d['status']=='ok' for d in read(x)['decisions']))
    run=read(path);spec=read(OUT/'frozen'/f"{run['case']}.json")
    trace={'selection_rule':'alphabetically first fresh case with two valid LLM refinements','case':run['case'],
           'frozen_spec':spec,'run_result':run,'calls':read(path.parent/'calls.json')}
    write(OUT/'worked_example_full.json',trace)
    description=[f"The example is {tex(run['case'])}, selected as the alphabetically first fresh case with two valid LLM refinements. It is illustrative, not an additional experiment. Demand and SUMO seed are {spec['sumo_seed']}; the random-selector seed for repeat zero is {20260927+int(spec['sumo_seed'])}. The complete case, including every proposed and rejected catalog entry, measured feedback, request, response, execution outcome, and final decision, is supplied as an anonymized JSON file.\n"]
    for j,dec in enumerate(spec['decisions']):
        parent=dec['parent'];args={k:v for k,v in parent['args'].items() if not k.endswith('_path')};fb=dec['feedback']['scope_objective']
        choice=run['decisions'][j]['proposal'];child=next(c for c in run['candidates'] if c['proposal']['proposal_id']==choice['proposal_id']);sc=child['scope_objective']
        description.append(f"\\subsection*{{Parent {j+1}: {tex(parent['action'])}}}\n"
          +"Semantic parent arguments: "+'; '.join(f"\\texttt{{{tex(k)}}} = \\path{{{v}}}" for k,v in args.items())+'. '
          +f"Measured feedback is {100*fb['top10_relative_improvement']:.3f}\\% Top-10 reduction, {100*fb['global_delay_relative_change']:+.3f}\\% global-delay change, and {100*fb['neighbor_delay_relative_change']:+.3f}\\% neighboring-loss change. "
          +f"Failed conditions: {tex(', '.join(dec['feedback']['failed_constraints']) or 'none')}. "
          +f"The catalog contains {len(dec['catalog'])} legal entries. Only the listed field changes; other semantic arguments retain the parent value.\n")
        description.extend([r'\begin{longtable}{lp{.23\linewidth}p{.48\linewidth}}',r'\toprule ID & Changed field & New value \\\midrule\endhead'])
        for c in dec['catalog']:
            description.append(f"{tex(c['mutation_id'])} & {tex(c['changed_field'])} & \\path{{{c['args'][c['changed_field']]}}} \\\\")
        description += [r'\bottomrule\end{longtable}']
        counts=Counter(x['disposition'] for x in dec['filter_audit'])
        description.append('Filter audit: '+', '.join(f"{tex(k)}={v}" for k,v in sorted(counts.items()))+'. '
            +f"The LLM selected \\texttt{{{tex(choice['refinement']['selected_mutation_id'])}}}. "
            +f"The executed child yields {100*sc['top10_relative_improvement']:.3f}\\% Top-10 reduction, {100*sc['global_delay_relative_change']:+.3f}\\% global-delay change, and {100*sc['neighbor_delay_relative_change']:+.3f}\\% neighboring-loss change. "
            +f"It {'passes' if sc['constraints_passed'] else 'fails'} the final acceptance rule.\n")
    selected=run['selected'];sc=selected['scope_objective']
    description.append(f"The final selection is \\texttt{{{tex(selected['proposal']['proposal_id'])}}}, with {100*sc['top10_relative_improvement']:.3f}\\% Top-10 reduction. "
        +f"The complete case uses {run['logical_sumo_evaluations']} logical candidate simulations. The supplementary case trace includes all three initial interventions, both refinements, both generated bundles, full post-execution KPIs, and the retained plan.\n")
    (GEN/'worked_example.tex').write_text('\n'.join(description))
    shutil.copyfile(ROOT/'paper/chatsumo_optimizer/trb_template.bib',PAPER/'references.bib')
    with (PAPER/'references.bib').open('a') as f:
        f.write('\n@misc{sumoEdgeData, author={{Eclipse SUMO}}, title={Lane- or Edge-based Traffic Measures}, year={2026}, url={https://sumo.dlr.de/docs/Simulation/Output/Lane-_or_Edge-based_Traffic_Measures.html}, note={Accessed 27 September 2026}}\n')
    supplement=PAPER/'supplementary_data';supplement.mkdir(exist_ok=True)
    # Original-source line inventory includes author declarations: local audit only.
    (supplement/'original_numeric_lines.csv').unlink(missing_ok=True)
    for src in (OUT/'analysis').glob('*'):
        if src.suffix in ['.csv','.json'] and src.name!='original_numeric_lines.csv':
            (supplement/src.name).write_text(anonymous(src.read_text()))
    (supplement/'worked_example.json').write_text(anonymous(json.dumps(trace,indent=2,ensure_ascii=False)))
    compact=[]
    for path in sorted((OUT/'frozen').glob('*.json')):
        d=read(path);compact.append({k:d[k] for k in ['case','sumo_seed','sumo_version','frozen_sha256','protocol','historical_audit']})
    write(supplement/'catalog_reconstruction_audit.json',compact)
    inventory=[]
    for folder in ['agents','experiment']:
        for path in sorted((ROOT/folder).glob('*.py')):
            inventory.append(dict(path=str(path.relative_to(ROOT)),sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
    write(OUT/'source_hashes.json',inventory)
    accounting_summary=dict(experiment_runs=len(accounting),llm_runs=sum(x['arm']=='llm' for x in accounting),
        actual_api_requests=sum(x['calls'] for x in accounting if x['arm']=='llm'),
        new_candidate_sumo=sum(x['new'] for x in accounting),cached_logical_evaluations=sum(x['reused'] for x in accounting),
        logical_evaluations=sum(x['logical'] for x in accounting),baseline_verifications=len(list((OUT/'verification').glob('*/verification.json'))))
    write(OUT/'delivery_accounting.json',accounting_summary)
    print(json.dumps(accounting_summary,indent=2));print('Worked case:',run['case'])

if __name__=='__main__':main()
