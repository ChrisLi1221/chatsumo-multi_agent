#!/usr/bin/env python3
"""Full corrected-feedback experiment: 3 initial + 2 mutations + 2 bundles + 4 joint.

Only seed-audited initial measurements are imported. All selection inputs and
LLM decisions are regenerated. Semantic SUMO caches never supply search history.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import copy
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import revision_matched as m
from experiment.continue_joint_feedback import joint_loop
from agents.bundle_optimizer import generate_pair_bundles
from agents.catalog_selectors import make_catalog_selector
from agents.debate_optimizer import build_finite_mutation_catalog, run_feedback_refinement
from agents.joint_optimizer import plan_signature
from agents.state_refactored import AgentState

SOURCE=ROOT/'experiment/llm_output_seed_corrected'
OUT=ROOT/'experiment/full_feedback_v1'
CACHE_ROOTS=[SOURCE,ROOT/'experiment/joint_feedback_continuation',ROOT/'experiment/joint_feedback_continuation_seed44']


class InfrastructureError(AssertionError):
    """Stop on unavailable transport/credentials, preserving a retryable journal."""


def audit_execution(candidate,seed,version):
    if not candidate.get('sumo_evaluated'):return dict(executed=False,status=candidate['status'])
    if candidate.get('optimized'):
        artifact=candidate['optimized']['artifacts']
    else:
        modification=m.read(ROOT/candidate['cache_source']/'04_modification.json')['result']
        artifact=modification['artifacts']
    probe={'optimized':{'artifacts':artifact}}
    assert m.candidate_seed(probe)==int(seed),'Actual simulation seed differs'
    assert m.version_from_tripinfo(artifact['tripinfo'])==version,'Actual SUMO version differs'
    return dict(executed=True,status=candidate['status'],seed=int(seed),version=version,tripinfo=artifact['tripinfo'])


def rebuild_decisions(initial,base,diagnosis,beam_width=2):
    """Initial-only feedback is identical across selectors and sibling decisions."""
    history=m.opt._feedback_history(initial,profile='delay',objective_scope='top10')
    parents=m.opt._feedback_parent_candidates(initial,profile='delay',objective_scope='top10',beam_width=beam_width)
    decisions=[]
    for candidate in parents:
        feedback=m.opt._candidate_feedback(candidate,base['kpi'],profile='delay',objective_scope='top10')
        audit=[]
        catalog=build_finite_mutation_catalog(candidate['proposal'],diagnosis=diagnosis,artifacts=base['artifacts'],
            simulation_feedback=feedback,evaluated_history=history,audit_log=audit)
        decisions.append(dict(parent=candidate['proposal'],feedback=feedback,catalog=catalog,filter_audit=audit,
                              catalog_sha256=m.digest(catalog)))
    return history,decisions


def prepare(case,args):
    dest=args.output_dir/'frozen'/f'{case}.json'
    prefix_path=args.source/'runs'/case/'llm_rep00/result.json'
    source_spec=m.read(args.source/'frozen'/f'{case}.json');prefix=m.read(prefix_path)
    assert prefix['require_seed'] and prefix['frozen_sha256']==source_spec['frozen_sha256']
    assert m.candidate_seed(prefix['baseline'])==int(source_spec['sumo_seed'])
    assert m.version_from_tripinfo(prefix['baseline']['optimized']['artifacts']['tripinfo'])==source_spec['sumo_version']
    assert len(source_spec['initial'])==3
    initial=copy.deepcopy(prefix['candidates'][:3])
    assert [plan_signature(c['proposal']) for c in initial]==[plan_signature(c['proposal']) for c in source_spec['initial']]
    for c in initial:assert not c['proposal'].get('refinement') and c['proposal']['action']!='bundle'
    for item in source_spec['input_files'].values():assert m.file_hash(item['path'])==item['sha256']
    base=source_spec['baseline'];audit=[audit_execution(c,source_spec['sumo_seed'],source_spec['sumo_version']) for c in [prefix['baseline']]+initial]
    verification=m.read(ROOT/'experiment/multra_revision/verification'/case/'verification.json')
    assert verification['status']=='exact_match' and m.normalize(verification['kpi'])==m.normalize(base['kpi'])
    files={k:Path(v['path']) for k,v in source_spec['input_files'].items()}
    cfg=Path(base['artifacts']['cfg']);files['cfg']=cfg
    additional=ET.parse(cfg).find('./input/additional-files')
    if additional is not None:
        for i,p in enumerate(m.opt._resolve_list(cfg.parent,additional.get('value',''))):files[f'additional_{i}']=p
    input_files={k:dict(path=str(p),sha256=m.file_hash(p)) for k,p in files.items()}
    if dest.exists():
        spec=m.read(dest);assert spec['source_sha256']==m.file_hash(prefix_path),'Initial source changed'
        assert spec['input_files']==input_files,'Inputs changed'
        assert m.digest({k:v for k,v in spec.items() if k!='frozen_sha256'})==spec['frozen_sha256'],'Frozen input corrupted'
        return spec
    history,decisions=rebuild_decisions(initial,base,source_spec['diagnosis'])
    spec=dict(version='full_corrected_v1',case=case,sumo_seed=source_spec['sumo_seed'],sumo_version=source_spec['sumo_version'],
        source=str(prefix_path.relative_to(ROOT)),source_sha256=m.file_hash(prefix_path),
        source_usage='Only first three initial candidates and baseline; no archived mutation decisions, parents or feedback.',
        input_files=input_files,baseline=base,baseline_candidate=prefix['baseline'],initial=initial,
        diagnosis=source_spec['diagnosis'],history=history,decisions=decisions,initial_execution_audit=audit,
        source_metadata={'case':source_spec['source_metadata']['case'],'weights':source_spec['source_metadata']['weights']},
        cache_identity={k:source_spec[k] for k in ['case','sumo_seed','sumo_version','frozen_sha256']})
    spec['frozen_sha256']=m.digest(spec);m.write(dest,spec)
    print('FROZEN',case,'parents',[d['parent']['proposal_id'] for d in decisions],'catalogs',[len(d['catalog']) for d in decisions],flush=True)
    return spec


class Journal:
    def __init__(self,spec,arm,repeat,args,state,dest):
        self.spec=spec;self.arm=arm;self.repeat=repeat;self.args=args;self.state=state;self.dest=dest
        self.calls=[];self.events=[];self.bridge=m.opt.LocalToolBridge()
        roots=[root for root in CACHE_ROOTS if (root/'frozen'/f"{spec['case']}.json").exists()]
        self.cache=m.cache_for(spec['cache_identity'],extra_cache_roots=roots,require_seed=True)
        for root in getattr(args,'reuse_cache_roots',[]):
            frozen=root/'frozen'/f"{spec['case']}.json"
            if not frozen.exists():continue
            cached_spec=m.read(frozen)
            assert cached_spec['input_files']==spec['input_files'],'Extra cache input files differ'
            assert int(cached_spec['sumo_seed'])==int(spec['sumo_seed']) and cached_spec['sumo_version']==spec['sumo_version']
            for path in sorted((root/'evaluation_cache'/spec['case']).glob('*/result.json')):
                candidate=m.read(path)
                if candidate.get('status')=='ok':
                    audit_execution(candidate,spec['sumo_seed'],spec['sumo_version'])
                    self.cache[plan_signature(candidate['proposal'])]=candidate

    def select(self,**kwargs):
        index=len(self.calls);path=self.dest/'calls'/f'{index:03d}.json'
        request=dict(system=kwargs['system'],payload=json.loads(kwargs['user']),scope=kwargs['usage_scope'],
            max_tokens=kwargs['max_tokens'],model=m.resolved_llm_config(model=self.args.model) if self.arm=='llm' else None)
        fingerprint=m.digest(request);seed=20260927+int(self.spec['sumo_seed'])+1000*self.repeat+index
        record=m.read(path) if path.exists() else dict(request=request,request_sha256=fingerprint,selector_seed=seed,attempts=[])
        assert record['request_sha256']==fingerprint,'Resume selection inputs changed'
        if record.get('status') not in ('ok','generation_error'):
            start=time.monotonic();offset=len(self.state.context.get('llm_response_metadata',[]))
            try:
                response=(m.call_llm(**kwargs,model=self.args.model,retries=0) if self.arm=='llm' else
                          make_catalog_selector(self.arm,seed)(**kwargs))
                record.update(status='ok',response=response);record.pop('error',None)
            except Exception as exc:
                key=os.getenv('OPENAI_API_KEY','');error=str(exc)
                record.update(status='generation_error',error=error.replace(key,'[REDACTED]') if key else error)
            metadata=copy.deepcopy(self.state.context.get('llm_response_metadata',[])[offset:])
            record['attempts'].append(dict(elapsed_s=time.monotonic()-start,response_metadata=metadata))
            if record['status']!='ok' and self.arm=='llm':
                last=metadata[-1] if metadata else {}
                if last.get('http_status') is None or last.get('http_status') in (401,403,429) or last.get('http_status',0)>=500:
                    record['status']='infrastructure_error'
            m.write(path,record)
        self.calls.append(record)
        if record['status']=='infrastructure_error':raise InfrastructureError(record['error'])
        if record['status']!='ok':raise RuntimeError(record['error'])
        return copy.deepcopy(record['response'])

    def evaluate(self,proposal):
        signature=plan_signature(proposal);index=len(self.events)
        checkpoint=self.dest/'evaluations'/f'{index:03d}.json'
        result_file=self.dest/'evaluations'/f'{index:03d}_result.json'
        if checkpoint.exists():
            event=m.read(checkpoint);assert event['signature']==signature,'Resumed evaluation changed'
            candidate=m.read(result_file)
        elif signature in self.cache:
            candidate=copy.deepcopy(self.cache[signature]);event=dict(signature=signature,new_sumo=0,reused=int(candidate.get('sumo_evaluated',False)))
        else:
            folder=self.args.output_dir/'evaluation_cache'/self.spec['case']/m.digest(signature)[:24]
            base=self.spec['baseline'];weights=self.spec['source_metadata']['weights'];scope=self.spec['diagnosis']['selection_objective_scope']
            if proposal['action']=='bundle':candidate=m.opt._evaluate_bundle(self.bridge,proposal,folder,base['artifacts'],base['kpi'],weights,10,{},scope)
            else:candidate=m.opt._evaluate_proposal(self.bridge,proposal,folder,base['artifacts'],base['kpi'],weights,10,scope)
            candidate['cache_source']=str(folder.relative_to(ROOT))
            audit_execution(candidate,self.spec['sumo_seed'],self.spec['sumo_version'])
            m.write(folder/'result.json',candidate)
            if candidate['status']=='ok':self.cache[signature]=copy.deepcopy(candidate)
            event=dict(signature=signature,new_sumo=int(candidate.get('sumo_evaluated',False)),reused=0)
        candidate['proposal']=copy.deepcopy(proposal)
        audit_execution(candidate,self.spec['sumo_seed'],self.spec['sumo_version'])
        m.write(result_file,candidate);m.write(checkpoint,event);self.events.append(event)
        return candidate


def sibling_catalog(catalog,selected_signatures):
    """Keep original IDs/order; remove complete semantic actions of prior siblings."""
    return [row for row in catalog if plan_signature(dict(action=row['action'],args=row.get('full_args',row['args']))) not in selected_signatures]


def execute(spec,arm,repeat,args):
    dest=args.output_dir/'runs'/spec['case']/f'{arm}_rep{repeat:02d}'
    child_count=getattr(args,'children_per_parent',1);initial_count=len(spec['initial'])
    wide=initial_count!=3 or child_count!=1
    protocol=dict(version='full_wide_v1' if wide else 'full_corrected_v1',frozen_sha256=spec['frozen_sha256'],arm=arm,repeat=repeat,
        model=m.resolved_llm_config(model=args.model) if arm=='llm' else None,max_completion_tokens=args.max_completion_tokens,
        initial_count=initial_count,single_refinements=2*child_count,bundle_top_k=4,bundle_limit=2,joint_rounds=args.joint_feedback_rounds,
        beam_width=2,budget=args.sumo_budget,selector_seed=20260927+int(spec['sumo_seed'])+1000*repeat)
    if wide:protocol['children_per_parent']=child_count
    feedback_mode=getattr(args,'joint_feedback_mode','aggregate')
    if feedback_mode!='aggregate':protocol['joint_feedback_mode']=feedback_mode
    if (dest/'result.json').exists():
        result=m.read(dest/'result.json');assert result['protocol']==protocol,'Resume protocol changed; use a new output directory'
        print('REUSED',spec['case'],arm,repeat,flush=True);return result
    if (dest/'protocol.json').exists():assert m.read(dest/'protocol.json')==protocol,'Partial-run protocol changed'
    m.write(dest/'protocol.json',protocol)
    # Common prompt workspace prevents selector-specific path strings changing stage-1 inputs.
    state=AgentState.create(goal=spec['source_metadata']['case']['goal'],workspace=str(args.output_dir/'prompt_context'/spec['case']))
    state.artifacts.set(**spec['baseline']['artifacts'])
    journal=Journal(spec,arm,repeat,args,state,dest);candidates=copy.deepcopy(spec['initial']);decisions=[]
    for dec in spec['decisions']:
        selected_siblings=set()
        for child_index in range(1,child_count+1):
            catalog=sibling_catalog(dec['catalog'],selected_siblings)
            entry=dict(parent=dec['parent']['proposal_id'],catalog_sha256=m.digest(catalog),feedback=dec['feedback'])
            if wide:entry.update(child_index=child_index,base_catalog_sha256=dec['catalog_sha256'],excluded_sibling_signatures=sorted(selected_siblings))
            try:
                if not catalog and wide:entry['status']='catalog_exhausted'
                else:
                    proposal=run_feedback_refinement(state,parent_proposal=dec['parent'],simulation_feedback=dec['feedback'],
                        diagnosis=spec['diagnosis'],evaluated_history=spec['history'],user_request=state.goal,objective_profile='delay',
                        search_round=2,mutation_index=child_index,catalog_override=catalog,max_completion_tokens=args.max_completion_tokens,
                        call_llm_fn=journal.select)
                    if arm!='llm':
                        proposal['proposal_source']=f'feedback_{arm}';proposal['refinement']['selector_kind']=arm
                    entry['proposal']=proposal
                    if proposal['validation']['ok']:
                        signature=plan_signature(proposal)
                        assert signature not in selected_siblings,'Duplicate sibling'
                        selected_siblings.add(signature)
                        if wide and signature in {plan_signature(c['proposal']) for c in candidates}:
                            entry['status']='duplicate_across_parents'
                        else:
                            candidate=journal.evaluate(proposal);candidates.append(candidate);entry['status']=candidate['status']
                    else:entry['status']='validation_failed'
            except (AssertionError,FileNotFoundError):raise
            except Exception as exc:entry.update(status='generation_error',error=str(exc))
            decisions.append(entry);m.write(dest/'decisions.json',decisions)
    quality={c['proposal']['proposal_id']:-c['scope_objective']['score'] for c in candidates if c['status']=='ok'}
    bundles=generate_pair_bundles([c['proposal'] for c in candidates if c['status']=='ok'],top_k=4,max_bundles=2,quality_by_id=quality)
    for proposal in bundles:candidates.append(journal.evaluate(proposal))
    before=m.opt._select_best_candidate([spec['baseline_candidate']]+candidates,profile='delay',objective_scope='top10')
    prefix_count=len(candidates)
    rounds=joint_loop(candidates,state=state,spec=spec,rounds=args.joint_feedback_rounds,beam_width=2,budget=args.sumo_budget,
        selector_kind=arm,select=journal.select,evaluate=journal.evaluate,max_tokens=args.max_completion_tokens,
        checkpoint=lambda records:m.write(dest/'joint_rounds.json',records),feedback_mode=feedback_mode)
    selected=m.opt._select_best_candidate([spec['baseline_candidate']]+candidates,profile='delay',objective_scope='top10')
    metadata=[r for call in journal.calls for attempt in call['attempts'] for r in attempt['response_metadata']]
    used=sum(bool(c.get('sumo_evaluated')) for c in candidates);assert used<=args.sumo_budget
    result=dict(case=spec['case'],arm=arm,repeat=repeat,protocol=protocol,baseline=spec['baseline_candidate'],
        candidates=candidates,selected=selected,before_selected=before,decisions=decisions,joint_rounds=rounds,
        prefix_candidate_count=prefix_count,budget=args.sumo_budget,logical_sumo_evaluations=used,
        new_sumo_evaluations=sum(e['new_sumo'] for e in journal.events),
        reused_evaluations=sum(bool(c.get('sumo_evaluated')) for c in spec['initial'])+sum(e['reused'] for e in journal.events),
        api_requests=sum(r.get('http_status') is not None for r in metadata),transport_failures=sum(r.get('http_status') is None for r in metadata),
        calls=len(journal.calls),joint_evaluations=sum(bool(c.get('sumo_evaluated')) for c in candidates[prefix_count:]),
        sumo_seed=spec['sumo_seed'],sumo_version=spec['sumo_version'],frozen_sha256=spec['frozen_sha256'])
    m.write(dest/'result.json',result)
    print('DONE',spec['case'],arm,repeat,'prefix',100*before['scope_objective']['top10_relative_improvement'],
          'final',100*selected['scope_objective']['top10_relative_improvement'],'new',result['new_sumo_evaluations'],flush=True)
    return result


def case_job(job):
    case,args=job;m.OUT=args.output_dir
    lock=args.output_dir/'locks'/f'{case}.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        spec=prepare(case,args)
        if args.prepare_only:return []
        assert spec['sumo_version'] in subprocess.check_output(['sumo','--version'],text=True).splitlines()[0]
        results=[]
        for arm in args.arms:
            for repeat in range(args.repeats if arm!='catalog_first' else 1):results.append(execute(spec,arm,repeat,args))
        return results


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=SOURCE);p.add_argument('--output-dir',type=Path,default=OUT)
    p.add_argument('--cases',nargs='+');p.add_argument('--arms',nargs='+',choices=m.ARMS,default=list(m.ARMS))
    p.add_argument('--repeats',type=int,default=1);p.add_argument('--workers',type=int,default=3)
    p.add_argument('--joint-feedback-rounds',type=int,default=2);p.add_argument('--sumo-budget',type=int,default=11)
    p.add_argument('--joint-feedback-mode',choices=['aggregate','interaction'],default='aggregate')
    p.add_argument('--max-completion-tokens',type=int,default=4096);p.add_argument('--model',default='gpt-5.6-luna')
    p.add_argument('--prepare-only',action='store_true')
    args=p.parse_args();os.chdir(ROOT);os.environ['PATH']='/usr/bin:'+os.environ['PATH']
    if min(args.repeats,args.workers,args.max_completion_tokens)<=0 or args.joint_feedback_rounds<0:p.error('Invalid limits')
    if args.sumo_budget<7:p.error('Budget must include the seven-slot initial/mutation/bundle phase')
    args.source=args.source.resolve();args.output_dir=args.output_dir.resolve()
    if args.joint_feedback_mode!='aggregate' and args.output_dir==OUT:
        p.error('Interaction feedback requires a new --output-dir; preserve full_feedback_v1')
    assert args.output_dir not in CACHE_ROOTS+[args.source,ROOT/'experiment/multra_revision'],'Use an independent output directory'
    cases=args.cases or [json.loads(line)['id'] for line in (ROOT/'experiment/selector-ablation-cases.jsonl').read_text().splitlines()]
    m.OUT=args.output_dir
    if args.workers==1:groups=[case_job((case,args)) for case in cases]
    else:
        with ProcessPoolExecutor(max_workers=args.workers) as pool:groups=list(pool.map(case_job,[(case,args) for case in cases]))
    results=[r for group in groups for r in group]
    if not args.prepare_only:
        m.write(args.output_dir/'execution_accounting.json',dict(cases=cases,arms=args.arms,repeats=args.repeats,arm_case_runs=len(results),
            new_sumo=sum(r['new_sumo_evaluations'] for r in results),reused=sum(r['reused_evaluations'] for r in results),
            logical=sum(r['logical_sumo_evaluations'] for r in results),api_requests=sum(r['api_requests'] for r in results),
            transport_failures=sum(r['transport_failures'] for r in results)))
    m.write(args.output_dir/'source_hashes.json',[dict(path=str(path.relative_to(ROOT)),sha256=m.file_hash(path))
        for folder in ['agents','experiment','providers','providers/Modification'] for path in sorted((ROOT/folder).glob('*.py'))])


if __name__=='__main__':main()
