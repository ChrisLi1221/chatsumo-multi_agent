from copy import deepcopy
from types import SimpleNamespace

import pytest

from experiment import full_feedback_experiment as full


def test_sibling_filter_preserves_ids_and_deduplicates_semantic_actions():
    catalog=[dict(mutation_id=f'm{i}',action='speed',args={'factor':i},full_args={'factor':i,'cfg_path':'baseline'}) for i in (1,2,3)]
    used={full.plan_signature(dict(action='speed',args={'factor':2,'cfg_path':'elsewhere'}))}
    assert [r['mutation_id'] for r in full.sibling_catalog(catalog,used)]==['m1','m3']
    assert len(catalog)==3


@pytest.mark.parametrize('first_child_status',['ok','error'])
def test_two_children_share_parent_feedback_without_chaining_and_remain_distinct(tmp_path,monkeypatch,first_child_status):
    parents=[dict(proposal_id=f'p{i}',action=f'action{i}',args={'value':0}) for i in range(2)]
    def candidate(proposal,status='ok'):
        return dict(proposal=proposal,status=status,sumo_evaluated=True,scope_objective={'score':1.,'top10_relative_improvement':0.})
    initial=[candidate(parents[0]),candidate(parents[1])]+[candidate(dict(proposal_id=f'i{i}',action=f'init{i}',args={})) for i in range(3)]
    decisions=[]
    for parent in parents:
        catalog=[dict(mutation_id=f'm{i}',action=parent['action'],args={'value':i},full_args={'value':i}) for i in (1,2,3)]
        decisions.append(dict(parent=parent,feedback={'measured':'initial'},catalog=catalog,catalog_sha256=full.m.digest(catalog)))
    baseline=candidate(dict(proposal_id='baseline',action='noop',args={}))
    spec=dict(case='test',frozen_sha256='test',initial=initial,decisions=decisions,history=[{'initial_only':True}],diagnosis={},
        source_metadata={'case':{'goal':'reduce delay'}},baseline={'artifacts':{}},baseline_candidate=baseline,sumo_seed=42,sumo_version='test')
    requests=[]
    def choose(state,**kwargs):
        requests.append(deepcopy(kwargs));parent=kwargs['parent_proposal'];row=kwargs['catalog_override'][0]
        return dict(proposal_id=f"{parent['proposal_id']}_child{kwargs['mutation_index']}",action=parent['action'],args=row['full_args'],
            parent_proposal_id=parent['proposal_id'],validation={'ok':True},refinement={})
    class Journal:
        def __init__(self,*args):self.calls=[];self.events=[]
        def select(self,**kwargs):raise AssertionError('No external API in test')
        def evaluate(self,proposal):
            self.events.append({'new_sumo':1,'reused':0})
            return candidate(proposal,first_child_status if proposal['proposal_id'].endswith('child1') else 'ok')
    monkeypatch.setattr(full,'Journal',Journal);monkeypatch.setattr(full,'run_feedback_refinement',choose)
    monkeypatch.setattr(full,'generate_pair_bundles',lambda *a,**kw:[])
    monkeypatch.setattr(full,'joint_loop',lambda *a,**kw:[])
    monkeypatch.setattr(full.m.opt,'_select_best_candidate',lambda *a,**kw:baseline)
    args=SimpleNamespace(output_dir=tmp_path,children_per_parent=2,max_completion_tokens=4096,joint_feedback_rounds=2,sumo_budget=15)
    result=full.execute(spec,'catalog_first',0,args)
    assert result['protocol']['initial_count']==5 and result['protocol']['single_refinements']==4
    assert len(result['candidates'])==9 and result['logical_sumo_evaluations']==9
    for offset in (0,2):
        a,b=requests[offset:offset+2]
        assert a['parent_proposal']==b['parent_proposal']==parents[offset//2]
        assert a['simulation_feedback']==b['simulation_feedback']=={'measured':'initial'}
        assert a['evaluated_history']==b['evaluated_history']==spec['history']
        assert [r['mutation_id'] for r in a['catalog_override']]==['m1','m2','m3']
        assert [r['mutation_id'] for r in b['catalog_override']]==['m2','m3']
    assert len({full.plan_signature(c['proposal']) for c in result['candidates'][5:]})==4


def test_exhausted_sibling_catalog_is_empty_without_fabricated_replacement():
    item={'mutation_id':'only','action':'speed','args':{'factor':.7},'full_args':{'factor':.7}}
    assert full.sibling_catalog([item],{full.plan_signature(dict(action='speed',args={'factor':.7}))})==[]
