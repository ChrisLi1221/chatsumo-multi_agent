#!/usr/bin/env python3
"""Build current manuscript evidence from audited logs, without new model calls."""
import csv
from collections import Counter
import json
from pathlib import Path
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import analyze_multra_revision as a
from experiment import full_feedback_experiment as full
from experiment.analyze_saved_selections import load
from agents.debate_optimizer import _mutation_payload

PAPER=ROOT/'paper/multra_revision';GEN=PAPER/'generated_current';FIG=PAPER/'figures_current'
SUP=PAPER/'supplementary_current';OUT=ROOT/'experiment/paper_current_analysis'
WIDE=ROOT/'experiment/wide_feedback_v1';STRICT=ROOT/'experiment/full_feedback_v1'
LABELS={'llm':'LLM','catalog_first':'Rule','catalog_random':'Random'}
COLORS={'llm':'#3F6FA8','catalog_first':'#4C9B88','catalog_random':'#E59D2F'}

def tex(s):return str(s).replace('_',r'\_').replace('#',r'\#').replace('%',r'\%').replace('&',r'\&')
def anon(s):return s.replace(str(ROOT),'PROJECT_ROOT').replace('/home/chris','USER_HOME')
def csvread(p):return list(csv.DictReader(p.open()))
def table(name,headers,rows,layout=None):
    layout=layout or 'l'+'r'*(len(headers)-1)
    text=[r'\begin{tabular}{'+layout+'}',r'\toprule',' & '.join(headers)+r' \\',r'\midrule']
    text+=[' & '.join(map(str,row))+r' \\' for row in rows]
    text += [r'\bottomrule',r'\end{tabular}'];(GEN/f'{name}.tex').write_text('\n'.join(text)+'\n')

def strict_prefix():
    rows=[];sources=[];shared={};calls=0
    for case in a.read(STRICT/'execution_accounting.json')['cases']:
        spec=a.read(STRICT/'frozen'/f'{case}.json')
        assert len(spec['initial'])==3 and len(spec['decisions'])==2
        for item in spec['input_files'].values():assert full.m.file_hash(item['path'])==item['sha256']
        for arm in LABELS:
            path=STRICT/'runs'/case/f'{arm}_rep00/result.json';d=a.read(path)
            assert d['frozen_sha256']==spec['frozen_sha256']
            pool=d['candidates'][:d['prefix_candidate_count']]
            assert full.m.digest(pool[:3])==full.m.digest(spec['initial']) and len(pool)<=7
            assert all(c['proposal'].get('refinement',{}).get('feedback_scope')!='joint_plan' for c in pool)
            for c in [d['baseline']]+pool:full.audit_execution(c,spec['sumo_seed'],spec['sumo_version'])
            assert full.m.candidate_seed(d['baseline'])==int(spec['sumo_seed'])
            for cp in sorted((path.parent/'calls').glob('*.json')):
                call=a.read(cp);req=call['request'];p=req['payload']
                if p['search_round']!=2:continue
                dec=next(x for x in spec['decisions'] if x['parent']==p['parent_proposal'])
                assert p['simulation_feedback']==dec['feedback'] and p['evaluated_history']==spec['history']
                assert p['mutation_catalog']==_mutation_payload(dec['catalog'])
                key=(case,p['parent_proposal']['proposal_id']);h=full.m.digest({k:req[k] for k in ['system','payload','scope','max_tokens']})
                shared.setdefault(key,h);assert shared[key]==h and call['status']=='ok'
                if arm=='llm':calls+=1
            best=full.m.opt._select_best_candidate([d['baseline']]+pool,profile='delay',objective_scope='top10')
            assert full.m.digest(best)==full.m.digest(d['before_selected'])
            sc=best['scope_objective']
            rows.append(dict(case=case,city=case.split('_equal_scale')[0],arm=arm,top10=100*sc['top10_relative_improvement'],
                global_reduction=-100*sc['global_delay_relative_change'],success=not best.get('is_baseline',False),
                evaluations=sum(bool(c.get('sumo_evaluated')) for c in pool),selected=best['proposal']['proposal_id']))
            sources.append(dict(path=str(path.relative_to(ROOT)),sha256=full.m.file_hash(path),prefix_candidate_count=len(pool)))
    assert calls==30 and len(shared)==30
    a.csvwrite(OUT/'strict_prefix_results.csv',rows)
    a.write(OUT/'strict_prefix_provenance.json',dict(
        interpretation='Retrospective extraction of the saved pre-joint endpoint; not a new independent simulation cohort. Later joint outcomes never enter this endpoint.',
        sources=sources,llm_calls=calls,common_prompts=[dict(case=k[0],parent=k[1],sha256=v) for k,v in shared.items()]))
    summary={arm:dict(top10_mean=a.mean([r['top10'] for r in rows if r['arm']==arm]),top10_sd=a.std([r['top10'] for r in rows if r['arm']==arm]),
        global_mean=a.mean([r['global_reduction'] for r in rows if r['arm']==arm]),successes=sum(r['success'] for r in rows if r['arm']==arm),
        evaluations=sum(r['evaluations'] for r in rows if r['arm']==arm)) for arm in LABELS}
    pairs={arm:a.paired({r['case']:r for r in rows if r['arm']=='llm'},{r['case']:r for r in rows if r['arm']==arm}) for arm in LABELS if arm!='llm'}
    previous=0
    for i,arm in enumerate(sorted(pairs,key=lambda x:pairs[x]['wilcoxon_p'])):
        previous=max(previous,min(1.,(2-i)*pairs[arm]['wilcoxon_p']));pairs[arm]['holm_wilcoxon_p']=previous
    a.write(OUT/'strict_prefix_summary.json',summary);a.write(OUT/'strict_prefix_paired.json',pairs)
    return summary,pairs

