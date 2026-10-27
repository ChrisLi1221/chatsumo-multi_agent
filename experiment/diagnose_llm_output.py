#!/usr/bin/env python3
"""Audit archived failures and replay identical selection requests without SUMO.

The replay changes only max_completion_tokens; no policy fallback or retries.
Results are separate from the manuscript experiment and resume by request hash.
"""
import argparse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import csv
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agents.llm_agents import call_llm, LLMOutputError, resolved_llm_config
from agents.state_refactored import AgentState
from agents.debate_optimizer import _resolve_mutation_selection

ARCHIVE = ROOT / 'experiment/multra_revision/runs'
OUT = ROOT / 'experiment/llm_output_diagnosis'


def read(path):
    return json.loads(path.read_text())


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False))
    temp.replace(path)


def audit():
    rows = []
    for path in sorted(ARCHIVE.glob('*/llm_rep*/calls.json')):
        for i, call in enumerate(read(path)):
            err = call.get('error', '')
            kind = ('http_output_limit' if 'max_tokens or model output limit was reached' in err else
                    'empty_visible_content' if err.endswith('LLM output is not JSON: ') else
                    'other_error' if err else 'ok')
            rows.append(dict(case=path.parent.parent.name, repeat=path.parent.name,
                             call_index=i, scope=call['scope'], category=kind,
                             error=err, source=str(path.relative_to(ROOT))))
    data = {'all': dict(Counter(r['category'] for r in rows)),
            'primary': dict(Counter(r['category'] for r in rows if r['repeat']=='llm_rep00')),
            'by_role': {role: dict(Counter(r['category'] for r in rows if r['scope'].split(':')[1]==role))
                        for role in sorted({r['scope'].split(':')[1] for r in rows})},
            'limitation': 'Archived logs omit finish_reason and reasoning-token details; empty content alone does not prove token exhaustion.',
            'requests': rows}
    write(OUT/'archive_audit.json', data)
    print(json.dumps({k:v for k,v in data.items() if k!='requests'}, indent=2))


def replay(job):
    path, index, call, budget, repeat, model = job
    user = json.dumps(call['payload'], ensure_ascii=False, default=str)
    request = dict(system=call['system'], user=user, model=model, max_completion_tokens=budget,
                   force_json=True, retries=0)
    fingerprint = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
    dest = OUT/'replays'/path.parent.parent.name/f'pick{index}_budget{budget}_rep{repeat}.json'
    if dest.exists():
        old = read(dest)
        assert old['request_sha256']==fingerprint, 'Changed request: choose a new --output-dir'
        return old
    state = AgentState.create(goal='Diagnose archived mutation selection output', workspace=str(dest.parent))
    started = time.monotonic()
    result = dict(case=path.parent.parent.name, call_index=index, scope=call['scope'],
                  budget=budget, repeat=repeat, source=str(path.relative_to(ROOT)),
                  archived_failed=bool(call.get('error')), request_sha256=fingerprint,
                  model=resolved_llm_config(model=model), protocol='same_prompt_budget_only_no_retry_no_fallback')
    try:
        response = call_llm(system=call['system'], user=user, model=model, max_tokens=budget,
                            retries=0, state=state, usage_scope=call['scope'])
        selected = _resolve_mutation_selection(response, call['payload']['mutation_catalog'])
        result.update(status='ok' if selected else 'invalid_mutation_id', response=response,
                      selected_mutation_id=selected['mutation_id'] if selected else None)
    except Exception as exc:
        # call_llm records sanitized details; do not log environment or headers.
        result.update(status=exc.category if isinstance(exc, LLMOutputError) else 'request_error',
                      error=str(exc))
    result.update(elapsed_s=time.monotonic()-started,
                  response_metadata=state.context.get('llm_response_metadata', []),
                  token_usage=state.context.get('token_usage', {}))
    write(dest, result)
    detail = result['response_metadata'][-1] if result['response_metadata'] else {}
    print(result['case'], index, budget, result['status'],
          'finish', detail.get('finish_reason'), 'usage', detail.get('usage', {}), flush=True)
    return result


