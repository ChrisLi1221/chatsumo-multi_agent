import json
from types import SimpleNamespace

import pytest

from experiment import full_feedback_experiment as full


def measured(pid,agent,gain,delay):
    return dict(status='ok',sumo_evaluated=True,proposal={'proposal_id':pid,'agent':agent,'action':'set_corridor_speed','args':{'speed_factor':.8}},
        optimized={'kpi':{'mean_delay':delay}},kpi_score=1-gain,
        scope_objective={'available':True,'score':1-gain,'constraints_passed':True,
                         'constraint_checks':{'top10_improvement':True},'top10_relative_improvement':gain})


def test_first_round_inputs_are_rebuilt_from_measured_initials(monkeypatch):
    initial=[measured('a','network_optimizer',.2,80),measured('b','signal_optimizer',.1,90)]
    seen=[]
    def catalog(parent,**kwargs):
        seen.append(kwargs)
        return [{'mutation_id':'mutation_001','action':parent['action'],'args':{'speed_factor':.9}}]
    monkeypatch.setattr(full,'build_finite_mutation_catalog',catalog)
    monkeypatch.setattr(full.m,'call_llm',lambda **kwargs:pytest.fail('Freeze must not call model'))
    history,decisions=full.rebuild_decisions(initial,{'kpi':{'mean_delay':100},'artifacts':{}},{})
    assert [d['feedback']['measured_kpi']['mean_delay'] for d in decisions]==[80,90]
    assert [d['parent']['proposal_id'] for d in decisions]==['a','b']
    assert all(s['evaluated_history']==history for s in seen)
    assert len(history)==2 and len(decisions)==2


def journal(tmp_path):
    obj=object.__new__(full.Journal)
    obj.spec={'sumo_seed':43};obj.arm='llm';obj.repeat=0
    obj.args=SimpleNamespace(model='test-model');obj.state=SimpleNamespace(context={})
    obj.dest=tmp_path;obj.calls=[]
    return obj


def request(obj):
    return dict(system='Choose an ID',user=json.dumps({'mutation_catalog':[{'mutation_id':'m1'}]}),
                usage_scope='feedback',max_tokens=4096,state=obj.state)


def test_resume_reuses_exact_response_and_rejects_changed_feedback(monkeypatch,tmp_path):
    obj=journal(tmp_path);calls=[]
    def choose(**kwargs):
        calls.append(kwargs);return {'mutation_id':'m1'}
    monkeypatch.setattr(full.m,'call_llm',choose)
    assert obj.select(**request(obj))=={'mutation_id':'m1'}
    resumed=journal(tmp_path)
    assert resumed.select(**request(resumed))=={'mutation_id':'m1'} and len(calls)==1
    changed=journal(tmp_path);kw=request(changed);kw['user']='{"feedback":"different"}'
    with pytest.raises(AssertionError,match='inputs changed'):changed.select(**kw)


def test_transport_failure_blocks_run_and_is_retryable_without_discarding_record(monkeypatch,tmp_path):
    first=journal(tmp_path)
    def blocked(**kw):
        kw['state'].context.setdefault('llm_response_metadata',[]).append({'outcome':'transport_or_protocol_error'})
        raise RuntimeError('DNS unavailable')
    monkeypatch.setattr(full.m,'call_llm',blocked)
    with pytest.raises(full.InfrastructureError):first.select(**request(first))
    assert full.m.read(tmp_path/'calls/000.json')['status']=='infrastructure_error'
    def recovered(**kw):
        kw['state'].context.setdefault('llm_response_metadata',[]).append({'http_status':200,'outcome':'ok'})
        return {'mutation_id':'m1'}
    monkeypatch.setattr(full.m,'call_llm',recovered)
    resumed=journal(tmp_path);assert resumed.select(**request(resumed))=={'mutation_id':'m1'}
    record=full.m.read(tmp_path/'calls/000.json');assert len(record['attempts'])==2 and record['status']=='ok'


def test_model_output_failure_is_preserved_without_resampling(monkeypatch,tmp_path):
    def length_error(**kw):
        kw['state'].context.setdefault('llm_response_metadata',[]).append({'http_status':200,'outcome':'output_token_limit'})
        raise RuntimeError('Output exhausted')
    monkeypatch.setattr(full.m,'call_llm',length_error)
    obj=journal(tmp_path)
    with pytest.raises(RuntimeError,match='Output exhausted'):obj.select(**request(obj))
    monkeypatch.setattr(full.m,'call_llm',lambda **kw:pytest.fail('Must not resample a saved model failure'))
    resumed=journal(tmp_path)
    with pytest.raises(RuntimeError,match='Output exhausted'):resumed.select(**request(resumed))


def test_cache_shards_without_the_requested_case_are_not_loaded(monkeypatch,tmp_path):
    matching=tmp_path/'complete';partial=tmp_path/'seed44'
    (matching/'frozen').mkdir(parents=True);(partial/'frozen').mkdir(parents=True)
    (matching/'frozen/case43.json').write_text('{}');(partial/'frozen/case44.json').write_text('{}')
    monkeypatch.setattr(full,'CACHE_ROOTS',[matching,partial]);seen=[]
    monkeypatch.setattr(full.m,'cache_for',lambda spec,**kw:seen.append(kw) or {})
    monkeypatch.setattr(full.m.opt,'LocalToolBridge',lambda:object())
    full.Journal({'case':'case43','cache_identity':{}},'llm',0,None,None,tmp_path)
    assert seen==[{'extra_cache_roots':[matching],'require_seed':True}]
