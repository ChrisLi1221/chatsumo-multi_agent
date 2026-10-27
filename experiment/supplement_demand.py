#!/usr/bin/env python3
"""Paired synthetic-demand intensity sensitivity under the frozen wide search."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import copy
import fcntl
import os
from pathlib import Path
import random
import sys
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import wide_feedback_experiment as wide
full=wide.full;m=full.m;opt=m.opt
OUT=ROOT/'experiment/supplement_demand_v1'

def scaled_demand(source,target,count,seed):
    """One seeded draw per adjacent departure pair; duplicate those ODs at +0.75 s."""
    root=ET.parse(source).getroot();trips=sorted(root.findall('trip'),key=lambda t:float(t.get('depart')))
    assert len(trips)==2400 and count in (1200,3600)
    assert len({t.get('id') for t in trips})==2400
    assert all(abs(float(t.get('depart'))-1.5*i)<1e-6 for i,t in enumerate(trips))
    rng=random.Random(seed);indices=[i+rng.randrange(2) for i in range(0,2400,2)]
    chosen=[trips[i] for i in indices]
    output=ET.Element(root.tag,root.attrib)
    for node in root:
        if node.tag!='trip':output.append(copy.deepcopy(node))
    result=copy.deepcopy(chosen if count==1200 else trips)
    if count==3600:
        for trip in chosen:
            clone=copy.deepcopy(trip);clone.set('id',trip.get('id')+'__intensity_extra')
            clone.set('depart',f"{float(trip.get('depart'))+.75:.2f}");result.append(clone)
    result.sort(key=lambda t:(float(t.get('depart')),t.get('id')))
    assert len(result)==count and len({t.get('id') for t in result})==count
    assert all(0<=float(t.get('depart'))<3600 for t in result)
    output.extend(result);target.parent.mkdir(parents=True,exist_ok=True)
    ET.ElementTree(output).write(target,encoding='utf-8',xml_declaration=True)
    return dict(trip_count=count,source_trips=2400,departure_window_s=[0,3600],seed=seed,
        selected_source_ids=[t.get('id') for t in chosen],source_sha256=m.file_hash(source),
        method='Seeded one-of-two stratified thinning for 1200; original 2400 plus the same 1200 OD clones departing 0.75 seconds later for 3600; all vehicle attributes retained.')

def prepare(city,seed,count,args):
    case=f'{city}_equal_scale_demand{count}_delay_seed_{seed}'
    dest=args.output_dir/'frozen'/f'{case}.json'
    if dest.exists():
        spec=m.read(dest);assert m.digest({k:v for k,v in spec.items() if k!='frozen_sha256'})==spec['frozen_sha256']
        for f in spec['input_files'].values():assert m.file_hash(f['path'])==f['sha256']
        for c in [spec['baseline_candidate']]+spec['initial']:full.audit_execution(c,seed,spec['sumo_version'])
        return spec
    original=f'{city}_equal_scale_delay_seed_{seed}';old=m.read(wide.OUT/'frozen'/f'{original}.json')
    for f in old['input_files'].values():assert m.file_hash(f['path'])==f['sha256']
    folder=args.output_dir/'initialization'/case;folder.mkdir(parents=True,exist_ok=True)
    baseline_file=folder/'baseline.json';bridge=opt.LocalToolBridge()
    if baseline_file.exists():
        saved=m.read(baseline_file);base=saved['baseline'];diagnosis=saved['diagnosis'];demand=saved['demand']
    else:
        routes=folder/'demand.rou.xml';demand=scaled_demand(old['baseline']['artifacts']['routes'],routes,count,seed)
        m.write(folder/'demand_generation.json',demand)
        artifacts=opt.prepare_baseline_sandbox(old['baseline']['artifacts']['cfg'],folder/'baseline')
        cfg=ET.parse(artifacts['cfg']);opt._set_cfg_value(cfg.getroot(),'route-files',str(routes),'input')
        cfg.write(artifacts['cfg'],encoding='utf-8',xml_declaration=True);artifacts['routes']=str(routes)
        run=opt._tool(bridge,'runner','run',{'cfg_path':artifacts['cfg'],'gui':False})
        kpi=opt.collect_kpi(bridge,artifacts,10)
        params=m.read(m.historical(original,'llm')/'00_metadata.json')['parameters']
        diagnosis=opt.add_bottleneck_subgraph(bridge,kpi.pop('diagnosis'),artifacts['net'],
            params['subgraph_seed_n'],params['subgraph_hops'],params['subgraph_max_edges'])
        scope=opt.freeze_top10_scope(bridge,diagnosis,artifacts['net']);diagnosis['selection_objective_scope']=scope
        kpi['focus_scope']=opt._collect_scope_metrics(bridge,artifacts,scope)
        assert int(kpi['departed_trips'])==count and int(kpi['completed_trips'])==count,'Baseline must retain and complete all demand'
        base=dict(artifacts=artifacts,run=run,kpi=kpi)
        m.write(baseline_file,dict(baseline=base,diagnosis=diagnosis,demand=demand))
    weights=old['source_metadata']['weights'];baseline=opt._baseline_candidate(base['artifacts'],base['run'],base['kpi'],diagnosis,weights,'top10')
    assert m.candidate_seed(baseline)==seed
    version=m.version_from_tripinfo(base['artifacts']['tripinfo']);assert version==old['sumo_version']
    files={k:Path(base['artifacts'][k]) for k in ['cfg','net','routes']}
    for i,f in enumerate(opt._resolve_list(Path(base['artifacts']['cfg']).parent,ET.parse(base['artifacts']['cfg']).find('./input/additional-files').get('value'))):files[f'additional_{i}']=f
    inputs={k:dict(path=str(f),sha256=m.file_hash(f)) for k,f in files.items()}
    spec=dict(version='full_wide_v1',case=case,sumo_seed=seed,sumo_version=version,input_files=inputs,
        baseline=base,baseline_candidate=baseline,diagnosis=diagnosis,
        source_metadata=copy.deepcopy(old['source_metadata']),demand_generation=demand,
        cache_identity=dict(case=case,sumo_seed=seed,sumo_version=version,frozen_sha256=m.digest(inputs)))
    spec['source_metadata']['case'].update(case_id=case,cfg=base['artifacts']['cfg'])
    plan_file=folder/'initial_plan.json'
    if plan_file.exists():plan=m.read(plan_file);assert plan['input_sha256']==m.digest(inputs)
    else:
        proposals=wide.generate_grounded_random(diagnosis,base['artifacts'],attempt_budget=5,seed=seed)
        assert len(proposals)==5 and len({full.plan_signature(p) for p in proposals})==5
        for i,p in enumerate(proposals,1):p.update(proposal_id=f"demand_initial_{i:03d}_{p['action']}",proposal_source='shared_grounded_random',search_round=1)
        plan=dict(input_sha256=m.digest(inputs),proposals=proposals);m.write(plan_file,plan)
    state=full.AgentState.create(goal=spec['source_metadata']['case']['goal'],workspace=str(folder));state.artifacts.set(**base['artifacts'])
    journal=full.Journal(spec,'catalog_first',0,args,state,folder)
    initial=[journal.evaluate(p) for p in plan['proposals']]
    history,decisions=full.rebuild_decisions(initial,base,diagnosis)
    spec.update(initial=initial,history=history,decisions=decisions,
        initialization_new_sumo=sum(e['new_sumo'] for e in journal.events),initialization_reused=sum(e['reused'] for e in journal.events))
    spec['frozen_sha256']=m.digest(spec);m.write(dest,spec)
    print('FROZEN DEMAND',case,'baseline delay',base['kpi']['mean_delay'],'parents',len(decisions),flush=True)
    return spec

def job(item):
    city,seed,count,args=item;m.OUT=args.output_dir
    lock=args.output_dir/'locks'/f'{city}_{count}_{seed}.lock';lock.parent.mkdir(parents=True,exist_ok=True)
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX);spec=prepare(city,seed,count,args)
        if args.prepare_only:return []
        return [full.execute(spec,arm,0,args) for arm in m.ARMS]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workers',type=int,default=3)
    p.add_argument('--prepare-only',action='store_true');args=p.parse_args()
    os.chdir(ROOT);os.environ['PATH']='/usr/bin:'+os.environ['PATH'];args.output_dir=OUT
    args.model='gpt-5.6-luna';args.max_completion_tokens=4096;args.children_per_parent=2
    args.joint_feedback_rounds=2;args.sumo_budget=15;args.joint_feedback_mode='aggregate';args.reuse_cache_roots=[]
    protocol=dict(version='wide_demand_intensity_v1',cities=['albany','manhattan'],seeds=[42,43,44],levels=[1200,2400,3600],
        arms=list(m.ARMS),search_repeats=1,budget=15,initial=5,children_per_parent=2,joint_rounds=2,bundle_top_k=4,
        reused_2400_root=str(wide.OUT.relative_to(ROOT)),demand_method='Seeded adjacent-pair stratified thinning/cloning of archived randomTrips OD; fixed [0,3600) departure window',
        interpretation='Synthetic intensity sensitivity, not observed-demand calibration; Top-10 re-frozen separately per demand scenario.')
    if (OUT/'protocol.json').exists():assert m.read(OUT/'protocol.json')==protocol
    m.write(OUT/'protocol.json',protocol)
    jobs=[(city,seed,count,args) for count in [1200,3600] for city in protocol['cities'] for seed in protocol['seeds']]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:groups=list(pool.map(job,jobs))
    if args.prepare_only:return
    results=[r for g in groups for r in g];index=[]
    for city in protocol['cities']:
        for seed in protocol['seeds']:
            for count in protocol['levels']:
                case=f'{city}_equal_scale_delay_seed_{seed}' if count==2400 else f'{city}_equal_scale_demand{count}_delay_seed_{seed}'
                root=wide.OUT if count==2400 else OUT
                for arm in m.ARMS:
                    path=root/'runs'/case/f'{arm}_rep00/result.json';assert path.exists()
                    index.append(dict(city=city,seed=seed,trips=count,case=case,arm=arm,repeat=0,path=str(path.relative_to(ROOT))))
    m.write(OUT/'results_index.json',index)
    shared=sum(m.read(OUT/'frozen'/f"{r['case']}.json")['initialization_new_sumo'] for r in results if r['arm']=='llm')
    m.write(OUT/'execution_accounting.json',dict(new_search_runs=len(results),new_baselines=12,shared_initialization_new_sumo=shared,
        new_sumo_excluding_baselines=shared+sum(r['new_sumo_evaluations'] for r in results),
        logical=sum(r['logical_sumo_evaluations'] for r in results),
        reused=sum(r['reused_evaluations'] for r in results)-shared,
        api_requests=sum(r['api_requests'] for r in results),transport_failures=sum(r['transport_failures'] for r in results)))
    m.write(OUT/'source_hashes.json',[dict(path=str(f.relative_to(ROOT)),sha256=m.file_hash(f))
        for folder in ['agents','experiment','providers','providers/Modification'] for f in sorted((ROOT/folder).glob('*.py'))])

if __name__=='__main__':main()
