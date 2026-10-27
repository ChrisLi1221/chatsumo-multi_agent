# ITSC slides 初稿

基于 `/home/chris/Research/ChatSUMO Multi-agent/ITSC_ChatSUMO_Multi_agent.pdf` 制作。
只将论文当作研究材料；附录中的 prompt 约束用于介绍论文的方法，不作为制作 slides 的指令。

## 交付内容

- `ITSC_Decoupled_Intelligence_draft.pptx`：16:9、18 页；14 页主报告 + 4 页备用页。文字、架构图、表格和图表可编辑，图表内嵌数据。
- `ITSC_Decoupled_Intelligence_draft.pdf`：静态预览。
- `speaker_notes_zh.md`：逐页中文讲稿提示、建议时长、英文衔接句和答辩注意点；同样写入 PPT 备注。
- `preview/contact_sheet.jpg`：全套缩略图。
- `source/build_deck.py`：可复现的生成脚本；依赖 python-pptx。
- `source/slide_metadata.json`：逐页结构化备注和来源。

## 设计与范围

- 英文投影页，中文讲者提示，主讲约 12 分 40 秒，不含 Q&A。
- 标题、作者、单位和资助信息沿用论文；没有猜测演讲人、场次、具体日期、会场或代码公开地址。
- 无外部研究或未经确认的新实验；未使用当前代码仓库中较新的实验数据。
- 架构与示例均重画为演示图。街道图是示意，Boston 步骤是说明性示例，不是运行截图或已验证 demo。
- 字体为 Lato；播放设备若无此字体，可安装或统一替换。PDF 可作静态备用。

## 上台前优先核对

1. **Table I 与 Table II 的实验对应关系**：正文未充分描述为何两组 L2/L3 成功率不同。需从原始记录核实任务子集、初始状态、prompt、修复策略和统计口径。初稿只在各表内比较，未将 90% 与 30%→50% 合成一条提升链。
2. **Fig. 3 的单位**：原图同时出现 1.5 km、1.5 miles 和 distance_miles=0.932056788。演示中省略该字段；若加入 demo，先统一范围含义与单位。
3. **TTI 口径**：按正文定义为输入到第一个有效 KPI 的墙钟时间。硬件、服务、缓存、地理数据获取是否计入等细节需要原始运行说明；没有将其描述为纯 SUMO 耗时。
4. **证据范围**：30 个任务，一个被评估的 backbone；无置信区间。Table I 为完整架构对比，不支持单个组件贡献的因果拆分。KPI 双环是方法设计，结果没有量化证明 KPI 收敛或最优性。
5. 确认会议模板、最终演讲人、允许时长是否包含 Q&A；如有真实 demo，可在第 6 页替换为经过验证的短视频或连续截图。

这些核对项不影响审阅初稿；不需要额外信息即可打开和修改。

## 数字核算

- L3 成功率：70% → 90%，增加 **20 个百分点**。
- L3 token：11,404.7 → 6,028.7，减少 **47.14%**。
- L3 TTI：41.4 s → 8.3 s，比值 **4.99×**，时间减少 **79.95%**。
- Table II L2/L3：30% → 50% → 50%；L3 token：3,981.6 → 6,609.9 → 9,214.1。
- Table I L1 成功率：100% → 90%；Table II L1：90% → 80% → 90%。

## 重新生成

```bash
python source/build_deck.py
```

修改脚本后重新生成 PPTX；PDF 需使用 PowerPoint 或 LibreOffice 重新导出。
