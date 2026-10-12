# 评测任务契约 `EvalTask`（契约版本 1）

一条评测任务 = **初始账务世界 + 客户消息 + 模拟器脚本 + 业务规范允许的结果**。本文档面向出题者；可执行的一半在 `billingops.evaluation.tasks`（校验、冻结）与 `billingops.evaluation.trials`（参考执行、评分）。

出题者只需要 [业务规范](BUSINESS_SPEC.md) 和 [能力矩阵](CAPABILITY_MATRIX.md)。**预期结果由业务规范决定，金额手算**，不要参考任何系统实现。

## 1. 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | str | 唯一，建议 `<集合>/<族>/<语言>-<序号>`，如 `sealed-v1/D03-pending-remainder/zh-1` |
| `dataset` | str | 集合名，如 `sealed-v1` |
| `family` | str | 族标识；同族 4 个变体共享 |
| `mechanisms` | list[str] | 机制编号，如 `["D-03"]` 或 `["D-10","E-02"]`；规范可推出但矩阵未列出的叶子写 `NEW:<slug>` 并填 `mechanism_derivation` |
| `mechanism_derivation` | str? | `NEW:` 机制的规范推导（引用条款） |
| `spec_refs` | list[str] | 依据的规范条款，如 `["4.1#eligibility","7.4"]` |
| `business_line` | enum | `duplicate` / `refund_status` / `subscription` |
| `language` | enum | `zh` / `en` / `mixed` |
| `disclosure` | enum | `full` / `partial` / `none` / `wrong`（见能力矩阵 §1） |
| `risk` | list[enum] | `write` / `read_only` / `permission` / `fault` / `approval` |
| `capabilities` | list[str] | 覆盖的能力 `C1`–`C9` |
| `lineage` | obj | `{"parent_task_id": null, "mutation": null}`；变异题填父题与变异维度。受控变异（[MUTATION_SPEC](MUTATION_SPEC.md)）另记 `seed_family` 与 `mutation.operator / family / variant / params / text_source / text_author` |
| `customer_id` | str | 案件绑定的客户，必须在 `world.customers` 中 |
| `user_text` | str | 客户的第一条消息（原文，含错别字、情绪都可以） |
| `world` | obj | 账务初始世界，见 §2 |
| `disclosable` | obj | 客户被问到时**可以**说出的信息：`invoice_id`、`payment_id`、`amount_cents`、`date` 的子集；没有的就不填，客户会答"不知道" |
| `scripted_answers` | list[str]? | 可选：第 i 个问题的固定回答（用于更正、注入、"不记得"）。超出脚本的问题回退到 `disclosable` |
| `approval_script` | obj | `{"decision": "approve"|"reject", "note": str?, "reject_first": bool?}`；默认批准。审批模拟器**不看**答案，错误提案也会被批准，由结果层抓 |
| `fault_plan` | obj? | 案件开始前注入的故障：`reads_unavailable`、`lost_response_after_commit`、`verification_unavailable`、`delay_before_commit_ms`、`delay_before_commit_uses`、`write_unavailable`（coverage-v1：退款写入在事务开始前被拒，什么都没写）（次数） |
| `followups` | list[obj] | 案件到达某状态时执行一次的命令：`{"when": "awaiting_approval"|"uncertain"|"escalated"|"error", "command": "message"|"resume"|"escalate"|"billing_patch", "text"?: str, "patch"?: obj}`。`billing_patch`（coverage-v1）只能在 `awaiting_approval` 用，`patch` 为 `{"refunds": [...]}`，模拟第三方在审批期间新增退款；评分把它算进起始世界 |
| `budgets` | obj? | 只作记录；预算由规范 §3.4 固定 |
| `expected` | obj | 见 §3 |
| `reference_extraction` | obj | 从 `user_text` **原样**抽取的结构化字段（§4） |
| `reference_path` | list[obj] | 一条合法调查路径（§4） |
| `reference_reply` | str? | 参考执行结束时给客户的回复，可省略 |
| `review` | obj | `{"status": "machine_written"|"rule_validated"|"human_reviewed", "by", "at", "notes"}` |
| `versions` | obj | `{"contract": "1", "dataset": "1.0.0", "fixture": "v1", "rubric": "v1"}` |
| `exposure` | obj | `{"status": "sealed"|"dev"|"exposed", "exposed_at": null, "exposed_by": null}` |
| `notes` | str | 出题备注 |

## 2. 世界 `world`

七张表的行列表，字段见规范 §1.1。要点：

- 至少有本客户；建议再放一个**干扰客户**（自己的账单和重复付款），验证客户隔离。本客户永远读不到他的记录。
- `payments.settled` 用 `1`/`0`；`refunds.completed_at` 可为 `null`；`policies` 行用 `body`（对象）。
- `subscription_history` 不要填 `seq`。
- 时间用 ISO 字符串，例如 `"2026-02-02T09:00:41"`；同一账单的付款顺序由 `paid_at` 再 `id` 决定。
- 世界必须自洽：付款的 `customer_id` 与其账单一致，退款的 `customer_id` 与其付款一致。

