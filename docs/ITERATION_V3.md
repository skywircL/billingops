# v3.1 行为版本与复现

旧版本：`versions/pre-v3/`；中间v3：`versions/v3/`；最终源码摘要：`versions/v3.1-freeze.json`。

目标选择独立于退款金额：`investigation/target.py` 只接受用户真实消息/后续更正中的编号，或完整账号候选范围内唯一的额外付款。不能用模型自己输出的目标证明客户意图。推断范围及摘要进入审批指纹，执行前重查；歧义或读失败不执行。定点付款仍允许只用客户范围内的付款列表建立关系，不强制固定工具顺序。

调查读8、澄清2、动态模型调用14、协议/提案修复2限制保留。候选不完整的提前提案返回缺失编号，消耗现有修复额度。范围重查属于执行前复核，与原有三项执行前读取一样，不计入调查读取；对已验证的有限候选范围重查，不无限扩大。

`requests_price_adjustment` 在共享提取步骤产生；固定路由优先处理该语义字段，避免相互矛盾的布尔标记把退差价误送到退款状态查询。结论输入共享，新增账户记录，不接收B的自由推理。

## 验证

```bash
.venv/bin/pytest -q
# 老录制只能配老源码；不会拿新prompt读取旧cassette
PYTHONPATH=versions/pre-v3/src .venv/bin/python -m pytest -q versions/pre-v3/tests/test_real_replays.py
# 真实复现需新输出目录；旧结果不可覆盖
.venv/bin/python -m billingops.evaluation run --cases-file data/challenge_cases_v1.json --model openai --out runs/my-v31-challenge
.venv/bin/python -m billingops.evaluation run --cases-file data/v3_validation.json --model openai --out runs/my-v31-branches
```

3条当前真实录制位于`data/replays/real-v3.1`。澄清录制的manifest明确记录实际传入的answer；回放复用该字节串，不能根据另一个问题重新生成不同的答案。原录制、v3开发失败和v3.1最终试验均保留。

完整结果与局限见[迭代报告](../analysis/v3.1-iteration/report.md)，剩余工作见[backlog](../analysis/v3.1-iteration/backlog.json)。本轮没有证明稳定性、总体优越性或因果上仅改prompt就能解决问题。
