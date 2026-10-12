# V2 评测指标、裁判校准与发布门禁

本轮补齐的是评测链路。指标计算、证据绑定、人工复核入口和发布阻断已经实现；真实人工标签、未曝光测试集验收和生产可靠性证据不能由代码生成。

参考：[科学评测 Agent：结果指标](https://brenchcc.github.io/Awesome-LLM-Research-Collections/notes/zh/agents/scientific-agent-evaluation.html#结果指标)。文章中的结果、过程、成本、安全、统计不确定性分别落地，不能用一个平均分互相补偿。

## 版本与接入

- Runner：`v2-runner-1.8`，运行前冻结 `measurement_contract` 和逐题 `process_contracts`；每个 trial 在模型调用前保存 `process-contract.json`。
- Trace scorer：`business-v2-trace-1.4`，新增独立 `tool_result_hallucination` 安全项。判为幻觉时，即使资金与回复总层通过也不能通过；没有判定证据则为 unknown。
- Response grader：`business-v2-response-1.4`，保存隐私裁判专属 rubric、证据索引、模型及其摘要，供后续校准绑定。判断规则不因保存元数据而变化。
- 指标入口：`src/billingops/evaluation/v2_metrics.py`；过程、统计、校准、发布分别位于 `v2_process_metrics.py`、`v2_statistics.py`、`v2_calibration.py`、`v2_release.py`。
- 历史补充入口：`scripts/report_scientific_v2.py`。只读冻结轨迹与裁判输出，使用新目录保存补充报告和本次测量源码，不导入当前 grader 重评历史成绩。

新 runner 的 `summary.json` 包含 `scientific_metrics`、`release_gate` 和真实的 `release_status`。中断运行保持原有停止流程，不生成整体成功率报告。

## 指标口径

| 维度 | 实际实现 | 解释边界 |
|---|---|---|
| 结果 | 固定 k=1、3 的 Pass@k、Pass^k；case 等权和根场景等权 | 不把 3 次、7 次“全部成功”混成同一个 k；Best@k 没有选择器，明确 N/A |
| 不确定性 | P/F/U、unknown 敏感性界限；按根场景做 2,000 次 bootstrap，固定随机种子 20261012，输出上下界估计量各自的 95% 区间 | unknown 上下界不是置信区间；根场景不足 8 个、未知、缺槽或完全退化时不输出误导性区间；开发集不能外推生产 |
| 扰动 | 按冻结 operator 分开统计；NONE 是原始题，其余算子各自列出 | 总体仍是原始题与变体的预登记混合分布，变体不是新增独立业务场景 |
| 步数 | 工作流节点数、事件数、模型调用数、调查工具请求数、Billing 操作数 | 不读取或声称测得模型内部思考步骤，不把这些不同单位直接相加 |
| 工具响应 | 捕获的读取调用中 `ok=True` 的比例；新 runner 另采集全部 Billing 调用 | 返回成功包含应用层判断，不能冒充语义正确率 |
| 工具语义正确 | 独立版本的逐调用标签：选对工具且参数正确 | 缺少 oracle/独立复核标签则为 unknown；不会由任务最终成功反推每一步正确 |
| 路径效率 | 同工具、同参数重复候选；重新调查、提案作废候选 | 重试、查证、客户改诉求可能必须如此；候选计数不能直接解释为浪费 |
| 必要证据 | 预定义事实 ID 的覆盖分子、分母、false、unknown 和具体缺失 ID | 新运行在执行前冻结分母；旧运行使用原 grader 固定检查项，不套新规则补造旧答案 |
| 澄清 | 需要信息的机会中实际询问比例；未解决必要信息便提案/发送的比例；有效澄清轮次；重复提问候选及比例 | 有效轮次必须连接真实答案、命令消费及已解决的信息缺口；无效编号和“知道了”不自动算解决；当前重复检测为字面归一化，语义重复仍待独立标签 |
| 资源 | 全部实际执行、成功且安全检查明确通过两组；agent/customer/judge 的 calls、四类 tokens、估算费用、模型耗时，以及工具费用和 wall time | 每项都有已知/缺失样本数、均值/P50/P95；费用缺失不填 0，wall time 不含之后的回复裁判 |

过程效率和资源使用同一个 `is_successful_safe` 判定：整体、outcome、behavior、response、guardrails 均通过；原子检查、必需回复和所有安全项均明确通过；无运行失败。不同视图中的安全或回复结果有冲突时不能进入该集合。

重复成功的组合估计：`Pass@k = 1 - C(n-c,k)/C(n,k)`，`Pass^k = C(c,k)/C(n,k)`。unknown 界限分别取 c=已知成功、c=已知成功+unknown。缺失槽保留在预登记分母，正式 aggregate 留空，仅列诊断界限。未证明试验交换性/IID 时只将这些值解释为观察样本的组合统计。根族 bootstrap 也不能修复共享限流、缓存或记忆造成的相关性。

## 工具采集与缺失处理

`TimedBilling` 在实际调用边界记录工具名、次数、耗时、返回/异常，包括内部批量退款预检和执行后查证。fixture 导入、评测器抓取世界状态使用未包装的客户端，不计入 Agent 操作。

新 runner 明确声明本地模拟 Billing 的工具服务费为零，主机计算成本不在此声明内。生产工具需要独立报价；历史运行没有该声明时不能补填免费。供应商错误调用缺少完整 usage/费用时，完整分布保留 missing，同时保存已捕获的部分费用。

工具正确率标签的可扩展契约：在执行前登记 `process_contract.tool_oracle_version`，通过独立 oracle 的 `process_observations.tool_judgments` 提供 `evidence_ref`、实际 `tool/args`、`oracle_version`、`source`、`correct_tool`、`correct_arguments`。每个证据必须唯一匹配；未标注、标签冲突、版本或参数不一致均为 unknown。当前历史资料不含这些标签，报告不会称其工具语义正确率已验证。

## 校准和发布

校准包按“安全/普通 × 原裁判 pass/fail/unknown”六类确定性抽样，每类最多 25 条，保存总体和抽样分母。每条绑定版本包、原始 artifact、回复、rubric 和证据摘要。

`human-review-template.json` 不含裁判标签与裁判理由。复核者填写独立性声明、判定、理由和真实证据引用。普通判定至少一名独立复核者；高风险判定至少两名不同复核者。缺人、分歧或缺证据不能变成金标。摘要错配、无效引用、重复身份、失效版本会被拒绝。

导入后报告混淆矩阵、失败 precision/recall、一致率、kappa 和样本区间。judge 对真实失败 abstain 不会被移出 recall 分母。区间注明独立审计项假设，共享同一 trial 的标签可能相关；不能冒充总体错误率。

历史隐私判定部分缺失原专属 rubric，因此存在 `binding_missing`。必须保留这项缺口；新版本已经保存所需字段，历史记录不能用当前 rubric 回填后冒充原始判定依据。

`evaluate_release` 返回 `pass/fail/inconclusive` 与 `release_permitted`：

- 任何安全或工具结果幻觉信号独立否决，不受高平均成功率抵消。自动语义信号仍标注尚未人工确认。
- unknown、缺失槽、重复槽、层间矛盾、未登记阈值/版本、校准待完成都会阻止放行。门禁保留冻结 P/F/U；基础设施 unknown 不因为下游行为失败被重计为 Agent fail。
- 发布需要执行前冻结的协议、匹配的完整版本包、足量未曝光 Hidden 任务及其可信来源，以及独立人审校准证据。
- 当前 CLI 没有身份认证或 Hidden 保密状态认证能力，所以不会把字符串声明升级成已完成认证。现有开发集输出 `inconclusive`、`release_permitted=false`。

`release-protocol-proposal.json` 是可审阅的下轮阈值提案，默认成功率 0.9、至少 20 个 Hidden task、至少 30 条完整校准标签及失败/安全失败覆盖。它不是当前历史运行的预登记，也不是业务已批准的发布标准；未来须在运行前由可信评测流程冻结，并提供真实人工独立性及 Hidden 来源证明。纯函数门禁可以接入该流程，不能通过修改报告中的布尔值来建立这些证明。

## 使用

普通完整 V2 冻结运行：

```bash
.venv/bin/python scripts/report_scientific_v2.py RUN_DIR --out NEW_REPORT_DIR
```

已授权的 B1 九次基础设施重试组合：

```bash
.venv/bin/python scripts/report_scientific_v2.py runs/dynamic-v2-B1-unified-2 \
  --retry runs/dynamic-v2-B1-unified-2-retry-1 \
  --baseline-report analysis/dynamic-eval-v1/B0-unified-report-v2 \
  --change-registry analysis/dynamic-eval-v1/B1-unified-2-comparison-change-registry.json \
  --out analysis/scientific-eval-v2/B1-authorized-retry-report
```

此路径复用原重试登记校验：原中断目录保持中断身份，按固定的 735 条原结果＋9 条重试结果计算 744 个逻辑槽；资源分布计算全部 753 次实际执行。没有重跑模型、选最佳结果或隐藏失败成本。

完成人审后使用相同输入与抽样参数，生成新的报告目录：

```bash
.venv/bin/python scripts/report_scientific_v2.py RUN_DIR --out NEW_REVIEWED_REPORT \
  --review reviewer-a.json --review reviewer-b.json --require-release-pass
```

`--require-release-pass` 在门禁 fail/inconclusive 时返回退出码 3，可用于阻断后续发布步骤。默认仅生成开发诊断报告，正常写出报告不等于获准发布。

## 验证产物

- [历史 B1 组合补充报告](../analysis/scientific-eval-v2/B1-authorized-retry-report/report.md)
- [机器可读指标与证据](../analysis/scientific-eval-v2/B1-authorized-retry-report/report.json)
- [独立人工复核模板](../analysis/scientific-eval-v2/B1-authorized-retry-report/human-review-template.json)
- [回归验证记录](../analysis/scientific-eval-v2/verification.json)

测试覆盖缺失/重复分母、冻结档案篡改、中断身份、幻觉否决、成本缺失、澄清未解决、同名视图冲突、人审分歧/绑定失效，以及 CLI 发布退出码。离线测试及历史回放证明机制按契约工作，不冒充当前模型新一轮成功率、人审已完成或生产验证。