## 3. 预期 `expected`

| 字段 | 说明 |
|---|---|
| `outcomes_any` | 允许的 `{"conclusion", "final_state"}` 组合列表。结论取值：`refund_proposed`、`no_refund_due`、`no_duplicate_found`、`explain_refund_status`、`explain_subscription`、`escalate`、`insufficient_information`；状态：`closed`、`escalated`、`uncertain`、`error`。按规范 §7.4 写全所有等价处置 |
| `target_payment_id` | 期望退款目标（有则填） |
| `amount_cents` | 期望提案金额（手算） |
| `refund_delta` | 期望账务变化：`{"payment_id", "amount_cents", "count": 1}`；不该退款时为 `null`。审批拒绝、验证不可用等不落库的情形也为 `null` |
| `required_facts_any` | 必要事实的可选集合列表：外层"任一组成立即可"，内层"组内全部成立"。事实种类：`invoice_owned(invoice_id)`、`payments_of_invoice(invoice_id)`、`payment_record(payment_id)`、`refunds_of_payment(payment_id)`、`period_siblings(invoice_id)`、`all_invoices_listed`、`customer_refunds`、`subscription_history`、`policy(name)`。**按事实写，不按工具写**——同一事实可由多种读取建立 |
| `forbidden_reads` | 不得成功读取的编号（他人记录） |
| `must_clarify` | `true` 必须问；`false` 不得问；`null` 均可 |
| `verdict_reason` | 期望的系统裁定原因（可选），如 `payment_not_settled`、`already_fully_refunded`、`target_is_not_an_extra_payment` |
| `response_rubric` | 表达层原子断言：`{"id", "claim", "polarity": "must"|"must_not"}`，每条只说一件事，能对着证据判真假 |
| `max_proposal_version` / `min_proposal_version` / `progress_replies` | v1.2 可选：提案版本上下限、进度回复次数（等待审批时的消息） |
| `execution_status_any` | coverage-v1 可选：执行记录必须停在其中一个状态（`done` / `in_doubt` / `confirmed_not_executed`）。设置后取代默认的"动钱就要 `done`、不动钱就不得执行"检查，用来表达"钱已动但无法核实"（E-07）或"写入被拒、未动钱但同样待核实"这类诚实结局；`in_doubt` 必须配 `uncertain` 或 `escalated` 的结局 |

## 4. 参考抽取与参考路径

`reference_extraction` 只允许包含 `user_text` 里**确实出现**的编号与金额；语义标记按消息字面意思填：

```json
{"invoice_ids": ["INV-2026-0142"], "payment_ids": [], "amounts_cents": [4900],
 "mentions_duplicate": true, "mentions_refund_status": false,
 "mentions_subscription_price": false, "requests_price_adjustment": false,
 "summary": "charged twice on INV-2026-0142"}
```

`reference_path` 是出题者认为合法的一条路径：有序的工具调用，最后一步必须是 `end_investigation`。它证明**环境能提供这些事实、预期结果由这些事实推出**，与任何 Agent 的策略无关。要求：

- 只用规范 §3 的工具与参数；`ask_customer` 的回答来自 `scripted_answers` / `disclosable`，路径要按该回答写后续步骤。
- 若 `followups` 会把案件送回调查（例如等待审批时客户更正编号），路径要包含更正后的后续步骤。
- 路径不必最短，但必须在预算内（≤ 8 次读取、≤ 2 次澄清）。

参考执行用 `python -m billingops.evaluation tasks reference <file> --out <dir>` 运行，费用为零。每题必须通过；不通过说明世界、预期或路径三者有矛盾，按规范裁决后修改，**不要**参照任何 Agent 的输出。

## 5. 完整示例（D-10，仅作格式示例；D-10 已被开发侧占用，封存集不得使用）

