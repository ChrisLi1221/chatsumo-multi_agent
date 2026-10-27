#!/usr/bin/env python3
"""Continue a common, seed-audited measured pool with adaptive joint feedback.

The default prefix is the saved LLM4096 pool for ALL selectors. New requests use
corrected joint measurements. This is a conditional continuation experiment,
not a frozen-parent comparison after trajectories diverge.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from experiment import revision_matched as m
from agents.joint_optimizer import JointCatalogExhausted, run_joint_feedback_refinement, plan_signature
from agents.catalog_selectors import make_catalog_selector
from agents.state_refactored import AgentState

SOURCE=ROOT/'experiment/llm_output_seed_corrected'
OUT=ROOT/'experiment/joint_feedback_continuation'


def joint_loop(candidates, *, state, spec, rounds, beam_width, budget, selector_kind,
               select, evaluate, max_tokens=4096, checkpoint=lambda records: None,
               feedback_mode='aggregate'):
    """Rebuild each child from baseline, then admit measured children as parents."""
    if feedback_mode not in ('aggregate','interaction'):raise ValueError('Unknown joint feedback mode')
    records=[];turns={};branches={};exhausted=set()
    for round_index in range(1,rounds+1):
        used=sum(bool(c.get('sumo_evaluated')) for c in candidates)
        record=dict(round=round_index,logical_before=used,parents=[],decisions=[])
        records.append(record)
        if used>=budget:
            record.update(status='stopped',reason='budget_exhausted');checkpoint(records);break
        parents=m.opt._feedback_parent_candidates(
            [c for c in candidates if c['proposal']['action']=='bundle'
             and c['proposal']['proposal_id'] not in exhausted],
            profile='delay',objective_scope='top10',beam_width=min(beam_width,budget-used),include_bundles=True)
        if not parents:
            record.update(status='stopped',reason='no_unexhausted_measured_bundle');checkpoint(records);break
        record['parents']=[c['proposal']['proposal_id'] for c in parents]
        for parent in parents:
            if sum(bool(c.get('sumo_evaluated')) for c in candidates)>=budget:break
            proposal=parent['proposal'];pid=proposal['proposal_id'];root=proposal.get('joint_root_id',pid)
            branches[pid]=branches.get(pid,0)+1
            feedback=m.opt._candidate_feedback(parent,spec['baseline']['kpi'],profile='delay',objective_scope='top10')
            history=m.opt._feedback_history(candidates,profile='delay',objective_scope='top10')
            isolated={c['proposal']['proposal_id']:m.opt._candidate_feedback(c,spec['baseline']['kpi'],profile='delay',objective_scope='top10')
                      for c in candidates if c['status']=='ok' and c['proposal']['action']!='bundle'}
            decision=dict(parent_id=pid,parent_signature=plan_signature(proposal),feedback=feedback)
            record['decisions'].append(decision)
            interaction=None
            if feedback_mode=='interaction':
                from agents.interaction_feedback import build_interaction_feedback
                interaction=build_interaction_feedback(parent,baseline=spec['baseline_candidate'],evaluated_candidates=candidates)
                decision['component_interactions']=interaction
            try:
                child=run_joint_feedback_refinement(state,parent_plan=proposal,simulation_feedback=feedback,
                    diagnosis=spec['diagnosis'],evaluated_history=history,
                    excluded_plan_signatures={plan_signature(c['proposal']) for c in candidates},
                    component_feedback=isolated,interaction_feedback=interaction,user_request=state.goal,objective_profile='delay',
                    search_round=2+round_index,mutation_index=branches[pid],component_start=turns.get(root,0),
                    selector_kind=selector_kind,max_completion_tokens=max_tokens,call_llm_fn=select)
            except JointCatalogExhausted as exc:
                exhausted.add(pid);decision.update(status='catalog_exhausted',error=str(exc));checkpoint(records);continue
            except (AssertionError,FileNotFoundError):
                raise  # Protocol/provenance corruption must never become a model failure.
            except Exception as exc:
                decision.update(status='generation_error',error=str(exc));checkpoint(records);continue
            decision['proposal']=child
            turns[root]=child['refinement']['selected_component_index']+1
            if not child['validation']['ok']:
                decision['status']='validation_failed';checkpoint(records);continue
            assert plan_signature(child) not in {plan_signature(c['proposal']) for c in candidates}
            candidate=evaluate(child)
            if candidate['status']=='ok':
                candidate['conditional_change']=dict(parent_proposal_id=pid,
                    top10_improvement_delta=candidate['scope_objective']['top10_relative_improvement']-parent['scope_objective']['top10_relative_improvement'])
            candidates.append(candidate)
            decision.update(status=candidate['status'],sumo_evaluated=candidate.get('sumo_evaluated',False),
                scope_objective=candidate.get('scope_objective'),error=candidate.get('error'))
            checkpoint(records)
        record['logical_after']=sum(bool(c.get('sumo_evaluated')) for c in candidates)
        record['status']='completed';checkpoint(records)
    return records


def execute(case,arm,args):
    prefix_path=args.source/'runs'/case/'llm_rep00/result.json'
    prefix=m.read(prefix_path);spec=m.read(args.source/'frozen'/f'{case}.json')
    assert prefix['require_seed'] and prefix['frozen_sha256']==spec['frozen_sha256']
    for item in spec['input_files'].values():assert m.file_hash(item['path'])==item['sha256']
    assert all(m.candidate_seed(c)==int(spec['sumo_seed']) for c in [prefix['baseline']]+prefix['candidates'] if c['status']=='ok')
    assert spec['sumo_version'] in subprocess.check_output(['sumo','--version'],text=True).splitlines()[0]
    dest=args.output_dir/'runs'/case/f'{arm}_rep00'
    protocol=dict(version=1,source=str(prefix_path.relative_to(ROOT)),source_sha256=m.file_hash(prefix_path),
        frozen_sha256=spec['frozen_sha256'],arm=arm,model=args.model if arm=='llm' else None,
        rounds=args.joint_feedback_rounds,beam_width=args.beam_width,budget=args.sumo_budget,
        max_completion_tokens=args.max_completion_tokens,selector_seed=20260927+int(spec['sumo_seed']),
        feedback='Corrected joint measurements; archived single-stage choices retained in common prefix.')
    if (dest/'result.json').exists():
        result=m.read(dest/'result.json');assert result['protocol']==protocol,'Resume protocol changed; use a new directory'
        print('REUSED',case,arm,flush=True);return result
    if (dest/'protocol.json').exists():assert m.read(dest/'protocol.json')==protocol,'Partial-run protocol changed'
    m.write(dest/'protocol.json',protocol)
    m.write(args.output_dir/'frozen'/f'{case}.json',spec)
    state=AgentState.create(goal=spec['source_metadata']['case']['goal'],workspace=str(dest))
    state.artifacts.set(**spec['baseline']['artifacts'])
    candidates=copy.deepcopy(prefix['candidates']);prefix_count=len(candidates)
    cache=m.cache_for(spec,extra_cache_roots=[args.source],require_seed=True)
    bridge=m.opt.LocalToolBridge();calls=[];evaluation_events=[]

    def select(**kwargs):
        index=len(calls);path=dest/'calls'/f'{index:03d}.json'
        request=dict(system=kwargs['system'],payload=json.loads(kwargs['user']),scope=kwargs['usage_scope'],
                     max_tokens=kwargs['max_tokens'],model=protocol['model'])
        fingerprint=m.digest(request);selector_seed=protocol['selector_seed']+index
        if path.exists():
            call=m.read(path);assert call['request_sha256']==fingerprint,'Resumed joint feedback changed'
        else:
            call=dict(request=request,request_sha256=fingerprint,selector_seed=selector_seed,api_attempt=arm=='llm')
            start=time.monotonic()
            metadata_start=len(state.context.get('llm_response_metadata',[]))
            try:
                response=(m.call_llm(**kwargs,model=args.model,retries=0) if arm=='llm' else
                          make_catalog_selector(arm,selector_seed)(**kwargs))
                call.update(status='ok',response=response)
            except Exception as exc:
                key=os.getenv('OPENAI_API_KEY','');error=str(exc)
                call.update(status='error',error=error.replace(key,'[REDACTED]') if key else error)
            call['elapsed_s']=time.monotonic()-start
            call['response_metadata']=copy.deepcopy(state.context.get('llm_response_metadata',[])[metadata_start:])
            m.write(path,call)
        calls.append(call)
        if call['status']!='ok':raise RuntimeError(call['error'])
        return copy.deepcopy(call['response'])

    def evaluate(proposal):
        signature=plan_signature(proposal)
        folder=args.output_dir/'evaluation_cache'/case/m.digest(signature)[:24]
        event_path=dest/'evaluations'/f'{len(evaluation_events):03d}.json'
        if event_path.exists():
            event=m.read(event_path);assert event['signature']==signature
            candidate=m.read(ROOT/event['cache_source']) if event.get('result_file') else None
            if candidate is None:raise AssertionError('Incomplete evaluation checkpoint')
        elif signature in cache:
            candidate=copy.deepcopy(cache[signature])
            event=dict(signature=signature,new_sumo=0,reused=int(candidate.get('sumo_evaluated',False)))
        else:
            candidate=m.opt._evaluate_bundle(bridge,proposal,folder,spec['baseline']['artifacts'],spec['baseline']['kpi'],
                spec['source_metadata']['weights'],10,{},spec['diagnosis']['selection_objective_scope'])
            candidate['cache_source']=str(folder.relative_to(ROOT))
            if candidate['status']=='ok':
                assert m.candidate_seed(candidate)==int(spec['sumo_seed']),'Executed seed mismatch'
                cache[signature]=copy.deepcopy(candidate)
            m.write(folder/'result.json',candidate)
            event=dict(signature=signature,new_sumo=int(candidate.get('sumo_evaluated',False)),reused=0)
        candidate['proposal']=copy.deepcopy(proposal)
        if candidate['status']=='ok':assert m.candidate_seed(candidate)==int(spec['sumo_seed'])
        # Store even reused results locally, including failures, for deterministic resume.
        result_file=dest/'evaluations'/f'{len(evaluation_events):03d}_result.json'
        m.write(result_file,candidate)
        event.update(result_file=True,cache_source=str(result_file.relative_to(ROOT)))
        m.write(event_path,event);evaluation_events.append(event)
        return candidate

    records=joint_loop(candidates,state=state,spec=spec,rounds=args.joint_feedback_rounds,beam_width=args.beam_width,
        budget=args.sumo_budget,selector_kind=arm,select=select,evaluate=evaluate,max_tokens=args.max_completion_tokens,
        checkpoint=lambda records:m.write(dest/'joint_rounds.json',records))
    selected=m.opt._select_best_candidate([prefix['baseline']]+candidates,profile='delay',objective_scope='top10')
    used=sum(bool(c.get('sumo_evaluated')) for c in candidates);assert used<=args.sumo_budget
    result=dict(case=case,arm=arm,protocol=protocol,baseline=prefix['baseline'],selected=selected,
        before_selected=prefix['selected'],candidates=candidates,joint_rounds=records,
        prefix_candidate_count=prefix_count,logical_sumo_evaluations=used,budget=args.sumo_budget,
        new_sumo_evaluations=sum(e['new_sumo'] for e in evaluation_events),
        reused_joint_evaluations=sum(e['reused'] for e in evaluation_events),
        joint_evaluations=sum(bool(c.get('sumo_evaluated')) for c in candidates[prefix_count:]),
        api_requests=sum(len(c.get('response_metadata',[])) for c in calls),
        model_invocations=sum(c['api_attempt'] for c in calls),calls=len(calls),sumo_seed=spec['sumo_seed'],
        sumo_version=spec['sumo_version'],frozen_sha256=spec['frozen_sha256'])
    m.write(dest/'result.json',result)
    print('DONE',case,arm,'before',100*prefix['selected']['scope_objective']['top10_relative_improvement'],
          'after',100*selected['scope_objective']['top10_relative_improvement'],'joint',result['joint_evaluations'],flush=True)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=SOURCE)
    parser.add_argument('--output-dir',type=Path,default=OUT)
    parser.add_argument('--cases',nargs='+');parser.add_argument('--arms',nargs='+',choices=m.ARMS,default=list(m.ARMS))
    parser.add_argument('--joint-feedback-rounds',type=int,default=2)
    parser.add_argument('--beam-width',type=int,default=2)
    parser.add_argument('--sumo-budget',type=int,default=11)
    parser.add_argument('--max-completion-tokens',type=int,default=4096)
    parser.add_argument('--model',default='gpt-5.6-luna')
    args=parser.parse_args();os.chdir(ROOT);os.environ['PATH']='/usr/bin:'+os.environ['PATH']
    if min(args.joint_feedback_rounds,args.beam_width,args.max_completion_tokens)<=0:parser.error('Round, beam and token limits must be positive')
    if args.sumo_budget<7:parser.error('Budget must include the seven-slot prefix')
    args.source=args.source.resolve();args.output_dir=args.output_dir.resolve()
    assert args.output_dir!=args.source,'Do not overwrite the seed-corrected prefix'
    m.OUT=args.output_dir
    cases=args.cases or [json.loads(line)['id'] for line in (ROOT/'experiment/selector-ablation-cases.jsonl').read_text().splitlines()]
    results=[execute(case,arm,args) for case in cases for arm in args.arms]
    m.write(args.output_dir/'execution_accounting.json',dict(cases=len(cases),arms=args.arms,
        arm_case_runs=len(results),new_sumo=sum(r['new_sumo_evaluations'] for r in results),
        reused_joint=sum(r['reused_joint_evaluations'] for r in results),api_requests=sum(r['api_requests'] for r in results),
        joint_evaluations=sum(r['joint_evaluations'] for r in results)))
    m.write(args.output_dir/'source_hashes.json',[dict(path=str(p.relative_to(ROOT)),sha256=m.file_hash(p))
        for folder in ['agents','experiment','providers','providers/Modification'] for p in sorted((ROOT/folder).glob('*.py'))])


if __name__=='__main__':main()
