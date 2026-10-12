# τ Retail：5 题接入试跑

固定 upstream `b7ea9074c1cba482b30687fecdb5c8425fd6f619`（1.0.1），使用官方 LLMAgent、官方用户模拟器、原任务与政策。Agent 和用户均为 `deepseek-flash`；NL judge 也显式改用该模型。**这是独立环境接入试跑，不是 BillingOps 的成绩，也不与默认 judge 的排行榜直接比较。**

选择 base 前5题 0–4，每题1次；seed300、temperature0、max_steps100、concurrency1。5题中4题reward=1；规模和选题方式不支持能力或稳定性推断。

实际 tasks.json 的 reward_basis 是 `DB + NL_ASSERTION`，与当前 docs/evaluation.md 所说 Retail 默认 `DB + COMMUNICATE` 不同。以固定版本任务和实际 reward_breakdown 为准；0/1题没有自然语言断言，该项为缺省通过。评分代码与任务均未改。

| 任务 | DB | NL | 总分 | 消息数 |
|---|---:|---:|---:|---:|
| 0 | 1.0 | 1.0 | 1.0 | 22 |
| 1 | 1.0 | 1.0 | 1.0 | 20 |
| 2 | 1.0 | 1.0 | 1.0 | 22 |
| 3 | 0.0 | 1.0 | 0.0 | 18 |
| 4 | 1.0 | 1.0 | 1.0 | 27 |

## 失败案例 3：用户模拟器在确认后立即终止

消息序号从0开始：

- 消息13：`get_order_details` 找到 `#W4776164` 中蓝色 S / cotton / v-neck 的 T-Shirt。
- 消息16：Agent 找到紫色 S / polyester / v-neck 替代项，计算差价 $0.05，并请求确认。
- 消息17：模拟用户同意修改并扣差价，但在同一消息输出 `###STOP###`。
- `termination_reason=user_stop`，没有后续 Agent 回合，也没有执行 `modify_pending_order_items`；DB=0，NL=1。

可确认机制是用户模拟器提前结束，使执行机会消失；不能仅凭总分0就归因为Agent不会调用修改工具。保留0分和完整轨迹，不事后删除此题。这里的文本来自模型生成的合成用户。

迁移到 BillingOps 的启示：把“同意执行”和“任务已完成”分开；用户模拟器必须等到写入及验证后才结束。未来扩展多轮用户模拟器时，增加批准后立即STOP的独立故障测试。当前 BillingOps 的确定性 ApprovalSimulator 不使用文本STOP，所以本次未改它。

## 可复现与限制

运行入口 `scripts/run_tau_pilot.py`，须使用独立的 `benchmarks/tau2-bench/.venv/bin/python`。核心安装另需 `websockets==17.1`、`audioop-lts==0.2.2` 才能在此 Python3.13 环境导入；未安装语音/RL训练框架，未修改 upstream 文件。

[完整输出](results.json)、[运行配置](manifest.json)、[逐题资源](summary.json)、[日志](run.log)均保留。原始SDK费用字段为0是 LiteLLM没有识别模型价格，**不是免费**。summary里的费用用BillingOps配置单价估算，只含Agent+用户对话，不含NL judge；不与BillingOps只算业务Agent的费用直接比较。

下一步不宜直接扩大到全量：先固定更可靠的用户模拟器配置，检查批准后结束的协议，再在新批次重跑。若要测BillingOps，应另做Retail域适配和公平的A流程；本轮未把官方LLMAgent称作自己的Agent。
