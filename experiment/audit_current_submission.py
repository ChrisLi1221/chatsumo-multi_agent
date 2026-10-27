#!/usr/bin/env python3
"""Read-only numerical and anonymity checks; no API calls or SUMO execution.

Run the existing cohort analyzers and build_current_manuscript.py first to
reconstruct inputs, executed seeds, catalogs, spatial XML and threshold grids.
This additional audit independently checks saved KPI arithmetic, report claims,
paired inference and the files actually delivered for review.
"""
from pathlib import Path
from collections import Counter
from zipfile import ZipFile
import csv
import hashlib
import itertools
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
import numpy as np
from scipy.stats import wilcoxon

ROOT = Path(__file__).resolve().parents[1]
PAPER = ROOT / 'paper/multra_revision'
OUT = ROOT / 'experiment/submission_consistency_audit'
ARMS = ['llm', 'catalog_first', 'catalog_random']
checks = []

def read(p): return json.loads(Path(p).read_text())
def rows(p): return list(csv.DictReader(Path(p).open()))
def check(name, condition, evidence=''):
    checks.append(dict(check=name, passed=bool(condition), evidence=evidence))
def near(a, b): return bool(np.isclose(float(a), float(b), rtol=1e-9, atol=1e-9))
def rounded(name, values, expected, digits=2):
    actual = [round(float(v), digits) for v in values]
    check(name, actual == expected, dict(actual=actual, expected=expected))

def arithmetic(d):
    count = 0
    for c in [d['baseline']] + d['candidates']:
        s = c.get('scope_objective') or {}
        if not s.get('available'):
            continue
        b, k = s['baseline'], s['current']
        assert near(s['top10_relative_improvement'], 1-k['top10_total_time_loss_s']/b['top10_total_time_loss_s'])
        assert near(s['global_delay_relative_change'], k['global_mean_delay_s']/b['global_mean_delay_s']-1)
        if b['neighbor_total_time_loss_s']:
            assert near(s['neighbor_delay_relative_change'], k['neighbor_total_time_loss_s']/b['neighbor_total_time_loss_s']-1)
        if not c.get('is_baseline'):
            assert s['constraints_passed'] == all(s['constraint_checks'].values())
        actual = c['optimized']['kpi']
        assert near(actual['mean_delay'], k['global_mean_delay_s'])
        assert near(actual['focus_scope']['target']['total_time_loss_s'], k['top10_total_time_loss_s'])
        count += 1
    assert d['logical_sumo_evaluations'] == sum(bool(c.get('sumo_evaluated')) for c in d['candidates'])
    return count

def paired_check(data, saved):
    llm = {r['case']: r for r in data if r['arm']=='llm'}
    ps = {}
    for arm in ARMS[1:]:
        other = {r['case']: r for r in data if r['arm']==arm}
        keys = sorted(llm)
        delta = np.array([float(llm[k]['top10'])-float(other[k]['top10']) for k in keys])
        delta[np.abs(delta)<1e-9] = 0
        cities = sorted({llm[k]['city'] for k in keys})
        cd = np.array([np.mean([v for k,v in zip(keys,delta) if llm[k]['city']==c]) for c in cities])
        sign_p = lambda x: sum(abs(np.dot(s,x)/len(x))>=abs(x.mean())-1e-12 for s in itertools.product([-1,1],repeat=len(x)))/2**len(x)
        p = saved[arm]
        assert near(delta.mean(), p['mean_difference_pp'])
        assert near(sign_p(delta), p['exact_case_signflip_p'])
        assert near(sign_p(cd), p['exact_city_signflip_p'])
        ps[arm] = float(wilcoxon(delta,zero_method='wilcox',method='approx',correction=False).pvalue)
        assert near(ps[arm], p['wilcoxon_p'])
        rng = np.random.default_rng(20260927)
        ci = np.quantile(np.mean(rng.choice(cd,size=(20000,len(cd)),replace=True),axis=1),[.025,.975])
        assert np.allclose(ci,p['city_cluster_bootstrap_ci95_pp'],rtol=0,atol=1e-9)
    previous = 0
    for i,arm in enumerate(sorted(ps,key=ps.get)):
        previous = max(previous,min(1,(2-i)*ps[arm]))
        assert near(previous,saved[arm]['holm_wilcoxon_p'])

