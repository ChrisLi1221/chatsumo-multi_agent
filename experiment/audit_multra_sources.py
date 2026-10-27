#!/usr/bin/env python3
"""Machine-readable checks behind the source/manuscript audit."""
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys
import xml.etree.ElementTree as ET
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment.analyze_multra_revision import read,write,csvwrite,load_all,mean,std
OUT=ROOT/'experiment/multra_revision';A=OUT/'analysis'

def main():
    runs=load_all();summary=read(A/'summary.json');claims=[]
    def claim(name,reported,observed,source,tolerance=.0051,status='verified'):
        if status=='verified':assert abs(float(reported)-float(observed))<=tolerance,(name,reported,observed)
        claims.append(dict(claim=name,reported=reported,recomputed=observed,status=status,source=source))
    for arm,top,sd,glob,succ,eff in [('llm',9.18,10.18,2.78,13,35.1),('grounded_random',9.,10.88,3.03,11,34.),('deterministic_heuristic',9.60,10.32,2.69,12,39.)]:
        s=summary['original_pipeline'][arm]
        for name,x,y,tol in [('Top10 mean',top,s['top10_mean'],.0051),('Top10 SD',sd,s['top10_sd'],.0051),('Global mean',glob,s['global_mean'],.0051),('Success count',succ,s['successes'],0),('Simulation efficiency',eff,100*s['feasible_improving']/s['evaluations'],.051)]:
            claim(arm+' '+name,x,y,'original_pipeline candidate logs',tol)
    pairs=read(A/'paired_statistics.json')
    claim('Original Wilcoxon random',.407,pairs['original_pipeline']['grounded_random']['original_zsplit_auto_p'],'zero_method=zsplit, method=auto',.00051)
    claim('Original Wilcoxon heuristic',.639,pairs['original_pipeline']['deterministic_heuristic']['original_zsplit_auto_p'],'zero_method=zsplit, method=auto',.00051)
    llm=[r for r in runs if r['cohort']=='historical_matched' and r['arm']=='llm']
    claim('LLM global reduction SD',5.08,std([r['global_reduction'] for r in llm]),'15 original case results')
    for arm,expected_valid in [('llm',100.),('grounded_random',100.),('deterministic_heuristic',97.3)]:
        rr=[r for r in runs if r['cohort']=='original_pipeline' and r['arm']==arm]
        valid=sum(r['counts']['validated_proposals'] for r in rr);raw=sum(r['counts']['raw_proposals'] for r in rr)
        claim(arm+' proposal validity',expected_valid,100*valid/raw,'emitted proposals only; excludes generation failures',.051)
    for arm,seed,x in [('llm',42,12.38),('llm',44,10.05),('deterministic_heuristic',43,9.03)]:
        rr=[r for r in runs if r['cohort']=='original_pipeline' and r['arm']==arm and r['case'].endswith('_'+str(seed))]
        claim(arm+' seed '+str(seed)+' mean',x,mean([r['top10'] for r in rr]),'five networks at one seed')
    expected={'albany':(6193,4309,107,112.80,.26,2.25,2.11,.17,.52,21.1),
              'troy':(6107,4355,57,99.79,.84,8.55,11.17,.75,1.27,28.6),
              'ann_arbor':(10151,7522,81,138.58,3.13,8.25,5.83,1.75,.46,30.0),
              'los_angeles':(6969,4954,172,175.76,1.26,5.58,6.84,.51,1.28,31.6),
              'manhattan':(5993,4065,256,245.31,13.03,21.27,14.23,10.71,7.55,66.7)}
    inventories=[]
    for city,e in expected.items():
        rr=[r for r in llm if r['city']==city];base=read(Path(rr[0]['run'])/'01_baseline.json');net=ET.parse(base['artifacts']['net']).getroot()
        observed=[sum(x.get('function')!='internal' for x in net.findall('edge')),len(net.findall('junction')),len(net.findall('tlLogic')),
                  mean([r['baseline']['mean_delay'] for r in rr]),std([r['baseline']['mean_delay'] for r in rr]),mean([r['top10'] for r in rr]),std([r['top10'] for r in rr]),mean([r['global_reduction'] for r in rr]),std([r['global_reduction'] for r in rr]),100*sum(r['counts']['feasible_improving_candidates'] for r in rr)/sum(r['evals'] for r in rr)]
        for name,x,y in zip(['edges','junctions','TLS','baseline mean','baseline SD','Top10 mean','Top10 SD','global mean','global SD','simulation efficiency'],e,observed):claim(city+' '+name,x,y,'network XML and three seed logs',.051 if name=='simulation efficiency' else .0051)
        inventories.append(dict(city=city,external_edges=observed[0],junctions_including_internal=observed[1],tls=observed[2]))
        for r in rr:
            b=read(Path(r['run'])/'01_baseline.json');demand=ET.parse(b['artifacts']['routes']).getroot()
            count=len(demand.findall('trip'))+len(demand.findall('vehicle'))
            claim(r['case']+' demand count',2400,count,b['artifacts']['routes'],0)
    stages=Counter()
    for r in llm:
        pid=r['selected'];stages['noop' if pid=='baseline_noop' else 'bundle' if pid.startswith('bundle') else 'refined' if '__refine_' in pid else 'initial']+=1
    for k,v in [('noop',2),('bundle',4),('refined',6),('initial',3)]:claim('selected LLM '+k,v,stages[k],'selected_proposal in 15 original logs',0)
    old=read(ROOT/'paper/generated/pareto_reselection_summary.json')
    claim('Old Pareto mean front size',1.53,old['mean_pareto_front_size'],'old generated audit; different feasible set',.0051)
    claim('Old Pareto membership count',11,old['current_selection_on_pareto_front'],'old generated audit; NOT four proofs of dominance',0)
    claims.append(dict(claim='Four primary plans dominated',reported=4,recomputed='2 excluded under global<=0; 2 baseline comparisons omit minimum gain',status='incorrect interpretation corrected',source='analyze_pareto_reselection.safety_feasible and corrected_pareto.json'))
    claims.append(dict(claim='Signal tool is Webster optimization',reported='Webster',recomputed='proportional_green_split_preserving_phase_states',status='incorrect method corrected',source='providers/Modification/tls_program.py:build_optimized_tllogic'))
    csvwrite(A/'numeric_claim_audit.csv',claims);write(A/'network_inventory.json',inventories)
    flat=[]
    for cohort,arms in pairs.items():
        for arm,p in arms.items():
            for row in p['paired_differences']:flat.append(dict(cohort=cohort,comparison='llm-minus-'+arm,**row))
    csvwrite(A/'paired_differences.csv',flat)
    files=[];settings=[]
    for fp in sorted((OUT/'frozen').glob('*.json')):
        f=read(fp);settings.append(dict(case=f['case'],sumo_seed=f['sumo_seed'],sumo_version=f['sumo_version'],catalogs=[x['catalog_sha256'] for x in f['decisions']]))
        for arm in ['llm','catalog_first','catalog_random']:
            from experiment.revision_matched import historical
            run=historical(f['case'],arm)
            for p in sorted(run.glob('**/06_comparison.json')):
                files.append(dict(path=str(p.relative_to(ROOT)),sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
    assert len(settings)==15
    ver=list((OUT/'verification').glob('*/verification.json'));assert len(ver)==15 and all(read(x)['status']=='exact_match' for x in ver)
    write(A/'cache_inventory.json',dict(settings=settings,comparison_files=files,baseline_replays='15/15 exact KPI match',
          unavailable=['observed traffic counts','calibrated OD matrix','historical full API response metadata','field validation','public release permission/license']))
    # Deterministic policy must remain identical after freezing and reconstruction.
    a={r['case']:r['top10'] for r in runs if r['cohort']=='historical_matched' and r['arm']=='catalog_first'}
    b={r['case']:r['top10'] for r in runs if r['cohort']=='fresh' and r['arm']=='catalog_first'}
    assert a==b
    print('Verified',sum(c['status']=='verified' for c in claims),'numerical claims; 15 baseline replays; 15 unchanged rule results.')

if __name__=='__main__':main()
