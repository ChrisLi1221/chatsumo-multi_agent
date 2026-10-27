#!/usr/bin/env python3
"""Recompute manuscript statistics, threshold sweeps, and edge-level side effects.

Offline analysis only. No API/SUMO calls. All no-op outcomes count as zero.
Sensitivity reselects from SAVED pools, not from counterfactual new searches.
"""
from __future__ import annotations
import csv
import itertools
import json
import os
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
import numpy as np
from scipy import stats
os.environ.setdefault('MPLCONFIGDIR','/tmp/simtio_matplotlib')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
OUT=ROOT/'experiment/multra_revision/analysis'
PAPER=ROOT/'paper/multra_revision'
LABELS={'llm':'LLM','catalog_first':'Rule','catalog_random':'Random',
        'grounded_random':'Whole-pipeline random','deterministic_heuristic':'Whole-pipeline heuristic'}

def read(p): return json.loads(Path(p).read_text())
def write(p,o):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(o,indent=2,ensure_ascii=False,default=str))
def csvwrite(p,rows):
    p=Path(p);p.parent.mkdir(parents=True,exist_ok=True)
    if not rows:return
    with p.open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
def mean(a): return float(np.mean(a))
def std(a): return float(np.std(a,ddof=1)) if len(a)>1 else 0.

def load_run(run,arm,cohort):
    d=read(run/'06_comparison.json');b=d['baseline_kpi']; pool=[]
    for row in d['candidate_ranking']:
        if row.get('is_baseline') or row['status']!='ok':continue
        folder=run/('bundles' if row['action']=='bundle' else 'candidates')/row['proposal_id']
        c=read(folder/'06_comparison.json');m=read(folder/'04_modification.json')['result']
        pool.append(dict(proposal_id=row['proposal_id'],action=row['action'],scope=c['scope_objective'],
                         kpi=c['optimized_kpi'],baseline=b,folder=str(folder),
                         route_validation=m.get('route_validation'),kpi_score=c['kpi_score']))
    met=d['proposal_source_metrics'];return dict(case=d['case_id'],city=d['case_id'].split('_equal_scale')[0],arm=arm,
            cohort=cohort,run=str(run),pool=pool,baseline=b,top10=100*d['scope_objective']['top10_relative_improvement'],
            global_reduction=-100*d['scope_objective']['global_delay_relative_change'],
            selected=d['selected_proposal']['proposal_id'],success=not d['baseline_retained'],
            evals=d['sumo_evaluation_count'],counts=met['counts'],model=read(run/'00_metadata.json')['llm'].get('model'))

def load_all():
    out=[]
    cases=[json.loads(s)['id'] for s in (ROOT/'experiment/selector-ablation-cases.jsonl').read_text().splitlines()]
    for case in cases:
        seed=case.rsplit('_',1)[1]
        llm=ROOT/f'experiment/luna-five-city-legal-mutation-seed{seed}'/case/'run_001'
        out.append(load_run(llm,'llm','historical_matched'))
        for arm in ['catalog_first','catalog_random']:
            p=ROOT/'experiment/selector-ablation'/case/'run_001'/f'random_initial_{arm}'
            out.append(load_run(p,arm,'historical_matched'))
        out.append(load_run(llm,'llm','original_pipeline'))
        for arm in ['grounded_random','deterministic_heuristic']:
            p=ROOT/f'experiment/luna-vs-baselines-legal-mutation-seed{seed}'/case/'run_001'/arm
            out.append(load_run(p,arm,'original_pipeline'))
    for p in sorted((ROOT/'experiment/multra_revision/runs').glob('*/*/result.json')):
        d=read(p);pool=[]
        for c in d['candidates']:
            if c['status']!='ok':continue
            pool.append(dict(proposal_id=c['proposal']['proposal_id'],action=c['proposal']['action'],scope=c['scope_objective'],
                 kpi=c['optimized']['kpi'],baseline=d['baseline']['optimized']['kpi'],folder=str(ROOT/c['cache_source']),
                 route_validation=c['modification'].get('route_validation'),kpi_score=c['kpi_score']))
        s=d['selected'];sc=s['scope_objective']
        out.append(dict(case=d['case'],city=d['case'].split('_equal_scale')[0],arm=d['arm'],cohort='fresh' if d['repeat']==0 else 'repeat',
                        repeat=d['repeat'],run=str(p.parent),pool=pool,baseline=d['baseline']['optimized']['kpi'],
                        top10=100*sc['top10_relative_improvement'],global_reduction=-100*sc['global_delay_relative_change'],
                        selected=s['proposal']['proposal_id'],success=not s.get('is_baseline',False),evals=d['logical_sumo_evaluations'],
                        counts={},model=(d['model'] or {}).get('model'),new_sumo=d['new_sumo_evaluations']))
    return out

