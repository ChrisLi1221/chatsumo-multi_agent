#!/usr/bin/env python3
"""Audit measured component contrasts or run paired common-prefix LLM pilots."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import copy
import csv
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from agents.interaction_feedback import build_interaction_feedback
from agents.joint_optimizer import plan_signature
from agents.state_refactored import AgentState
from experiment import full_feedback_experiment as full
from experiment.continue_joint_feedback import joint_loop
m=full.m
SOURCE=full.OUT
OUT=ROOT/'experiment/interaction_feedback_v1'


def audit(source,output):
    rows=[]
    for path in sorted((source/'runs').glob('*/llm_rep00/result.json')):
        run=m.read(path);pool=copy.deepcopy(run['candidates'][:run['prefix_candidate_count']])
        full.audit_execution(run['baseline'],run['sumo_seed'],run['sumo_version'])
        for c in run['candidates']:full.audit_execution(c,run['sumo_seed'],run['sumo_version'])
        for record in run['joint_rounds']:
            for index,decision in enumerate(record['decisions']):
                parent=next(c for c in pool if c['proposal']['proposal_id']==decision['parent_id'])
                evidence=build_interaction_feedback(parent,baseline=run['baseline'],evaluated_candidates=pool)
                destination=output/'audit'/run['case']/f"round_{record['round']}_decision_{index}.json"
                m.write(destination,dict(source=str(path),source_sha256=m.file_hash(path),visible_candidate_ids=[c['proposal']['proposal_id'] for c in pool],evidence=evidence))
                rows.append(dict(case=run['case'],round=record['round'],parent=decision['parent_id'],
                    measured_components=sum(c['isolated_available'] for c in evidence['components']),
                    has_previous_joint_contrast=evidence['previous_conditional_change']['available'],
                    newly_worsened_edges=evidence['spatial']['newly_worsened_edge_count'],
                    top10_interaction_residual=evidence['metrics']['top10_total_time_loss_s']['interaction_residual']))
                child=(decision.get('proposal') or {}).get('proposal_id')
                found=next((c for c in run['candidates'] if c['proposal']['proposal_id']==child),None)
                if found:pool.append(found)
    m.write(output/'audit/summary.json',dict(parents=len(rows),fully_measured=sum(r['measured_components']==2 for r in rows),
        partially_measured=sum(r['measured_components']==1 for r in rows),unmeasured=sum(r['measured_components']==0 for r in rows),records=rows))
    print('AUDITED',len(rows),'joint parent contexts',flush=True)


def execute(case,mode,args,repeat=0):
    source=args.source/'runs'/case/'llm_rep00/result.json';run=m.read(source)
    spec=m.read(args.source/'frozen'/f'{case}.json')
    assert spec['frozen_sha256']==run['frozen_sha256']
    assert m.digest({k:v for k,v in spec.items() if k!='frozen_sha256'})==spec['frozen_sha256']
    for item in spec['input_files'].values():assert m.file_hash(item['path'])==item['sha256']
    assert spec['sumo_version'] in subprocess.check_output(['sumo','--version'],text=True).splitlines()[0]
    for candidate in [run['baseline']]+run['candidates']:
        full.audit_execution(candidate,spec['sumo_seed'],spec['sumo_version'])
    prefix=run['candidates'][:run['prefix_candidate_count']]
    dest=args.output_dir/'runs'/case/f'{mode}_rep{repeat:02d}'
    protocol=dict(version='interaction_feedback_paired_v1',mode=mode,source=str(source),source_sha256=m.file_hash(source),
        prefix_sha256=m.digest(prefix),model=m.resolved_llm_config(model=args.model),max_completion_tokens=args.max_completion_tokens,
        rounds=2,beam_width=2,budget=11,repeat=repeat,component_schedule='round_robin_skip_exhausted',
        evidence='Own measured history only; no added isolated simulations',randomized_sampling_seed=None)
    if (dest/'result.json').exists():
        saved=m.read(dest/'result.json');assert saved['protocol']==protocol,'Resume protocol changed'
        print('REUSED',case,mode,repeat,flush=True);return saved
    if (dest/'protocol.json').exists():assert m.read(dest/'protocol.json')==protocol,'Partial protocol changed'
    m.write(dest/'protocol.json',protocol);m.write(args.output_dir/'frozen'/f'{case}.json',spec)
    state=AgentState.create(goal=spec['source_metadata']['case']['goal'],workspace=str(args.output_dir/'prompt_context'/case))
    state.artifacts.set(**spec['baseline']['artifacts'])
    journal=full.Journal(spec,'llm',repeat,args,state,dest)
    # Hidden deterministic cache can answer a selected action; never adds history.
    for candidate in run['candidates']:
        if candidate['status']=='ok':journal.cache[plan_signature(candidate['proposal'])]=copy.deepcopy(candidate)
    candidates=copy.deepcopy(prefix)
    records=joint_loop(candidates,state=state,spec=spec,rounds=2,beam_width=2,budget=11,
        selector_kind='llm',select=journal.select,evaluate=journal.evaluate,max_tokens=args.max_completion_tokens,
        checkpoint=lambda records:m.write(dest/'joint_rounds.json',records),feedback_mode=mode)
    selected=m.opt._select_best_candidate([run['baseline']]+candidates,profile='delay',objective_scope='top10')
    metadata=[row for call in journal.calls for attempt in call['attempts'] for row in attempt['response_metadata']]
    result=dict(case=case,mode=mode,repeat=repeat,protocol=protocol,baseline=run['baseline'],before_selected=run['before_selected'],
        selected=selected,candidates=candidates,joint_rounds=records,prefix_candidate_count=len(prefix),
        sumo_seed=spec['sumo_seed'],sumo_version=spec['sumo_version'],budget=11,
        logical_sumo_evaluations=sum(bool(c.get('sumo_evaluated')) for c in candidates),
        new_sumo_evaluations=sum(e['new_sumo'] for e in journal.events),
        reused_evaluations=sum(bool(c.get('sumo_evaluated')) for c in prefix)+sum(e['reused'] for e in journal.events),
        api_requests=sum(row.get('http_status') is not None for row in metadata),
        transport_failures=sum(row.get('http_status') is None for row in metadata))
    assert result['logical_sumo_evaluations']<=11
    m.write(dest/'result.json',result)
    print('DONE',case,mode,repeat,'top10',100*selected['scope_objective']['top10_relative_improvement'],flush=True)
    return result


def case_job(job):
    case,args=job;m.OUT=args.output_dir
    lock=args.output_dir/'locks'/f'{case}.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        # Alternate order across cases/repeats to reduce a fixed temporal ordering.
        return [execute(case,mode,args,repeat) for repeat in range(args.repeats)
            for mode in (['aggregate','interaction'] if (args.cases.index(case)+repeat)%2==0 else ['interaction','aggregate'])]


def analyze(args,results):
    out=args.output_dir/'analysis';out.mkdir(parents=True,exist_ok=True);rows=[];proof=[]
    for r in results:
        scope=r['selected']['scope_objective']
        for c in [r['baseline']]+r['candidates']:full.audit_execution(c,r['sumo_seed'],r['sumo_version'])
        visible=list(r['candidates'][:r['prefix_candidate_count']])
        for record in r['joint_rounds']:
            for decision in record['decisions']:
                parent=next(c for c in visible if c['proposal']['proposal_id']==decision['parent_id'])
                if r['mode']=='interaction':
                    expected=build_interaction_feedback(parent,baseline=r['baseline'],evaluated_candidates=visible)
                    assert decision['component_interactions']==expected,'Recorded interaction differs from visible measurements'
                proposal=decision.get('proposal') or {}
                child=next((c for c in r['candidates'] if c['proposal']['proposal_id']==proposal.get('proposal_id')),None)
                if child:
                    changed=proposal['refinement']['selected_component_index']
                    assert all(action==parent['proposal']['actions'][i] for i,action in enumerate(proposal['actions']) if i!=changed)
                    visible.append(child)
        rows.append(dict(case=r['case'],mode=r['mode'],repeat=r['repeat'],
            before_top10=100*r['before_selected']['scope_objective']['top10_relative_improvement'],
            top10=100*scope['top10_relative_improvement'],global_reduction=-100*scope['global_delay_relative_change'],
            success=not r['selected'].get('is_baseline') and bool(scope['constraints_passed']),
            logical=r['logical_sumo_evaluations'],new_sumo=r['new_sumo_evaluations'],reused=r['reused_evaluations'],api_requests=r['api_requests']))
    for case in args.cases:
        for repeat in range(args.repeats):
            runs=[next(r for r in results if r['case']==case and r['mode']==mode and r['repeat']==repeat) for mode in ['aggregate','interaction']]
            assert runs[0]['protocol']['prefix_sha256']==runs[1]['protocol']['prefix_sha256']
            # First joint selection must have identical parent, feedback and catalog.
            calls=[m.read(args.output_dir/'runs'/case/f'{mode}_rep{repeat:02d}/calls/000.json')['request'] for mode in ['aggregate','interaction']]
            payloads=[copy.deepcopy(c['payload']) for c in calls]
            payloads[1]['joint_context'].pop('component_interactions')
            assert payloads[0]==payloads[1], 'Paired initial joint selection differs beyond interaction evidence'
            assert calls[1]['system'].startswith(calls[0]['system'])
            assert all(calls[0][key]==calls[1][key] for key in ['scope','max_tokens','model'])
            proof.append(dict(case=case,repeat=repeat,prefix_sha256=runs[0]['protocol']['prefix_sha256'],shared_payload_sha256=m.digest(payloads[0])))
    m.write(out/'paired_input_audit.json',proof)
    with (out/'case_results.csv').open('w') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
    summary={}
    for mode in ['aggregate','interaction']:
        group=[r for r in rows if r['mode']==mode]
        summary[mode]=dict(runs=len(group),mean_top10=sum(r['top10'] for r in group)/len(group),
            successes=sum(r['success'] for r in group),**{key:sum(r[key] for r in group) for key in ['logical','new_sumo','reused','api_requests']})
    m.write(out/'summary.json',summary)
    diffs=[]
    for case in args.cases:
        values={mode:sum(r['top10'] for r in rows if r['case']==case and r['mode']==mode)/args.repeats for mode in summary}
        diffs.append(dict(case=case,**values,difference_pp=values['interaction']-values['aggregate']))
    m.write(out/'paired_differences.json',diffs)
    if args.plots:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig,ax=plt.subplots(figsize=(8,4),layout='constrained')
        for offset,mode,label in [(-.24,'prefix','Shared prefix'),(0.,'aggregate','Aggregate feedback'),(.24,'interaction','Component interactions')]:
            values=[next(r['before_top10'] for r in rows if r['case']==case) if mode=='prefix'
                else sum(r['top10'] for r in rows if r['case']==case and r['mode']==mode)/args.repeats for case in args.cases]
            ax.bar([i+offset for i in range(len(args.cases))],values,width=.24,label=label)
        ax.set(xticks=range(len(args.cases)),xticklabels=[c.split('_equal_scale')[0].replace('_',' ').title() for c in args.cases],
            ylabel='Final Top-10 reduction (%)',title=f'Common-prefix pilot; {args.repeats} sample(s) per condition')
        ax.legend();ax.spines[['top','right']].set_visible(False)
        fig.savefig(out/'paired_pilot.png',dpi=160);fig.savefig(out/'paired_pilot.pdf');plt.close(fig)
    lines=['# 组件相互影响反馈：共同起点试运行','',
        '两组均为新 LLM 调用，共享 full_feedback_v1 的同一七候选前缀，首个联合请求除新增反馈和对应说明外完全一致。每组两轮、beam=2、总预算上限 11，未增加单组件仿真。后续路径允许分叉；这是条件续跑试验，不是重新运行全流程或已证明提示有效。','',
        '| 反馈 | 平均 Top-10 改善 (%) | 成功数 | 逻辑仿真 | 新增 SUMO | API 请求 |','|---|---:|---:|---:|---:|---:|']
    for mode,s in summary.items():lines.append(f"| {mode} | {s['mean_top10']:.3f} | {s['successes']}/{s['runs']} | {s['logical']} | {s['new_sumo']} | {s['api_requests']} |")
    lines+=['','| 案例 | aggregate (%) | interaction (%) | 差值 (百分点) |','|---|---:|---:|---:|']
    for d in diffs:lines.append(f"| {d['case']} | {d['aggregate']:.3f} | {d['interaction']:.3f} | {d['difference_pp']:+.3f} |")
    lines+=['',f'每案例每组重复数为 {args.repeats}；当前试运行规模不足以支持优势或稳定性结论。控制组也重新采样，不能直接与原版已保存的单次结果归因比较。',
        '所有结果核验实际 SUMO seed/version。第一次选择核验共同前缀、父方案、catalog、历史、联合 KPI 和请求参数；后续证据只来自各自当时可见的已测候选。',
        '相互影响残差为 Y(A+B)−Y(A)−Y(B)+Y(基线)，是固定需求、种子及动作顺序下的描述性对照；不等于因果归因。缺少精确参数的单独测量时残差为空，仍提供可获得的条件差值及路段变化。']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    m.write(args.output_dir/'execution_accounting.json',dict(cases=args.cases,repeats=args.repeats,
        runs=len(results),new_sumo=sum(r['new_sumo_evaluations'] for r in results),
        reused=sum(r['reused_evaluations'] for r in results),api_requests=sum(r['api_requests'] for r in results),
        transport_failures=sum(r['transport_failures'] for r in results)))
    print(json.dumps(summary),flush=True)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=SOURCE);p.add_argument('--output-dir',type=Path,default=OUT)
    p.add_argument('--run',action='store_true');p.add_argument('--analyze-only',action='store_true')
    p.add_argument('--plots',action='store_true',help='Generate figures; requires matplotlib (available in system python3)')
    p.add_argument('--cases',nargs='+');p.add_argument('--workers',type=int,default=3);p.add_argument('--repeats',type=int,default=1)
    p.add_argument('--model',default='gpt-5.6-luna');p.add_argument('--max-completion-tokens',type=int,default=4096)
    args=p.parse_args();os.chdir(ROOT);os.environ['PATH']='/usr/bin:'+os.environ['PATH']
    args.source=args.source.resolve();args.output_dir=args.output_dir.resolve()
    if args.output_dir in [args.source,full.OUT,*full.CACHE_ROOTS,ROOT/'experiment/multra_revision']:p.error('Use a new output directory')
    if min(args.workers,args.repeats,args.max_completion_tokens)<1:p.error('Limits must be positive')
    args.cases=args.cases or [x.name for x in sorted((args.source/'runs').iterdir()) if x.name.endswith('_seed_42')]
    if args.analyze_only:
        results=[m.read(args.output_dir/'runs'/case/f'{mode}_rep{repeat:02d}/result.json')
            for case in args.cases for repeat in range(args.repeats) for mode in ['aggregate','interaction']]
        analyze(args,results);return
    audit(args.source,args.output_dir)
    if args.run:
        if args.workers==1:groups=[case_job((case,args)) for case in args.cases]
        else:
            with ProcessPoolExecutor(max_workers=args.workers) as pool:groups=list(pool.map(case_job,[(case,args) for case in args.cases]))
        analyze(args,[r for group in groups for r in group])
    m.write(args.output_dir/'source_hashes.json',[dict(path=str(path.relative_to(ROOT)),sha256=m.file_hash(path))
        for folder in ['agents','experiment','providers','providers/Modification'] for path in sorted((ROOT/folder).glob('*.py'))])


if __name__=='__main__':main()
