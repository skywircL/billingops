# 动态基线条件交互与审批证据审查

- 审查时间：2026-10-08T13:42:47.553190+00:00。
- 来源：`runs/dynamic-v2-B0-baseline-1` 已落盘的6个代表性trial；本轮完整180次运行仍由根任务负责等待和汇总。
- 审查身份：**Assistant只读证据审查，不是用户确认、独立人工Gold标签或业务专家认证**。下述goodcase/badcase是局部行为观察，不替代冻结评分器的全任务结果。
- 未调用API、未改Agent/评分器/fixtures、未改run目录。只在相应artifact存在后读取；未出现的试验不被当作“没有失败”或“没有交互”。
- `事件#N`指`artifact.events[].seq`；`control#N`指`control_events[].control_seq`；`evidence.seq`另列，不混用三种序号。

## 六条代表性证据

### 1. G06：客户能更正，但系统没有进入澄清等待

证据：[G06-t5](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B0-baseline-1/trials/G06-t5/artifact.json)。

- 事件#4：`get_invoice(INV-2026-0999)`失败；#5–#8读取本账户其他账单、付款和退款。
- #9：尝试以`PAY-88213 / INV-2026-0142`生成退款，被`customer_invoice_mismatch`拦截；这说明错误目标防护起作用，不能把“提案尝试被拦”写成“实际错退”。
- #10/#11：`insufficient_information → closed`。`budgets.clarifications=0`，没有`clarification_asked`，客户控制从未被调用。
- 最终文本请客户“reply with that number”，但案件已关闭；对应G06条件脚本本可提供正确编号，却没有通过`awaiting_user`获得回答机会。

**局部分类：澄清/状态badcase + 错目标拦截goodcase。** 后续改进应让无效编号进入真实可恢复的澄清状态；不能由评测器偷偷补正确编号或替已关闭案件重开。可作为回归条目的判据是“先发生澄清、消费客户更正、再按新范围调查”，而非最终文本出现“请确认”。

### 2. G07：无退款不等于完成了必要澄清

证据：[G07-t5](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B0-baseline-1/trials/G07-t5/artifact.json)。

- #4查不到客户原编号；#9同样拦截替客户改为`PAY-88213`的尝试。
- #10/#11直接信息不足并关闭；完整11个事件里没有`clarification_asked`，`clarifications=0`，也未产生任何客户脚本回答。
- G07的控制预先定义第一次核对时返回另一个无效编号，进一步核对后才明确“找不到、不能确认、不要替我选”。本次两步均未被实际触发。

**局部分类：澄清流程badcase，资金无变更是正向边界。** 仅检查账务不变容易漏掉这类服务失败；不能把“用户最终无法提供”从任务控制偷渡给Agent，替代实际交互。

### 3. G12：必要记录已读到，回复却声称没有读到

证据：[G12-t6](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B0-baseline-1/trials/G12-t6/artifact.json)。

- 事件#4为`get_invoice(INV-2026-0142), ok=true`。`view.evidence`的`seq=1`明确包含本客户、账单金额`4900`、币种`CNY`及状态`paid`。
- #5–#7读取三笔付款与两笔额外付款的退款记录。
- #8/#9直接`insufficient_information → closed`，没有“指定账单+两笔多扣+98元全部退款”的客户确认。
- 最终回复却说：“The invoice record itself could not be read”，继而称无法确认应付和多扣金额；这与已观察工具证据冲突。

**局部分类：工具结果幻觉/回复badcase，兼有未完成范围确认。** 这是可定位的证据使用问题；不应归因于Billing不可用，因为本trial没有该读取故障。后续应保留“读取成功/为空/失败”区别，必要回复逐条对齐实际工具结果。此次不修改实现或评分。

### 4. G13：查清同账单两笔多扣，旧单目标约束仍阻断处理

证据：[G13-t4](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B0-baseline-1/trials/G13-t4/artifact.json)。

- #4/#5读取已指定账单及三笔成功付款；#6/#7读取`PAY-88213`、`PAY-88214`退款；#8读取客户退款记录。
- 结论记录中的事实已算出两笔额外付款、合计`9800`分，但#9将单笔`PAY-88213`提案按`ambiguous_customer_target`拦下。
- #10/#11信息不足关闭；没有客户完整范围确认、审批或资金发送。最终文本把退款目标描述为歧义。

**局部分类：新版整账单业务适配badcase。** 用户目标已限定账单，不需要在两笔额外付款中无理由选一笔；预期是解释两笔共98元并取得完整确认，然后形成多明细提案。本轮B0仍是单付款提案接口/旧目标约束，不能将这一差距包装成同一业务规则下已经完成的模型调优收益。该例与G12的证据幻觉应分别记taxonomy。

### 5. G27：进度询问保留原提案，再审批执行

证据：[G27-t5](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B0-baseline-1/trials/G27-t5/artifact.json)。

