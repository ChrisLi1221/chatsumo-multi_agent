#!/usr/bin/env python3
"""Analyze adaptive joint continuation separately from the frozen-selector study."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import analyze_multra_revision as a
from experiment import revision_matched as m


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=ROOT/'experiment/joint_feedback_continuation')
    args=parser.parse_args();out=args.root/'analysis';out.mkdir(parents=True,exist_ok=True)
    rows=[];details=[];curves=[];seed_audit=[];raw={}
    for path in sorted((args.root/'runs').glob('*/*/result.json')):
        d=a.read(path);raw[(d['case'],d['arm'])]=d
        before=d['before_selected'];after=d['selected'];new=d['candidates'][d['prefix_candidate_count']:]
        before_top=100*before['scope_objective']['top10_relative_improvement']
        after_top=100*after['scope_objective']['top10_relative_improvement']
        assert after_top+1e-8>=before_top,'Best-so-far guard regressed'
        row=dict(case=d['case'],city=d['case'].split('_equal_scale')[0],arm=d['arm'],top10=after_top,
            before_top10=before_top,gain_pp=after_top-before_top,
            before_success=not before.get('is_baseline',False),success=not after.get('is_baseline',False),
            global_reduction=-100*after['scope_objective']['global_delay_relative_change'],
            joint_evaluations=d['joint_evaluations'],logical_evaluations=d['logical_sumo_evaluations'],
            new_sumo=d['new_sumo_evaluations'],reused_joint=d['reused_joint_evaluations'],api_requests=d['api_requests'],
            generation_failures=sum(t['status']=='generation_error' for r in d['joint_rounds'] for t in r['decisions']),
            selected=after['proposal']['proposal_id'])
        rows.append(row)
        for c in [d['baseline']]+d['candidates']:
            if c['status']=='ok':
                seed=m.candidate_seed(c);assert seed==int(d['sumo_seed'])
                seed_audit.append(dict(case=d['case'],arm=d['arm'],proposal_id=c['proposal']['proposal_id'],executed_seed=seed))
        current=list(d['candidates'][:d['prefix_candidate_count']]);curves.append(dict(case=d['case'],arm=d['arm'],round=0,top10=before_top))
        by_id={c['proposal']['proposal_id']:c for c in d['candidates']}
        new_map={c['proposal']['proposal_id']:c for c in new};new_ids=set(new_map)
        for record in d['joint_rounds']:
            for decision in record['decisions']:
                proposal=decision.get('proposal') or {};child=new_map.get(proposal.get('proposal_id'));ref=proposal.get('refinement') or {}
                parent=by_id[decision['parent_id']]
                assert decision['feedback']==m.opt._candidate_feedback(parent,d['baseline']['optimized']['kpi'],profile='delay',objective_scope='top10')
                if child:
                    changed=ref['selected_component_index']
                    assert len(ref['changed_fields'])==1
                    assert all(action==parent['proposal']['actions'][i] for i,action in enumerate(proposal['actions']) if i!=changed)
                if child:current.append(child)
                scope=(child or {}).get('scope_objective') or {}
                details.append(dict(case=d['case'],arm=d['arm'],round=record['round'],parent=decision['parent_id'],
                    parent_is_new_joint_child=decision['parent_id'] in new_ids,status=decision['status'],
                    proposal_id=proposal.get('proposal_id'),specialist=ref.get('selected_specialist'),
                    changed_component=ref.get('selected_component_index'),mutation_id=ref.get('selected_mutation_id'),
                    catalog_size=len(ref.get('mutation_catalog',[])),accepted=bool(scope.get('constraints_passed')),
                    top10=100*scope['top10_relative_improvement'] if scope else None,
                    parent_delta_pp=100*child['conditional_change']['top10_improvement_delta'] if child and child.get('conditional_change') else None,
                    failed_constraints=';'.join(k for k,v in scope.get('constraint_checks',{}).items() if not v),error=decision.get('error')))
            best=m.opt._select_best_candidate([d['baseline']]+current,profile='delay',objective_scope='top10')
            curves.append(dict(case=d['case'],arm=d['arm'],round=record['round'],top10=100*best['scope_objective']['top10_relative_improvement']))
    assert rows,'No completed runs'
    arms=[arm for arm in m.ARMS if any(r['arm']==arm for r in rows)]
    cases={r['case'] for r in rows}
    for case in cases:
        assert all((case,arm) in raw for arm in arms),'Incomplete arm-case comparison'
        assert len({raw[(case,arm)]['protocol']['source_sha256'] for arm in arms})==1,'Initial pools differ'
    a.csvwrite(out/'case_results.csv',rows);a.csvwrite(out/'joint_decisions.csv',details)
    a.csvwrite(out/'round_results.csv',curves);a.csvwrite(out/'executed_seed_audit.csv',seed_audit)
    summary={}
    for arm in arms:
        rr=[r for r in rows if r['arm']==arm];dd=[r for r in details if r['arm']==arm]
        summary[arm]=dict(cases=len(rr),before_mean=a.mean([r['before_top10'] for r in rr]),
            after_mean=a.mean([r['top10'] for r in rr]),after_sd=a.std([r['top10'] for r in rr]),
            gain_mean_pp=a.mean([r['gain_pp'] for r in rr]),global_reduction_mean=a.mean([r['global_reduction'] for r in rr]),
            before_successes=sum(r['before_success'] for r in rr),successes=sum(r['success'] for r in rr),
            improved_cases=sum(r['gain_pp']>1e-7 for r in rr),joint_evaluations=sum(r['joint_evaluations'] for r in rr),
            new_sumo=sum(r['new_sumo'] for r in rr),reused_joint=sum(r['reused_joint'] for r in rr),
            api_requests=sum(r['api_requests'] for r in rr),generation_failures=sum(r['generation_failures'] for r in rr),
            accepted_joint_candidates=sum(r['accepted'] for r in dd),new_children_reused_as_parents=sum(r['parent_is_new_joint_child'] for r in dd))
    paired={}
    if 'llm' in arms:
        llm={r['case']:r for r in rows if r['arm']=='llm'}
        for arm in arms:
            if arm!='llm':paired[arm]=a.paired(llm,{r['case']:r for r in rows if r['arm']==arm})
        previous=0
        for i,arm in enumerate(sorted(paired,key=lambda arm:paired[arm]['wilcoxon_p'])):
            previous=max(previous,min(1.,(len(paired)-i)*paired[arm]['wilcoxon_p']))
            paired[arm]['holm_wilcoxon_p']=previous
    a.write(out/'summary.json',summary);a.write(out/'paired_statistics.json',paired)
    if 'llm' in arms:
        example=max((r for r in rows if r['arm']=='llm'),key=lambda r:r['gain_pp'])
        d=raw[(example['case'],'llm')]
        a.write(out/'worked_joint_example.json',dict(case=example['case'],
            example_selection_rule='Largest observed LLM continuation gain; illustrative, not representative.',
            source_protocol=d['protocol'],before_selected=d['before_selected'],selected=d['selected'],
            joint_rounds=d['joint_rounds'],new_candidates=d['candidates'][d['prefix_candidate_count']:]))
    fig,axes=a.plt.subplots(1,2,figsize=(10,4),layout='constrained')
    labels={'llm':'LLM','catalog_first':'Rule','catalog_random':'Random'}
    for arm in arms:
        rounds=sorted({r['round'] for r in curves if r['arm']==arm})
        axes[0].plot(rounds,[a.mean([r['top10'] for r in curves if r['arm']==arm and r['round']==rnd]) for rnd in rounds],marker='o',label=labels[arm])
        values=[r['gain_pp'] for r in rows if r['arm']==arm];i=arms.index(arm)
        axes[1].scatter([i]*len(values),values);axes[1].plot([i-.2,i+.2],[a.mean(values)]*2,color='black')
    axes[0].set(xticks=sorted({r['round'] for r in curves}),xlabel='Additional joint feedback round',ylabel='Mean Top-10 reduction (%)',title='Common measured starting pool');axes[0].legend()
    axes[1].set(xticks=range(len(arms)),xticklabels=[labels[x] for x in arms],ylabel='Gain over common prefix (pp)',title='Per-case continuation gains')
    for ax in axes:ax.spines[['top','right']].set_visible(False)
    fig.savefig(out/'joint_progress.pdf');fig.savefig(out/'joint_progress.png',dpi=160);a.plt.close(fig)
    lines=['# 联合反馈循环实验','',
        '所有选择器从同一个已修正 seed 的 LLM4096 候选池出发（每例最多 7 次逻辑评估）。默认追加两轮联合反馈、每轮 beam=2，总上限 11 次候选仿真，基线另计。新 bundle 可成为后续父方案；轮换一个组件的专家，其余组件固定；每次从基线重建完整组合并重新仿真。旧候选保留，因此最终最优可行值不会下降。',
        '', '本实验衡量共同起点上的自适应联合搜索。首轮相同父方案下首个调用使用相同 catalog；每次新结果都会更新历史，后续父方案/catalog 可以因选择而分叉，不能称为逐步冻结父方案的 LLM 消融。起点仍包含根据旧单方案反馈保存的选择，本轮没有重采样这些选择；新增决策全部使用正确 seed 的联合实测反馈。','',
        '| 方法 | 起点 Top-10 (%) | 最终均值 ± SD (%) | 平均增益 (pp) | 成功案例 | 提升案例 | 新增逻辑仿真 |',
        '|---|---:|---:|---:|---:|---:|---:|']
    for arm,s in summary.items():lines.append(f"| {labels[arm]} | {s['before_mean']:.2f} | {s['after_mean']:.2f} ± {s['after_sd']:.2f} | {s['gain_mean_pp']:.2f} | {s['successes']}/{s['cases']} | {s['improved_cases']}/{s['cases']} | {s['joint_evaluations']} |")
    lines+=['','## 执行核对','',
        f"本目录完成 {len(rows)} 个 arm–case。新增实际 SUMO 调用 {sum(r['new_sumo'] for r in rows)} 次，复用联合仿真 {sum(r['reused_joint'] for r in rows)} 次，实际 API 请求 {sum(r['api_requests'] for r in rows)} 次。沙箱 DNS 失败的首次试跑单独保留在 failed_attempts，不混入这里的实验结果。",
        f"LLM 生成失败 {summary.get('llm',{}).get('generation_failures',0)} 次。所有成功候选均通过实际 tripinfo 的 SUMO seed 核验。",
        *[f"- {labels[arm]}：新 bundle 再作为父方案 {s['new_children_reused_as_parents']} 次；通过接受约束的新增候选 {s['accepted_joint_candidates']} 个。" for arm,s in summary.items()],
        '', '## 统计与边界','',
        '按同 city–seed 配对，使用精确双侧符号翻转检验；另报告去除零差值的 Wilcoxon 正态近似和两项比较的 Holm 校正。城市聚类分析按五个城市计算，bootstrap 20,000 次、seed=20260927。额外预算带来的提升不能全部归因于 LLM；应比较相同预算下的三组策略。',
        *[f"- LLM − {labels[arm]}：{p['mean_difference_pp']:+.3f} pp；案例符号翻转 p={p['exact_case_signflip_p']:.4f}，城市 p={p['exact_city_signflip_p']:.4f}，Holm–Wilcoxon p={p['holm_wilcoxon_p']:.4f}。" for arm,p in paired.items()],
        '单次 LLM 采样；需求仍为每城 2,400 合成 trips；没有新增校准需求、重复采样、阈值敏感性或远端空间副作用结果。论文旧数值未覆盖。',
        'seed 44 的原始 SUMO 文件位于并行执行目录 experiment/joint_feedback_continuation_seed44；主目录中的 15 份结果已按字节哈希核对，来源见 shard_imports.json。保存数据时须同时保留该目录及公共起点。','',
        '## 复现','', '```bash',
        '/home/chris/.conda/envs/chatsumo/bin/python -u experiment/continue_joint_feedback.py',
        'python3 experiment/analyze_joint_continuation.py',
        '```','',
        '默认 SUMO 1.27.1，需求/SUMO seed 为 42、43、44。随机选择每次请求的 seed 为 20260927 + SUMO_seed + 请求序号（从零计）。输出预算 4096，默认两轮、beam=2、总候选预算 11。改变模型、轮数、预算必须指定新 --output-dir；已有响应和完成评估按哈希验证后重用。完整请求、catalog、联合反馈和响应在 runs/*/*/calls/；父子关系和结果在 joint_rounds.json。']
    (out/'REPORT.md').write_text('\n'.join(lines)+'\n')
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
