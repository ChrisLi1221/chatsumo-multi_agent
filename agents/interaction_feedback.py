"""Measured component contrasts; never infer an unmeasured isolated action.

Only the caller's already evaluated search pool is visible here. This module
does not query the simulation cache, call an LLM, or run SUMO.
"""
from functools import lru_cache
import math
from pathlib import Path
import xml.etree.ElementTree as ET

from agents.joint_optimizer import plan_signature


METRICS = {
    'top10_total_time_loss_s': ('vehicle_seconds', True),
    'neighbor_total_time_loss_s': ('vehicle_seconds', True),
    'global_mean_delay_s': ('seconds_per_vehicle', True),
    'throughput': ('fraction', False),
    'teleport_count': ('vehicles', True),
}


def _values(candidate):
    scope = candidate.get('scope_objective') or {}
    return {key: scope.get('current', {}).get(key) for key in METRICS}


def _finite(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _contrast(baseline, joint, singles):
    result = {}
    for key, (unit, lower) in METRICS.items():
        b, j = baseline.get(key), joint.get(key)
        alone = [s.get(key) if s else None for s in singles]
        row = dict(unit=unit, lower_is_better=lower, baseline=b, joint=j, isolated=alone)
        row['joint_minus_baseline'] = j-b if all(map(_finite, [b, j])) else None
        # The marginal change of component i requires the OTHER component alone.
        row['adding_component_given_other'] = [j-alone[1-i] if all(map(_finite, [j, alone[1-i]])) else None for i in range(2)]
        row['interaction_residual'] = j-alone[0]-alone[1]+b if all(map(_finite, [b, j, *alone])) else None
        result[key] = row
    return result


@lru_cache(maxsize=128)
def _read_edges(path, size, mtime_ns):
    losses = {}
    for _, node in ET.iterparse(path, events=['end']):
        if node.tag == 'edge':
            if 'timeLoss' not in node.attrib and float(node.get('sampledSeconds','nan'))!=0:
                raise AssertionError(f'Edge timeLoss unavailable: {path}')
            edge = node.get('id')
            losses[edge] = losses.get(edge, 0.) + float(node.get('timeLoss','0'))
        node.clear()
    return losses


def _edges(candidate):
    value = candidate.get('optimized', {}).get('artifacts', {}).get('edgedata')
    if not value:
        return None
    path = Path(value).resolve();stat = path.stat()
    return _read_edges(str(path), stat.st_size, stat.st_mtime_ns)


@lru_cache(maxsize=16)
def _geometry(path, size, mtime_ns):
    result = {}
    root = ET.parse(path).getroot()
    for edge in root.findall('edge'):
        if edge.get('function') == 'internal':
            continue
        lane = edge.find('lane')
        shape = lane.get('shape', '') if lane is not None else ''
        points = [tuple(map(float, p.split(','))) for p in shape.split()]
        # Explicit accumulation preserves the archived Python 3.10 prompt bytes;
        # Python 3.12+ changed sum(float) to compensated summation.
        totals = [0., 0.]
        for point in points:
            for i in (0, 1):totals[i] += point[i]
        result[edge.get('id')] = dict(name=edge.get('name', ''),
            midpoint_xy_m=[value/len(points) for value in totals] if points else None)
    return result


def _spatial(base, parent, singles, limit):
    b, j = _edges(base), _edges(parent)
    isolated = [_edges(c) if c else None for c in singles]
    if b is None or j is None:
        return dict(available=False, reason='Baseline or joint edge time-loss output unavailable')
    focus = base['optimized']['kpi']['focus_scope']
    target = set(focus['target']['edge_ids']);neighbors = set(focus['neighbors']['edge_ids'])
    # Refuse incompatible edge-output semantics instead of reporting false contrasts.
    for candidate, edges in [(base,b),(parent,j), *zip(singles,isolated)]:
        if edges is not None:
            for ids, key in [(target,'top10_total_time_loss_s'),(neighbors,'neighbor_total_time_loss_s')]:
                expected = _values(candidate)[key]
                if _finite(expected):
                    assert math.isclose(sum(edges.get(e,0.) for e in ids),expected,abs_tol=1e-5), 'Edge output and KPI disagree'
    net = base['optimized']['artifacts'].get('net');geo = {}
    if net:
        path = Path(net).resolve();stat = path.stat();geo = _geometry(str(path),stat.st_size,stat.st_mtime_ns)
    union = set(b)|set(j)
    for values in isolated:
        union.update(values or {})
    rows = []
    for edge in sorted(union):
        base_loss, joint_loss = b.get(edge,0.), j.get(edge,0.)
        alone = [s.get(edge,0.) if s is not None else None for s in isolated]
        complete = all(s is not None for s in isolated)
        rows.append(dict(edge_id=edge, region='top10' if edge in target else 'neighbor' if edge in neighbors else 'outside_monitored_neighborhood',
            **geo.get(edge,{}), baseline_time_loss_s=base_loss, joint_time_loss_s=joint_loss, isolated_time_loss_s=alone,
            joint_minus_baseline_s=joint_loss-base_loss,
            adding_component_given_other_s=[joint_loss-alone[1-i] if alone[1-i] is not None else None for i in range(2)],
            interaction_residual_s=joint_loss-alone[0]-alone[1]+base_loss if complete else None,
            newly_worsened_in_joint=(joint_loss-base_loss>1e-6 and all(v-base_loss<=1e-6 for v in alone)) if complete else None))
    worsened = sorted([r for r in rows if r['joint_minus_baseline_s']>1e-6],key=lambda r:(-r['joint_minus_baseline_s'],r['edge_id']))
    antagonistic = sorted([r for r in rows if r['interaction_residual_s'] is not None and r['interaction_residual_s']>1e-6],key=lambda r:(-r['interaction_residual_s'],r['edge_id']))
    new = [r for r in worsened if r['newly_worsened_in_joint']]
    return dict(available=True,unit='vehicle_seconds',edge_count=len(rows),
        missing_edge_record_policy='Zero accumulated loss for absent edge records; this is not zero travel cost or proof that removing an edge is beneficial.',
        complete_isolated_edge_data=all(s is not None for s in isolated),
        worsened_edge_count=len(worsened),newly_worsened_edge_count=len(new) if all(s is not None for s in isolated) else None,
        worst_joint_worsening=worsened[:limit],worst_outside_neighborhood=[r for r in worsened if r['region']=='outside_monitored_neighborhood'][:limit],
        newly_worsened_edges=new[:limit],largest_positive_interaction_residual=antagonistic[:limit],display_limit_per_list=limit)


def build_interaction_feedback(parent, *, baseline, evaluated_candidates, edge_limit=5):
    """Contrast baseline, exact A, exact B and ordered A+B, with missingness.

Inputs must belong to the same audited case/seed. Exact semantic matching can
find a renamed component, but never substitutes an ancestor with other args.
    """
    parts = parent['proposal'].get('proposals') or []
    if parent['proposal']['action'] != 'bundle' or len(parts) != 2:
        raise ValueError('Expected a measured two-component bundle')
    if parent.get('status') != 'ok':
        raise ValueError('Joint outcome was not measured successfully')
    singles = []
    for part in parts:
        singles.append(next((c for c in evaluated_candidates if c.get('status')=='ok'
            and c['proposal']['action']!='bundle' and plan_signature(c['proposal'])==plan_signature(part)),None))
    base_values = _values(baseline)
    for candidate in [parent]+[c for c in singles if c]:
        assert candidate['scope_objective']['baseline']==baseline['scope_objective']['baseline'], 'Interaction baseline mismatch'
    metric = _contrast(base_values,_values(parent),[_values(c) if c else None for c in singles])
    checks = parent['scope_objective'].get('constraint_checks',{})
    newly_failed = [key for key, passed in checks.items() if not passed and all(
        c is not None and c['scope_objective'].get('constraint_checks',{}).get(key) is True for c in singles)]
    prior_id = parent['proposal'].get('parent_proposal_id')
    prior = next((c for c in evaluated_candidates if c['proposal']['proposal_id']==prior_id and c.get('status')=='ok'),None)
    conditional = dict(available=False,reason='No measured direct joint parent in visible history')
    if prior and prior['proposal']['action']=='bundle':
        old = prior['proposal'].get('proposals',[])
        changed = [i for i in range(2) if len(old)==2 and plan_signature(old[i])!=plan_signature(parts[i])]
        if len(changed)==1:
            values = _values(prior)
            conditional = dict(available=True,prior_plan_id=prior_id,changed_component_index=changed[0],
                fixed_component_index=1-changed[0],current_minus_prior={k:_values(parent)[k]-values[k]
                    if all(map(_finite,[_values(parent)[k],values[k]])) else None for k in METRICS},
                interpretation='Measured change after one component modification with the partner fixed; not an isolated effect.')
    return dict(version='component_interactions_v1',parent_plan_id=parent['proposal']['proposal_id'],
        evidence_policy='Only successful isolated measurements already visible in this search; no hidden cache or extra simulations.',
        interpretation='Residual = Y(A+B)-Y(A)-Y(B)+Y(baseline), in raw units. For loss/delay, positive means worse than the additive reference. This is a descriptive interaction contrast for this fixed demand, seed and action order, not causal blame, a prediction, or a guarantee of additivity.',
        components=[dict(index=i,proposal_id=p['proposal_id'],action=p['action'],semantic_signature=plan_signature(p),
            isolated_available=c is not None,isolated_source_id=c['proposal']['proposal_id'] if c else None,
            missing_reason=None if c else 'Exact current component has not been successfully evaluated alone in visible history') for i,(p,c) in enumerate(zip(parts,singles))],
        metrics=metric,newly_failed_constraints=newly_failed if all(c is not None for c in singles) else None,
        spatial=_spatial(baseline,parent,singles,edge_limit),previous_conditional_change=conditional)
