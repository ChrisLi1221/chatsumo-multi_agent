"""Protect the frozen-selector intervention and offline acceptance boundary."""
import copy
from agents import grounded_baselines as grounded
from agents import debate_optimizer as debate


def test_role_specific_pool_does_not_validate_unrequested_families(monkeypatch):
    grounded._LEGAL_POOL_CACHE.clear()
    monkeypatch.setattr(grounded, '_raw_role_candidate_specs', lambda d: {
        'signal_optimizer': [{'action':'tls_optimize_and_apply','args':{'tl_id':'T'}}],
        'network_optimizer': [{'action':'remove_edge','args':{'edge_id':'E'}}],
        'demand_routing_optimizer': []})
    seen=[]
    def validate(action,args,artifacts):
        seen.append(action);return {'ok':True}
    monkeypatch.setattr(grounded,'validate_modifier_capability',validate)
    assert list(grounded.build_legal_candidate_pool({}, {},optimizer='signal'))==['signal_optimizer']
    assert seen==['tls_optimize_and_apply']
    all_pool=grounded.build_legal_candidate_pool({}, {},optimizer='all')
    assert all_pool['network_optimizer'][0]['action']=='remove_edge'
    grounded._LEGAL_POOL_CACHE.clear()


def test_catalog_audit_explains_rejections_without_changing_choices(monkeypatch):
    monkeypatch.setattr(debate,'validate_modifier_capability',lambda a,b,c:{'ok':b.get('speed_factor')!=.7})
    parent={'action':'set_corridor_speed','args':{'edge_id':'E','edge_count':5,'speed_factor':.9}}
    history=[{'action':'set_corridor_speed','args':{'edge_id':'X','edge_count':5,'speed_factor':.9}}]
    diagnosis={'top_congested_edges':[{'edge_id':'E','length_m':20},{'edge_id':'X','length_m':20}]}
    before=copy.deepcopy(parent);audit=[]
    cat=debate.build_finite_mutation_catalog(parent,diagnosis=diagnosis,evaluated_history=history,audit_log=audit)
    plain=debate.build_finite_mutation_catalog(parent,diagnosis=diagnosis,evaluated_history=history)
    assert cat==plain and parent==before
    assert [c['args']['speed_factor'] for c in cat]==[.8]
    assert {a['disposition'] for a in audit}=={'invalid','accepted','not_exactly_one_changed_field','duplicate_or_evaluated'}
    assert next(a for a in audit if a['disposition']=='accepted')['mutation_id']=='mutation_001'
