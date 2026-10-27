# LLM 输出失败：诊断、修复与复验（2026-09-27）

后续交通评估另存目录，未覆盖论文实验。初次评估 Top-10 均值 8.72%，成功 11/15，但实际配置审计随后发现旧重路由会丢失 SUMO seed；该数值不能作为完全固定 seed 的证据。配置继承已修复，三组条件重放与逐候选审计见 [修正配置后的 30 条选择与 bundle 评估](llm_output_seed_corrected/analysis/REPORT.md)。下文的“未运行 SUMO”仅描述输出诊断阶段。

## 结论

已复现并确认一个直接原因：feedback 调用将 **500** 传给 `max_completion_tokens`，这个上限包含 reasoning 和可见输出。预算耗尽时，模型还没有生成最终 JSON，API 返回 `finish_reason=length` 和空字符串。旧代码没有检查终止原因，直接解析空字符串，因此将其误记为 `LLM output is not JSON`。

固定全部 15 个主实验案例的 30 条原始请求，只将预算从 500 调整为 4096，总共重放 60 次。模型仍为 `gpt-5.6-luna`，system/user 内容、catalog、反馈和 JSON 模式不变；无传输重试、无规则兜底、无 SUMO 调用。默认模型采样未固定，因此这是同一输入集合上的重新采样，不能把两次调用当成完全相同的随机实现。

| 输出预算（含 reasoning） | 合法 catalog 选择 | 失败 | 平均实际 completion tokens | 实际范围 |
|---|---:|---:|---:|---:|
| 500 | 18/30（60%） | 12/30 | 440.97 | 323–500 |
| 4096 | 30/30（100%） | 0/30 | 470.67 | 327–699 |

本次 12 个失败响应全部满足：HTTP 200、`finish_reason=length`、`completion_tokens=500`、`reasoning_tokens=500`、`content=""`。这是输出预算耗尽的直接证据，不是网络异常、拒绝、非法 mutation ID，或已生成完整 JSON 后解析器误读。4096 只是上限，并不意味着每次消耗 4096 tokens。

原主实验的 10 条失败请求在 4096 预算下全部得到合法选择。30 条新响应进一步通过真实 `run_feedback_refinement` 路径验证：生成的提示词与存档逐项一致，默认预算正确传入，ID 能解析成原 catalog 中的动作，并通过现有 executable validator。验证没有执行候选仿真。

## 历史证据能说明什么

对原主实验和重复实验的全部 54 条调用审计得到：32 条成功、21 条空可见内容、1 条 HTTP 400 明确提示达到输出上限。主实验单独为 20 条成功和 10 条空内容。按角色，signal 有 16 条空内容及 1 条上限报错，network 有 5 条空内容，routing 没有失败。

旧日志没有保存 `finish_reason` 和 reasoning token 明细，因此**不能追溯证明那 21 条历史空响应全部由同一原因造成**。本轮固定输入重放确认了这个机制，并验证扩大预算在测试集合内消除了故障。30/30 是观察结果，不是未来调用零失败的保证。

## 已修改的代码

- `agents/llm_agents.py`：每次 HTTP 尝试记录终止原因、原始可见内容、完整 usage（含 reasoning tokens）、输出预算、HTTP 状态、请求/响应 ID、模型和耗时。日志不包含请求授权头，并脱敏返回内容中的本地密钥。
- 使用 `LLMOutputError.category` 区分预算耗尽、空内容、无 choices、拒绝/过滤、意外工具调用、格式错误等。解析前先检查终止状态；即使截断内容碰巧能解析成 JSON，也不接受它。预算耗尽或拒绝不会用同一参数自动重试。
- `agents/debate_optimizer.py`：feedback 的默认预算从 500 提高到 4096；参数可覆盖，实际值写入 refinement 记录。保留原提示词和完整输出契约，避免同时改变选择政策。
- `agents/joint_optimizer.py`：联合方案修正同样传递该预算。
- 两个优化实验入口支持 `--feedback-max-completion-tokens`，默认 4096。常规实验的 `00_metadata.json` 保存逐次响应诊断。
- `experiment/revision_matched.py` 显式保持 500，以保留已交付论文的历史协议；不把修复后的新方法悄悄混入旧实验。
- `experiment/diagnose_llm_output.py`：独立审计、固定请求重放、结果统计和真实解析/验证路径的离线复验。

回归验证：**127 tests passed**。新增测试覆盖预算耗尽（包括看似完整的 JSON）、usage 保存、空内容与格式错误的区分、拒绝、HTTP 上限错误、日志脱敏、默认/显式预算，以及联合方案预算传递。

## 复现

在仓库根目录执行，现有本地配置读取 API key，不在命令中填写：

```bash
# 以下两个步骤只读已有日志/响应，不发 API 请求。
/home/chris/.conda/envs/chatsumo/bin/python experiment/diagnose_llm_output.py audit
/home/chris/.conda/envs/chatsumo/bin/python experiment/diagnose_llm_output.py verify

# 全部 30 条固定请求 × 两种预算；已完成结果会校验哈希后复用。
/home/chris/.conda/envs/chatsumo/bin/python experiment/diagnose_llm_output.py run --budgets 500 4096
/home/chris/.conda/envs/chatsumo/bin/python experiment/diagnose_llm_output.py summarize

# 独立重复时使用新目录，避免覆盖这次证据。
/home/chris/.conda/envs/chatsumo/bin/python experiment/diagnose_llm_output.py run \
  --budgets 500 4096 --output-dir experiment/llm_output_diagnosis_replication

python3 -m pytest -q
```

日后使用 `experiment/run_optimization_only.py` 或 `experiment/run_proposal_source_comparison.py` 时，默认采用修复后的 4096；也可显式添加 `--feedback-max-completion-tokens 4096`。需要复现旧预算时指定 500。其他参数仍按原实验设置，新的交通实验使用独立输出目录。

数据在 `experiment/llm_output_diagnosis/`：

- `archive_audit.json`：54 条历史请求的分类和来源。
- `replays/`：60 条新响应的终止原因、raw content、usage 和请求哈希。
- `requests.csv`、`summary.json`：汇总与同输入配对记录。
- `pipeline_verification.json`：30 条新响应通过实际 catalog 解析/验证的记录。
- `source_hashes.json`：诊断与修复代码快照。

## 对论文结论的影响

本轮只证明输出可靠性改善，没有重新运行候选 SUMO 或更新 Top-10 改善率。旧稿的 8.23% 等结果仍属于 500-token 协议。后续评价 LLM 优化能力时，应在独立实验中使用修复后的预算，重新执行选择后的候选与 bundle，再与相同搜索结构的规则/随机比较。不能将旧的空响应率解释为模型天然不能遵循 JSON，也不能把这次 30/30 合法输出说成交通效果提高。

参考：[OpenAI 官方 reasoning 指南](https://developers.openai.com/api/docs/guides/reasoning)说明推理也占用生成预算，可能在产生可见输出前耗尽预算；本项目的具体故障由本轮保存的 Chat Completions 响应直接确认。
