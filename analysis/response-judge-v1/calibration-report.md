# 表达层裁判校准（response-judge-v1，2026-09-22）

40 条回复、113 条原子断言。标注来源：**模型预标注，由用户在会话中逐条确认**（`labels.json` 每条带 `annotation_provenance`）。这不是独立盲标，也不是多标注者一致性；`assistant-draft-manifest.json` 保留了预标注的原始建议与其摘要。分层与 tune/validate 划分在任何裁判判定可见之前就按种子固定（`items.json` 生成时），标注人看不到裁判判定（判定存放在 `judge-verdicts.json`）。

人工判定分布：true 54 / false 59 / unknown 0。断言含义：`human` 表示"断言对这条回复成立"，`must_not` 不反转真值（`must_not` 断言为 `false` 才是合格回复）。

## 一致率

| 裁判版本 | 切分 | n | 一致 | 裁判 unknown | 假阳（裁判 true / 人工 false） | 假阴 |
|---|---|---:|---:|---:|---:|---:|
| j1（首轮正式批次使用） | tune | 61 | 58 | 3 | 0 | 0 |
| j1 | **validate** | 52 | **50** | 1 | 1 | 0 |
| j3（情态措辞修订） | tune | 61 | 58 | 3 | 0 | 0 |
| j3 | **validate** | 52 | **51** | 1 | 0 | 0 |
| j4（token 预算 + 重试提示） | tune | 61 | 60 | 0 | 0 | 1 |
| j4 | **validate** | 52 | **52** | 0 | 0 | 0 |
| j4 + 断言措辞 v2 | tune | 61 | 60 | 1 | 0 | 0 |
| j4 + 断言措辞 v2 | **validate** | 52 | **52** | 0 | 0 | 0 |

`unknown` 计入 n 但不计入一致。样本量小、单标注者，报告计数而非小数点后两位。

## 分歧与弃权

- **唯一实质分歧（j1，validate）**：`hist::v3-unverifiable-target::B` 的 `claims_handoff_or_completion`。回复写的是"An operator must decide whether that duplicate relates to your request"，裁判读成"声称已转交"，人工判为未声称。**这是情态（必须发生）与事实（已经发生）的混淆。**
- **裁判弃权 4 条**：3 条模型主动填 `unknown` 且未给理由；1 条（j3 新增可见）模型两次都没有调用 `record_verdict`——j1/j2 会把它静默记成 `unknown`，j3 单独标为 `no_tool_call`，与真实弃权分开。**诊断结果：这不是弃权，是每条断言 600 tokens 的上限在工具调用之前被推理内容耗尽**（同一条断言在 1600 tokens 下正常给出判定，`stop_reason` 由 `length` 变为 `tool_calls`）。封存集上这类 `no_tool_call` 占 21/37 个 unknown。
- **j4 的唯一分歧（tune）**：`says_unverified` 断言原措辞是"回复说记录无法核实 / 定位"，而这条回复找到了记录、但无法确定是否应退。标注人按"是否应退未能核实"判 true，裁判按"记录已定位"判 false。这是断言措辞缺陷，不是裁判缺陷。

## 据此做的修订

**j3**（情态与协议）
1. 裁判提示词区分情态与事实：「an operator must decide」「needs approval」「we will check further」描述的是尚需发生的事，不构成"已转交 / 已支付 / 已完成"的断言。
2. `claims_handoff_or_completion` 断言措辞改写，把上面这句排除条款写进断言本身。
3. 没有工具调用时重试一次，仍无则记 `no_tool_call`（与弃权、与模型错误三者分离）。

**j4**（token 预算）
4. 每条断言的上限由 600 提到 1800 tokens——本模型在工具调用前先产出推理内容；截断时重试并明确告知"上一轮 token 用尽，请直接给判定"。annotation 集上 `no_tool_call` 与 `unknown` 归零。

**断言措辞 v2**（`GENERIC_RUBRIC_VERSION = "v2"`）
5. `says_unverified` 改为"回复说明依据现有记录无法解决该请求——记录找不到，或记录已读但不能确定是否应退——而不是说不欠"。这是把标注人在 tune 切分上写明的理解写进断言，不是新增判定标准，因此原标注仍然有效。

j3/j4 的修订依据来自 tune 切分（两条同类弃权、一条截断、一条措辞分歧）；但提升同时出现在 validate 切分上，而我已经查看过其中的分歧条目，**因此这套 validate 切分自此视为已暴露**：下一轮要用新的一批回复重新划分 validate，才能再次得到未经查看的一致率。

## 口径结论

- 表达层保持**辅助质量层**：程序可确认的严重项（泄漏、已确认虚构）进门禁，普通表达问题单独展示，不计入业务成功率。
- 可报告区间：**未暴露的盲测是 j1 的 50/52**（其中 1 条假阳、1 条弃权）；修订后的 j4 + 断言 v2 在同一批上 52/52、tune 60/61，但这批 validate 已暴露，不能当作独立验证。单标注者、113 条断言，不足以支撑跨模型或跨版本的裁判可靠性主张。
- 首轮正式批次的表达层数字由 j1/j2 产生，已依次用 j3、j4 重跑（`runs/v4-sealed-v1-deepseek-flash/judge-j3/`、`judge-j4/`）；三版都保留，门禁用 `--judge-name` 选择读哪一版。

## 复现

```bash
P=.venv/bin/python
$P -m billingops.evaluation judge ingest-labels --sheet analysis/response-judge-v1/sheet.md \
    --labels analysis/response-judge-v1/labels.json --by <annotator> --provenance "<how these labels were produced>"
$P -m billingops.evaluation judge rejudge-items --folder analysis/response-judge-v1 --tasks data/tasks/calibration-v1.json
$P -m billingops.evaluation judge calibrate --judged analysis/response-judge-v1/judge-j3.jsonl \
    --labels analysis/response-judge-v1/labels.json
```

`labels.blank.json` 是未填写的原件；`calibration-j1.json` / `calibration-j3.json` 是两次校准的完整输出（含逐条分歧）。
