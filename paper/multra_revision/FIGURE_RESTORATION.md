# 原稿图片恢复记录（2026-10-04）

> **用户授权重绘更新：** Figure 1 现使用 `figures_current/Framework_updated.pdf`，由内置图像生成工具以原 `Framework.png` 为参考制作。保留原图风格，更新为现行流程；原始 `Framework.pdf` 继续保留。新图现采用生成式底图＋单独排版的 Arial Bold 矢量文字；文字可编辑，图标与底图仍为位图。最新样式更新已删除底部 Candidate budget 整块，并将背景改为白底、平面轮廓图标，弱化渐变与立体装饰；预算定义仍保留在方法正文。编辑源文件及重建方法见 `figures_current/Framework_typography.tex`、`figures_current/Framework_README.md`。下方原图恢复记录是之前步骤；最终提示词和技术记录见 `framework_redesign_prompt.txt`、`framework_redesign_record.json`。

原稿 `paper/chatsumo_optimizer/main.tex` 实际引用四张图片。此前整合新实验时遗漏了这四张图的展示内容；此次恢复框架与机制两张原图，并为现行结果更新两张结果图。原始目录未改动。

| 原稿引用 | 当前正文 | 处理 |
|---|---|---|
| `figures/Framework.pdf` | Figure 1，§3，`figures_current/Framework.pdf` | 按用户要求完整恢复原 PDF，逐字节一致；正文与图注说明当前初始化和联合反馈扩展，advisory judge/offline Pareto 不决定本轮结果。新绘框图仅作备用文件，不再替换主图 |
| `figures/Optimization_cropped.pdf` | Figure 2，§3.3，`figures_current/intervention_mechanisms_original.pdf` | 原 PDF 逐字节复制，SHA-256 相等；图注明确参数只是机制示意，并非本轮实测参数或结果 |
| `figures/proposal_source_comparison.pdf` | Figure 3，§5.1，`figures_current/proposal_source_comparison_current.pdf` | 保留按城市对比和成功率/候选占比的双面板结构；改用 `wide_feedback_v1` 的 45 个主实验 rep00 结果；不混入旧 9.18/9.00/9.60% 结果 |
| `figures/proposal_source_action_distribution.pdf` | Figure 4，§5.1，`figures_current/proposal_source_action_distribution_current.pdf` | 保留干预组成和方案来源双面板；新增 joint refinement 类别；用已保存候选记录逐案核对最终选择 |

已有自适应阶段/空间副作用图现在为 Figure 5，需求敏感性图为 Figure 6。当前正文共六张编号图。

## 数据及定义

结果图来自 `experiment/wide_feedback_v1/analysis/case_results.csv` 和 `candidate_results.csv`，仅主实验 repeat=0，每策略 15 案例。不增加仿真或 LLM 调用。

- 各城市改善柱为三个需求种子的均值和样本标准差，包括 no-op。
- 三种策略均成功 13/15；可接受候选占比分别为 LLM 78/203、随机 82/210、规则 72/199。这是已评估候选占比，包含缓存，并非新增 SUMO 仿真的效率，也不是独立统计样本。
- LLM 最终方案来源：初始 3、单方案变异 5、初始 bundle 2、联合修正 3、no-op 2。
- 最终组合类别将初始 bundle 和其联合修正后代合并；来源面板将二者分开。
- SHA-256、城市数值、分类计数与指标定义位于 `supplementary_current/figure_audit/`，随证据包交付。

## 重建

从项目根目录运行：

```bash
MPLCONFIGDIR=/tmp/simtio_matplotlib python3 experiment/restore_manuscript_figures.py
```

该步骤也已接入 `experiment/build_current_manuscript.py`，防止下次重建遗漏。随后按 README 编译 LaTeX，并运行 `python3 experiment/export_multra_documents.py` 同步 Word 和源文件/证据包。

所有新绘制图均保留 PDF 和 PNG；矢量图嵌入 TrueType 字体。机制图保持用户原始内容，没有使用图像生成工具改画或生成实验数据。恢复前正文及归档保存在 `paper/multra_revision_history/pre_figure_restoration_20261004/`。
