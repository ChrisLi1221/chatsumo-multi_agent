from copy import deepcopy
import json

import pytest

from agents.interaction_feedback import build_interaction_feedback
from tests.test_joint_optimizer import _components


def example(tmp_path):
    base_values=dict(top10_total_time_loss_s=100.,neighbor_total_time_loss_s=50.,global_mean_delay_s=10.,throughput=1.,teleport_count=0)
    parts=_components()
    def candidate(pid,losses,delay,proposal):
        path=tmp_path/f'{pid}.xml'
        path.write_text('<meandata><interval>'+''.join(f'<edge id="{e}" timeLoss="{v}"/>' for e,v in losses.items())+'</interval></meandata>')
        vals=dict(base_values,top10_total_time_loss_s=losses['t'],neighbor_total_time_loss_s=losses['n'],global_mean_delay_s=delay)
        return dict(status='ok',proposal=proposal,optimized=dict(artifacts={'edgedata':str(path)},
            kpi={'focus_scope':{'target':{'edge_ids':['t']},'neighbors':{'edge_ids':['n']}}}),
            scope_objective=dict(baseline=base_values,current=vals,constraint_checks={'global_delay':delay<=10.1}))
    baseline=candidate('base',dict(t=100,n=50,r=20),10,dict(proposal_id='baseline',action='noop'))
    a=candidate('a',dict(t=80,n=45,r=18),9,parts[0])
    b=candidate('b',dict(t=90,n=47,r=19),9.5,parts[1])
    joint=candidate('joint',dict(t=85,n=55,r=60),11,dict(proposal_id='joint',action='bundle',proposals=parts))
    return baseline,a,b,joint


def test_raw_interaction_and_conditional_marginals_and_new_remote_harm(tmp_path):
    base,a,b,joint=example(tmp_path)
    report=build_interaction_feedback(joint,baseline=base,evaluated_candidates=[a,b,joint])
    metric=report['metrics']['top10_total_time_loss_s']
    assert metric['interaction_residual']==15 # 85 - 80 - 90 + 100
    assert metric['adding_component_given_other']==[-5,5]
    assert report['newly_failed_constraints']==['global_delay']
    remote=report['spatial']['worst_outside_neighborhood'][0]
    assert remote['edge_id']=='r' and remote['newly_worsened_in_joint']
    assert remote['interaction_residual_s']==43
    assert report['spatial']['newly_worsened_edge_count']==2
    json.dumps(report,allow_nan=False)


def test_renamed_exact_action_is_found_but_changed_args_never_use_ancestor(tmp_path):
    base,a,b,joint=example(tmp_path)
    joint['proposal']['proposals']=deepcopy(joint['proposal']['proposals'])
    joint['proposal']['proposals'][0]['proposal_id']='renamed'
    report=build_interaction_feedback(joint,baseline=base,evaluated_candidates=[a,b])
    assert report['components'][0]['isolated_source_id']==a['proposal']['proposal_id']
    joint['proposal']['proposals'][0]['args']['speed_factor']=.7
    report=build_interaction_feedback(joint,baseline=base,evaluated_candidates=[a,b])
    assert not report['components'][0]['isolated_available']
    assert report['metrics']['top10_total_time_loss_s']['interaction_residual'] is None
    assert report['metrics']['top10_total_time_loss_s']['adding_component_given_other']==[-5,None]
    assert report['spatial']['newly_worsened_edge_count'] is None
    assert report['newly_failed_constraints'] is None


def test_unseen_measurements_are_not_looked_up_and_inputs_not_modified(tmp_path):
    base,a,b,joint=example(tmp_path);original=deepcopy(joint)
    report=build_interaction_feedback(joint,baseline=base,evaluated_candidates=[])
    assert all(not c['isolated_available'] for c in report['components'])
    assert joint==original


def test_measured_joint_parent_contrast_is_available_without_isolated_child(tmp_path):
    base,a,b,prior=example(tmp_path);child=deepcopy(prior)
    child['proposal']['proposal_id']='child';child['proposal']['parent_proposal_id']='joint'
    child['proposal']['proposals'][0]['args']['speed_factor']=.7
    child['scope_objective']['current']['global_mean_delay_s']=10
    report=build_interaction_feedback(child,baseline=base,evaluated_candidates=[a,b,prior,child])
    contrast=report['previous_conditional_change']
    assert contrast['available'] and contrast['changed_component_index']==0
    assert contrast['current_minus_prior']['global_mean_delay_s']==-1
    assert not report['components'][0]['isolated_available']


def test_corrupt_baseline_or_edge_measurement_is_rejected(tmp_path):
    base,a,b,joint=example(tmp_path)
    joint['scope_objective']['baseline']=dict(joint['scope_objective']['baseline'],global_mean_delay_s=12)
    with pytest.raises(AssertionError,match='baseline mismatch'):
        build_interaction_feedback(joint,baseline=base,evaluated_candidates=[a,b])
    joint['scope_objective']['baseline']=base['scope_objective']['baseline']
    joint['scope_objective']['current']['top10_total_time_loss_s']=999
    with pytest.raises(AssertionError,match='Edge output and KPI disagree'):
        build_interaction_feedback(joint,baseline=base,evaluated_candidates=[a,b])


def test_empty_edge_records_are_zero_but_missing_sampled_loss_is_not(tmp_path):
    from agents.interaction_feedback import _edges
    path=tmp_path/'edges.xml'
    candidate={'optimized':{'artifacts':{'edgedata':str(path)}}}
    path.write_text('<meandata><interval><edge id="empty" sampledSeconds="0.00"/></interval></meandata>')
    assert _edges(candidate)=={'empty':0.}
    path.write_text('<meandata><interval><edge id="active" sampledSeconds="5.00"/></interval></meandata>')
    with pytest.raises(AssertionError,match='timeLoss unavailable'):_edges(candidate)