def main():
    for d in [GEN,FIG,SUP,OUT]:d.mkdir(parents=True,exist_ok=True)
    strict,sp=strict_prefix();wide=a.read(WIDE/'analysis/summary.json');wp=a.read(WIDE/'analysis/paired_statistics.json')
    rows=[]
    for cohort,summary in [('Frozen prefix (7)',strict),('Adaptive search (15)',wide)]:
        for arm,s in summary.items():rows.append([cohort,LABELS[arm],f"{s['top10_mean']:.2f} $\\pm$ {s['top10_sd']:.2f}",f"{s['global_mean']:.2f}",f"{s['successes']}/15",s['evaluations']])
    table('main_results',['Protocol (cap)','Selector',r'Top-10 (\%)',r'Global (\%)','Success','Evals'],rows,'llrrrr')
    table('paired',['Protocol','LLM minus',r'$\Delta$ (pp)',r'$p_{\mathrm{case}}$',r'$p_{\mathrm{city}}$',r'Holm $p_W$'],
        [[cohort,LABELS[arm],f"{p['mean_difference_pp']:.2f}",f"{p['exact_case_signflip_p']:.3f}",f"{p['exact_city_signflip_p']:.3f}",f"{p['holm_wilcoxon_p']:.3f}"]
         for cohort,ps in [('Frozen prefix',sp),('Adaptive search',wp)] for arm,p in ps.items()],'llrrrr')
    # Inventory and baseline numbers are regenerated from the current frozen files.
    networks=[]
    for city in ['albany','ann_arbor','los_angeles','manhattan','troy']:
        specs=[a.read(WIDE/'frozen'/f'{city}_equal_scale_delay_seed_{s}.json') for s in [42,43,44]]
        net=ET.parse(specs[0]['baseline']['artifacts']['net']).getroot();vals=[s['baseline']['kpi']['mean_delay'] for s in specs]
        networks.append([city.replace('_',' ').title(),sum(e.get('function')!='internal' for e in net.findall('edge')),len(net.findall('junction')),
            len({e.get('id') for e in net.findall('tlLogic')}),f'{a.mean(vals):.2f} $\\pm$ {a.std(vals):.2f}'])
    table('networks',['Network','External edges','Junctions','TLS',r'Delay (s)'],networks)
    stable=csvread(ROOT/'experiment/supplement_stability_v1/analysis/within_case_stability.csv');rows=[]
    for city in ['albany','ann_arbor','los_angeles','manhattan','troy']:
        group={r['arm']:r for r in stable if r['city']==city};l=group['llm'];r=group['catalog_random'];h=group['catalog_first']
        rows.append([city.replace('_',' ').title(),f"{float(l['top10_mean']):.2f} $\\pm$ {float(l['top10_sd']):.2f}",
            f"{float(l['top10_min']):.2f}--{float(l['top10_max']):.2f}",f"{float(r['top10_mean']):.2f} $\\pm$ {float(r['top10_sd']):.2f}",f"{float(h['top10_mean']):.2f}"])
    table('stability',['Network',r'LLM mean $\pm$ SD','LLM range',r'Random mean $\pm$ SD','Rule'],rows)
    ds=a.read(ROOT/'experiment/supplement_demand_v1/analysis/summary.json')
    table('demand',['Trips','Selector',r'Top-10 mean $\pm$ SD (\%)','Success',r'Global (\%)'],
        [[r['trips'],LABELS[r['arm']],f"{r['top10_mean']:.2f} $\\pm$ {r['top10_sd']:.2f}",f"{r['successes']}/6",f"{r['global_mean']:.2f}"] for r in ds],'rlrrr')
    sweeps=csvread(WIDE/'analysis/threshold_sensitivity.csv');settings=[(.01,.01,.05),(.01,0.,.05),(.01,.01,0.),(.01,.01,.1),(.02,.01,.05)]
    rows=[]
    for t,g,n in settings:
        row=[f'{100*x:g}' for x in [t,g,n]]
        for arm in LABELS:
            r=next(r for r in sweeps if r['arm']==arm and tuple(float(r[k]) for k in ['top10_threshold','global_limit','neighbor_limit'])==(t,g,n))
            row.append(f"{round(15*float(r['success_rate']))}/15 ({float(r['top10_mean']):.2f})")
        rows.append(row)
    table('thresholds',[r'$R_T\geq$',r'$\Delta_D\leq$',r'$\Delta_N\leq$','LLM','Rule','Random'],rows)
    spatial=csvread(WIDE/'analysis/spatial_selected.csv');rows=[];ss={}
    for arm in LABELS:
        rr=[r for r in spatial if r['arm']==arm];values=[float(r['max_remote_increase_s']) for r in rr]
        ss[arm]=dict(remote_cases=sum(int(r['remote_edges_increase_over_60s'])>0 for r in rr),median_max=float(a.np.median(values)),maximum=max(values),
            maximum_cell=max(float(r['max_remote_500m_cell_net_increase_s']) for r in rr))
        s=ss[arm];rows.append([LABELS[arm],f"{s['remote_cases']}/15",f"{s['median_max']:.1f}",f"{s['maximum']:.1f}",f"{s['maximum_cell']:.1f}"])
    table('spatial',['Selector','Cases',r'Median max edge (s)',r'Max edge (s)',r'Max cell (s)'],rows)
    a.write(OUT/'spatial_summary.json',ss)
    stages=csvread(WIDE/'analysis/stage_results.csv');names=['Initial','Mutation','Bundle','Joint 1','Joint 2']
    fig,axes=a.plt.subplots(1,2,figsize=(10,3.6),layout='constrained')
    for i,arm in enumerate(LABELS):
        axes[0].plot(range(5),[a.mean([float(r['top10']) for r in stages if r['arm']==arm and r['stage']==n]) for n in names],marker='o',label=LABELS[arm],color=COLORS[arm])
        vals=[float(r['max_remote_increase_s']) for r in spatial if r['arm']==arm]
        axes[1].scatter([i]*len(vals),vals,alpha=.65,color=COLORS[arm]);axes[1].plot([i-.2,i+.2],[float(a.np.median(vals))]*2,color='black')
    axes[0].set(xticks=range(5),xticklabels=names,ylabel='Mean Top-10 reduction (%)',title='Adaptive search, 15 cases');axes[0].legend()
    axes[1].set(xticks=range(3),xticklabels=list(LABELS.values()),ylabel='Worst remote-edge increase (vehicle-seconds)',title='Spatial side effects of retained plans')
    for ax in axes:ax.spines[['top','right']].set_visible(False)
    for suffix in ['pdf','png']:fig.savefig(FIG/f'adaptive_search.{suffix}',dpi=180)
    a.plt.close(fig)
    for cohort in ['stability','demand']:
        for suffix in ['pdf','png']:shutil.copyfile(ROOT/f'experiment/supplement_{cohort}_v1/analysis/{cohort}.{suffix}',FIG/f'{cohort}.{suffix}')
    worked_case()
    # Only current evidence enters the reviewer package, never legacy generated data.
    for name,source in [('strict_prefix',OUT),('adaptive',WIDE/'analysis'),('stability',ROOT/'experiment/supplement_stability_v1/analysis'),('demand',ROOT/'experiment/supplement_demand_v1/analysis'),('interaction_pilot',ROOT/'experiment/interaction_feedback_v1/analysis')]:
        target=SUP/name;target.mkdir(exist_ok=True)
        for f in source.iterdir():
            if f.suffix in ['.json','.csv']:(target/f.name).write_text(anon(f.read_text()))
    (SUP/'README.md').write_text('Current evidence only. strict_prefix is a retrospective pre-joint endpoint from the corrected three-initial protocol; adaptive is the fifteen-budget main search. Stability repeats and demand scenarios reuse their recorded primary rows and are not independent additions to the 15-case sample. Local paths are replaced by PROJECT_ROOT. Hashes record original files and requests before anonymization, not the redacted file bytes. The worked_current_case trace uses the current adaptive protocol; it is placed alongside its source/extraction audit and is not part of the seven-budget result. Full raw SUMO XML files remain in the local experiment archive.\n')
    with (SUP/'README.md').open('a') as stream:
        stream.write(' Spatial fields named nearest_target_midpoint_m retain their legacy name: coordinates are arithmetic means of edge shape vertices, not geometric midpoints. Supplement model_metadata_summary files include reused repeat-zero/2400-trip calls; execution_accounting reports only newly added work, so these API totals are intentionally different.\n')
    macros=[]
    for prefix,summ in [('Wide',wide),('Strict',strict)]:
        for arm,label in LABELS.items():macros.append('\\newcommand{\\'+prefix+label+'Top}{'+f"{summ[arm]['top10_mean']:.2f}"+'}')
    (GEN/'result_macros.tex').write_text('\n'.join(macros)+'\n')
    from experiment.restore_manuscript_figures import main as restore_figures
    restore_figures()
    print('Current tables, figures, strict-prefix audit and anonymous evidence written.')

