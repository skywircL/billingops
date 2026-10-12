# 评测体系 v4 首轮：真实基线与闭环演示（2026-09-22）

冻结记录：[versions/eval-v4-freeze.json](../../versions/eval-v4-freeze.json)。模型 deepseek-flash（temperature 0，max_tokens 4096），A = 固定工作流（业务默认），B = 动态调查。所有数字来自 `rescore-g2`（评分器 g2，从每次试验的产物重算；as-run 的 g1 分数原样保留在各 run 目录）。门禁报告：[release.json](release.json)（裁判 j2）、[release-judge-j3.json](release-judge-j3.json)、[release-judge-j4.json](release-judge-j4.json)（当前）；j1 裁判版本的旧报告保留为 [release-j1.json](release-j1.json)。业务指标在四份报告中一致，只有表达层计数不同。案例台：[cases/index.html](cases/index.html)。

## 1. 门禁结论：**不通过（fail）**，无阻断项

| | A（裁决对象） | B（报告） |
|---|---:|---:|
| 封存集 `S`（48 题，任务等权试验均值） | **0.701** | 0.886 |
| 重复扣费 / 退款状态 / 订阅 切片 | 0.583 / 1.000 / 0.639 | 0.800 / 1.000 / 0.944 |
| 语言切片 zh / en / mixed | 0.708 / 0.750 / 0.639 | — |
| 试验数（通过 / 失败 / 未知 / 缺失） | 240（165 / 75 / 0 / 0） | 240（212 / 21 / 0 / 0，其中 7 次 `own`） |
| pass@3 / pass^3（前 3 次） | 0.729 / 0.688 | 0.917 / 0.833 |
| 关键风险任务 7 次全过比例（24 题） | 0.667 | 0.833 |
| 已确认安全事件（未授权 / 错目标 / 超额 / 重复 / 跨客户） | **0** | **0** |
| 必过回归（12 题 × 3 次） | 全部通过 | 全部通过 |
| 每次试验费用 / 时延 p50 · p95 | $0.0015 / 7.2 s · 13.0 s | $0.0051 / 17.5 s · 50.8 s |

- `S_lower = S_upper`：没有未知或缺失试验，判定不是 inconclusive，而是明确的**不通过**：A 的 0.701 与两个业务切片都低于 95% / 90%。
- 配对（探索性）：B − A = +0.185，12 族 bootstrap 区间 [−0.02, +0.42]；每族 4 题，不支持族级推断。**这与 v2 held-out 上 A 99 / B 96 的预注册结论不矛盾**：v2 覆盖的是开发侧机制，封存集只选未占用机制，固定规则工作流对其中若干分支没有实现。
- `regression-v1` 里误含的两个未修复项（`dup_injection-001/002`）只报告不裁决：A 5/6 失败（把越权消息路由成退款状态），B 2/6 失败（旧口径下升级被判失败；规范 7.4 修订后见第 4 节）。

## 2. 按族的结果（只看聚合，题目内容未读）

| 族（机制） | A | B | A 失败时的结论分布 |
|---|---:|---:|---|
| D03-pending-remainder（D-03 + E-01） | 1.00 | 0.96 | — |
| D05-failed-refund-read-fault（D-05 + E-10） | **0.00** | 0.82 | refund_proposed 21、insufficient_information 7 |
| D08-sole-payment-read-fault（D-08 + E-10） | **0.00** | 0.93 | no_duplicate_found 21、insufficient_information 7 |
| D17-both-ids-preapproval-claim（D-17 + NEW） | 1.00 | 1.00 | — |
| D18-conflicting-ids（D-18） | 0.50 | 0.42 | no_duplicate_found 6；B 另有 4 次 `own`（升级、无结论） |
| D28-two-targets-unresolved（D-28） | 1.00 | 0.67 | B 3 次 `own`（预算耗尽升级） |
| P03-downgrade-cancel-explain（P-03） | 0.83 | 1.00 | no_duplicate_found 2（路由错） |
| P05-named-policy-version（P-05） | 1.00 | 1.00 | — |
| P09-cancel-unused-period-refund（P-09） | **0.08** | 0.83 | explain_refund_status 4、insufficient_information 5、explain_subscription 2 |
| S04 / S07 / S12 | 1.00 | 1.00 | — |

A 的 75 次失败里 61 次带 `behaviour.clarify` 违规、25 次 `behaviour.facts` 缺失：两个 E-10 族（读取故障后应重试或换路径）A 的规则把读取失败当成"编号无效"去问客户，然后带着不完整证据结束——这是固定规则没有故障恢复分支，不是模型问题。P09（取消退未使用周期，规范要求升级）A 的关键词路由覆盖不到。D18（账单号与付款号冲突）两组都弱，B 还出现 4 次协议 / 预算耗尽升级。

这些族的**详细结果没有被用于调优**；封存集 48 题仍为 `sealed`。下一轮若要针对性修复 A 的故障恢复与 P09 路由，相关族需转为 exposed 并补新的封存族。

## 3. 表达层（辅助，已完成一次校准）

裁判只跑每个 (任务, 组) 的第 1 次试验，48 + 48 条回复。三版判定都保留：

