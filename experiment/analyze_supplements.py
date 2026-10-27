#!/usr/bin/env python3
"""Audited stability and demand summaries; no API calls or traffic simulation."""
import argparse
from collections import Counter,defaultdict
import itertools
import math
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import analyze_multra_revision as a
from experiment.analyze_saved_selections import load
from experiment import full_feedback_experiment as full
from agents.debate_optimizer import _mutation_payload

LABELS={'llm':'LLM','catalog_first':'Rule','catalog_random':'Random'}
COLORS={'llm':'#3F6FA8','catalog_first':'#4C9B88','catalog_random':'#E59D2F'}

def summarize(rows):
    return dict(n=len(rows),top10_mean=a.mean([r['top10'] for r in rows]),top10_sd=a.std([r['top10'] for r in rows]),
        top10_min=min(r['top10'] for r in rows),top10_max=max(r['top10'] for r in rows),
        global_mean=a.mean([r['global_reduction'] for r in rows]),global_sd=a.std([r['global_reduction'] for r in rows]),
        successes=sum(r['success'] for r in rows),success_rate=a.mean([r['success'] for r in rows]),
        logical=sum(r['evals'] for r in rows))

def audit(index):
    rows=[];calls=[];audits=[];prompts={};baseline_rows={};specs={}
    for item in index:
        path=ROOT/item['path'];r,d=load(path,item['arm']);root=path.parents[3]
        key=(str(root),d['case'])
        if key not in specs:
            specs[key]=a.read(root/'frozen'/f"{d['case']}.json")
            for f in specs[key]['input_files'].values():assert full.m.file_hash(f['path'])==f['sha256']
        spec=specs[key]
        assert full.m.digest({k:v for k,v in spec.items() if k!='frozen_sha256'})==spec['frozen_sha256']
        assert d['frozen_sha256']==spec['frozen_sha256'] and d['protocol']['version']=='full_wide_v1'
        assert d['protocol']['initial_count']==5 and d['protocol']['children_per_parent']==2
        assert d['protocol']['budget']==15 and d['protocol']['joint_rounds']==2
        assert d['protocol'].get('joint_feedback_mode','aggregate')=='aggregate'
        assert full.m.digest(d['candidates'][:5])==full.m.digest(spec['initial'])
        assert d['logical_sumo_evaluations']==sum(bool(c.get('sumo_evaluated')) for c in d['candidates'])<=15
        assert d['logical_sumo_evaluations']==d['new_sumo_evaluations']+d['reused_evaluations']
        r.update({k:v for k,v in item.items() if k!='path'});r['seed']=int(d['sumo_seed']);r.setdefault('trips',2400)
        r.update(api_requests=d['api_requests'],new_sumo=d['new_sumo_evaluations'],reused=d['reused_evaluations'],
            mean_delay_s=d['selected']['optimized']['kpi']['mean_delay'],baseline_delay_s=spec['baseline']['kpi']['mean_delay'],
            baseline_teleports=spec['baseline']['kpi']['teleport_count'],
            selected_teleports=d['selected']['optimized']['kpi']['teleport_count'])
        rows.append(r)
        initial_best=full.m.opt._select_best_candidate([d['baseline']]+d['candidates'][:5],profile='delay',objective_scope='top10')
        r.update(initial_top10=100*initial_best['scope_objective']['top10_relative_improvement'],
            initial_success=not initial_best.get('is_baseline',False))
        baseline_rows[d['case']]=dict(case=d['case'],city=r['city'],seed=r['seed'],trips=r['trips'],
            mean_delay_s=spec['baseline']['kpi']['mean_delay'],teleports=spec['baseline']['kpi']['teleport_count'],
            completed=spec['baseline']['kpi']['completed_trips'],top10_loss_s=spec['baseline']['kpi']['focus_scope']['target']['total_time_loss_s'])
        assert full.m.candidate_seed(d['baseline'])==int(d['sumo_seed'])
        for c in [d['baseline']]+d['candidates']:
            proof=full.audit_execution(c,d['sumo_seed'],d['sumo_version'])
            audits.append(dict(case=d['case'],arm=d['arm'],repeat=d['repeat'],proposal=c['proposal']['proposal_id'],proof=proof))
        seen={}
        for dec in d['decisions']:
            parent=next(p for p in spec['decisions'] if p['parent']['proposal_id']==dec['parent'])
            sibling=seen.setdefault(dec['parent'],set());catalog=full.sibling_catalog(parent['catalog'],sibling)
            assert dec['catalog_sha256']==full.m.digest(catalog)
            prop=dec.get('proposal') or {}
            if prop.get('validation',{}).get('ok'):
                sig=full.plan_signature(prop)
                assert sig not in sibling and sig in {full.plan_signature(dict(action=x['action'],args=x['full_args'])) for x in catalog}
                sibling.add(sig)
        for cp in sorted((path.parent/'calls').glob('*.json')):
            call=a.read(cp);req=call['request'];p=req['payload'];initial=p['search_round']==2
            pid=p['parent_proposal']['proposal_id']
            if initial:
                dec=next(x for x in d['decisions'] if x['parent']==pid and x['child_index']==p['mutation_index'])
                parent=next(x for x in spec['decisions'] if x['parent']['proposal_id']==pid)
                assert p['parent_proposal']==parent['parent'] and p['simulation_feedback']==parent['feedback'] and p['evaluated_history']==spec['history']
                catalog=full.sibling_catalog(parent['catalog'],set(dec['excluded_sibling_signatures']))
                assert p['mutation_catalog']==_mutation_payload(catalog)
                if p['mutation_index']==1 and ':repair' not in req['scope']:
                    fingerprint=full.m.digest({k:req[k] for k in ['system','payload','scope','max_tokens']})
                    key=(d['case'],pid);prompts.setdefault(key,fingerprint);assert prompts[key]==fingerprint
            response=call.get('response') or {}
            calls.append(dict(case=d['case'],city=r['city'],trips=r['trips'],arm=d['arm'],repeat=d['repeat'],
                parent=pid,call=cp.name,status=call['status'],phase='single' if initial else 'joint',
                child=p['mutation_index'],request_sha256=full.m.digest({k:req[k] for k in ['system','payload','scope','max_tokens']}),
                selected=response.get('selected_mutation_id',response.get('mutation_id','FAILED')),
                repair=':repair' in req['scope'],
                finish_reasons=[x.get('finish_reason') for att in call['attempts'] for x in att['response_metadata'] if x.get('http_status')]))
        by_id={c['proposal']['proposal_id']:c for c in d['candidates']}
        for rr in d['joint_rounds']:
            for dec in rr['decisions']:
                parent=by_id[dec['parent_id']]
                assert dec['feedback']==full.m.opt._candidate_feedback(parent,spec['baseline']['kpi'],profile='delay',objective_scope='top10')
                prop=dec.get('proposal') or {}
                if prop:
                    changed=prop['refinement']['selected_component_index']
                    assert all(x==parent['proposal']['actions'][i] for i,x in enumerate(prop['actions']) if i!=changed)
    return rows,calls,audits,list(baseline_rows.values()),prompts

