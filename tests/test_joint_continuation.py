from copy import deepcopy
from types import SimpleNamespace

import pytest

from experiment import continue_joint_feedback as continuation


def measured(proposal, improvement):
    return dict(status='ok',sumo_evaluated=True,proposal=proposal,kpi_score=1-improvement,
        optimized={'kpi':{'mean_delay':100*(1-improvement)}},
        scope_objective=dict(available=True,score=1-improvement,constraints_passed=True,
            constraint_checks={'top10_improvement':True},top10_relative_improvement=improvement))


@pytest.mark.parametrize('budget,expected',[(1,0),(2,1),(3,2)])
def test_measured_child_becomes_next_parent_and_budget_is_shared(monkeypatch,budget,expected):
    parent={'proposal_id':'parent','action':'bundle','agent':'bundle_optimizer',
            'actions':[{'action':'speed','args':{'value':1}}]}
    candidates=[measured(parent,.1)];seen=[]
    def refine(state,**kw):
        seen.append(deepcopy(kw))
        child=deepcopy(kw['parent_plan'])
        child.update(proposal_id=f'child{len(seen)}',joint_root_id='parent',validation={'ok':True},
                     refinement={'selected_component_index':kw['component_start']%2})
        child['actions'][0]['args']['value']+=1
        return child
    monkeypatch.setattr(continuation,'run_joint_feedback_refinement',refine)
    records=continuation.joint_loop(candidates,state=SimpleNamespace(goal='reduce delay'),
        spec={'baseline':{'kpi':{}},'diagnosis':{}},rounds=2,beam_width=1,budget=budget,
        selector_kind='catalog_first',select=lambda **kw:None,
        evaluate=lambda p:measured(p,.1+.1*len(seen)))
    assert len(candidates)==1+expected and len(seen)==expected
    if expected==2:
        assert seen[1]['parent_plan']['proposal_id']=='child1'
        assert seen[1]['simulation_feedback']['measured_kpi']['mean_delay']==80
        assert seen[1]['component_start']==1
        assert len(seen[1]['evaluated_history'])==2
        assert records[1]['parents']==['child1']


def test_exhausted_parent_does_not_trigger_a_model_call_or_simulation(monkeypatch):
    candidates=[measured({'proposal_id':'p','action':'bundle','agent':'bundle_optimizer'},.1)]
    def exhausted(*args,**kwargs):raise continuation.JointCatalogExhausted('no legal moves')
    monkeypatch.setattr(continuation,'run_joint_feedback_refinement',exhausted)
    records=continuation.joint_loop(candidates,state=SimpleNamespace(goal='g'),
        spec={'baseline':{'kpi':{}},'diagnosis':{}},rounds=2,beam_width=1,budget=3,
        selector_kind='llm',select=lambda **kw:pytest.fail('Unexpected model call'),
        evaluate=lambda p:pytest.fail('Unexpected simulation'))
    assert records[0]['decisions'][0]['status']=='catalog_exhausted'
    assert records[1]['reason']=='no_unexhausted_measured_bundle'
    assert len(candidates)==1


def test_corrupt_checkpoint_cannot_be_reported_as_model_failure(monkeypatch):
    def corrupt(*args,**kwargs):raise AssertionError('checkpoint mismatch')
    monkeypatch.setattr(continuation,'run_joint_feedback_refinement',corrupt)
    with pytest.raises(AssertionError,match='checkpoint mismatch'):
        continuation.joint_loop([measured({'proposal_id':'p','action':'bundle','agent':'bundle_optimizer'},.1)],
            state=SimpleNamespace(goal='g'),spec={'baseline':{'kpi':{}},'diagnosis':{}},
            rounds=1,beam_width=1,budget=2,selector_kind='llm',select=None,evaluate=None)
