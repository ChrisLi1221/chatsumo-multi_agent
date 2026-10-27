#!/usr/bin/env python3
"""Freeze, audit, and replay selector experiments without changing search structure.

Run with the chatsumo Python environment. Historical data are read-only. Each
frozen parent receives the SAME initial-only history, regardless of other picks.
Cache reuse is by complete semantic action, case, input hashes, and SUMO version;
cached evaluations still consume the logical seven-evaluation research budget.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from experiment import run_optimization_only as opt
from agents.debate_optimizer import build_finite_mutation_catalog, run_feedback_refinement
from agents.catalog_selectors import make_catalog_selector
from agents.joint_optimizer import plan_signature
from agents.state_refactored import AgentState
from agents.bundle_optimizer import generate_pair_bundles
from agents.llm_agents import call_llm, resolved_llm_config

OUT = ROOT / 'experiment/multra_revision'
ARMS = ('llm', 'catalog_first', 'catalog_random')

def read(p):
    return json.loads(Path(p).read_text())

def write(p, obj):
    p = Path(p); p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    tmp.replace(p)

def digest(obj):
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()

def file_hash(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()

def normalize(obj):
    if isinstance(obj, dict):
        return {k: normalize(v) for k,v in obj.items() if k not in
                {'source', 'cfg_path','net_path','route_path','out_dir','proposal_source','selector_kind'}}
    if isinstance(obj, list): return [normalize(v) for v in obj]
    if isinstance(obj, str):
        if obj.startswith(str(ROOT)):
            obj = re.sub(r'/experiment/(?:luna-five-city-legal-mutation-seed\d+|selector-ablation)/[^/]+/run_\d+(?:/random_initial_catalog_(?:first|random))?', '/experiment/MATCHED_RUN', obj)
        for a in ('random_initial_catalog_first','random_initial_catalog_random','random_initial_llm'):
            obj = obj.replace(a, 'matched')
    return obj

def historical(case, arm):
    seed = case.rsplit('_',1)[1]
    if arm == 'llm': return ROOT/f'experiment/luna-five-city-legal-mutation-seed{seed}'/case/'run_001'
    return ROOT/'experiment/selector-ablation'/case/'run_001'/f'random_initial_{arm}'

def version_from_tripinfo(p):
    with Path(p).open() as f: head = f.read(4000)
    m = re.search(r'Eclipse SUMO sumo(?: Version)? ([\d.]+)', head)
    return m.group(1) if m else None

def candidate_from_disk(folder, proposal=None):
    c = read(folder/'06_comparison.json')
    if proposal is None:
        proposal = dict(c['selected_action'])
        proposal.update(proposal_id=c['proposal_id'], agent=c['agent'])
    return dict(status='ok', sumo_evaluated=True, proposal=copy.deepcopy(proposal),
                selected_action=c['selected_action'], modification=read(folder/'04_modification.json')['result'],
                optimized=read(folder/'05_optimized.json'), objective=c['objective'],
                scope_objective=c['scope_objective'], kpi_score=c['kpi_score'],
                cache_source=str(folder.relative_to(ROOT)))

def load_initial(run):
    d=read(run/'03_debate.json')
    pp=[p for p in d['evaluation_proposals'] if p.get('search_round',1)==1]
    assert len(pp)==3, (run,len(pp))
    rows={r['proposal_id']:r for r in read(run/'04_candidates.json')['candidates']}
    result=[]
    for p in pp:
        folder=run/'candidates'/p['proposal_id']
        if (folder/'06_comparison.json').exists(): result.append(candidate_from_disk(folder,p))
        else:
            row=rows[p['proposal_id']]
            result.append(dict(status=row['status'],sumo_evaluated=row.get('sumo_evaluated',False),
                               proposal=p,error=row.get('error'),cache_source=str(folder.relative_to(ROOT))))
    return result

def freeze(case):
    run=historical(case,'llm'); base=read(run/'01_baseline.json'); meta=read(run/'00_metadata.json')
    diagnosis=read(run/'02_diagnosis.json'); initial=load_initial(run)
    parents=opt._feedback_parent_candidates(initial,profile='delay',objective_scope='top10',beam_width=2)
    hist=opt._feedback_history(initial,profile='delay',objective_scope='top10')
    decisions=[]
    for parent in parents:
        feedback=opt._candidate_feedback(parent,base['kpi'],profile='delay',objective_scope='top10')
        audit=[]
        cat=build_finite_mutation_catalog(parent['proposal'],diagnosis=diagnosis,artifacts=base['artifacts'],
                                         simulation_feedback=feedback,evaluated_history=hist,audit_log=audit)
        semantic=[{k:r[k] for k in ['mutation_id','action','changed_field','args','hint']} for r in cat]
        decisions.append(dict(parent=parent['proposal'],feedback=feedback,catalog=cat,
                              catalog_sha256=digest(semantic),filter_audit=audit))
    inputs={k:dict(path=base['artifacts'][k],sha256=file_hash(base['artifacts'][k])) for k in ['net','routes']}
    cfg=ET.parse(base['artifacts']['cfg']).getroot()
    spec=dict(case=case,source_run=str(run.relative_to(ROOT)),source_metadata=meta,
              baseline=base,diagnosis=diagnosis,initial=initial,history=hist,decisions=decisions,
              input_files=inputs,sumo_seed=cfg.find('.//seed').get('value'),
              sumo_version=version_from_tripinfo(base['artifacts']['tripinfo']),
              protocol=dict(initial=3,mutations=2,bundles=2,budget=7,bundle_top_k=4,
                            history='initial_only_frozen',joint_feedback=False,
                            bundle_rule='sum_of_negative_single_top10_scores_then_compatibility'))
    spec['frozen_sha256']=digest(dict(inputs=inputs,initial=[plan_signature(x['proposal']) for x in initial],
                                     catalogs=[x['catalog_sha256'] for x in decisions],seed=spec['sumo_seed']))
    audit_arms={}
    for arm in ARMS:
        other=historical(case,arm); obase=read(other/'01_baseline.json'); od=read(other/'03_debate.json')
        oi=load_initial(other); odiag=read(other/'02_diagnosis.json')
        assert normalize(obase['kpi'])==normalize(base['kpi']), (case,arm,'baseline')
        assert normalize(odiag)==normalize(diagnosis), (case,arm,'diagnosis')
        assert [plan_signature(x['proposal']) for x in oi]==[plan_signature(x['proposal']) for x in initial]
        assert all(file_hash(obase['artifacts'][k])==inputs[k]['sha256'] for k in inputs)
        assert ET.parse(obase['artifacts']['cfg']).find('.//seed').get('value')==spec['sumo_seed']
        assert version_from_tripinfo(obase['artifacts']['tripinfo'])==spec['sumo_version']
        rounds=od['feedback_search']['rounds']; records=rounds[0]['refinements']
        op=opt._feedback_parent_candidates(oi,profile='delay',objective_scope='top10',beam_width=2)
        assert [normalize(x['proposal']['proposal_id']) for x in op]==[normalize(x['parent']['proposal_id']) for x in decisions]
        assert normalize(rounds[0]['parents'])==[normalize(x['parent']['proposal_id']) for x in decisions]
        checks=[]
        for j,dec in enumerate(decisions):
            rec=records[j]
            assert normalize(rec['simulation_feedback'])==normalize(dec['feedback'])
            # Rebuild separately with this arm's own artifacts, parent and initial history.
            cat=build_finite_mutation_catalog(op[j]['proposal'],diagnosis=odiag,artifacts=obase['artifacts'],
                    simulation_feedback=rec['simulation_feedback'],
                    evaluated_history=opt._feedback_history(oi,profile='delay',objective_scope='top10'))
            assert digest([{k:r[k] for k in ['mutation_id','action','changed_field','args','hint']} for r in cat])==dec['catalog_sha256']
            chosen=next((p for p in od['proposals'] if p['proposal_id']==rec.get('proposal_id')),None)
            if chosen:
                ref=chosen['refinement']; assert ref['mutation_catalog_ids']==[x['mutation_id'] for x in cat]
                selected=next(x for x in cat if x['mutation_id']==ref['selected_mutation_id'])
                assert plan_signature(chosen)==plan_signature(dict(action=selected['action'],args=selected['args']))
            checks.append(dict(parent_id=rec['parent_proposal_id'],catalog_sha256=dec['catalog_sha256'],
                               status=rec['status'],selected_mutation_id=chosen['refinement']['selected_mutation_id'] if chosen else None,
                               historical_full_catalog_logged=False,reconstruction_verified=True))
        audit_arms[arm]=checks
    spec['historical_audit']=audit_arms
    write(OUT/'frozen'/f'{case}.json',spec)
    print('FROZEN',case,[len(x['catalog']) for x in decisions],flush=True)
    return spec

def candidate_seed(candidate):
    """Read the executed configuration embedded in SUMO tripinfo, not run labels."""
    path=candidate['optimized']['artifacts']['tripinfo']
    with Path(path).open() as f:head=f.read(30000)
    m=re.search(r'<sumoConfiguration\b.*?</sumoConfiguration>',head,re.S)
    if not m:return None
    cfg=ET.fromstring(m.group(0));random=cfg.find('.//random')
    if random is not None and random.get('value','false').lower()=='true':return None
    seed=cfg.find('.//seed')
    return int(seed.get('value')) if seed is not None else 23423  # Verified from installed SUMO 1.27.1 template.

def cache_for(spec, extra_cache_roots=(), require_seed=False):
    cache={}
    for arm in ARMS:
        run=historical(spec['case'],arm)
        for kind in ['candidates','bundles']:
            for p in sorted((run/kind).glob('*/06_comparison.json')):
                c=candidate_from_disk(p.parent)
                if version_from_tripinfo(c['optimized']['artifacts']['tripinfo'])!=spec['sumo_version']: continue
                cache.setdefault(plan_signature(c['proposal']),c)
    for root in [*map(Path, extra_cache_roots), OUT]:
        if root != OUT:
            cached_spec=read(root/'frozen'/f"{spec['case']}.json")
            assert cached_spec['frozen_sha256']==spec['frozen_sha256'], 'Cache inputs differ'
        for p in sorted((root/'evaluation_cache'/spec['case']).glob('*/result.json')):
            c=read(p)
            if c.get('status')=='ok' and version_from_tripinfo(c['optimized']['artifacts']['tripinfo'])==spec['sumo_version']:
                cache[plan_signature(c['proposal'])]=c
    if require_seed:cache={s:c for s,c in cache.items() if candidate_seed(c)==int(spec['sumo_seed'])}
    return cache

def execute(spec,arm,repeat,model,*,max_completion_tokens=500,selector_fn=None,
            selection_provenance=None,prompt_workspace=None,extra_cache_roots=(),require_seed=False):
    if selector_fn is not None:
        assert arm=='llm' and selection_provenance, 'Saved selection requires explicit provenance'
    dest=OUT/'runs'/spec['case']/f'{arm}_rep{repeat:02d}'
    if (dest/'result.json').exists():
        previous=read(dest/'result.json')
        assert previous['frozen_sha256']==spec['frozen_sha256'], 'Resume inputs changed'
        assert previous.get('selection_provenance')==selection_provenance, 'Resume selection source changed'
        assert previous.get('max_completion_tokens',500)==max_completion_tokens, 'Resume token budget changed'
        assert previous.get('require_seed',False)==require_seed, 'Resume execution seed policy changed'
        if arm=='llm': assert previous['model']['model']==model, 'Use a separate output directory for a different model'
        return previous
    for inp in spec['input_files'].values(): assert file_hash(inp['path'])==inp['sha256']
    version=subprocess.check_output(['sumo','--version'],text=True).splitlines()[0]
    assert spec['sumo_version'] in version, (version,spec['sumo_version'])
    base=spec['baseline']; weights=spec['source_metadata']['weights']; cache=cache_for(spec,extra_cache_roots,require_seed)
    bridge=opt.LocalToolBridge(); calls=[]; candidates=copy.deepcopy(spec['initial']); new_sumo=0
    reused=sum(int(c.get('sumo_evaluated',False)) for c in candidates)
    if require_seed:candidates=[];reused=0
    state=AgentState.create(goal=spec['source_metadata']['case']['goal'],workspace=prompt_workspace or str(dest))
    state.artifacts.set(**base['artifacts'])
    selector_seed=20260927+int(spec['sumo_seed'])+1000*repeat
    fn=make_catalog_selector(arm,selector_seed) if arm!='llm' else selector_fn
    def select(**kwargs):
        payload=json.loads(kwargs['user'])
        record={'scope':kwargs['usage_scope'],'system':kwargs['system'],'payload':payload,'started':time.time()}
        calls.append(record)
        try:
            response=fn(**kwargs) if fn else call_llm(**kwargs,model=model,retries=0)
            record['response']=response
            return response
        except Exception as e:
            # Never persist credentials possibly echoed by a remote service.
            error=str(e); key=os.getenv('OPENAI_API_KEY','')
            record['error']=error.replace(key,'[REDACTED]') if key else error
            raise RuntimeError(record['error']) from None
        finally:
            record['elapsed_s']=time.time()-record['started'];write(dest/'calls.json',calls)
    def evaluate(p):
        nonlocal new_sumo,reused
        sig=plan_signature(p)
        if sig in cache:
            c=copy.deepcopy(cache[sig]);c['proposal']=copy.deepcopy(p);reused+=1;return c
        folder=OUT/'evaluation_cache'/spec['case']/digest(sig)[:24]
        if p['action']=='bundle':
            c=opt._evaluate_bundle(bridge,p,folder,base['artifacts'],base['kpi'],weights,10,{},spec['diagnosis']['selection_objective_scope'])
        else:
            c=opt._evaluate_proposal(bridge,p,folder,base['artifacts'],base['kpi'],weights,10,spec['diagnosis']['selection_objective_scope'])
        new_sumo+=int(c.get('sumo_evaluated',False));c['cache_source']=str(folder.relative_to(ROOT))
        if require_seed and c['status']=='ok':assert candidate_seed(c)==int(spec['sumo_seed']), 'Executed seed mismatch'
        write(folder/'result.json',c)
        if c['status']=='ok': cache[sig]=c
        return c
    if require_seed:
        for initial in spec['initial']:candidates.append(evaluate(initial['proposal']))
    decisions=[]
    for j,dec in enumerate(spec['decisions']):
        try:
            p=run_feedback_refinement(state,parent_proposal=dec['parent'],simulation_feedback=dec['feedback'],
                 diagnosis=spec['diagnosis'],evaluated_history=spec['history'],user_request=state.goal,
                 objective_profile='delay',search_round=2,mutation_index=1,catalog_override=dec['catalog'],
                 max_completion_tokens=max_completion_tokens,call_llm_fn=select)
            entry=dict(parent=dec['parent']['proposal_id'],catalog_sha256=dec['catalog_sha256'],proposal=p)
            if p['validation']['ok']:
                c=evaluate(p); candidates.append(c);entry['status']=c['status']
            else: entry['status']='validation_failed'
        except Exception as e:
            if selector_fn is not None or require_seed:
                # Corrupt/mismatched replay inputs must abort, not become an LLM failure.
                raise
            entry=dict(parent=dec['parent']['proposal_id'],catalog_sha256=dec['catalog_sha256'],status='generation_error',error=str(e))
        decisions.append(entry);write(dest/'decisions.json',decisions)
    quality={c['proposal']['proposal_id']:-c['scope_objective']['score'] for c in candidates if c['status']=='ok'}
    bundles=generate_pair_bundles([c['proposal'] for c in candidates if c['status']=='ok'],top_k=4,max_bundles=2,quality_by_id=quality)
    for p in bundles: candidates.append(evaluate(p))
    baseline=opt._baseline_candidate(base['artifacts'],base['run'],base['kpi'],spec['diagnosis'],weights,'top10')
    selected=opt._select_best_candidate([baseline]+candidates,profile='delay',objective_scope='top10')
    result=dict(case=spec['case'],arm=arm,repeat=repeat,selector_seed=selector_seed,
                frozen_sha256=spec['frozen_sha256'],model=resolved_llm_config(model=model) if arm=='llm' else None,
                sumo_seed=spec['sumo_seed'],sumo_version=version,budget=7,
                logical_sumo_evaluations=sum(int(c.get('sumo_evaluated',False)) for c in candidates),
                new_sumo_evaluations=new_sumo,reused_evaluations=reused,decisions=decisions,
                candidates=candidates,selected=selected,baseline=baseline,
                token_usage=state.context.get('token_usage',{}),calls=len(calls),
                max_completion_tokens=max_completion_tokens,selection_provenance=selection_provenance,
                require_seed=require_seed,feedback_provenance='archived_frozen_feedback',
                actual_api_requests=len(state.context.get('llm_response_metadata',[])),
                llm_response_metadata=state.context.get('llm_response_metadata',[]))
    assert result['logical_sumo_evaluations']<=7
    write(dest/'result.json',result)
    print('DONE',spec['case'],arm,repeat,'top10',selected['scope_objective']['top10_relative_improvement'],
          'new',new_sumo,'reused',reused,flush=True)
    return result

def main():
    global OUT
    ap=argparse.ArgumentParser();ap.add_argument('mode',choices=['freeze','run','probe','verify'])
    ap.add_argument('--cases',nargs='*');ap.add_argument('--arms',nargs='+',default=list(ARMS),choices=ARMS)
    ap.add_argument('--repeats',type=int,default=1);ap.add_argument('--model',default='gpt-5.6-luna')
    ap.add_argument('--output-dir',type=Path,default=OUT)
    args=ap.parse_args();os.chdir(ROOT)
    OUT=args.output_dir.resolve()
    # Pin both SUMO and netconvert to the same installation used in archived XML.
    os.environ['PATH']='/usr/bin:'+os.environ['PATH']
    if args.mode=='probe':
        try:
            response=call_llm(system='Return JSON.',user='Return {"status":"ok"}.',model=args.model,max_tokens=40,retries=0,timeout=20)
            result=dict(status='ok',model=args.model,response=response)
        except Exception as e:
            msg=str(e);key=os.getenv('OPENAI_API_KEY','');msg=msg.replace(key,'[REDACTED]') if key else msg
            result=dict(status='blocked',model=args.model,error=msg)
        write(OUT/f'api_probe_{args.model}.json',result);print(json.dumps(result));return
    cases=args.cases or [json.loads(x)['id'] for x in (ROOT/'experiment/selector-ablation-cases.jsonl').read_text().splitlines()]
    for case in cases:
        if args.mode=='freeze':
            if not (OUT/'frozen'/f'{case}.json').exists(): freeze(case)
        elif args.mode=='verify':
            spec=read(OUT/'frozen'/f'{case}.json');base=spec['baseline'];bridge=opt.LocalToolBridge()
            artifacts=opt.prepare_baseline_sandbox(spec['source_metadata']['case']['cfg'],OUT/'verification'/case)
            opt._tool(bridge,'runner','run',{'cfg_path':artifacts['cfg'],'gui':False})
            kpi=opt.collect_kpi(bridge,artifacts,10,spec['diagnosis']['selection_objective_scope']);kpi.pop('diagnosis')
            assert normalize(kpi)==normalize(base['kpi']), 'Fresh baseline differs; do not mix cache and new simulations'
            write(OUT/'verification'/case/'verification.json',dict(status='exact_match',version=version_from_tripinfo(artifacts['tripinfo']),kpi=kpi))
            print('BASELINE VERIFIED',case,flush=True)
        else:
            spec=read(OUT/'frozen'/f'{case}.json')
            for repeat in range(args.repeats):
                for arm in args.arms: execute(spec,arm,repeat,args.model)

if __name__=='__main__': main()