def main():
    p=argparse.ArgumentParser();p.add_argument('cohort',choices=['stability','demand']);args=p.parse_args()
    root=ROOT/f'experiment/supplement_{args.cohort}_v1';out=root/'analysis';out.mkdir(parents=True,exist_ok=True)
    index=a.read(root/'results_index.json');rows,calls,audits,baselines,prompts=audit(index)
    a.csvwrite(out/'case_results.csv',[{k:v for k,v in r.items() if k not in ['pool','baseline']} for r in rows])
    a.csvwrite(out/'selection_requests.csv',calls);a.csvwrite(out/'baseline_results.csv',baselines)
    metadata=[meta for item in index if item['arm']=='llm'
        for cp in sorted(((ROOT/item['path']).parent/'calls').glob('*.json'))
        for attempt in a.read(cp)['attempts'] for meta in attempt['response_metadata']]
    a.write(out/'model_metadata_summary.json',dict(
        response_models=dict(Counter(x.get('response_model') for x in metadata if x.get('http_status'))),
        finish_reasons=dict(Counter(x.get('finish_reason') for x in metadata if x.get('http_status'))),
        prompt_tokens=sum(x.get('usage',{}).get('prompt_tokens',0) for x in metadata),
        completion_tokens=sum(x.get('usage',{}).get('completion_tokens',0) for x in metadata),
        transport_failures=sum(x.get('http_status') is None for x in metadata)))
    raw=[a.read(ROOT/item['path']) for item in index]
    status_summary={arm:dict(
        single=dict(Counter(dec['status'] for r in raw if r['arm']==arm for dec in r['decisions'])),
        joint=dict(Counter(dec['status'] for r in raw if r['arm']==arm for rd in r['joint_rounds'] for dec in rd['decisions']))) for arm in LABELS}
    a.write(out/'decision_status_summary.json',status_summary)
    a.csvwrite(out/'candidate_errors.csv',[dict(case=r['case'],arm=r['arm'],repeat=r['repeat'],
        proposal=c['proposal']['proposal_id'],action=c['proposal']['action'],status=c['status'],
        sumo_evaluated=c.get('sumo_evaluated',False),error=c.get('error'))
        for r in raw for c in r['candidates'] if c['status']!='ok'])
    a.write(out/'execution_seed_audit.json',audits)
    a.write(out/'common_first_requests.json',[dict(case=k[0],parent=k[1],sha256=v) for k,v in prompts.items()])
    account=a.read(root/'execution_accounting.json');parts=[];tables=[]
    if args.cohort=='stability':
        groups=[]
        for city in sorted({r['city'] for r in rows}):
            for arm in LABELS:
                rr=[r for r in rows if r['city']==city and r['arm']==arm]
                assert len(rr)==(1 if arm=='catalog_first' else 5)
                groups.append(dict(city=city,arm=arm,**summarize(rr)))
        a.csvwrite(out/'within_case_stability.csv',groups)
        choices=[]
        for case,parent in prompts:
            for arm in LABELS:
                cc=[c for c in calls if c['case']==case and c['parent']==parent and c['arm']==arm and c['phase']=='single' and c['child']==1 and not c['repair']]
                freq=Counter(c['selected'] for c in cc);n=len(cc)
                assert len({c['request_sha256'] for c in cc})==1
                choices.append(dict(case=case,parent=parent,arm=arm,n=n,unique_choices=len(freq),frequencies=dict(freq),
                    entropy_bits=-sum(v/n*math.log2(v/n) for v in freq.values()),
                    pairwise_agreement=sum(v*(v-1) for v in freq.values())/(n*(n-1)) if n>1 else None))
        a.write(out/'fixed_catalog_choice_frequencies.json',choices)
        summary={arm:dict(cases=5,runs=len([r for r in rows if r['arm']==arm]),
            **summarize([r for r in rows if r['arm']==arm]),
            mean_within_case_sd=a.mean([g['top10_sd'] for g in groups if g['arm']==arm]),
            variable_fixed_catalog_parents=sum(c['unique_choices']>1 for c in choices if c['arm']==arm)) for arm in LABELS}
        averages={arm:{city:dict(city=city,top10=next(g['top10_mean'] for g in groups if g['city']==city and g['arm']==arm)) for city in sorted({r['city'] for r in rows})} for arm in LABELS}
        pairs={arm:a.paired(averages['llm'],averages[arm]) for arm in LABELS if arm!='llm'}
        parts += ['# Frozen-demand sampling stability','Five cities, demand/SUMO seed 42; five independent LLM and random searches per city, deterministic rule once. Existing repeat 0 is retained. All comparisons use the unchanged 5-initial, 2-children-per-parent, 15-slot protocol.',
            'Within-case SD describes search sampling. Repeats are not independent cities. Fixed-catalog choice frequencies use only each frozen parent’s first child; sibling catalogs and later trajectories depend on earlier selections. Paired inference averages repeats within city first (n=5), with limited precision. Rule SD=0 denotes deterministic sampling, not certainty across demand.',
            '| City | Method | Mean ± within-case SD (%) | Range (%) | Success |','|---|---|---:|---:|---:|']
        parts += [f"| {g['city']} | {LABELS[g['arm']]} | {g['top10_mean']:.2f} ± {g['top10_sd']:.2f} | {g['top10_min']:.2f}–{g['top10_max']:.2f} | {g['successes']}/{g['n']} |" for g in groups]
        initial_cases={r['case']:r['initial_success'] for r in rows}
        parts += [f"Shared initial candidates already contain a feasible plan in {sum(initial_cases.values())}/{len(initial_cases)} cases. The retained-best guard therefore limits the information provided by final success rates; final improvement and mutation-choice variation are more informative here."]
        fig,axes=a.plt.subplots(1,5,figsize=(14,3.7),sharey=True,layout='constrained')
        for ax,city in zip(axes,sorted({r['city'] for r in rows})):
            for i,arm in enumerate(LABELS):
                vals=[r['top10'] for r in rows if r['city']==city and r['arm']==arm]
                ax.scatter([i+(j-(len(vals)-1)/2)*.045 for j in range(len(vals))],vals,s=24,color=COLORS[arm])
                ax.plot([i-.2,i+.2],[a.mean(vals)]*2,color='black')
            ax.set(title=city.replace('_',' ').title(),xticks=range(3),xticklabels=list(LABELS.values()))
        axes[0].set_ylabel('Final Top-10 reduction (%)')
        tables=groups
    else:
        groups=[dict(trips=n,arm=arm,**summarize([r for r in rows if r['trips']==n and r['arm']==arm])) for n in [1200,2400,3600] for arm in LABELS]
        assert len(rows)==54 and all(g['n']==6 for g in groups)
        a.csvwrite(out/'demand_summary.csv',groups)
        by_city=[dict(city=city,trips=n,arm=arm,**summarize([r for r in rows if r['trips']==n and r['arm']==arm and r['city']==city])) for city in ['albany','manhattan'] for n in [1200,2400,3600] for arm in LABELS]
        a.csvwrite(out/'demand_city_summary.csv',by_city);summary=groups;pairs={}
        a.csvwrite(out/'teleport_results.csv',[{k:r[k] for k in ['city','seed','trips','arm','baseline_teleports','selected_teleports','success']} for r in rows])
        for n in [1200,2400,3600]:
            means={arm:{r['case']:r for r in rows if r['arm']==arm and r['trips']==n} for arm in LABELS}
            for arm in ['catalog_first','catalog_random']:pairs[f'{n}_{arm}']=a.paired(means['llm'],means[arm])
        parts += ['# Synthetic demand-intensity sensitivity','Albany and Manhattan; demand/SUMO seeds 42, 43, 44; 1,200 / 2,400 / 3,600 trips over the same one-hour departure window. The 2,400-trip cohort is reused. Five initial actions, parent selection, mutation rules, bundle pool and 15-slot budget are shared across methods within each scenario.',
            'From each archived 2,400-trip demand, one member of every adjacent departure pair is selected by seeded sampling. These 1,200 trips form low demand. High demand retains all original trips and adds the same selected OD pairs with unique IDs and departure shifted +0.75 s. This preserves vehicle attributes and limits OD-distribution changes, but does not exactly fix empirical OD shares at every level. It is synthetic intensity sensitivity, not observed-data calibration.',
            'Baseline, Top-10 bottlenecks, diagnosis and initial candidates are regenerated per new scenario. Thus percentages refer to each scenario’s own fixed Top-10, not an identical road set across demand levels. One search sample per method/scenario; demand and sampling effects cannot be fully separated. Only two cities are included; six city–seed pairs and city-cluster statistics are exploratory.',
            '| Trips | Method | Mean ± across-case SD (%) | Success | Global delay reduction (%) |','|---:|---|---:|---:|---:|']
        parts += [f"| {g['trips']} | {LABELS[g['arm']]} | {g['top10_mean']:.2f} ± {g['top10_sd']:.2f} | {g['successes']}/{g['n']} | {g['global_mean']:.2f} |" for g in groups]
        stressed=[b for b in baselines if b['teleports']>0]
        if stressed:
            parts += ['Baselines with teleports: '+ '; '.join(f"{b['city']} / {b['trips']} trips / seed {b['seed']}: {b['teleports']}" for b in stressed)+'. These are congested simulation stress tests with vehicle teleport recovery, not uninterrupted physical trips. The unchanged safety rule requires no increase relative to that scenario baseline, not zero teleports. See teleport_results.csv for absolute selected counts; apparent time-loss improvements need this qualification.']
        fig,axes=a.plt.subplots(1,3,figsize=(12,3.8),layout='constrained')
        for ax,city in zip(axes[:2],['albany','manhattan']):
            for arm in LABELS:
                gg=[g for g in by_city if g['city']==city and g['arm']==arm]
                ax.errorbar([g['trips'] for g in gg],[g['top10_mean'] for g in gg],yerr=[g['top10_sd'] for g in gg],marker='o',capsize=3,label=LABELS[arm],color=COLORS[arm])
            ax.set(title=city.title(),xlabel='Synthetic trips / hour',ylabel='Top-10 reduction (%)',xticks=[1200,2400,3600]);ax.legend()
        for city in ['albany','manhattan']:
            axes[2].plot([1200,2400,3600],[a.mean([b['mean_delay_s'] for b in baselines if b['city']==city and b['trips']==n]) for n in [1200,2400,3600]],marker='o',label=city.title())
        axes[2].set(xlabel='Synthetic trips / hour',ylabel='Baseline mean delay (s)',xticks=[1200,2400,3600]);axes[2].legend()
        tables=groups
    # Holm correction within each prespecified family of two LLM comparisons.
    families=[list(pairs)] if args.cohort=='stability' else [[k for k in pairs if k.startswith(str(n)+'_')] for n in [1200,2400,3600]]
    for family in families:
        previous=0
        for i,key in enumerate(sorted(family,key=lambda k:pairs[k]['wilcoxon_p'])):
            previous=max(previous,min(1.,(len(family)-i)*pairs[key]['wilcoxon_p']));pairs[key]['holm_wilcoxon_p']=previous
    a.write(out/'summary.json',summary);a.write(out/'paired_statistics.json',pairs)
    for ax in axes:ax.spines[['top','right']].set_visible(False)
    fig.savefig(out/f'{args.cohort}.pdf');fig.savefig(out/f'{args.cohort}.png',dpi=160);a.plt.close(fig)
    # Reuse every saved measured candidate for threshold and spatial safety checks.
    sweeps=[]
    for t,g,n in itertools.product([0.,.005,.01,.02,.05],[0.,.005,.01,.02,.05],[0.,.025,.05,.1]):
        for level in ([2400] if args.cohort=='stability' else [1200,2400,3600]):
            for arm in LABELS:
                selected=[a.reselect(r,t,g,n) for r in rows if r['arm']==arm and r['trips']==level]
                sweeps.append(dict(trips=level,arm=arm,top10_threshold=t,global_limit=g,neighbor_limit=n,
                    success_rate=a.mean([x is not None for x in selected]),top10_mean=a.mean([100*x['scope']['top10_relative_improvement'] if x else 0 for x in selected])))
    ranking_groups=defaultdict(list)
    for row in sweeps:ranking_groups[tuple(row[k] for k in ['trips','top10_threshold','global_limit','neighbor_limit'])].append(row)
    for group in ranking_groups.values():
        for row in group:
            row['rank_success']=1+sum(x['success_rate']>row['success_rate'] for x in group)
            row['rank_top10']=1+sum(x['top10_mean']>row['top10_mean'] for x in group)
    a.csvwrite(out/'threshold_sensitivity.csv',sweeps)
    spatial=[];worst=[]
    for r in rows:
        s,w,_=a.spatial(r);s.update(repeat=r['repeat'],trips=r['trips']);spatial.append(s)
        worst.extend(dict(**x,repeat=r['repeat'],trips=r['trips']) for x in w)
    a.csvwrite(out/'spatial_selected.csv',spatial);a.csvwrite(out/'worst_remote_edges.csv',worst)
    failures=sum(c['status']!='ok' for c in calls)
    parts += ['',f'Model/selector request failures: {failures}. Execution accounting (new work only): `{account}`.',
        'All initial actions, sibling exclusions, common first-parent requests, actual SUMO seeds/versions and joint measured feedback were audited. Cached evaluations consume logical budget. Baselines are counted separately. Unused search slots remain unused; the existing top-4 compatibility bottleneck was retained.',
        'Statistical outputs: paired Top-10 differences in percentage points; two-sided exact sign-flip tests at case and city level; Wilcoxon with Holm adjustment within each pair of comparisons; city-cluster bootstrap is descriptive with so few cities. SD is a sample SD, not a confidence interval.',
        'Threshold sensitivity reselects from already evaluated candidates (100 threshold combinations); it does not rerun a search under changed constraints. Spatial results and worst remote edges are available as CSV.',
        'Current manuscript integration is checked separately in paper/multra_revision/CONSISTENCY_ANONYMITY_AUDIT_20261004.md. These results do not establish universal LLM superiority.']
    rendered=parts[0]
    for previous,current in zip(parts,parts[1:]):rendered+=('\n' if previous.startswith('|') and current.startswith('|') else '\n\n')+current
    (out/'REPORT.md').write_text(rendered+'\n')
    tex=['\\begin{tabular}{llrr}','\\hline',r'Case/level & Method & Top-10 mean $\pm$ SD (\%) & Success \\',r'\hline']
    for g in tables:
        label=g.get('city',str(g.get('trips'))).replace('_',' ')
        tex.append(f"{label} & {LABELS[g['arm']]} & {g['top10_mean']:.2f} $\\pm$ {g['top10_sd']:.2f} & {g['successes']}/{g['n']} \\\\")
    tex += [r'\hline',r'\end{tabular}'];(out/'results_table.tex').write_text('\n'.join(tex)+'\n')
    print(out/'REPORT.md')

if __name__=='__main__':main()