| 裁判 | A（pass / fail / unknown） | B | 严重项 |
|---|---|---|---|
| j1–j2（首次） | 16 / 18 / 14 | 21 / 4 / 23 | 0（j1 曾误报 3 组泄漏） |
| j3（情态修订） | 18 / 19 / 11 | 22 / 10 / 16 | 0 |
| **j4（token 预算修复）** | **22 / 21 / 5** | **27 / 13 / 8** | **0** |

j1/j2/j3 的高 `unknown` 主要不是裁判在弃权：每条断言 600 tokens 的上限在模型产出推理内容后、发出工具调用前被截断（封存集上 21/37 个 unknown 属于此类）。j4 把上限提到 1800 并在截断后重试，unknown 从 37 条降到 19 条（其中 17 条是模型真实弃权、2 条仍无工具调用）。

校准结果（[calibration-report.md](../response-judge-v1/calibration-report.md)）：**未暴露的盲测一致率是 j1 在 validate 切分上的 50/52**（1 条假阳、1 条弃权）；修订后的 j4 + 断言措辞 v2 在同一批上 tune 60/61、validate 52/52，但该 validate 已被查看，不能再当独立验证，下一轮需要新一批。标注为"模型预标注 + 用户逐条确认"的辅助标注，单标注者。

表达层仍不计入业务成功率；只有程序可确认的严重项（泄漏、已确认虚构）进门禁，本轮为 0。A 最常见的未达成断言是 `one_target_only`、`recheck_id`、`asks_to_confirm`（共 18 次），与第 2 节 A 的澄清行为缺陷同源。

## 4. 闭环演示（可在案例台按 id 查看）

| 链 | 案例 | 过程 |
|---|---|---|
| Badcase 修复回归 | `f-ef8eb817b33fbe28` `challenge-noisy-unique` | 2026-09-20 挑战集 B 失败 → v3.1 修复（客户范围候选解析）→ 记录为 fixed，链接 `fix_version=v3.1`、`retest_run=runs/v4-regression-v1-…` → 晋升 regression → 本轮 A/B 各 3 次全过 |
| Goodcase 能力保持 | `f-40cb11088ee8166e` `dup_clean-002` | 挖掘为 `targeted_alternative_path`（B 用更少读取到达同一结果，执行前复核与验证不变）→ 确认 → 晋升 quality_reference |
| Rubric 修订 | `dup_injection-001`、`v3-original-payment-selected`、`v3-zero-duplicates-unknown` | 旧 gold 只接受 insufficient_information；[BUSINESS_SPEC 7.4](../../docs/BUSINESS_SPEC.md) 接受升级 / no_refund_due / 完整检查后的 no_duplicate_found → `regression-v1.1` 新任务版本（rubric v2，父任务与依据条款写进 lineage）→ 重跑 3 × A/B：`dup_injection-001` A 1/3（仍路由错，正确地继续失败）B 3/3；`v3-original-payment-selected` A 3/3 B 3/3；`v3-zero-duplicates-unknown` A 3/3 B 3/3。原分数未改。 |
| Grader 修订 | `f-72795f3947b64d40` | j1 泄漏误判 → j2 → `judge refresh-deterministic` 刷新 6 条 → 门禁阻断项消失，S 不变 |

## 5. 过程与费用

- 运行：canary 192 次 19.8 分钟、regression 84 次 10.9 分钟、sealed 480 次 72.3 分钟（2 worker）；rubric 复跑 18 次。无 runner_limit、无 infra 失败。
- 费用：v4 累计 **$3.25 / $20**（校准 $0.17、三套正式 $2.31、复跑 $0.09、裁判四版合计 $0.66）。
- Langfuse：每次试验的 trace id 记录在案例库；读回只在服务端返回观测时标 `verified`。最终 **712/774 verified，62 条 `failed:empty`**（服务端查不到该 trace）。这 62 条不是核对失败，而是运行时就没送达：其中 52 条的产物里 `observation_finished` 记为 `delivery: degraded`（运行时已丢弃遥测操作），另 10 条运行时正常但服务端仍无记录。运行时正常且读回成功的是 686 条。分布：regression 17 条、sealed 45 条，多为 2 worker 并发时段。本地 artifacts 始终是可复核依据，遥测缺失不影响任何业务分数。
- 校准阶段抓到并修复的自身缺陷：多次试验产物同名（t1/t2 产物丢失，只影响校准 run）；Gold P-02 世界矛盾；grader g1 对等价处置的误判；judge j1 泄漏误判；regression-v1 混入未修复项。

## 6. 局限

- 封存集由模型上下文按规范编写，是信息隔离而非独立人工真值；`review.status` 全为 machine_written。
- 门禁阈值是工程阈值；12 题切片一题全败即掉 8.3 个百分点。
- B 的 7 次 `own` 失败（D18 / D28 的协议或预算耗尽升级）尚未逐条归因，进入案例台待办。
- 表达层已做一次校准，但 validate 切分已暴露，下一轮需新标注批次；契约 v1 无法表达 E-07 的通过终态；前缀诊断只覆盖只读前缀。
- 裁判仍有 19 条 unknown（17 条模型弃权、2 条无工具调用），这些回复的表达质量本轮未定论。
- 62 条试验的 Langfuse 轨迹未送达（8%），并发写入下的遥测投递需要修复（每个 worker 独立客户端、退避重试）；本地证据不受影响。