def eligible(c,t=.01,g=.01,n=.05):
    s=c['scope'];r=c.get('route_validation') or {}
    return (s.get('available',False) and s['top10_relative_improvement']>=t-1e-12
            and s['global_delay_relative_change']<=g+1e-12
            and (s['neighbor_delay_relative_change'] is None or s['neighbor_delay_relative_change']<=n+1e-12)
            and s['throughput_change']>=-1e-12 and s['teleport_change']<=0
            and r.get('ok',True))

def reselect(run,t=.01,g=.01,n=.05):
    feasible=[c for c in run['pool'] if eligible(c,t,g,n)]
    best=max(feasible,key=lambda c:(c['scope']['top10_relative_improvement'],-c['kpi_score'])) if feasible else None
    # Baseline normalized weighted score is 0.5+0.1-0.25-0.15=0.2.
    # Keep its tie preference even when the improvement threshold is zero.
    if best and (best['scope']['top10_relative_improvement'],-best['kpi_score'])<=(0.,-.2): return None
    return best

def paired(a,b):
    keys=sorted(a.keys()&b.keys());d=np.array([a[k]['top10']-b[k]['top10'] for k in keys]);d[np.abs(d)<1e-9]=0
    if not len(d):return {}
    cities=sorted(set(a[k]['city'] for k in keys));cd=np.array([mean([d[j] for j,k in enumerate(keys) if a[k]['city']==c]) for c in cities])
    rng=np.random.default_rng(20260927)
    boot=np.mean(rng.choice(cd,size=(20000,len(cd)),replace=True),axis=1)
    signs=np.array(list(itertools.product([-1,1],repeat=len(d))))
    csigns=np.array(list(itertools.product([-1,1],repeat=len(cd))))
    return dict(n=len(d),mean_difference_pp=mean(d),median_difference_pp=float(np.median(d)),
                wins=int(sum(d>0)),ties=int(sum(d==0)),losses=int(sum(d<0)),
                wilcoxon_p=float(stats.wilcoxon(d,zero_method='wilcox',method='approx',correction=False).pvalue) if np.any(d) else 1.,
                original_zsplit_auto_p=float(stats.wilcoxon(d,zero_method='zsplit',method='auto',correction=False).pvalue) if np.any(d) else 1.,
                exact_case_signflip_p=mean(np.abs(signs@d/len(d))>=abs(mean(d))-1e-12),
                exact_city_signflip_p=mean(np.abs(csigns@cd/len(cd))>=abs(mean(cd))-1e-12),
                city_cluster_bootstrap_ci95_pp=np.quantile(boot,[.025,.975]).tolist(),
                paired_differences=[dict(case=k,difference_pp=float(x)) for k,x in zip(keys,d)])

@lru_cache(maxsize=128)
def edge_losses(path):
    loss=defaultdict(float)
    for event,e in ET.iterparse(path,events=['end']):
        if e.tag=='edge':loss[e.get('id')]+=float(e.get('timeLoss','0'));e.clear()
    return dict(loss)