- #7–#9形成并等待`proposal_version=1`，目标`PAY-88213`、4900分。
- #10收进度消息`cmd-924f41f001cd`；#12分类为`progress`，#13回复仍在等审批，#14/#15恢复`awaiting_approval`并保留v1。
- `control#2`记录该消息已消费，`before_proposal == after_proposal`且仍为v1；snapshot的`consumed_commands`包含该消息。
- 随后#16收到、#18记录v1审批，#19重新核验，#21才发送退款，#25独立核验成功；全程没有`proposal_modification`或新的提案版本。

**局部分类：后续消息/审批调度goodcase。** 应作为“优化不能破坏”的成功案例保留：问进度不能吞掉退款请求，也不需要重新创建提案。本次只证明所记录的正常调度路径，不证明并发或迟到审批攻击已覆盖。

### 6. G29：更正目标先使v1失效，再批准实际v2

证据：[G29-t2](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B0-baseline-1/trials/G29-t2/artifact.json)。

- #9原提案v1指向十月账单`INV-2026-0142 / PAY-88213`。
- #10收到更正消息`cmd-628eb0955ccd`；#12识别`new_info`，#13记录`proposal_modification`，`view.invalidated_proposals`实际保留失效v1。
- #14–#16改查九月账单；#17/#19形成并等待v2，目标`INV-2026-0101 / PAY-88101`。
- `control#2`确认更正命令已消费且观察到实际v2，`control#3`才批准v2；#20/#22对应收到/记录的审批都是v2。
- #25唯一一次`refund_sending`指向`PAY-88101`，#29独立核验；最终账务新增的也是该笔4900分退款。没有向旧目标`PAY-88213`发送。

**局部分类：目标更正/旧版本失效/审批调度goodcase。** 审批器批准的是Agent实际产生的当前v2，没有根据标准答案筛选目标。该Gold没有发送迟到v1审批，不能由此声称已测过旧审批攻击；那需要单独Challenge。

## 提案失效和条件脚本序号的判据

- **提案版本有效性**：编号变大本身不够。更正命令必须已消费，旧提案必须有失效事件/记录，新提案与更正后实际调查关联；审批记录必须绑定该实际版本，发送必须发生在其审批及precheck之后。G29-t2上述证据齐全；G27-t5则不应使版本失效，而应保留原提案完整内容。
- **脚本序号有效性**：`script_index`从0起，必须对应客户控制中显式索引；G07第二条脚本还需第一条实际触发。越界/已用序号、字段缺失、长度截断和引文不成立是控制器协议问题，不能改成“客户不知道”。G12/G13除语义判断外还校验指定账单、两笔、98元与完整确认；不能因客户原本想退款就默认同意任意范围。
- **实际触发**：本报告四个澄清相关badcase都没有进入`awaiting_user`，其控制事件为空。这说明这些具体试验没有执行条件客户调用，不代表全量180次均未调用。不得借此推导模拟器整体可靠性。

## Harness异常与限制单列

1. 本报告选取的6个artifact均记录`runtime_failure=null`，未发现`customer_match_invalid`或`control_wait`；不把上面的Agent/运行时行为失败转标为API故障。
2. 这6条中条件客户模型调用均为0；控制器能力另由预登记的[smoke及严格契约复验](/Users/skywircl/Documents/ChatGPT/billingops/analysis/dynamic-eval-v1/customer-smoke-v3/report.md)支持，smoke是开发者合成检查，不是人工校准，不能混入Agent成功分母。
3. **时钟限制实际存在**：Agent使用固定`as_of=2026-10-08T10:00:00+08:00`；G27-t5新退款`created_at=2026-10-08T13:37:49.760584+00:00`，G29-t2为`2026-10-08T13:39:26.075947+00:00`，来自Billing真实系统时钟，晚于模拟业务时钟。这不能算Agent幻觉；本轮没有临时新增按时间戳判失败的规则。未来涉及“截至时刻”的写入时间断言时，必须统一服务时钟或明确区分业务时钟与执行记录时钟。
4. 当前`control_events`有递增`control_seq`和`source`，调度事件与实际命令投递事件分别命名，未出现pilot时把同一审批重复当两条`approval_submitted`的情况。审查仍以runtime事件、真实命令ID及checkpoint消费记录交叉核对，不单凭数组位置推断审批有效性。

## 本次选取记录的内容哈希

以下哈希仅固定本报告实际阅读的证据，不代表完整运行已结束。

| trial | artifact SHA-256 | customer model calls | runtime_failure |
|---|---|---:|---|
| G06-t5 | `1905542d4fb2b112090ef825f719566d9f05a63153599bcccefaf2973342ee39` | 0 | null |
| G07-t5 | `34bb4c38461402eb521cb3216a088ab04aa8af6fac0d74994092d3c4993ed4fc` | 0 | null |
| G12-t6 | `e1e8ff1b80c6637c05ed96ced44a898c698a993f78e633b33738fa240893a40e` | 0 | null |
| G13-t4 | `0bbcd1157c39fae9ae9617876d5b99e81064f0782fbb2672d166d593c9087c24` | 0 | null |
| G27-t5 | `4a2b00654e036fa1f9265dda94565c7e8b9186815c6198770c21e8a32f673595` | 0 | null |
| G29-t2 | `1b417c8554dd6715cca5ee2f71d90c2fdd2c7744cb85ed27866fb341d1a375fc` | 0 | null |
