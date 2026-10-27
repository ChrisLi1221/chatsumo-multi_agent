#!/usr/bin/env python3
"""Restore original figure roles using the current protocol and saved measurements.

No LLM requests or SUMO runs. The original intervention artwork is copied intact.
The workflow is a new vector diagram; result charts are regenerated from CSVs.
"""
from pathlib import Path
from collections import Counter
import csv
import hashlib
import json
import shutil
import subprocess

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / 'paper/multra_revision'
FIG = PAPER / 'figures_current'
DATA = ROOT / 'experiment/wide_feedback_v1/analysis'
EVIDENCE = PAPER / 'supplementary_current/figure_audit'
ARMS = ['llm', 'catalog_random', 'catalog_first']
LABELS = ['LLM', 'Random', 'Rule']
COLORS = ['#3F6FA8', '#E59D2F', '#4C9B88']
CITIES = ['albany', 'troy', 'ann_arbor', 'los_angeles', 'manhattan']


def rows(name):
    with (DATA / name).open() as f:
        return list(csv.DictReader(f))


def save(fig, name):
    for suffix in ['pdf', 'png']:
        fig.savefig(FIG / f'{name}.{suffix}', dpi=300, bbox_inches='tight')
    plt.close(fig)


def workflow():
    fig, ax = plt.subplots(figsize=(11, 6.7))
    ax.set(xlim=(0, 11), ylim=(0, 6.7)); ax.axis('off')
    def box(x, y, title, body, color):
        ax.add_patch(FancyBboxPatch((x, y), 3.2, 2.05,
            boxstyle='round,pad=0.04,rounding_size=0.12',
            facecolor=color, edgecolor='#35485b', linewidth=1.15))
        ax.text(x+.15, y+1.79, title, fontsize=12, weight='bold', va='top')
        ax.text(x+.15, y+1.34, body, fontsize=11, va='top', linespacing=1.45)
    box(.15, 4.3, '1  Scenario and baseline',
        'Network, demand, SUMO seed\nBaseline run and diagnosis\nFrozen Top-10 and neighbors\nNetwork and edge KPIs', '#edf2f7')
    box(3.9, 4.3, '2  Shared initialization',
        'Five grounded initial actions\nValidate and measure each\nShared across selectors\nSelect two measured parents', '#eaf3fc')
    box(7.65, 4.3, '3  Refine single plans',
        'Signal / network / routing roles\nLegal mutation catalogs\nLLM / Random / Rule selects\nUp to two children per parent', '#eaf3fc')
    box(7.65, 1.35, '4  Compose plans',
        'Four top-ranked singles\nCheck pair compatibility\nUp to two ordered bundles\nJoint SUMO evaluation', '#f0ecf7')
    box(3.9, 1.35, '5  Joint-feedback loop',
        'Two rounds; beam width two\nChange one component field\nFix partner; re-simulate\nChildren can become parents', '#f0ecf7')
    box(.15, 1.35, '6  Constrained selection',
        'Measured plans + baseline\nTop-10, delay, spillover checks\nThroughput / teleport checks\nBest eligible plan, else no-op', '#eaf4ee')
    for start, end in [((3.4,5.3),(3.84,5.3)),((7.15,5.3),(7.59,5.3)),
                       ((9.25,4.24),(9.25,3.46)),((7.59,2.35),(7.16,2.35)),
                       ((3.84,2.35),(3.41,2.35))]:
        ax.add_patch(FancyArrowPatch(start,end,arrowstyle='-|>',mutation_scale=17,
                                    color='#35485b',linewidth=1.5))
    ax.add_patch(FancyArrowPatch((6.55,1.32),(4.45,1.32),
        connectionstyle='arc3,rad=-.6',arrowstyle='-|>',mutation_scale=16,
        color='#77559b',linewidth=1.5))
    ax.text(5.5,.48,'Measured joint feedback',ha='center',fontsize=10,color='#634884')
    ax.text(5.5,.06,'Adaptive cap: 15 candidate evaluations; baseline separate. Cached evaluations consume budget.',
            ha='center',fontsize=10)
    save(fig,'framework_current')