def summarize():
    rows = [read(p) for p in sorted((OUT/'replays').glob('*/*.json'))]
    by_budget = {}
    for budget in sorted({r['budget'] for r in rows}):
        rr = [r for r in rows if r['budget']==budget]
        by_budget[budget] = dict(n=len(rr), valid=sum(r['status']=='ok' for r in rr),
            outcomes=dict(Counter(r['status'] for r in rr)),
            archived_failures_retested=sum(r['archived_failed'] for r in rr),
            recovered_archived_failures=sum(r['archived_failed'] and r['status']=='ok' for r in rr),
            prompt_tokens=sum(r['token_usage'].get('prompt_tokens',0) for r in rr),
            completion_tokens=sum(r['token_usage'].get('completion_tokens',0) for r in rr))
    paired = defaultdict(dict)
    flat = []
    for r in rows:
        key = (r['case'],r['call_index'],r['repeat']); paired[key][r['budget']] = r['status']
        m = r['response_metadata'][-1] if r['response_metadata'] else {}
        u = m.get('usage', {}); detail = u.get('completion_tokens_details') or {}
        flat.append({k:r[k] for k in ['case','call_index','scope','budget','repeat','archived_failed','status']} |
                    dict(finish_reason=m.get('finish_reason'), content_chars=m.get('content_chars'),
                         completion_tokens=u.get('completion_tokens'), reasoning_tokens=detail.get('reasoning_tokens'),
                         elapsed_s=r['elapsed_s']))
    write(OUT/'summary.json', dict(by_budget=by_budget,
          paired_requests=[dict(case=k[0],call_index=k[1],repeat=k[2],outcomes=v) for k,v in paired.items()],
          interpretation='Repeated stochastic calls; no SUMO rerun and no traffic-performance claim.'))
    if flat:
        with (OUT/'requests.csv').open('w') as f:
            w=csv.DictWriter(f,fieldnames=list(flat[0]));w.writeheader();w.writerows(flat)
    print(json.dumps(by_budget,indent=2))


def verify_pipeline():
    """Replay saved model decisions through the actual catalog resolver/validator."""
    from agents.debate_optimizer import run_feedback_refinement
    checked=[]
    for path in sorted((OUT/'replays').glob('*/pick*_budget4096_rep0.json')):
        record=read(path); assert record['status']=='ok'
        spec=read(ROOT/'experiment/multra_revision/frozen'/f"{record['case']}.json")
        call=read(ROOT/record['source'])[record['call_index']]
        decision=spec['decisions'][record['call_index']]
        state=AgentState.create(goal=call['payload']['user_request'],
                                workspace=call['payload']['artifacts']['workspace'])
        state.artifacts.set(**spec['baseline']['artifacts'])
        def saved_response(**kwargs):
            assert kwargs['max_tokens']==4096
            assert kwargs['system']==call['system']
            assert json.loads(kwargs['user'])==call['payload']
            return record['response']
        result=run_feedback_refinement(state,parent_proposal=decision['parent'],
            simulation_feedback=decision['feedback'],diagnosis=spec['diagnosis'],
            evaluated_history=spec['history'],user_request=state.goal,
            objective_profile='delay',search_round=2,mutation_index=1,
            catalog_override=decision['catalog'],call_llm_fn=saved_response)
        assert result['validation']['ok'], result['validation']
        assert result['refinement']['selected_mutation_id']==record['selected_mutation_id']
        checked.append(dict(case=record['case'],call_index=record['call_index'],
                            selected_id=record['selected_mutation_id'],validation='ok',
                            budget=result['refinement']['max_completion_tokens']))
    assert len(checked)==30, 'Expected the complete 15-case primary set'
    write(OUT/'pipeline_verification.json',checked)
    print('30/30 saved responses passed unchanged-prompt, catalog-resolution, and executable-validation checks; no API/SUMO calls.')


def main():
    global OUT
    p = argparse.ArgumentParser()
    p.add_argument('mode', choices=['audit','run','summarize','verify'])
    p.add_argument('--cases', nargs='+')
    p.add_argument('--budgets', nargs='+', type=int, default=[500,4096])
    p.add_argument('--repeats', type=int, default=1)
    p.add_argument('--workers', type=int, default=2)
    p.add_argument('--model', default='gpt-5.6-luna')
    p.add_argument('--output-dir', type=Path, default=OUT)
    args = p.parse_args(); OUT=args.output_dir.resolve()
    if args.mode=='audit':audit();return
    if args.mode=='summarize':summarize();return
    if args.mode=='verify':verify_pipeline();return
    assert all(b>0 for b in args.budgets) and args.repeats>0 and args.workers>0
    jobs=[]
    for path in sorted(ARCHIVE.glob('*/llm_rep00/calls.json')):
        if args.cases and path.parent.parent.name not in args.cases:continue
        for i,call in enumerate(read(path)):
            for repeat in range(args.repeats):
                for budget in args.budgets:jobs.append((path,i,call,budget,repeat,args.model))
    assert jobs, 'No matching archived calls'
    with ThreadPoolExecutor(max_workers=args.workers) as pool:list(pool.map(replay,jobs))
    summarize()


if __name__=='__main__':main()
