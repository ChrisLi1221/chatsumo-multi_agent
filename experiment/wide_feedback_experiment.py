#!/usr/bin/env python3
"""Five shared initial candidates; two siblings per parent; 15-slot search."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from agents.grounded_baselines import generate_grounded_random
from agents.joint_optimizer import plan_signature
from agents.state_refactored import AgentState
from experiment import full_feedback_experiment as full
m=full.m
SOURCE=full.OUT
OUT=ROOT/'experiment/wide_feedback_v1'


def prepare(case,args):
    source=args.source/'frozen'/f'{case}.json';old=m.read(source)
    assert m.digest({k:v for k,v in old.items() if k!='frozen_sha256'})==old['frozen_sha256']
    assert len(old['initial'])==3 and old['version']=='full_corrected_v1'
    for item in old['input_files'].values():assert m.file_hash(item['path'])==item['sha256']
    assert spec_version(old) in subprocess.check_output(['sumo','--version'],text=True).splitlines()[0]
    for c in [old['baseline_candidate']]+old['initial']:full.audit_execution(c,old['sumo_seed'],old['sumo_version'])
    assert m.candidate_seed(old['baseline_candidate'])==int(old['sumo_seed'])
    destination=args.output_dir/'frozen'/f'{case}.json'
    if destination.exists():
        spec=m.read(destination)
        assert spec['initial_source_sha256']==m.file_hash(source),'Initial source changed'
        assert spec['input_files']==old['input_files'] and len(spec['initial'])==5
        assert m.digest({k:v for k,v in spec.items() if k!='frozen_sha256'})==spec['frozen_sha256']
        for c in spec['initial']:full.audit_execution(c,spec['sumo_seed'],spec['sumo_version'])
        return spec
    init_dir=args.output_dir/'initialization'/case
    plan_path=init_dir/'plan.json'
    protocol=dict(version='wide_initial_v1',source_sha256=m.file_hash(source),seed=int(old['sumo_seed']),
        generation='Replay the same grounded random draw stream for five proposals; assert first three semantic actions match before using draws four and five.')
    if plan_path.exists():
        plan=m.read(plan_path);assert plan['protocol']==protocol
    else:
        proposals=generate_grounded_random(old['diagnosis'],old['baseline']['artifacts'],attempt_budget=5,seed=int(old['sumo_seed']))
        assert len(proposals)==5 and len({plan_signature(p) for p in proposals})==5
        assert [plan_signature(p) for p in proposals[:3]]==[plan_signature(c['proposal']) for c in old['initial']], 'Initialization generator does not replay the archived first three actions'
        for index,proposal in enumerate(proposals[3:],4):
            assert proposal['validation']['ok']
            proposal.update(proposal_id=f"wide_initial_{index:03d}_{proposal['action']}",proposal_source='shared_grounded_random',search_round=1)
        plan=dict(protocol=protocol,generated_proposals=proposals)
        m.write(plan_path,plan) # Freeze all actions BEFORE measuring either new one.
    state=AgentState.create(goal=old['source_metadata']['case']['goal'],workspace=str(init_dir))
    state.artifacts.set(**old['baseline']['artifacts'])
    journal=full.Journal(old,'catalog_first',0,args,state,init_dir)
    initial=copy.deepcopy(old['initial'])
    for proposal in plan['generated_proposals'][3:]:initial.append(journal.evaluate(proposal))
    history,decisions=full.rebuild_decisions(initial,old['baseline'],old['diagnosis'])
    spec=copy.deepcopy(old);spec.pop('frozen_sha256')
    spec.update(version='full_wide_v1',initial_source=str(source.relative_to(ROOT)),initial_source_sha256=m.file_hash(source),
        initial=initial,history=history,decisions=decisions,initialization_protocol=protocol,
        source_usage='First three measurements retained; draws four and five generated solely from baseline diagnosis and shared by every arm.',
        initialization_new_sumo=sum(e['new_sumo'] for e in journal.events),initialization_reused=sum(e['reused'] for e in journal.events),
        initial_execution_audit=[full.audit_execution(c,old['sumo_seed'],old['sumo_version']) for c in initial])
    spec['frozen_sha256']=m.digest(spec);m.write(destination,spec)
    print('FROZEN WIDE',case,'initial',len(initial),'parents',[d['parent']['proposal_id'] for d in decisions],
        'catalogs',[len(d['catalog']) for d in decisions],flush=True)
    return spec


def spec_version(spec):
    return spec['sumo_version']


def case_job(job):
    case,args=job;m.OUT=args.output_dir
    path=args.output_dir/'locks'/f'{case}.lock';path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        spec=prepare(case,args)
        if args.prepare_only:return []
        return [full.execute(spec,arm,repeat,args) for arm in args.arms
                for repeat in range(args.repeats if arm!='catalog_first' else 1)]


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=SOURCE);p.add_argument('--output-dir',type=Path,default=OUT)
    p.add_argument('--cases',nargs='+');p.add_argument('--arms',nargs='+',choices=m.ARMS,default=list(m.ARMS))
    p.add_argument('--workers',type=int,default=3);p.add_argument('--repeats',type=int,default=1)
    p.add_argument('--model',default='gpt-5.6-luna');p.add_argument('--max-completion-tokens',type=int,default=4096)
    p.add_argument('--prepare-only',action='store_true',help='Freeze five shared actions and measure the two new initial candidates; no API calls')
    args=p.parse_args();os.chdir(ROOT);os.environ['PATH']='/usr/bin:'+os.environ['PATH']
    args.source=args.source.resolve();args.output_dir=args.output_dir.resolve()
    if min(args.workers,args.repeats,args.max_completion_tokens)<1:p.error('Limits must be positive')
    if args.output_dir in [args.source,SOURCE,*full.CACHE_ROOTS,ROOT/'experiment/multra_revision',ROOT/'experiment/interaction_feedback_v1']:
        p.error('Use an independent output directory')
    args.children_per_parent=2;args.joint_feedback_rounds=2;args.sumo_budget=15;args.joint_feedback_mode='aggregate'
    args.reuse_cache_roots=[args.source,ROOT/'experiment/interaction_feedback_v1']
    cases=args.cases or [json.loads(line)['id'] for line in (ROOT/'experiment/selector-ablation-cases.jsonl').read_text().splitlines()]
    if args.workers==1:groups=[case_job((case,args)) for case in cases]
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:groups=list(pool.map(case_job,[(case,args) for case in cases]))
    results=[r for group in groups for r in group]
    shared_new=sum(m.read(args.output_dir/'frozen'/f'{case}.json')['initialization_new_sumo'] for case in cases)
    if not args.prepare_only:
        # Initial new simulations are physically executed once, then reused by arms.
        # Every run charges all five measured initials against its logical budget.
        reused=sum(r['reused_evaluations'] for r in results)
        m.write(args.output_dir/'execution_accounting.json',dict(cases=cases,arms=args.arms,repeats=args.repeats,arm_case_runs=len(results),
            shared_initialization_new_sumo=shared_new,new_sumo=shared_new+sum(r['new_sumo_evaluations'] for r in results),
            reused=reused-shared_new,logical=sum(r['logical_sumo_evaluations'] for r in results),
            api_requests=sum(r['api_requests'] for r in results),transport_failures=sum(r['transport_failures'] for r in results)))
    m.write(args.output_dir/'source_hashes.json',[dict(path=str(path.relative_to(ROOT)),sha256=m.file_hash(path))
        for folder in ['agents','experiment','providers','providers/Modification'] for path in sorted((ROOT/folder).glob('*.py'))])


if __name__=='__main__':main()
