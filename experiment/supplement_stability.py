#!/usr/bin/env python3
"""Repeat the frozen wide protocol without overwriting its primary manifest."""
import argparse
from concurrent.futures import ProcessPoolExecutor
import fcntl
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import wide_feedback_experiment as wide
full=wide.full;m=full.m
OUT=ROOT/'experiment/supplement_stability_v1'
CITIES=['albany','ann_arbor','los_angeles','manhattan','troy']

def job(item):
    case,args=item;m.OUT=args.output_dir
    lock=args.output_dir/'locks'/f'{case}.lock'
    with lock.open('a') as handle:
        fcntl.flock(handle,fcntl.LOCK_EX)
        spec=wide.prepare(case,args)
        return [full.execute(spec,arm,repeat,args) for arm in m.ARMS
                for repeat in range(1 if arm=='catalog_first' else 5)]

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--workers',type=int,default=3)
    args=p.parse_args();os.chdir(ROOT);os.environ['PATH']='/usr/bin:'+os.environ['PATH']
    args.source=wide.SOURCE;args.output_dir=wide.OUT;args.model='gpt-5.6-luna';args.max_completion_tokens=4096
    args.children_per_parent=2;args.joint_feedback_rounds=2;args.sumo_budget=15;args.joint_feedback_mode='aggregate'
    args.reuse_cache_roots=[args.source,ROOT/'experiment/interaction_feedback_v1'];args.prepare_only=False
    cases=[f'{city}_equal_scale_delay_seed_42' for city in CITIES]
    protocol=dict(version='wide_stability_v1',cases=cases,arms=list(m.ARMS),repeats=5,
        primary_root=str(wide.OUT.relative_to(ROOT)),sampling='Default model sampling; independent requests, no fixed model seed',
        reused='Existing repeat 0 for every method, deterministic rule once',new_search_runs=40,
        interpretation='Five fixed-demand cities; repeats are within-case samples, not independent city replicates.')
    if (OUT/'protocol.json').exists():assert m.read(OUT/'protocol.json')==protocol
    m.write(OUT/'protocol.json',protocol)
    with ProcessPoolExecutor(max_workers=args.workers) as pool:groups=list(pool.map(job,[(c,args) for c in cases]))
    results=[r for g in groups for r in g]
    m.write(OUT/'results_index.json',[dict(case=r['case'],arm=r['arm'],repeat=r['repeat'],
        path=str((wide.OUT/'runs'/r['case']/f"{r['arm']}_rep{r['repeat']:02d}"/'result.json').relative_to(ROOT))) for r in results])
    new=[r for r in results if r['repeat']>0]
    m.write(OUT/'execution_accounting.json',dict(new_search_runs=len(new),new_sumo=sum(r['new_sumo_evaluations'] for r in new),
        logical=sum(r['logical_sumo_evaluations'] for r in new),reused=sum(r['reused_evaluations'] for r in new),
        api_requests=sum(r['api_requests'] for r in new),transport_failures=sum(r['transport_failures'] for r in new)))
    m.write(OUT/'source_hashes.json',[dict(path=str(f.relative_to(ROOT)),sha256=m.file_hash(f))
        for folder in ['agents','experiment','providers','providers/Modification'] for f in sorted((ROOT/folder).glob('*.py'))])

if __name__=='__main__':main()