def main():
    OUT.mkdir(exist_ok=True)
    wide = ROOT/'experiment/wide_feedback_v1'
    strict = ROOT/'experiment/paper_current_analysis'
    all_paths = set(wide.glob('runs/*/*/result.json'))
    all_paths.update((ROOT/'experiment/full_feedback_v1').glob('runs/*/*/result.json'))
    for cohort in ['stability','demand']:
        for item in read(ROOT/f'experiment/supplement_{cohort}_v1/results_index.json'):
            all_paths.add(ROOT/item['path'])
    count = sum(arithmetic(read(p)) for p in sorted(all_paths))
    check('Raw recorded KPI arithmetic and logical evaluation counts', True, dict(unique_runs=len(all_paths),candidate_records=count))
    for name,folder,expected in [('Frozen prefix',strict,[8.50,9.36,9.55]),('Adaptive',wide/'analysis',[11.53,12.10,14.81])]:
        prefix = 'strict_prefix_' if name=='Frozen prefix' else ''
        summary = read(folder/f'{prefix}summary.json')
        data = rows(folder/('strict_prefix_results.csv' if prefix else 'case_results.csv'))
        rounded(name+' manuscript means',[summary[a]['top10_mean'] for a in ARMS],expected)
        for arm in ARMS:
            rr = [r for r in data if r['arm']==arm]
            assert len(rr)==15
            vals = [float(r['top10']) for r in rr]
            assert near(np.mean(vals),summary[arm]['top10_mean'])
            assert near(np.std(vals,ddof=1),summary[arm]['top10_sd'])
            assert sum(r['success']=='True' for r in rr)==summary[arm]['successes']
            for r in rr:
                source = ROOT/'experiment'/('full_feedback_v1' if prefix else 'wide_feedback_v1')/'runs'/r['case']/f'{arm}_rep00/result.json'
                selected = read(source)['before_selected' if prefix else 'selected']
                assert near(r['top10'],100*selected['scope_objective']['top10_relative_improvement'])
        paired_check(data,read(folder/('strict_prefix_paired.json' if prefix else 'paired_statistics.json')))
        check(name+' raw endpoints, SD, success and independent paired statistics',True)
    w = read(wide/'analysis/summary.json')
    rounded('Adaptive pre-joint means',[w[a]['before_joint_mean'] for a in ARMS],[10.88,11.81,11.96])
    rounded('Adaptive stage increments',[w[a]['top10_mean']-w[a]['before_joint_mean'] for a in ARMS],[.65,.29,2.84])
    account = read(wide/'execution_accounting.json')
    check('Adaptive execution accounting', all(account[k]==v for k,v in dict(logical=612,new_sumo=225,reused=387,api_requests=108,transport_failures=10,shared_initialization_new_sumo=22).items()))
    candidates = rows(wide/'analysis/candidate_results.csv')
    accepted = [sum(r['arm']==a and r['accepted']=='True' for r in candidates) for a in ARMS]
    check('Figure 3 accepted evaluation counts',accepted==[78,72,82],accepted)
    stages = Counter(r['stage'] for r in candidates if r['arm']=='llm' and r['selected']=='True')
    check('Figure 4 selected LLM origins',stages==dict(initial=3,mutation=5,bundle=2,joint=3),dict(stages))
    ba = rows(wide/'analysis/bundle_pool_audit.csv')
    blocked = [r for r in ba if r['actual_top4_bundles']=='0']
    check('Nine top-four compatibility exclusions',len(blocked)==9 and all(int(r['legal_bundles_if_all_singles_considered'])>0 for r in blocked))
    sweeps = rows(wide/'analysis/threshold_sensitivity.csv')
    check('100 threshold settings; random leads mean',len(sweeps)==300 and all(r['rank_top10']=='1' for r in sweeps if r['arm']=='catalog_random'))
    spatial = rows(wide/'analysis/spatial_selected.csv')
    check('Remote deterioration counts',[sum(r['arm']==a and int(r['remote_edges_increase_over_60s'])>0 for r in spatial) for a in ARMS]==[13,13,13])
    sr = [r for r in spatial if r['arm']=='llm']
    rounded('Spatial paragraph',[np.median([float(r['max_remote_increase_s']) for r in sr]),max(float(r['max_remote_increase_s']) for r in sr),max(float(r['max_remote_500m_cell_net_increase_s']) for r in sr)],[1350.3,5478.7,9081.1],1)
    stable = ROOT/'experiment/supplement_stability_v1'
    st = read(stable/'analysis/summary.json')
    rounded('Stability subset means',[st[a]['top10_mean'] for a in ARMS],[14.60,16.61,15.43])
    rounded('Within-case sampling SD',[st[a]['mean_within_case_sd'] for a in ['llm','catalog_random']],[3.09,2.55])
    check('Variable identical-input catalogs',[st[a]['variable_fixed_catalog_parents'] for a in ['llm','catalog_random']]==[7,10])
    sa = read(stable/'execution_accounting.json')
    check('Additional stability accounting',all(sa[k]==v for k,v in dict(new_search_runs=40,logical=568,new_sumo=119,reused=449,api_requests=160).items()))
    demand = ROOT/'experiment/supplement_demand_v1'
    da = read(demand/'execution_accounting.json')
    check('Additional demand accounting',all(da[k]==v for k,v in dict(new_search_runs=36,new_baselines=12,logical=489,new_sumo_excluding_baselines=305,reused=184,api_requests=88,transport_failures=8).items()))
    records = [read(ROOT/i['path']) for i in read(demand/'results_index.json') if i['arm']=='llm']
    manhattan = sorted([d for d in records if 'manhattan' in d['case'] and d['baseline']['optimized']['kpi']['completed_trips']==3600],key=lambda d:d['sumo_seed'])
    check('High-demand Manhattan teleports',[d['baseline']['optimized']['kpi']['teleport_count'] for d in manhattan]==[84,93,100] and [d['selected']['optimized']['kpi']['teleport_count'] for d in manhattan]==[60,93,84])
    manuscript = (PAPER/'main.tex').read_text()
    abstract = manuscript.split(r'\begin{abstract}')[1].split(r'\end{abstract}')[0]
    check('Abstract under 250 words',len(abstract.split())<=250,len(abstract.split()))
    highlights = [s.lstrip('- ').strip() for s in (PAPER/'highlights.txt').read_text().splitlines() if s.strip()]
    check('Highlights count and length',3<=len(highlights)<=5 and all(len(s)<=85 for s in highlights),[len(s) for s in highlights])
    check('Six figures and eight main tables',manuscript.count(r'\includegraphics')==6 and manuscript.count(r'\begin{table}')==8)
    # Scan the actual delivered packages, including decompressed DOCX and PDF metadata/text.
    identifiers = {'author names':r'Shuyang|Ruimin', 'affiliation/funding':r'Rensselaer|@rpi\.edu|IBM[–— -]+RPI|FCRC',
                   'personal paths':r'/home/|/Users/', 'self-reference metadata':r'3508471|105759|ChatSUMO',
                   'API credential':r'sk-(?:proj-)?[A-Za-z0-9_-]{25,}'}
    findings=[]
    text_cache={}
    def inspect(name,data):
        suffix=Path(name).suffix.lower()
        digest=hashlib.sha256(data).hexdigest()
        if digest in text_cache: text=text_cache[digest]
        elif suffix=='.pdf':
            text=subprocess.check_output(['pdftotext','-','-'],input=data).decode(errors='replace')
            text+='\n'+subprocess.check_output(['pdfinfo','-'],input=data).decode(errors='replace')
        elif suffix=='.docx':
            import io
            with ZipFile(io.BytesIO(data)) as z:
                text='\n'.join(z.read(n).decode(errors='replace') for n in z.namelist() if n.endswith('.xml') or n.endswith('.rels'))
                core=ET.fromstring(z.read('docProps/core.xml'))
                for e in core:
                    if e.tag.rsplit('}',1)[-1] in ['creator','lastModifiedBy'] and (e.text or '').strip():findings.append(dict(file=name,kind='nonempty author metadata'))
        elif suffix in ['.tex','.bib','.bbl','.json','.csv','.txt','.md','.csl']:
            text=data.decode(errors='replace')
        else:return
        text_cache[digest]=text
        for kind,pattern in identifiers.items():
            if re.search(pattern,text,re.I):findings.append(dict(file=name,kind=kind))
    manifest={}
    for name in ['simtio_anonymous_submission.zip','manuscript_source.zip','supplementary_evidence.zip']:
        with ZipFile(PAPER/name) as z:
            manifest[name]=z.namelist()
            check(name+' excludes identifying author files',not any('title_page' in n or 'cover_letter' in n or 'references_full' in n for n in z.namelist()))
            for n in z.namelist():inspect(name+'/'+n,z.read(n))
    check('No identity or credential matches in review packages',not findings,findings)
    check('Only current six PDF figures packaged',len([n for n in manifest['manuscript_source.zip'] if n.startswith('figures_current/') and n.endswith('.pdf')])==6)
    check('Anonymous self-references included',all(x in (PAPER/'main.bbl').read_text() for x in ['anonymous2024','anonymous2026','Details omitted']))
    bib_keys = re.findall(r'@\w+\s*\{\s*([^,]+),', (PAPER/'references.bib').read_text())
    cited_keys = {key.strip() for group in re.findall(r'\\citation\{([^}]+)\}', (PAPER/'main.aux').read_text()) for key in group.split(',')}
    check('28 references, each cited, with no missing or duplicate keys',
          len(bib_keys) == len(set(bib_keys)) == 28 and set(bib_keys) == cited_keys,
          dict(bibliography=len(bib_keys), cited=len(cited_keys), uncited=sorted(set(bib_keys)-cited_keys), missing=sorted(cited_keys-set(bib_keys))))
    log=(PAPER/'main.log').read_text()
    check('No unresolved citations, references or overfull boxes',not any(x in log for x in ['undefined references','undefined citations','Overfull','! LaTeX Error']))
    output=dict(checks=checks,passed=sum(c['passed'] for c in checks),failed=sum(not c['passed'] for c in checks),raw_run_count=len(all_paths),raw_candidate_records=count,package_manifest=manifest)
    (OUT/'audit.json').write_text(json.dumps(output,indent=2)+'\n')
    print(json.dumps({k:v for k,v in output.items() if k not in ['checks','package_manifest']}))
    for c in checks:
        if not c['passed']:print(json.dumps(c))
    return int(any(not c['passed'] for c in checks))

if __name__=='__main__':sys.exit(main())