@lru_cache(maxsize=10)
def geometry(path):
    root=ET.parse(path).getroot();out={}
    for e in root.findall('edge'):
        if e.get('function')=='internal':continue
        lane=e.find('lane')
        if lane is None:continue
        points=[list(map(float,x.split(','))) for x in lane.get('shape','').split()]
        if not points:continue
        xy=np.mean(points,axis=0)
        out[e.get('id')]=dict(name=e.get('name',''),x=float(xy[0]),y=float(xy[1]))
    return out

def spatial(run):
    # Selected outcomes including no-op. Scope definition stays baseline-frozen.
    folder=Path(run['run']);base=run['baseline'];target=set(base['focus_scope']['target']['edge_ids']);neighbors=set(base['focus_scope']['neighbors']['edge_ids'])
    if run['cohort'] in ['fresh','repeat']:
        raw=read(folder/'result.json');ba=raw['baseline']['optimized']['artifacts'];sa=raw['selected']['optimized']['artifacts']
    else:
        ba=read(folder/'01_baseline.json')['artifacts']
        if run['selected']=='baseline_noop':sa=ba
        else:
            c=next(c for c in run['pool'] if c['proposal_id']==run['selected']);sa=read(Path(c['folder'])/'05_optimized.json')['artifacts']
    b=edge_losses(ba['edgedata']);s=edge_losses(sa['edgedata']);geo=geometry(ba['net']);edges=sorted(set(b)|set(s));remote=[e for e in edges if e not in target|neighbors]
    delta={e:s.get(e,0)-b.get(e,0) for e in edges}
    # Confirm edge parser matches the objective saved in the experiment.
    assert abs(sum(b.get(e,0) for e in target)-base['focus_scope']['target']['total_time_loss_s'])<1e-5
    regional=defaultdict(float)
    for e in remote:
        if e in geo:regional[(int(geo[e]['x']//500),int(geo[e]['y']//500))]+=delta[e]
    worst=sorted(remote,key=lambda e:delta[e],reverse=True)[:10]
    topxy=np.array([[geo[e]['x'],geo[e]['y']] for e in target if e in geo])
    details=[]
    for e in worst:
        info=geo.get(e,{})
        distance=float(np.min(np.linalg.norm(topxy-[info['x'],info['y']],axis=1))) if info and len(topxy) else None
        details.append(dict(case=run['case'],cohort=run['cohort'],arm=run['arm'],edge=e,name=info.get('name',''),
                            baseline_loss_s=b.get(e,0),selected_loss_s=s.get(e,0),increase_s=delta[e],
                            nearest_target_midpoint_m=distance))
    vals=np.array([delta[e] for e in edges]);rv=np.array([delta[e] for e in remote])
    summary=dict(case=run['case'],cohort=run['cohort'],arm=run['arm'],selected=run['selected'],
                 active_union_edges=len(edges),outside_one_hop_edges=len(remote),
                 fraction_edges_worsened=mean(vals>1e-6),p95_signed_edge_change_s=float(np.quantile(vals,.95)),
                 max_remote_increase_s=max(0.,float(max(rv,default=0.))),
                 remote_positive_loss_s=float(sum(rv[rv>0])),
                 max_remote_500m_cell_net_increase_s=max(0.,max(regional.values(),default=0.)),
                 remote_edges_increase_over_60s=int(sum(rv>60)),
                 remote_edges_over20pct_and60s=sum(delta[e]>60 and delta[e]>.2*b.get(e,0) for e in remote),
                 max_remote_edge=worst[0] if worst else None)
    return summary,details,rv

def main():
    OUT.mkdir(parents=True,exist_ok=True);(PAPER/'generated').mkdir(parents=True,exist_ok=True);(PAPER/'figures').mkdir(exist_ok=True)
    runs=load_all();summary={};pairs={};tables=[]
    for cohort in ['original_pipeline','historical_matched','fresh']:
        rr=[r for r in runs if r['cohort']==cohort]
        if not rr:continue
        summary[cohort]={}
        for arm in sorted(set(r['arm'] for r in rr)):
            subset=[r for r in rr if r['arm']==arm]
            summary[cohort][arm]=dict(n=len(subset),top10_mean=mean([r['top10'] for r in subset]),top10_sd=std([r['top10'] for r in subset]),
                global_mean=mean([r['global_reduction'] for r in subset]),successes=sum(r['success'] for r in subset),
                evaluations=sum(r['evals'] for r in subset),
                feasible_improving=sum(sum(eligible(c) for c in r['pool']) for r in subset),
                successful_simulations=sum(len(r['pool']) for r in subset))
        a={r['case']:r for r in rr if r['arm']=='llm'}
        pairs[cohort]={arm:paired(a,{r['case']:r for r in rr if r['arm']==arm}) for arm in summary[cohort] if arm!='llm'}
        valid=[(arm,p) for arm,p in pairs[cohort].items() if p]
        ranked=sorted(valid,key=lambda x:x[1]['wilcoxon_p']);prev=0
        for i,(arm,p) in enumerate(ranked):prev=max(prev,min(1.,(len(ranked)-i)*p['wilcoxon_p']));p['holm_wilcoxon_p']=prev
        lines=[r'\begin{tabular}{lrrrr}',r'\toprule',r'Selector & Top-10 (\%) & Global (\%) & Success & Evaluations \\',r'\midrule']
        for arm,s in summary[cohort].items():lines.append(f"{LABELS[arm]} & {s['top10_mean']:.2f} $\\pm$ {s['top10_sd']:.2f} & {s['global_mean']:.2f} & {s['successes']}/{s['n']} & {s['evaluations']} \\\\")
        lines += [r'\bottomrule',r'\end{tabular}'];(PAPER/'generated'/f'{cohort}.tex').write_text('\n'.join(lines))
    write(OUT/'summary.json',summary);write(OUT/'paired_statistics.json',pairs)
    csvwrite(OUT/'case_results.csv',[{k:r.get(k) for k in ['cohort','case','arm','repeat','top10','global_reduction','success','evals','selected','run']} for r in runs])
    # Recover each baseline decision exactly before alternative thresholds.
    checks=[]
    for r in runs:
        s=reselect(r);observed=s['scope']['top10_relative_improvement']*100 if s else 0.
        checks.append(dict(case=r['case'],arm=r['arm'],cohort=r['cohort'],difference_pp=observed-r['top10']))
        assert abs(observed-r['top10'])<1e-7,(r['case'],r['arm'],observed,r['top10'])
    write(OUT/'reselection_verification.json',checks)
    thresholds=list(itertools.product([0.,.005,.01,.02,.05],[0.,.005,.01,.02,.05],[0.,.025,.05,.1]))
    sweep=[]
    for cohort in ['original_pipeline','historical_matched','fresh']:
        rr=[r for r in runs if r['cohort']==cohort]
        for t,g,n in thresholds:
            for arm in sorted(set(r['arm'] for r in rr)):
                ss=[reselect(r,t,g,n) for r in rr if r['arm']==arm]
                sweep.append(dict(cohort=cohort,arm=arm,top10_threshold=t,global_limit=g,neighbor_limit=n,
                    n=len(ss),successes=sum(s is not None for s in ss),mean_top10=mean([100*s['scope']['top10_relative_improvement'] if s else 0 for s in ss])))
    csvwrite(OUT/'threshold_sensitivity.csv',sweep)
    # Summaries of strict baseline and single-axis changes; all 100 grids are retained.
    chosen=[(.01,.01,.05),(.005,.01,.05),(.02,.01,.05),(.01,0.,.05),(.01,.02,.05),(.01,.01,0.),(.01,.01,.10)]
    text=[r'\begin{tabular}{rrrrrr}',r'\toprule',r'$\tau_T$ & $\tau_G$ & $\tau_N$ & LLM & Rule & Random \\',r'\midrule']
    figure_cohort='fresh' if all(summary.get('fresh',{}).get(a,{}).get('n')==15 for a in ['llm','catalog_first','catalog_random']) else 'historical_matched'
    for t,g,n in chosen:
        ss=[next(x for x in sweep if x['cohort']==figure_cohort and x['arm']==a and (x['top10_threshold'],x['global_limit'],x['neighbor_limit'])==(t,g,n)) for a in ['llm','catalog_first','catalog_random']]
        text.append(f'{100*t:g} & {100*g:g} & {100*n:g} & '+ ' & '.join(f"{x['successes']}/15 ({x['mean_top10']:.2f})" for x in ss)+r' \\')
    text += [r'\bottomrule',r'\end{tabular}'];(PAPER/'generated/sensitivity.tex').write_text('\n'.join(text))
    spatial_rows=[];worst_rows=[];distributions=defaultdict(list)
    for r in runs:
        if r['cohort'] not in ['historical_matched','fresh']:continue
        s,w,rv=spatial(r);spatial_rows.append(s);worst_rows.extend(w)
        if r['cohort']==figure_cohort:distributions[r['arm']].extend(rv.tolist())
    csvwrite(OUT/'spatial_selected.csv',spatial_rows);csvwrite(OUT/'worst_remote_edges.csv',worst_rows)
    spatial_summary={}
    for arm in ['llm','catalog_first','catalog_random']:
        ss=[s for s in spatial_rows if s['cohort']=='historical_matched' and s['arm']==arm]
        spatial_summary[arm]=dict(cases_with_remote_over60s=sum(s['max_remote_increase_s']>60 for s in ss),
            median_max_remote_s=float(np.median([s['max_remote_increase_s'] for s in ss])),
            maximum_remote_s=max(s['max_remote_increase_s'] for s in ss),
            maximum_cell_s=max(s['max_remote_500m_cell_net_increase_s'] for s in ss))
    write(OUT/'spatial_summary.json',spatial_summary)
    fresh_spatial={}
    for arm in ['llm','catalog_first','catalog_random']:
        ss=[s for s in spatial_rows if s['cohort']=='fresh' and s['arm']==arm]
        if ss:
            fresh_spatial[arm]=dict(cases_with_remote_over60s=sum(s['max_remote_increase_s']>60 for s in ss),
                median_max_remote_s=float(np.median([s['max_remote_increase_s'] for s in ss])),
                maximum_remote_s=max(s['max_remote_increase_s'] for s in ss),
                maximum_cell_s=max(s['max_remote_500m_cell_net_increase_s'] for s in ss))
    write(OUT/'fresh_spatial_summary.json',fresh_spatial)
    # One compact figure separates effect sizes, threshold sensitivity and spatial tail.
    fig,ax=plt.subplots(1,3,figsize=(13,3.6),layout='constrained')
    arms=['llm','catalog_first','catalog_random'];colors=['#176b99','#e3a12d','#49805b']
    for i,a in enumerate(arms):
        rr=[r for r in runs if r['cohort']==figure_cohort and r['arm']==a]
        ax[0].scatter(np.full(len(rr),i),[r['top10'] for r in rr],alpha=.65,color=colors[i],s=24)
        ax[0].plot([i-.2,i+.2],[mean([r['top10'] for r in rr])]*2,color='black',lw=2)
        ss=[s for s in sweep if s['cohort']==figure_cohort and s['arm']==a and s['top10_threshold']==.01 and s['neighbor_limit']==.05]
        ax[1].plot([100*s['global_limit'] for s in ss],[100*s['successes']/s['n'] for s in ss],'-o',color=colors[i],label=LABELS[a])
        vals=np.sort(np.array(distributions[a]));vals=vals[vals>1e-6]
        ax[2].plot(vals,np.arange(1,len(vals)+1)/len(vals),color=colors[i],label=LABELS[a])
    ax[0].set(xticks=range(3),xticklabels=['LLM','Rule','Random'],ylabel='Top-10 reduction (%)',title='(a) Matched cases (n=15)')
    ax[1].set(xlabel='Allowed global delay increase (%)',ylabel='Feasible-plan success (%)',title='(b) Saved-pool reselection');ax[1].legend(fontsize=8)
    ax[2].set(xlabel='Positive time-loss change per remote edge (s)',ylabel='Empirical cumulative probability',title='(c) Outside one-hop neighborhood',xscale='log')
    for x in ax:x.spines[['top','right']].set_visible(False)
    fig.savefig(PAPER/'figures/revision_results.pdf');fig.savefig(PAPER/'figures/revision_results.png',dpi=160);plt.close(fig)
    # Audit all numerical source lines, preserving exact original context.
    source=(ROOT/'paper/chatsumo_optimizer/main.tex').read_text().splitlines()
    numeric=[dict(line=i,text=l) for i,l in enumerate(source,1) if re.search(r'\d',l) and not l.lstrip().startswith('%')]
    csvwrite(OUT/'original_numeric_lines.csv',numeric)
    # LLM resampling is conditional on fixed city-demand input, including failed requests.
    rep=[]
    for case in sorted(set(r['case'] for r in runs if r['cohort']=='repeat')):
        rr=[r for r in runs if r['case']==case and r['cohort'] in ['fresh','repeat'] and r['arm']=='llm']
        ds=[read(Path(r['run'])/'result.json')['decisions'] for r in rr]
        choices=[]
        for j in range(2):
            ids=[x[j].get('proposal',{}).get('refinement',{}).get('selected_mutation_id') or 'FAILED' for x in ds]
            counts=Counter(ids);probs=np.array(list(counts.values()))/len(ids)
            choices.append(dict(parent_index=j,counts=dict(counts),entropy_bits=float(-sum(probs*np.log2(probs)))))
        rep.append(dict(case=case,n=len(rr),successes=sum(r['success'] for r in rr),mean_top10=mean([r['top10'] for r in rr]),
                        sd_top10=std([r['top10'] for r in rr]),min_top10=min(r['top10'] for r in rr),max_top10=max(r['top10'] for r in rr),choices=choices))
    write(OUT/'llm_stability.json',rep)
    llm_cases=[r for r in runs if r['cohort']=='historical_matched' and r['arm']=='llm']
    pareto=[]
    for r in llm_cases:
        pool=[c for c in r['pool'] if eligible(c)]
        ids=[c['proposal_id'] for c in pool]+['baseline_noop']
        vectors=[[-c['scope']['top10_relative_improvement'],c['scope']['global_delay_relative_change'],
                  c['kpi']['co2']/r['baseline']['co2'],-c['kpi']['mean_speed']/r['baseline']['mean_speed']] for c in pool]+[[0,0,1,-1]]
        vectors=np.array(vectors)
        front=[ids[i] for i in range(len(vectors)) if not any(np.all(x<=vectors[i]+1e-9) and np.any(x<vectors[i]-1e-9) for x in vectors)]
        pareto.append(dict(case=r['case'],selected=r['selected'],front=front,selected_nondominated=r['selected'] in front))
    write(OUT/'corrected_pareto.json',pareto)
    runtime=[]
    for p in sorted((ROOT/'experiment/multra_revision/runs').glob('*/*/result.json')):
        d=read(p);runtime.append(dict(case=d['case'],arm=d['arm'],repeat=d['repeat'],
             logical=d['logical_sumo_evaluations'],new=d['new_sumo_evaluations'],reused=d['reused_evaluations'],
             calls=d['calls'],failed_decisions=sum(x['status'] not in ['ok'] for x in d['decisions']),
             tokens=d.get('token_usage',{})))
    write(OUT/'execution_accounting.json',runtime)
    print(json.dumps({'summary':summary,'paired':{k:{a:{q:v for q,v in s.items() if q!='paired_differences'} for a,s in ps.items()} for k,ps in pairs.items()},'spatial':spatial_summary,'stability':rep},indent=2))

if __name__=='__main__':main()