def main():
    FIG.mkdir(exist_ok=True); EVIDENCE.mkdir(parents=True,exist_ok=True)
    plt.rcParams.update({'pdf.fonttype':42, 'ps.fonttype':42, 'font.size':10,
                         'axes.spines.top':False, 'axes.spines.right':False})
    original=ROOT/'paper/chatsumo_optimizer/figures/Optimization_cropped.pdf'
    target=FIG/'intervention_mechanisms_original.pdf'
    shutil.copyfile(original,target)
    subprocess.run(['pdftoppm','-f','1','-l','1','-r','300','-png','-singlefile',
                    str(target),str(target.with_suffix(''))],check=True)
    assert original.read_bytes()==target.read_bytes()
    framework_original=ROOT/'paper/chatsumo_optimizer/figures/Framework.pdf'
    framework_target=FIG/'Framework.pdf'
    shutil.copyfile(framework_original,framework_target)
    subprocess.run(['pdftoppm','-f','1','-l','1','-r','300','-png','-singlefile',
                    str(framework_target),str(framework_target.with_suffix(''))],check=True)
    assert framework_original.read_bytes()==framework_target.read_bytes()
    # Keep original and code-drawn alternatives. The author-requested image-generated
    # Framework_updated asset is preserved separately and used by the current main.tex.
    workflow()
    cases=rows('case_results.csv'); candidates=rows('candidate_results.csv')
    assert len(cases)==45 and all(r['repeat']=='0' for r in cases)
    assert len({(r['case'],r['arm']) for r in cases})==45
    stats={}; detail=[]
    fig,axes=plt.subplots(1,2,figsize=(12,4.3),gridspec_kw={'width_ratios':[1.65,1]},layout='constrained')
    x=np.arange(5); width=.24
    for i,(arm,label,color) in enumerate(zip(ARMS,LABELS,COLORS)):
        cr=[r for r in cases if r['arm']==arm]; ca=[r for r in candidates if r['arm']==arm]
        assert len(cr)==15
        means=[]; sds=[]
        for city in CITIES:
            rr=[r for r in cr if r['city']==city]; assert len(rr)==3
            vals=[float(r['top10']) for r in rr]
            means.append(float(np.mean(vals)));sds.append(float(np.std(vals,ddof=1)))
            detail.append(dict(arm=arm,city=city,n=3,top10_mean=means[-1],top10_sd=sds[-1]))
        axes[0].bar(x+(i-1)*width,means,width,yerr=sds,capsize=3,color=color,label=label,
                    error_kw={'linewidth':1,'ecolor':'#515151'})
        evaluated=[r for r in ca if r['sumo_evaluated']=='True']
        accepted=[r for r in evaluated if r['accepted']=='True']
        successes=sum(r['success']=='True' for r in cr)
        assert len(evaluated)==sum(int(r['evals']) for r in cr)
        assert all(r['sumo_evaluated']=='True' for r in ca if r['accepted']=='True')
        stats[arm]=dict(cases=len(cr),successes=successes,evaluations=len(evaluated),
                        accepted_evaluations=len(accepted),top10_mean=float(np.mean([float(r['top10']) for r in cr])))
        values=[100*successes/len(cr),100*len(accepted)/len(evaluated)]
        for j,v in enumerate(values):
            axes[1].bar(i+(j-.5)*.34,v,.34,color=color,alpha=1 if j==0 else .5,hatch=None if j==0 else '//')
            axes[1].text(i+(j-.5)*.34,v+1.8,f'{v:.1f}',ha='center',fontsize=9)
    axes[0].set(xticks=x,xticklabels=[c.replace('_',' ').title() for c in CITIES],
                ylabel='Top-10 time-loss reduction (%)',title='(a) Adaptive search by network')
    axes[0].tick_params(axis='x',labelrotation=12);axes[0].legend(ncol=3,fontsize=9)
    axes[1].set(xticks=range(3),xticklabels=LABELS,ylim=(0,116),ylabel='Rate (%)',title='(b) Success and accepted evaluations')
    from matplotlib.patches import Patch
    axes[1].legend(handles=[Patch(facecolor='#888888',label='Case success'),
                            Patch(facecolor='#cccccc',hatch='//',label='Accepted evaluation share')],fontsize=8,loc='upper right')
    for ax in axes:ax.set_axisbelow(True);ax.grid(axis='y',alpha=.25)
    save(fig,'proposal_source_comparison_current')

    action_groups=[('tls_optimize_and_apply','Signal','#3F6FA8'),('set_corridor_speed','Corridor speed','#E59D2F'),
                   ('bundle','Bundle','#8064A2'),('no_op','No-op','#A6ADB5')]
    stage_groups=[('initial','Initial','#97B4D5'),('mutation','Single\nrefinement','#3F6FA8'),
                  ('bundle','Initial\nbundle','#AC94C5'),('joint','Joint\nrefinement','#8064A2'),('no_op','No-op','#A6ADB5')]
    for arm in ARMS:
        selected=[r for r in candidates if r['arm']==arm and r['selected']=='True']
        ac=Counter(r['action'] for r in selected);sc=Counter(r['stage'] for r in selected)
        for case in [r for r in cases if r['arm']==arm]:
            match=[r for r in selected if r['case']==case['case']]
            if case['success']=='True':
                assert len(match)==1 and match[0]['proposal_id']==case['selected']
            else:assert not match
        ac['no_op']=15-len(selected);sc['no_op']=15-len(selected)
        assert set(ac)<=set(x[0] for x in action_groups)
        assert set(sc)<=set(x[0] for x in stage_groups)
        assert sum(ac.values())==sum(sc.values())==15
        stats[arm]['selected_actions']=dict(ac);stats[arm]['selected_stages']=dict(sc)
    fig,axes=plt.subplots(1,2,figsize=(12,4),gridspec_kw={'width_ratios':[1.2,1]},layout='constrained')
    left=np.zeros(3)
    for key,label,color in action_groups:
        v=np.array([stats[arm]['selected_actions'].get(key,0) for arm in ARMS])
        axes[0].barh(range(3),v,left=left,color=color,label=label,height=.62)
        for i,n in enumerate(v):
            if n:axes[0].text(left[i]+n/2,i,str(n),ha='center',va='center',weight='bold',color='white' if key!='no_op' else '#333333')
        left+=v
    axes[0].set(yticks=range(3),yticklabels=LABELS,xticks=range(0,16,3),xlim=(0,15),xlabel='Selected cases',title='(a) Final intervention composition')
    axes[0].invert_yaxis();axes[0].legend(loc='upper center',bbox_to_anchor=(.5,-.2),ncol=4,fontsize=8)
    v=[stats['llm']['selected_stages'].get(key,0) for key,_,_ in stage_groups]
    axes[1].bar(range(5),v,color=[c for _,_,c in stage_groups])
    for i,n in enumerate(v):axes[1].text(i,n+.12,str(n),ha='center')
    axes[1].set(xticks=range(5),xticklabels=[l for _,l,_ in stage_groups],ylabel='Cases',ylim=(0,max(v)+1.3),title='(b) Origin of selected LLM plans')
    for ax in axes:ax.set_axisbelow(True);ax.grid(axis='y' if ax==axes[1] else 'x',alpha=.25)
    save(fig,'proposal_source_action_distribution_current')
    provenance={'cohort':'wide_feedback_v1, primary repeat 0 only, 15 cases per arm',
                'source_hashes':{f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in [DATA/'case_results.csv',DATA/'candidate_results.csv']},
                'original_mechanism_sha256':hashlib.sha256(original.read_bytes()).hexdigest(),
                'original_framework_sha256':hashlib.sha256(framework_original.read_bytes()).hexdigest(),
                'definition':'Accepted evaluation share = accepted recorded candidates / SUMO-evaluated candidates, including cache reuse; not new-SUMO efficiency. Selected bundle includes initial and joint-refined bundles.',
                'statistics':stats}
    (EVIDENCE/'figure_statistics.json').write_text(json.dumps(provenance,indent=2)+'\n')
    with (EVIDENCE/'city_results.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(detail[0]));w.writeheader();w.writerows(detail)
    print(json.dumps(stats,indent=2))


if __name__=='__main__':main()
