#!/usr/bin/env python3
"""Analyze the saved-response traffic evaluation without changing the paper cohort."""
from collections import Counter
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from experiment import analyze_multra_revision as a
from experiment.revision_matched import candidate_seed

RUNS=ROOT/'experiment/llm_output_evaluation'
ARCHIVE=ROOT/'experiment/multra_revision'
OUT=RUNS/'analysis'
LABELS={'llm4096':'LLM (4096)','catalog_first':'Rule','catalog_random':'Random','llm500':'Archived LLM (500)'}


def load(path,arm):
    d=a.read(path);pool=[]
    for c in d['candidates']:
        if c['status']!='ok':continue
        pool.append(dict(proposal_id=c['proposal']['proposal_id'],action=c['proposal']['action'],
                         scope=c['scope_objective'],kpi=c['optimized']['kpi'],
                         baseline=d['baseline']['optimized']['kpi'],folder=str(ROOT/c['cache_source']),
                         route_validation=c['modification'].get('route_validation'),kpi_score=c['kpi_score']))
    s=d['selected'];sc=s['scope_objective']
    row=dict(case=d['case'],city=d['case'].split('_equal_scale')[0],arm=arm,cohort='fresh',run=str(path.parent),
        pool=pool,baseline=d['baseline']['optimized']['kpi'],top10=100*sc['top10_relative_improvement'],
        global_reduction=-100*sc['global_delay_relative_change'],selected=s['proposal']['proposal_id'],
        success=not s.get('is_baseline',False),evals=d['logical_sumo_evaluations'])
    best=a.reselect(row)
    assert abs((100*best['scope']['top10_relative_improvement'] if best else 0)-row['top10'])<1e-7
    return row,d


def stage(candidate):
    if candidate.get('is_baseline'):return 'baseline'
    p=candidate['proposal']
    return 'bundle' if p['action']=='bundle' else 'refinement' if p.get('refinement') else 'initial'


