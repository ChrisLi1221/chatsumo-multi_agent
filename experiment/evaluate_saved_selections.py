#!/usr/bin/env python3
"""Evaluate the 30 saved 4096-token decisions and their ordinary pair bundles.

Offline: API calls are forbidden. Original manuscript experiments stay read-only.
"""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from experiment import revision_matched as matched
from agents.debate_optimizer import run_feedback_refinement
from agents.state_refactored import AgentState

ARCHIVE=ROOT/'experiment/multra_revision'
REPLAYS=ROOT/'experiment/llm_output_diagnosis/replays'
OUT=ROOT/'experiment/llm_output_evaluation'


class SavedSelector:
    def __init__(self, case, spec, replay_root=REPLAYS):
        self.index=0; self.records=[]; self.calls=[]; entries=[]
        for i in range(2):
            path=replay_root/case/f'pick{i}_budget4096_rep0.json'
            record=matched.read(path)
            assert record['case']==case and record['call_index']==i
            assert record['status']=='ok' and record['budget']==4096 and record['repeat']==0
            source=ROOT/record['source'];call=matched.read(source)[i]
            request=dict(system=call['system'],user=json.dumps(call['payload'],ensure_ascii=False,default=str),
                         model=record['model']['model'],max_completion_tokens=4096,force_json=True,retries=0)
            fingerprint=hashlib.sha256(json.dumps(request,sort_keys=True).encode()).hexdigest()
            assert fingerprint==record['request_sha256'], 'Saved prompt fingerprint changed'
            assert call['payload']['parent_proposal']==spec['decisions'][i]['parent']
            assert call['payload']['simulation_feedback']==spec['decisions'][i]['feedback']
            assert record['selected_mutation_id'] in {x['mutation_id'] for x in spec['decisions'][i]['catalog']}
            self.records.append(record);self.calls.append(call)
            entries.append(dict(path=str(path.relative_to(ROOT)),sha256=matched.file_hash(path),
                                request_sha256=fingerprint,selected_id=record['selected_mutation_id']))
        assert len({r['model']['model'] for r in self.records})==1
        self.model=self.records[0]['model']['model']
        self.workspace=self.calls[0]['payload']['artifacts']['workspace']
        self.provenance=dict(mode='saved_4096_token_decisions',entries=entries,
                             new_api_requests=0,initial_only_history=True)

    def __call__(self, **kwargs):
        assert self.index<2, 'Unexpected extra selection or repair attempt'
        call=self.calls[self.index];record=self.records[self.index]
        assert kwargs['max_tokens']==4096
        assert kwargs['system']==call['system'], 'System prompt changed'
        assert json.loads(kwargs['user'])==call['payload'], 'Frozen selection input changed'
        assert kwargs['usage_scope']==call['scope']
        self.index+=1
        return copy.deepcopy(record['response'])


def preflight(case):
    spec=matched.read(ARCHIVE/'frozen'/f'{case}.json')
    for item in spec['input_files'].values(): assert matched.file_hash(item['path'])==item['sha256']
    verification=matched.read(ARCHIVE/'verification'/case/'verification.json')
    assert verification['status']=='exact_match'
    assert matched.normalize(verification['kpi'])==matched.normalize(spec['baseline']['kpi'])
    selector=SavedSelector(case,spec)
    state=AgentState.create(goal=spec['source_metadata']['case']['goal'],workspace=selector.workspace)
    state.artifacts.set(**spec['baseline']['artifacts'])
    for i,d in enumerate(spec['decisions']):
        proposal=run_feedback_refinement(state,parent_proposal=d['parent'],simulation_feedback=d['feedback'],
            diagnosis=spec['diagnosis'],evaluated_history=spec['history'],user_request=state.goal,
            objective_profile='delay',search_round=2,mutation_index=1,catalog_override=d['catalog'],
            max_completion_tokens=4096,call_llm_fn=selector)
        assert proposal['validation']['ok'], proposal['validation']
        assert proposal['refinement']['selected_mutation_id']==selector.records[i]['selected_mutation_id']
    assert selector.index==2
    return spec