def worked_case():
    case=a.read(WIDE/'execution_accounting.json')['cases'][0];spec=a.read(WIDE/'frozen'/f'{case}.json')
    folder=WIDE/'runs'/case/'llm_rep00';r=a.read(folder/'result.json')
    aliases={c['proposal']['proposal_id']:f'C{i}' for i,c in enumerate(r['candidates'],1)}
    trace=dict(selection_rule='First case in the predetermined main-cohort order; no outcome filtering.',frozen_inputs=spec,result=r,
        display_aliases=aliases,calls=[a.read(p) for p in sorted((folder/'calls').glob('*.json'))])
    a.write(OUT/'worked_current_case.json',trace)
    lines=[f"This is Albany, demand/SUMO seed 42, the first case in the prespecified case order, selected without performance filtering. The complete trace includes all five initial actions, both base catalogs, four single selections, both bundles, two joint rounds, exact requests, and executed KPIs. Display labels C1--C15 below map to the original proposal IDs in the supplementary JSON.\n"]
    for j,dec in enumerate(spec['decisions'],1):
        parent=dec['parent'];fb=dec['feedback']['scope_objective']
        lines += [f"\\subsection*{{Parent {j}: {tex(parent['action'])}}}",
            'Parent '+aliases[parent['proposal_id']]+': '+ '; '.join(r'\texttt{'+tex(k)+r'} = \path{'+str(v)+'}' for k,v in parent['args'].items() if not k.endswith('_path'))+'.',
            f"Measured Top-10 reduction is {100*fb['top10_relative_improvement']:.3f}\\%, global-delay change {100*fb['global_delay_relative_change']:+.3f}\\%, and neighboring-loss change {100*fb['neighbor_delay_relative_change']:+.3f}\\%. Failed constraints: {tex(', '.join(dec['feedback']['failed_constraints']) or 'none')}.",
            r'\begin{longtable}{lp{.21\linewidth}>{\raggedright\arraybackslash}p{.50\linewidth}}',r'\toprule ID & Changed field & New value \\\midrule\endhead']
        for c in dec['catalog']:lines.append(tex(c['mutation_id'])+' & '+tex(c['changed_field'])+r' & \path{'+str(c['args'][c['changed_field']])+r'} \\')
        lines += [r'\bottomrule\end{longtable}']
        count=Counter(x['disposition'] for x in dec['filter_audit'])
        lines += ['Filter dispositions: '+', '.join(f'{tex(k)}={v}' for k,v in sorted(count.items()))+'.']
        for rejected in dec['filter_audit']:
            if rejected['disposition']=='accepted':continue
            validation=rejected.get('validation',{})
            reasons=[str(v) for k,vv in validation.items() if k.endswith('_errors') for v in vv]
            reason='; '.join(reasons) or rejected['disposition']
            lines.append('Rejected change: '+tex(rejected['field'])+r' = \path{'+str(rejected['value'])+'}; '+tex(reason)+'.')
        for d in [d for d in r['decisions'] if d['parent']==parent['proposal_id']]:
            p=d['proposal'];sc=next(c['scope_objective'] for c in r['candidates'] if c['proposal']['proposal_id']==p['proposal_id'])
            lines.append(f"Child {d['child_index']} selects \\texttt{{{tex(p['refinement']['selected_mutation_id'])}}} ({aliases[p['proposal_id']]}): Top-10 reduction {100*sc['top10_relative_improvement']:.3f}\\%, global change {100*sc['global_delay_relative_change']:+.3f}\\%, neighboring change {100*sc['neighbor_delay_relative_change']:+.3f}\\%; {'passes' if sc['constraints_passed'] else 'fails'} final constraints.")
        lines += ['For the second child, the first selected semantic action is excluded; remaining IDs retain their original labels. The parent feedback and initial-only history remain unchanged.']
    lines += [r'\subsection*{Composition, continued feedback, and final screening}']
    for c in r['candidates']:
        p=c['proposal']
        if p['action']=='bundle' and not p.get('refinement'):
            lines += ['Bundle '+aliases[p['proposal_id']]+' executes '+', then '.join(aliases[x['proposal_id']] for x in p['proposals'])+' from the baseline.']
    lines += [
        r'\begin{longtable}{llrrrrl}',r'\toprule Plan & Stage & $R_T$ (\%) & $\Delta_D$ (\%) & $\Delta_N$ (\%) & Teleports & Accept \\\midrule\endhead']
    for c in r['candidates']:
        p=c['proposal'];s=c['scope_objective'];stage='Joint' if p.get('refinement',{}).get('feedback_scope')=='joint_plan' else 'Bundle' if p['action']=='bundle' else 'Mutation' if p.get('refinement') else 'Initial'
        lines.append(f"{aliases[p['proposal_id']]} & {stage} & {100*s['top10_relative_improvement']:.3f} & {100*s['global_delay_relative_change']:+.3f} & {100*s['neighbor_delay_relative_change']:+.3f} & {c['optimized']['kpi']['teleport_count']} & {'Yes' if s['constraints_passed'] else 'No'} \\\\")
    lines += [r'\bottomrule\end{longtable}']
    for rd in r['joint_rounds']:
        for d in rd['decisions']:
            p=d.get('proposal')
            if p:
                lines.append(f"Joint round {rd['round']}: {aliases[d['parent_id']]} $\\rightarrow$ {aliases[p['proposal_id']]}, modifying component {p['refinement']['selected_component_index']+1} via \\texttt{{{tex(p['refinement']['selected_mutation_id'])}}}. The unchanged component and full conditional catalog are retained in the trace.")
    selected=r['selected'];lines += [f"The retained plan is {aliases[selected['proposal']['proposal_id']]}, an initial intervention, with {100*selected['scope_objective']['top10_relative_improvement']:.3f}\\% Top-10 reduction. The run uses {r['logical_sumo_evaluations']} logical candidate evaluations. All five initial proposals are measured before parent selection; neither added joint search nor a plausible explanation is assumed to improve the incumbent."]
    (GEN/'worked_example.tex').write_text('\n\n'.join(lines)+'\n')

if __name__=='__main__':main()