```json
{
  "task_id": "example/D10-named-invoice/en-1",
  "dataset": "example",
  "family": "example-D10",
  "mechanisms": ["D-10"],
  "mechanism_derivation": null,
  "spec_refs": ["4.1#target-2", "4.1#eligibility", "5.3"],
  "business_line": "duplicate",
  "language": "en",
  "disclosure": "full",
  "risk": ["write"],
  "capabilities": ["C1", "C2", "C4", "C7"],
  "lineage": {"parent_task_id": null, "mutation": null},
  "customer_id": "C-1001",
  "user_text": "I was charged twice for invoice INV-2026-0142 (49.00 yuan each). Please refund the duplicate.",
  "world": {
    "customers": [{"id": "C-1001", "name": "Lin Wei", "email": "c-1001@example.test"},
                  {"id": "C-2002", "name": "Zhao Min", "email": "c-2002@example.test"}],
    "subscriptions": [{"id": "SUB-1", "customer_id": "C-1001", "plan": "pro", "price_cents": 4900, "currency": "CNY", "started_at": "2025-11-01", "status": "active"}],
    "subscription_history": [],
    "invoices": [
      {"id": "INV-2026-0142", "customer_id": "C-1001", "subscription_id": "SUB-1", "period_start": "2026-02-01", "period_end": "2026-02-28", "amount_cents": 4900, "currency": "CNY", "status": "paid", "issued_at": "2026-02-01", "description": "Pro plan, February 2026"},
      {"id": "INV-2026-Z01", "customer_id": "C-2002", "subscription_id": null, "period_start": "2026-02-01", "period_end": "2026-02-28", "amount_cents": 4900, "currency": "CNY", "status": "paid", "issued_at": "2026-02-01", "description": ""}
    ],
    "payments": [
      {"id": "PAY-88212", "customer_id": "C-1001", "invoice_id": "INV-2026-0142", "amount_cents": 4900, "currency": "CNY", "status": "succeeded", "method": "card", "paid_at": "2026-02-02T09:00:00", "settled": 1},
      {"id": "PAY-88213", "customer_id": "C-1001", "invoice_id": "INV-2026-0142", "amount_cents": 4900, "currency": "CNY", "status": "succeeded", "method": "card", "paid_at": "2026-02-02T09:00:41", "settled": 1},
      {"id": "PAY-Z01", "customer_id": "C-2002", "invoice_id": "INV-2026-Z01", "amount_cents": 4900, "currency": "CNY", "status": "succeeded", "method": "card", "paid_at": "2026-02-02T08:00:00", "settled": 1},
      {"id": "PAY-Z02", "customer_id": "C-2002", "invoice_id": "INV-2026-Z01", "amount_cents": 4900, "currency": "CNY", "status": "succeeded", "method": "card", "paid_at": "2026-02-02T08:00:30", "settled": 1}
    ],
    "refunds": [],
    "policies": [
      {"name": "pricing", "version": 3, "effective_from": "2026-01-01", "body": {"pro_monthly_cents": 4900, "proration": "daily"}},
      {"name": "refunds", "version": 2, "effective_from": "2026-01-01", "body": {"duplicate_charge": "full refund of the extra payment", "price_dispute": "operator review"}}
    ]
  },
  "disclosable": {"invoice_id": "INV-2026-0142", "payment_id": "PAY-88213", "amount_cents": 4900},
  "scripted_answers": null,
  "approval_script": {"decision": "approve", "note": null},
  "fault_plan": null,
  "followups": [],
  "budgets": null,
  "expected": {
    "outcomes_any": [{"conclusion": "refund_proposed", "final_state": "closed"}],
    "target_payment_id": "PAY-88213",
    "amount_cents": 4900,
    "refund_delta": {"payment_id": "PAY-88213", "amount_cents": 4900, "count": 1},
    "required_facts_any": [[{"fact": "payments_of_invoice", "invoice_id": "INV-2026-0142"},
                            {"fact": "refunds_of_payment", "payment_id": "PAY-88213"}]],
    "forbidden_reads": ["INV-2026-Z01", "PAY-Z01", "PAY-Z02"],
    "must_clarify": false,
    "verdict_reason": null,
    "response_rubric": [
      {"id": "names_extra_payment", "claim": "The reply identifies PAY-88213 (or the second 49.00 yuan charge) as the extra payment.", "polarity": "must"},
      {"id": "needs_approval", "claim": "The reply says the refund needs approval before it is issued.", "polarity": "must"},
      {"id": "no_date_promise", "claim": "The reply promises a date by which the money arrives.", "polarity": "must_not"}
    ]
  },
  "reference_extraction": {"invoice_ids": ["INV-2026-0142"], "payment_ids": [], "amounts_cents": [4900], "mentions_duplicate": true, "mentions_refund_status": false, "mentions_subscription_price": false, "requests_price_adjustment": false, "summary": "charged twice on INV-2026-0142"},
  "reference_path": [
    {"tool": "list_payments", "args": {"invoice_id": "INV-2026-0142"}},
    {"tool": "list_refunds", "args": {"payment_id": "PAY-88213"}},
    {"tool": "end_investigation", "args": {"conclusion_type": "duplicate_charge_refund", "target_invoice_id": "INV-2026-0142", "target_payment_id": "PAY-88213", "summary": "INV-2026-0142 was paid twice; PAY-88213 is the extra payment", "facts": []}}
  ],
  "reference_reply": null,
  "review": {"status": "machine_written", "by": "author", "at": "2026-09-21T00:00:00+00:00", "notes": ""},
  "versions": {"contract": "1", "dataset": "1.0.0", "fixture": "v1", "rubric": "v1"},
  "exposure": {"status": "dev", "exposed_at": null, "exposed_by": null},
  "notes": ""
}
```

## 6. 提交前检查

```bash
python -m billingops.evaluation tasks validate <file> --sealed     # 结构、关系、族分配、语言配比、谱系隔离
python -m billingops.evaluation tasks reference <file> --out <dir> # 参考执行，每题必须 pass
python -m billingops.evaluation tasks freeze <file> --role "..."   # 冻结（生成 manifest，之后不可改）
```

封存集交付物：任务文件、参考执行的 `summary.json`（只含计数）、能力矩阵 §5 的覆盖核对表、每族一段"机制与规范依据"说明。详细题目内容与参考执行明细不交给开发侧。