def main():
    global RUNS,OUT
    parser=argparse.ArgumentParser()
    parser.add_argument('--seed-corrected',action='store_true')
    args=parser.parse_args()
    if args.seed_corrected:
        RUNS=ROOT/'experiment/llm_output_seed_corrected';OUT=RUNS/'analysis'
    OUT.mkdir(parents=True,exist_ok=True)
    cases=[json.loads(s)['id'] for s in (ROOT/'experiment/selector-ablation-cases.jsonl').read_text().splitlines()]
    runs=[];raw={};candidate_rows=[];effects=[];provenance=[];mutation_comparison=[]
    for case in cases:
        spec=a.read(ARCHIVE/'frozen'/f'{case}.json')
        for arm in LABELS:
            path=(RUNS if arm=='llm4096' or (args.seed_corrected and arm!='llm500') else ARCHIVE)/'runs'/case/f"{'llm' if arm.startswith('llm') else arm}_rep00/result.json"
            r,d=load(path,arm);assert d['frozen_sha256']==spec['frozen_sha256'];runs.append(r);raw[(case,arm)]=d
            assert d['budget']==7 and d['sumo_seed']==spec['sumo_seed']
            if args.seed_corrected and arm!='llm500':
                assert d['require_seed']
                assert all(candidate_seed(c)==int(spec['sumo_seed']) for c in [d['baseline']]+d['candidates'] if c['status']=='ok')
            provenance.append(dict(case=case,arm=arm,result=str(path.relative_to(ROOT)),frozen_sha256=d['frozen_sha256']))
        d=raw[(case,'llm4096')];selected=d['selected'];r=next(r for r in runs if r['case']==case and r['arm']=='llm4096')
        assert d['actual_api_requests']==0 and d['max_completion_tokens']==4096
        assert len(d['decisions'])==2
        for i,c in enumerate(d['candidates']):
            sc=c.get('scope_objective') or {};kind=stage(c)
            if c['status']=='ok':
                cp=next(p for p in r['pool'] if p['proposal_id']==c['proposal']['proposal_id']);accepted=a.eligible(cp)
            else:accepted=False
            candidate_rows.append(dict(case=case,kind=kind,proposal_id=c['proposal']['proposal_id'],
                action=c['proposal']['action'],status=c['status'],sumo_evaluated=c.get('sumo_evaluated',False),
                accepted=accepted,selected=c['proposal']['proposal_id']==r['selected'],
                mutation_id=(c['proposal'].get('refinement') or {}).get('selected_mutation_id'),
                top10_reduction_pct=100*sc['top10_relative_improvement'] if sc.get('available') else None,
                global_delay_reduction_pct=-100*sc['global_delay_relative_change'] if sc.get('available') else None,
                neighbor_change_pct=100*sc['neighbor_delay_relative_change'] if sc.get('neighbor_delay_relative_change') is not None else None,
                failed_constraints=';'.join(k for k,v in sc.get('constraint_checks',{}).items() if not v),
                cache_source=c.get('cache_source')))
        feasible_singles=[c for c in r['pool'] if c['action']!='bundle' and a.eligible(c)]
        single_best=max([0.]+[100*c['scope']['top10_relative_improvement'] for c in feasible_singles])
        old=next(q for q in runs if q['case']==case and q['arm']=='llm500')
        oldraw=raw[(case,'llm500')]
        for i,decision in enumerate(d['decisions']):
            olddecision=oldraw['decisions'][i]
            mutation_comparison.append(dict(case=case,parent_index=i,parent=decision['parent'],
                old_status=olddecision['status'],
                old_mutation=(olddecision.get('proposal',{}).get('refinement') or {}).get('selected_mutation_id'),
                new_mutation=decision['proposal']['refinement']['selected_mutation_id'],
                new_status=decision['status']))
        effects.append(dict(case=case,selected_stage=stage(selected),best_feasible_single_pct=single_best,
                            final_top10_pct=r['top10'],bundle_gain_pp=r['top10']-single_best,
                            old_llm_top10_pct=old['top10'],change_from_old_pp=r['top10']-old['top10'],
                            new_sumo=d['new_sumo_evaluations'],reused=d['reused_evaluations'],logical=d['logical_sumo_evaluations']))
    a.write(OUT/'cohort_provenance.json',provenance)
    a.csvwrite(OUT/'candidate_results.csv',candidate_rows);a.csvwrite(OUT/'composition_effects.csv',effects)
    a.csvwrite(OUT/'mutation_comparison.csv',mutation_comparison)
    a.csvwrite(OUT/'case_results.csv',[{k:r[k] for k in ['case','city','arm','top10','global_reduction','success','evals','selected']} for r in runs])
    summary={}
    for arm in LABELS:
        rr=[r for r in runs if r['arm']==arm]
        summary[arm]=dict(n=len(rr),top10_mean=a.mean([r['top10'] for r in rr]),top10_sd=a.std([r['top10'] for r in rr]),
            global_mean=a.mean([r['global_reduction'] for r in rr]),successes=sum(r['success'] for r in rr),
            evaluations=sum(r['evals'] for r in rr),successful_simulations=sum(len(r['pool']) for r in rr),
            feasible_candidates=sum(sum(a.eligible(c) for c in r['pool']) for r in rr))
    pairs={arm:a.paired({r['case']:r for r in runs if r['arm']=='llm4096'},
                       {r['case']:r for r in runs if r['arm']==arm}) for arm in ['catalog_first','catalog_random','llm500']}
    ranked=sorted(['catalog_first','catalog_random'],key=lambda k:pairs[k]['wilcoxon_p']);prev=0.
    for i,arm in enumerate(ranked):
        prev=max(prev,min(1.,(2-i)*pairs[arm]['wilcoxon_p']));pairs[arm]['holm_wilcoxon_p']=prev
    pairs['llm500']['interpretation']='Descriptive old/new comparison; new stochastic samples and changed output budget, not a pure causal estimate. The archived cohort also includes mis-seeded rerouting candidates.'
    a.write(OUT/'summary.json',summary);a.write(OUT/'paired_statistics.json',pairs)
    a.csvwrite(OUT/'paired_differences.csv',[dict(comparator=arm,**d) for arm,p in pairs.items() for d in p['paired_differences']])
    spatial=[];worst=[]
    for r in runs:
        s,w,_=a.spatial(r);spatial.append(s);worst.extend(w)
    a.csvwrite(OUT/'spatial_selected.csv',spatial);a.csvwrite(OUT/'worst_remote_edges.csv',worst)
    spatial_summary={arm:dict(cases_remote_over60s=sum(s['remote_edges_increase_over_60s']>0 for s in spatial if s['arm']==arm),
        maximum_remote_edge_increase_s=max(s['max_remote_increase_s'] for s in spatial if s['arm']==arm),
        maximum_remote_cell_increase_s=max(s['max_remote_500m_cell_net_increase_s'] for s in spatial if s['arm']==arm)) for arm in LABELS}
    a.write(OUT/'spatial_summary.json',spatial_summary)
    stages=Counter(e['selected_stage'] for e in effects)
    counts={k:dict(proposed=sum(r['kind']==k for r in candidate_rows),
                   simulated=sum(r['kind']==k and r['sumo_evaluated'] for r in candidate_rows),
                   successful=sum(r['kind']==k and r['status']=='ok' for r in candidate_rows),
                   accepted=sum(r['kind']==k and r['accepted'] for r in candidate_rows)) for k in ['initial','refinement','bundle']}
    a.write(OUT/'stage_accounting.json',dict(candidates=counts,selected=dict(stages),
                                          cases_bundle_beats_best_single=sum(e['bundle_gain_pp']>1e-7 for e in effects)))
    fig,axes=a.plt.subplots(1,2,figsize=(10,4),layout='constrained')
    arms=list(LABELS);colors=['#176b99','#e3a12d','#49805b','#999999']
    for i,arm in enumerate(arms):
        vals=[r['top10'] for r in runs if r['arm']==arm]
        axes[0].scatter([i]*len(vals),vals,color=colors[i],alpha=.7)
        axes[0].plot([i-.22,i+.22],[a.mean(vals)]*2,color='black')
    axes[0].set(xticks=range(4),xticklabels=['LLM 4096','Rule','Random','LLM 500\n(archived)'],ylabel='Top-10 reduction (%)',title='Corrected replay + historical reference (n=15)' if args.seed_corrected else 'Historical execution (seed issue; n=15)')
    old=[e['old_llm_top10_pct'] for e in effects];new=[e['final_top10_pct'] for e in effects]
    axes[1].scatter(old,new,color=colors[0]);limit=max(old+new)+2
    axes[1].plot([0,limit],[0,limit],ls='--',color='gray')
    axes[1].set(xlabel='Archived LLM 500: Top-10 reduction (%)',ylabel='Saved LLM 4096: Top-10 reduction (%)',title='Old/new descriptive comparison')
    for ax in axes:ax.spines[['top','right']].set_visible(False)
    fig.savefig(OUT/'comparison.pdf');fig.savefig(OUT/'comparison.png',dpi=160);a.plt.close(fig)
    description=('固定 15 个 city–seed 案例，使用输出诊断中首次保存的 4096-token 响应，没有按仿真效果重新选择响应。三组保持相同初始化动作、父方案、catalog、冻结反馈、七次逻辑仿真上限及 bundle 规则。修复重路由配置继承后，三组受影响候选均重新评估；所有成功候选的实际 SUMO seed 均由 tripinfo 内嵌配置核验。bundle 按修正后的候选 KPI 重新生成。'
        ' **这是已保存选择的条件重放：模型/规则/随机仍依据修复前的冻结反馈选择 mutation，不是基于修正反馈重新采样的完整搜索。** 历史 LLM 500 行仅供描述，含错误 seed 的候选池，不属于修正后的三组对照。' if args.seed_corrected else
        '**历史评估，存在已确认的 seed 丢失问题，不可称为完全固定 SUMO seed 的消融。** 重路由及相关 bundle 的部分配置使用默认 23423；修正后的三组条件重放见 ../../llm_output_seed_corrected/analysis/REPORT.md。这里保留修复前原始数值。使用固定 15 个案例及首次保存的 4096-token 响应，规则和随机复用原 fresh cohort。')
    report=['# 已保存的 30 条 LLM 选择与 bundle 评估','',description,'',
            '| 策略 | Top-10 改善：均值 ± SD (%) | 全网延误下降 (%) | 成功案例 | 逻辑仿真次数 |',
            '|---|---:|---:|---:|---:|']
    for arm,s in summary.items():report.append(f"| {LABELS[arm]} | {s['top10_mean']:.2f} ± {s['top10_sd']:.2f} | {s['global_mean']:.2f} | {s['successes']}/15 | {s['evaluations']} |")
    report+=['','## 配对比较','',
             'Top-10 差值单位为百分点。使用同案例配对、精确双侧符号翻转检验；另报告零差值剔除、无连续性校正的 Wilcoxon 正态近似，对规则/随机两项比较做 Holm 校正。城市聚类敏感性以五个城市为单位，bootstrap 为 20,000 次，种子 20260927。独立城市少，区间仅作描述。','']
    for arm,p in pairs.items():
        report.append(f"- 新 LLM − {LABELS[arm]}：平均 {p['mean_difference_pp']:+.3f} pp，胜/平/负 {p['wins']}/{p['ties']}/{p['losses']}；案例符号翻转 p={p['exact_case_signflip_p']:.4f}，城市 p={p['exact_city_signflip_p']:.4f}；城市 bootstrap 95% 区间 {p['city_cluster_bootstrap_ci95_pp']}。")
        if 'holm_wilcoxon_p' in p:report.append(f"  Holm 校正后的 Wilcoxon p={p['holm_wilcoxon_p']:.4f}。")
    account=a.read(RUNS/'execution_accounting.json')
    legacy_calls=a.read(ROOT/'experiment/llm_output_evaluation/execution_accounting.json')['new_candidate_sumo'] if args.seed_corrected else 0
    report+=['','## 执行与组合贡献','',
        f"新增 API 请求为 0；复用既有 30 次模型响应。新增候选 SUMO 调用 {account['new_candidate_sumo']} 次，缓存逻辑评估 {account['reused_evaluations']} 次；共 {account['logical_evaluations']} 次逻辑评估。基线复用此前完整 KPI 一致的验证。",
        *([f"配置修复阶段的上述调用数涵盖全部三组。另有初次历史配置评估的 {legacy_calls} 次 SUMO 调用；本任务累计新增 {legacy_calls+account['new_candidate_sumo']} 次候选 SUMO 调用。下表仅统计 LLM 组，完整三组逐案例次数见 case_results.csv。"] if args.seed_corrected else []),
        '', '| 阶段 | 提出数 | SUMO 评估数 | 仿真成功数 | 通过接受条件 |',
        '|---|---:|---:|---:|---:|',
        *[f"| {k} | {v['proposed']} | {v['simulated']} | {v['successful']} | {v['accepted']} |" for k,v in counts.items()],
        '', f"最终选择阶段：`{json.dumps(dict(stages),ensure_ascii=False)}`。",
        f"{sum(e['bundle_gain_pp']>1e-7 for e in effects)}/15 个案例的最终 bundle 优于该搜索中最好的可行单方案；逐例增益见 composition_effects.csv。",
        *[f"- {e['case']}：最佳可行单方案（无则基线）{e['best_feasible_single_pct']:.2f}%，最终 bundle {e['final_top10_pct']:.2f}%，增益 {e['bundle_gain_pp']:.2f} pp。" for e in effects if e['bundle_gain_pp']>1e-7],
        '', '## 变化最大的案例与退步来源', '',
        'Ann Arbor/seed 42 从 5.90% 提高到 18.25%；其新 bundle 比本轮最好的可行单方案再提高 4.44 个百分点。Troy/seed 44 的 bundle 比本轮最好的可行单方案高 11.03 个百分点，最终结果与旧版本相同。',
        '两处退步都发生在旧模型本来能正常输出的案例：Ann Arbor/seed 43 的信号选择从 mutation_005 变为 mutation_004，旧可行单方案改善 14.88%，新单方案仅改善 4.55% 且邻边约束不通过；最终由 bundle 得到 5.36%。Albany/seed 44 的网络选择从 mutation_001 变为 mutation_003，单方案改善从 2.49% 变成 0.85%，低于 1% 接受阈值，最终保留基线。具体 ID 仅在各自冻结 catalog 内有意义。',
        '因此，合法输出率恢复不保证每次选择质量提高；本轮均值上升但成功率下降，且仍未显示相对非 LLM 对照的统计优势。',
        '', '## 空间副作用与解释边界','',
        f"新 LLM 的 {spatial_summary['llm4096']['cases_remote_over60s']}/15 个结果存在监测邻域以外单边累计时间损失增加超过 60 秒；最严重边增加 {spatial_summary['llm4096']['maximum_remote_edge_increase_s']:.2f} 车秒，最严重 500 米单元净增加 {spatial_summary['llm4096']['maximum_remote_cell_increase_s']:.2f} 车秒。该量不是每位驾驶员的额外延误。",
        '新旧 LLM 使用不同的随机调用实现且输出预算不同；差异不能全部归因于预算修复。所有结果仍来自 2,400 trips 的固定合成需求，不能推断真实城市效益。这里只评估一次已保存的选择集合，尚未做修复后五次重复或阈值重筛。原论文数据与 PDF 保持原版本。',
        '', '## 复现命令','', '在仓库根目录运行：','', '```bash',
        '/home/chris/.conda/envs/chatsumo/bin/python experiment/evaluate_saved_selections.py --preflight-only',
        '/home/chris/.conda/envs/chatsumo/bin/python -u experiment/evaluate_saved_selections.py',
        'python3 experiment/analyze_saved_selections.py','```','',
        '需求/SUMO 种子 42、43、44；随机对照选择种子 20260927 + SUMO_seed（repeat=0）。SUMO 1.27.1。结果写入 experiment/llm_output_evaluation；存在的结果会校验冻结输入、预算及保存响应哈希后复用。']
    (OUT/'REPORT.md').write_text('\n'.join(report)+'\n')
    if args.seed_corrected:
        audit=[];changes=[];feedback_audit=[]
        for case in cases:
            spec=a.read(ARCHIVE/'frozen'/f'{case}.json')
            for decision in spec['decisions']:
                parent=next(c for c in spec['initial'] if c['proposal']['proposal_id']==decision['parent']['proposal_id'])
                seed=candidate_seed(parent)
                feedback_audit.append(dict(case=case,parent=decision['parent']['proposal_id'],action=decision['parent']['action'],
                    expected_seed=spec['sumo_seed'],executed_parent_seed=seed,mismatch=seed!=int(spec['sumo_seed'])))
            for arm in ['llm4096','catalog_first','catalog_random']:
                folder=ROOT/'experiment/llm_output_evaluation' if arm=='llm4096' else ARCHIVE
                legacy=a.read(folder/'runs'/case/f"{'llm' if arm=='llm4096' else arm}_rep00/result.json")
                corrected=raw[(case,arm)]
                for cohort,d in [('legacy',legacy),('corrected',corrected)]:
                    for c in d['candidates']:
                        if c['status']!='ok':continue
                        seed=candidate_seed(c)
                        audit.append(dict(case=case,arm=arm,cohort=cohort,proposal_id=c['proposal']['proposal_id'],action=c['proposal']['action'],
                            expected_seed=d['sumo_seed'],executed_seed=seed,mismatch=seed!=int(d['sumo_seed']),
                            selected=c['proposal']['proposal_id']==d['selected']['proposal']['proposal_id'],
                            tripinfo=c['optimized']['artifacts']['tripinfo']))
                changes.append(dict(case=case,arm=arm,
                    legacy_top10_pct=100*legacy['selected']['scope_objective']['top10_relative_improvement'],
                    corrected_top10_pct=100*corrected['selected']['scope_objective']['top10_relative_improvement'],
                    legacy_selected=legacy['selected']['proposal']['proposal_id'],corrected_selected=corrected['selected']['proposal']['proposal_id']))
        a.csvwrite(OUT/'executed_seed_audit.csv',audit);a.csvwrite(OUT/'configuration_fix_effects.csv',changes)
        a.csvwrite(OUT/'frozen_feedback_seed_audit.csv',feedback_audit)
        audit_summary={cohort:dict(successful_candidates=sum(r['cohort']==cohort for r in audit),
            mismatched_candidates=sum(r['cohort']==cohort and r['mismatch'] for r in audit),
            selected_mismatches=sum(r['cohort']==cohort and r['mismatch'] and r['selected'] for r in audit),
            unique_mismatched_tripinfo=len({r['tripinfo'] for r in audit if r['cohort']==cohort and r['mismatch']}))
            for cohort in ['legacy','corrected']}
        a.write(OUT/'executed_seed_summary.json',audit_summary)
        report_text=(OUT/'REPORT.md').read_text()
        start=report_text.index('## 变化最大的案例与退步来源')
        end=report_text.index('## 空间副作用与解释边界')
        affected=[r for r in changes if abs(r['legacy_top10_pct']-r['corrected_top10_pct'])>1e-7]
        fix_report=['## 配置继承修复审计','',
            '旧重路由写配置函数没有继承源配置，丢失 seed，并可能丢失时间设置和信号附加文件。已改为克隆源配置后替换输入，保留运行设置和信号方案。默认 seed 23423 由本机 SUMO 1.27.1 的 --save-template 确认；逐候选核对实际 tripinfo 内的 sumoConfiguration，未使用结果 JSON 中的 seed 标签代替。',
            f"三组历史池共有 {audit_summary['legacy']['mismatched_candidates']} 个逻辑候选（{audit_summary['legacy']['unique_mismatched_tripinfo']} 份唯一 tripinfo）seed 不符；历史最终入选方案中不符数为 {audit_summary['legacy']['selected_mismatches']}。修正后三组候选 seed 不符数为 {audit_summary['corrected']['mismatched_candidates']}。",
            f'修复后 {len(affected)}/45 个 arm–case 的最终 Top-10 数值改变。逐候选审计见 executed_seed_audit.csv，前后对照见 configuration_fix_effects.csv。',
            f"30 次决策中有 {sum(r['mismatch'] for r in feedback_audit)} 次冻结父方案反馈来自默认 seed 23423；本次为保持已保存选择的来源完整，没有替换这些输入。其余反馈/历史字段也保持原记录。逐项记录见 frozen_feedback_seed_audit.csv。",
            *[f"- {r['case']} / {r['arm']}: {r['legacy_top10_pct']:.4f}% → {r['corrected_top10_pct']:.4f}%。" for r in affected],
            '', '修复输出失败只能保证选择可执行，不能保证选择更优。本次仍未证明 LLM 相对规则/随机的统计优势。完整的新搜索需用修正后的实测反馈重新调用选择器；当前保持原 30 条选择不变。','',]
        report_text=report_text[:start]+'\n'.join(fix_report)+'\n'+report_text[end:]
        report_text=report_text.replace('experiment/evaluate_saved_selections.py --preflight-only','experiment/evaluate_saved_selections.py --seed-corrected --preflight-only').replace('experiment/evaluate_saved_selections.py\n','experiment/evaluate_saved_selections.py --seed-corrected\n').replace('python3 experiment/analyze_saved_selections.py','python3 experiment/analyze_saved_selections.py --seed-corrected')
        report_text=report_text.replace('结果写入 experiment/llm_output_evaluation','结果写入 experiment/llm_output_seed_corrected')
        report_text=report_text.replace('原论文数据与 PDF 保持原版本。','原论文数据与 PDF 保持原版本，其中“全部候选固定 SUMO seed”的表述已被本次审计推翻，必须修订后才能投稿。')
        (OUT/'REPORT.md').write_text(report_text)
    print(json.dumps(dict(summary=summary,stages=dict(stages),spatial=spatial_summary),indent=2))


if __name__=='__main__':main()
