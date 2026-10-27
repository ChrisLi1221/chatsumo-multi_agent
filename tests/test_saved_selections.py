"""Saved decisions must remain tied to their original input and response files."""
import hashlib
import json

import pytest

from experiment import evaluate_saved_selections as saved
from experiment import revision_matched as matched


@pytest.fixture
def replay_inputs(tmp_path,monkeypatch):
    monkeypatch.setattr(saved,'ROOT',tmp_path)
    root=tmp_path/'replays';case='case';folder=root/case;folder.mkdir(parents=True)
    calls=[];decisions=[]
    for i in range(2):
        parent={'proposal_id':f'parent{i}'};feedback={'measured':i}
        payload={'parent_proposal':parent,'simulation_feedback':feedback,'artifacts':{'workspace':'original'}}
        calls.append({'system':'original system','payload':payload,'scope':f'role{i}'})
        decisions.append({'parent':parent,'feedback':feedback,'catalog':[{'mutation_id':'mutation_001'}]})
    (tmp_path/'calls.json').write_text(json.dumps(calls))
    for i,call in enumerate(calls):
        req=dict(system=call['system'],user=json.dumps(call['payload'],ensure_ascii=False,default=str),
                 model='test-model',max_completion_tokens=4096,force_json=True,retries=0)
        record=dict(case=case,call_index=i,status='ok',budget=4096,repeat=0,source='calls.json',
            model={'model':'test-model'},request_sha256=hashlib.sha256(json.dumps(req,sort_keys=True).encode()).hexdigest(),
            selected_mutation_id='mutation_001',response={'mutation_id':'mutation_001'})
        (folder/f'pick{i}_budget4096_rep0.json').write_text(json.dumps(record))
    return case,{'decisions':decisions},root,calls


def test_saved_selector_consumes_exactly_two_original_decisions(replay_inputs):
    case,spec,root,calls=replay_inputs;selector=saved.SavedSelector(case,spec,root)
    for call in calls:
        assert selector(max_tokens=4096,system=call['system'],user=json.dumps(call['payload']),
                        usage_scope=call['scope'])=={'mutation_id':'mutation_001'}
    assert selector.index==2
    with pytest.raises(AssertionError,match='extra selection'):selector()


def test_saved_selector_rejects_changed_feedback(replay_inputs):
    case,spec,root,calls=replay_inputs;selector=saved.SavedSelector(case,spec,root)
    payload=dict(calls[0]['payload'],simulation_feedback={'measured':999})
    with pytest.raises(AssertionError,match='input changed'):
        selector(max_tokens=4096,system=calls[0]['system'],user=json.dumps(payload),usage_scope=calls[0]['scope'])
    assert selector.index==0


def test_saved_selector_rejects_modified_source_prompt(replay_inputs):
    case,spec,root,calls=replay_inputs;calls[0]['system']='different system'
    (root.parent/'calls.json').write_text(json.dumps(calls))
    with pytest.raises(AssertionError,match='fingerprint changed'):saved.SavedSelector(case,spec,root)


def test_resume_cannot_reuse_results_from_a_different_response(monkeypatch,tmp_path):
    monkeypatch.setattr(matched,'OUT',tmp_path)
    path=tmp_path/'runs/case/llm_rep00/result.json';path.parent.mkdir(parents=True)
    path.write_text(json.dumps({'frozen_sha256':'same','selection_provenance':{'response':'old'}}))
    with pytest.raises(AssertionError,match='selection source changed'):
        matched.execute({'case':'case','frozen_sha256':'same'},'llm',0,'test-model',
                        selector_fn=lambda **kw: {},selection_provenance={'response':'new'},max_completion_tokens=4096)
