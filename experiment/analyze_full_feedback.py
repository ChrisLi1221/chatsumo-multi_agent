#!/usr/bin/env python3
"""Audit and summarize the full corrected-feedback cohort, without API calls."""
import argparse
from collections import Counter
import itertools
import json
from pathlib import Path
import sys
from functools import lru_cache
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import analyze_multra_revision as a
from experiment import full_feedback_experiment as full
from experiment.analyze_saved_selections import load
from agents.debate_optimizer import _mutation_payload

LABELS={'llm':'LLM','catalog_first':'Rule','catalog_random':'Random'}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=full.OUT)
    args=parser.parse_args();root=args.root.resolve();out=root/'analysis';out.mkdir(parents=True,exist_ok=True)
    account=a.read(root/'execution_accounting.json');runs=[];raw={};stages=[];candidates=[];audits=[];shared={};requests=[];model_metadata=[]
    for case in account['cases']:
        spec=a.read(root/'frozen'/f'{case}.json')
        for arm in account['arms']:
            for repeat in range(account['repeats'] if arm!='catalog_first' else 1):
                path=root/'runs'/case/f'{arm}_rep{repeat:02d}/result.json';r,d=load(path,arm)
                assert d['protocol']['version'] in ('full_corrected_v1','full_wide_v1') and d['frozen_sha256']==spec['frozen_sha256']
                assert d['logical_sumo_evaluations']<=d['budget']
                initial_count=d['protocol']['initial_count']
                assert full.m.digest(d['candidates'][:initial_count])==full.m.digest(spec['initial'])
                siblings={}
                for decision in d['decisions']:
                    parent_id=decision['parent'];seen=siblings.setdefault(parent_id,set())
                    frozen_decision=next(x for x in spec['decisions'] if x['parent']['proposal_id']==parent_id)
                    catalog=full.sibling_catalog(frozen_decision['catalog'],seen)
                    assert decision['catalog_sha256']==full.m.digest(catalog)
                    proposal=decision.get('proposal') or {}
                    if proposal.get('validation',{}).get('ok'):
                        signature=full.plan_signature(proposal);assert signature not in seen
                        assert signature in {full.plan_signature(dict(action=c['action'],args=c['full_args'])) for c in catalog}
                        seen.add(signature)
                r.update(repeat=repeat,new_sumo=d['new_sumo_evaluations'],reused=d['reused_evaluations'],api_requests=d['api_requests'],
                    before_top10=100*d['before_selected']['scope_objective']['top10_relative_improvement'])
                runs.append(r);raw[(case,arm,repeat)]=d
                for c in [d['baseline']]+d['candidates']:
                    proof=full.audit_execution(c,d['sumo_seed'],d['sumo_version'])
                    if c.get('is_baseline'):assert full.m.candidate_seed(c)==int(d['sumo_seed'])
                    audits.append(dict(case=case,arm=arm,repeat=repeat,proposal_id=c['proposal']['proposal_id'],proof=proof))
                    if c.get('is_baseline'):continue
                    p=c['proposal'];s=c.get('scope_objective') or {}
                    kind='joint' if p.get('refinement',{}).get('feedback_scope')=='joint_plan' else 'bundle' if p['action']=='bundle' else 'mutation' if p.get('refinement') else 'initial'
                    candidates.append(dict(case=case,arm=arm,repeat=repeat,stage=kind,proposal_id=p['proposal_id'],
                        parent_id=p.get('parent_proposal_id'),action=p['action'],status=c['status'],sumo_evaluated=c.get('sumo_evaluated',False),
                        accepted=bool(s.get('constraints_passed')),selected=p['proposal_id']==d['selected']['proposal']['proposal_id'],
                        top10=100*s['top10_relative_improvement'] if s.get('available') else None,
                        global_reduction=-100*s['global_delay_relative_change'] if s.get('available') else None,
                        failed_constraints=';'.join(k for k,v in s.get('constraint_checks',{}).items() if not v)))
                for call_path in sorted((path.parent/'calls').glob('*.json')):
                    call=a.read(call_path);request=call['request'];payload=request['payload']
                    model_metadata.extend(r for attempt in call['attempts'] for r in attempt['response_metadata'])
                    requests.append(dict(case=case,arm=arm,repeat=repeat,call=call_path.name,status=call['status'],
                        phase='mutation' if payload['search_round']==2 else 'joint',scope=request['scope'],
                        selected_mutation=(call.get('response') or {}).get('selected_mutation_id',(call.get('response') or {}).get('mutation_id'))))
                    if payload['search_round']==2:
                        pid=payload['parent_proposal']['proposal_id'];dec=next(x for x in spec['decisions'] if x['parent']['proposal_id']==pid)
                        assert payload['simulation_feedback']==dec['feedback'] and payload['evaluated_history']==spec['history']
                        assert payload['parent_proposal']==dec['parent']
                        child_index=payload['mutation_index']
                        entry=next(x for x in d['decisions'] if x['parent']==pid and x.get('child_index',1)==child_index)
                        catalog=full.sibling_catalog(dec['catalog'],set(entry.get('excluded_sibling_signatures',[])))
                        assert payload['mutation_catalog']==_mutation_payload(catalog)
                        if ':repair' not in request['scope'] and child_index==1:
                            fingerprint=full.m.digest({k:request[k] for k in ['system','payload','scope','max_tokens']})
                            key=(case,pid);shared.setdefault(key,fingerprint);assert shared[key]==fingerprint,'Initial selector prompts differ'
                pool=list(d['candidates'][:initial_count]);new=d['candidates'][initial_count:d['prefix_candidate_count']]
                def stage(name):
                    best=full.m.opt._select_best_candidate([d['baseline']]+pool,profile='delay',objective_scope='top10')
                    stages.append(dict(case=case,arm=arm,repeat=repeat,stage=name,
                        top10=100*best['scope_objective']['top10_relative_improvement'],logical=sum(bool(c.get('sumo_evaluated')) for c in pool)))
                stage('Initial')
                pool.extend(c for c in new if c['proposal']['action']!='bundle');stage('Mutation')
                pool.extend(c for c in new if c['proposal']['action']=='bundle');stage('Bundle')
                by_id={c['proposal']['proposal_id']:c for c in d['candidates']}
                for round_record in d['joint_rounds']:
                    for decision in round_record['decisions']:
                        parent=by_id[decision['parent_id']]
                        assert decision['feedback']==full.m.opt._candidate_feedback(parent,spec['baseline']['kpi'],profile='delay',objective_scope='top10')
                        proposal=decision.get('proposal') or {};candidate=by_id.get(proposal.get('proposal_id'))
                        if candidate:
                            changed=proposal['refinement']['selected_component_index'];assert len(proposal['refinement']['changed_fields'])==1
                            assert all(action==parent['proposal']['actions'][i] for i,action in enumerate(proposal['actions']) if i!=changed)
                            pool.append(candidate)
                    stage(f"Joint {round_record['round']}")
                for n in range(len(d['joint_rounds'])+1,d['protocol']['joint_rounds']+1):stage(f'Joint {n}')
    a.csvwrite(out/'case_results.csv',[{k:r[k] for k in ['case','city','arm','repeat','top10','before_top10','global_reduction','success','evals','new_sumo','reused','api_requests','selected']} for r in runs])
    a.csvwrite(out/'candidate_results.csv',candidates);a.csvwrite(out/'stage_results.csv',stages);a.csvwrite(out/'selection_requests.csv',requests)
    a.csvwrite(out/'sibling_audit.csv',[dict(case=case,arm=arm,repeat=repeat,parent=decision['parent'],child_index=decision.get('child_index',1),
        status=decision['status'],proposal_id=(decision.get('proposal') or {}).get('proposal_id'),catalog_sha256=decision['catalog_sha256'])
        for (case,arm,repeat),d in raw.items() for decision in d['decisions']])
    a.write(out/'model_metadata_summary.json',dict(response_models=dict(Counter(r.get('response_model') for r in model_metadata if r.get('response_model'))),
        finish_reasons=dict(Counter(r.get('finish_reason') for r in model_metadata if r.get('finish_reason'))),
        prompt_tokens=sum(r.get('usage',{}).get('prompt_tokens',0) for r in model_metadata),
        completion_tokens=sum(r.get('usage',{}).get('completion_tokens',0) for r in model_metadata)))
    a.write(out/'execution_seed_audit.json',audits);a.write(out/'shared_first_round_prompts.json',[dict(case=k[0],parent=k[1],prompt_sha256=v) for k,v in shared.items()])
    summary={};case_means={}
    for arm in account['arms']:
        rr=[r for r in runs if r['arm']==arm];case_means[arm]={}
        for case in account['cases']:
            cr=[r for r in rr if r['case']==case]
            case_means[arm][case]=dict(case=case,city=cr[0]['city'],top10=a.mean([r['top10'] for r in cr]))
        summary[arm]=dict(cases=len(case_means[arm]),runs=len(rr),top10_mean=a.mean([r['top10'] for r in rr]),top10_sd=a.std([r['top10'] for r in rr]),
            before_joint_mean=a.mean([r['before_top10'] for r in rr]),global_mean=a.mean([r['global_reduction'] for r in rr]),
            successes=sum(r['success'] for r in rr),evaluations=sum(r['evals'] for r in rr),api_requests=sum(r['api_requests'] for r in rr),
            new_sumo=sum(r['new_sumo'] for r in rr),generation_failures=sum(r['arm']==arm and r['status']=='generation_error' for r in requests))
    pairs={arm:a.paired(case_means['llm'],case_means[arm]) for arm in account['arms'] if arm!='llm'} if 'llm' in case_means else {}
    prev=0
    for i,arm in enumerate(sorted(pairs,key=lambda arm:pairs[arm]['wilcoxon_p'])):
        prev=max(prev,min(1.,(len(pairs)-i)*pairs[arm]['wilcoxon_p']));pairs[arm]['holm_wilcoxon_p']=prev
    a.write(out/'summary.json',summary);a.write(out/'paired_statistics.json',pairs)
    a.csvwrite(out/'paired_differences.csv',[dict(comparator=arm,**row) for arm,p in pairs.items() for row in p['paired_differences']])
    spatial=[];worst=[]
    for r in runs:
        s,w,_=a.spatial(r);s['repeat']=r['repeat'];spatial.append(s)
        worst.extend(dict(**row,repeat=r['repeat']) for row in w)
    a.csvwrite(out/'spatial_selected.csv',spatial);a.csvwrite(out/'worst_remote_edges.csv',worst)
    sweeps=[]
    for t,g,n in itertools.product([0.,.005,.01,.02,.05],[0.,.005,.01,.02,.05],[0.,.025,.05,.1]):
        group=[]
        for arm in account['arms']:
            selected=[a.reselect(r,t,g,n) for r in runs if r['arm']==arm]
            group.append(dict(arm=arm,top10_threshold=t,global_limit=g,neighbor_limit=n,
                success_rate=a.mean([c is not None for c in selected]),top10_mean=a.mean([100*c['scope']['top10_relative_improvement'] if c else 0 for c in selected])))
        for row in group:
            row['rank_success']=1+sum(r['success_rate']>row['success_rate'] for r in group)
            row['rank_top10']=1+sum(r['top10_mean']>row['top10_mean'] for r in group)
        sweeps.extend(group)
    a.csvwrite(out/'threshold_sensitivity.csv',sweeps)
    changes=[]
    for case in account['cases']:
        old=a.read(full.SOURCE/'frozen'/f'{case}.json');new=a.read(root/'frozen'/f'{case}.json')
        changes.append(dict(case=case,old_parents=[d['parent']['proposal_id'] for d in old['decisions']],
            new_parents=[d['parent']['proposal_id'] for d in new['decisions']],
            changed=[d['parent']['proposal_id'] for d in old['decisions']]!=[d['parent']['proposal_id'] for d in new['decisions']]))
    a.write(out/'rebuilt_parent_audit.json',changes)
    if 'llm' in account['arms']:
        example_case=account['cases'][0]
        a.write(out/'worked_full_case.json',dict(case=example_case,selection_rule='First case in the requested case list, not selected for performance.',
            frozen_inputs=a.read(root/'frozen'/f'{example_case}.json'),
            result=raw[(example_case,'llm',0)]))
    a.write(out/'threshold_rank_summary.json',{arm:dict(grid_points=sum(r['arm']==arm for r in sweeps),
        top10_first_or_tied=sum(r['arm']==arm and r['rank_top10']==1 for r in sweeps),
        success_first_or_tied=sum(r['arm']==arm and r['rank_success']==1 for r in sweeps)) for arm in account['arms']})
    stage_names=['Initial','Mutation','Bundle']+[f'Joint {i}' for i in range(1,max(d['protocol']['joint_rounds'] for d in raw.values())+1)]
    fig,axes=a.plt.subplots(1,2,figsize=(10,4),layout='constrained')
    for arm in account['arms']:
        axes[0].plot(range(len(stage_names)),[a.mean([r['top10'] for r in stages if r['arm']==arm and r['stage']==name]) for name in stage_names],marker='o',label=LABELS[arm])
        vals=[r['top10'] for r in runs if r['arm']==arm];i=account['arms'].index(arm)
        axes[1].scatter([i]*len(vals),vals,alpha=.7);axes[1].plot([i-.2,i+.2],[a.mean(vals)]*2,color='black')
    axes[0].set(xticks=range(len(stage_names)),xticklabels=stage_names,ylabel='Mean Top-10 reduction (%)',title='Full search with corrected feedback');axes[0].legend()
    axes[1].set(xticks=range(len(account['arms'])),xticklabels=[LABELS[x] for x in account['arms']],ylabel='Final Top-10 reduction (%)',title='All outcomes, including no-op')
    for ax in axes:ax.spines[['top','right']].set_visible(False)
    fig.savefig(out/'full_search.pdf');fig.savefig(out/'full_search.png',dpi=160);a.plt.close(fig)
    settings={(d['protocol']['joint_rounds'],d['budget']) for d in raw.values()}
    assert len(settings)==1,'Do not mix search budgets or joint round settings'
    modes={d['protocol'].get('joint_feedback_mode','aggregate') for d in raw.values()}
    assert len(modes)==1,'Do not mix joint feedback modes'
    joint_round_count,budget_limit=next(iter(settings))
    structures={(d['protocol']['initial_count'],d['protocol']['single_refinements']) for d in raw.values()}
    assert len(structures)==1,'Do not mix initial or child counts'
    initial_count,mutation_count=next(iter(structures));wide=initial_count==5
    if wide:
        bundle_audit=[]
        from agents import bundle_optimizer
        # Read-only completed cohort: cache identical network/anchor/length reads
        # within this audit only. Production simulations keep their original reader.
        cached_inventory=lru_cache(maxsize=512)(bundle_optimizer.corridor_speed_inventory)
        with patch.object(bundle_optimizer,'corridor_speed_inventory',cached_inventory):
            for (case,arm,repeat),d in raw.items():
                singles=[c for c in d['candidates'][:d['prefix_candidate_count']] if c['status']=='ok' and c['proposal']['action']!='bundle']
                quality={c['proposal']['proposal_id']:-c['scope_objective']['score'] for c in singles}
                alternatives=full.generate_pair_bundles([c['proposal'] for c in singles],top_k=len(singles),max_bundles=2,quality_by_id=quality)
                actual=sum(c['proposal']['action']=='bundle' for c in d['candidates'][:d['prefix_candidate_count']])
                bundle_audit.append(dict(case=case,arm=arm,repeat=repeat,successful_singles=len(singles),
                    actual_top4_bundles=actual,legal_bundles_if_all_singles_considered=len(alternatives),
                    unused_budget=d['budget']-d['logical_sumo_evaluations'],
                    note='Structural compatibility audit only; alternative bundles were not simulated'))
        a.csvwrite(out/'bundle_pool_audit.csv',bundle_audit)
        comparison=[]
        for row in runs:
            old_path=full.OUT/'runs'/row['case']/f"{row['arm']}_rep{row['repeat']:02d}/result.json"
            if not old_path.exists():continue
            previous=a.read(old_path)
            old_gain=100*previous['selected']['scope_objective']['top10_relative_improvement']
            comparison.append(dict(case=row['case'],arm=row['arm'],repeat=row['repeat'],old_budget=previous['budget'],new_budget=budget_limit,
                old_top10=old_gain,new_top10=row['top10'],difference_pp=row['top10']-old_gain,
                interpretation='Descriptive only: both structure and budget changed'))
        a.csvwrite(out/'wide_vs_previous.csv',comparison)
    runner='wide_feedback_experiment.py' if wide else 'full_feedback_experiment.py'
    lines=['# 五初始方案、双子代实验 v1' if wide else '# 完整正确反馈实验 v1','',
        f'联合反馈模式：{next(iter(modes))}。',
        f'从共同的 {initial_count} 个初始动作及其正确 seed 的实测结果开始。首轮父方案、反馈、历史和合法 catalog 全部重新生成，每个父方案的首次选择请求在三组间按 system/payload/scope/token budget 逐项哈希核验一致。没有沿用历史 LLM mutation 或历史 bundle 作为搜索起点。相同动作的确定性 SUMO 结果可以复用，但不会把缓存中其他候选提前加入搜索历史。',
        '第二子代直接从同一个父方案生成，仅排除已选兄弟动作，保持初始历史和父方案反馈不变；剩余 catalog 可能随首次选择而不同，不宣称第二次请求完全相同。' if wide else '',
        '', f'流程：{initial_count} 个初始方案 → 最多 {mutation_count} 个单方案 mutation → 最多 2 个 bundle → {joint_round_count} 轮联合反馈，每轮 beam=2。本队列候选仿真上限 {budget_limit}，公共基线另计；缓存也消耗逻辑预算。最终结果比较相同初始化、结构和预算下的完整自适应流程。','',
        '| 方法 | Top-10 均值 ± SD (%) | 联合循环前 (%) | 全网延误下降 (%) | 成功运行 | 实际逻辑评估 |',
        '|---|---:|---:|---:|---:|---:|']
    for arm,s in summary.items():lines.append(f"| {LABELS[arm]} | {s['top10_mean']:.2f} ± {s['top10_sd']:.2f} | {s['before_joint_mean']:.2f} | {s['global_mean']:.2f} | {s['successes']}/{s['runs']} | {s['evaluations']} |")
    lines+=['','## 统计','',
        '同 city–seed 配对的精确双侧符号翻转检验；Wilcoxon 删除零差值、采用无连续性校正的正态近似，两项比较做 Holm 校正。按五个城市聚类的 bootstrap 为 20,000 次，seed=20260927，区间仅作描述。如果重复数大于一，先对每个 city–seed 内的重复取均值，再进行案例配对；不会把模型重复当成独立城市案例。',
        *[f"- LLM − {LABELS[arm]}：{p['mean_difference_pp']:+.3f} pp，案例符号翻转 p={p['exact_case_signflip_p']:.4f}，城市 p={p['exact_city_signflip_p']:.4f}，Holm–Wilcoxon p={p['holm_wilcoxon_p']:.4f}。" for arm,p in pairs.items()],
        '', '## 执行与输入审计','',
        f"完成 {account['arm_case_runs']} 个运行；新增实际 SUMO {account['new_sumo']} 次、缓存逻辑评估 {account['reused']} 次；HTTP API 请求 {account['api_requests']} 次。另记录 {account['transport_failures']} 次未取得 HTTP 响应的传输故障，不算模型选择失败。",
        f"其中新增公共初始化仿真 {account.get('shared_initialization_new_sumo',0)} 次，只实际执行一次，但每组均计入其逻辑预算。" if wide else '',
        f"重建后 {sum(c['changed'] for c in changes)}/{len(changes)} 个案例的首轮父方案列表改变；见 rebuilt_parent_audit.json。实测初始候选包含不可行及仿真前拒绝的方案，未删除这些案例或换成更好的初始方案。",
        'frozen/ 保存新父方案、正确实测反馈、全部合法 catalog 和逐项过滤记录。runs/ 保存全部新响应、模型终止原因、评估账本及联合父子关系。实际 seed/version 从 tripinfo 内嵌配置核验，不以目录名或结果标签代替。','',
        '## 敏感性与空间影响','',
        'threshold_sensitivity.csv 对 100 组接受阈值重筛固定候选池，分别报告成功率排名及 Top-10 均值排名；这不是改变阈值后重新搜索。spatial_selected.csv 与 worst_remote_edges.csv 保存最终方案的全网恶化分布、最严重远端边与 500 米单元指标。',
        *[f"- {LABELS[arm]}：{sum(s['remote_edges_increase_over_60s']>0 for s in spatial if s['arm']==arm)} 个结果存在监测邻域外单边累计时间损失增加超过 60 车秒；最大远端单边恶化 {max(s['max_remote_increase_s'] for s in spatial if s['arm']==arm):.2f} 车秒。" for arm in account['arms']],
        '', '## 复现与尚未完成','', '```bash',
        f'/home/chris/.conda/envs/chatsumo/bin/python experiment/{runner} --prepare-only',
        f'/home/chris/.conda/envs/chatsumo/bin/python experiment/{runner} --workers 3',
        f'python3 experiment/analyze_full_feedback.py --root {root.relative_to(ROOT) if ROOT in root.parents else root}', '```','',
        f"当前重复数：{account['repeats']}。默认模型 gpt-5.6-luna、生成预算 4096、默认采样。需求/SUMO seeds=42,43,44，SUMO 1.27.1；随机选择 seed=20260927+SUMO_seed+1000×repeat+请求序号。规则只运行一次；--repeats 5 可补齐 LLM/随机的五次采样，已有结果原样保留。",
        '目前仍为固定 2,400 合成 trips，未新增校准需求；主实验的一次 LLM 采样不能替代稳定性评估。旧论文及 PDF 未自动替换。新表格、图和可审计结果在本目录，正式改稿应与后续稳定性结果一并整合。',
        '本版本预算 15，高于旧版本的 11。与旧结果的差异不能单独归因于扩大搜索宽度；尚未运行 15 预算的增加深度对照。' if wide else '']
    if wide:
        lines+=['',f"组合池审计：{sum(r['actual_top4_bundles']==0 for r in bundle_audit)}/{len(bundle_audit)} 个运行没有形成初始 bundle；其中 {sum(r['actual_top4_bundles']==0 and r['legal_bundles_if_all_singles_considered']>0 for r in bundle_audit)} 个运行在考虑全部已测单方案时仍有合法组合。原 top-k=4 规则未在本轮中途调整。详情见 bundle_pool_audit.csv；这些替代组合只做兼容性检查，未追加仿真。"]
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