def main():
    p=argparse.ArgumentParser();p.add_argument('--cases',nargs='+');p.add_argument('--output-dir',type=Path,default=OUT)
    p.add_argument('--preflight-only',action='store_true')
    p.add_argument('--seed-corrected',action='store_true',help='Re-evaluate all three arms with corrected execution seeds; retain frozen feedback and choices.')
    args=p.parse_args();os.chdir(ROOT);os.environ['PATH']='/usr/bin:'+os.environ['PATH']
    matched.OUT=args.output_dir.resolve()
    if args.seed_corrected and matched.OUT==OUT:matched.OUT=ROOT/'experiment/llm_output_seed_corrected'
    assert matched.OUT!=ARCHIVE, 'Never overwrite the manuscript cohort'
    # Fail closed if a future refactor accidentally tries to call the API.
    def api_forbidden(*args,**kwargs):raise AssertionError('API calls forbidden during saved-response evaluation')
    matched.call_llm=api_forbidden
    cases=args.cases or [json.loads(s)['id'] for s in (ROOT/'experiment/selector-ablation-cases.jsonl').read_text().splitlines()]
    specs={case:preflight(case) for case in cases}
    print('PREFLIGHT OK:',len(specs),'cases,',2*len(specs),'saved decisions',flush=True)
    if args.preflight_only:return
    for case,spec in specs.items():
        frozen=matched.OUT/'frozen'/f'{case}.json'
        if frozen.exists(): assert matched.read(frozen)==spec
        else:matched.write(frozen,spec)
        for arm in (matched.ARMS if args.seed_corrected else ['llm']):
            selector=SavedSelector(case,spec)
            result=matched.execute(spec,arm,0,selector.model,max_completion_tokens=4096,
                selector_fn=selector if arm=='llm' else None,
                selection_provenance=selector.provenance if arm=='llm' else None,
                prompt_workspace=selector.workspace if arm=='llm' else None,
                extra_cache_roots=[ARCHIVE,OUT] if args.seed_corrected else [ARCHIVE],require_seed=args.seed_corrected)
            assert result['actual_api_requests']==0
            expected=([r['selected_mutation_id'] for r in selector.records] if arm=='llm' else
                [d['proposal']['refinement']['selected_mutation_id'] for d in matched.read(ARCHIVE/'runs'/case/f'{arm}_rep00/result.json')['decisions']])
            assert [d['proposal']['refinement']['selected_mutation_id'] for d in result['decisions']]==expected
    paths=[matched.OUT/'runs'/case/f'{arm}_rep00/result.json' for case in cases for arm in (matched.ARMS if args.seed_corrected else ['llm'])]
    results=[matched.read(path) for path in paths]
    counts=dict(cases=len(cases),arm_case_runs=len(results),saved_choices=sum(len(r['decisions']) for r in results if r['arm']=='llm'),new_api_requests=0,
        seed_corrected=args.seed_corrected,feedback='Original frozen feedback retained; corrected initial outcomes affect subsequent bundle ranking only.',
        new_candidate_sumo=sum(r['new_sumo_evaluations'] for r in results),
        reused_evaluations=sum(r['reused_evaluations'] for r in results),
        logical_evaluations=sum(r['logical_sumo_evaluations'] for r in results),
        bundle_proposals=sum(c['proposal']['action']=='bundle' for r in results for c in r['candidates']),
        baseline_verifications='Reused 15-case exact-KPI verification; no baseline reruns')
    matched.write(matched.OUT/'execution_accounting.json',counts)
    matched.write(matched.OUT/'source_hashes.json',[dict(path=str(path.relative_to(ROOT)),sha256=matched.file_hash(path))
        for folder in ['agents','experiment','providers','providers/Modification'] for path in sorted((ROOT/folder).glob('*.py'))])
    print(json.dumps(counts,indent=2))


if __name__=='__main__':main()
