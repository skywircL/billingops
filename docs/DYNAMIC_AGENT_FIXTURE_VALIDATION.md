# 动态 Agent V2：业务修订与账务环境验收

日期：2026-10-08。本轮按用户“将以上问题解决，然后进入下一步”的要求，已落实32条候选的业务修订，进入完整账务环境构建和离线验收。32/32账务环境参考操作通过，全套代码测试275项通过（含36项新契约测试）。最终验收数字和执行包摘要见[机器记录](../analysis/dynamic-eval-v1/fixture-stage-validation-v1.json)。这份记录不包含新一轮真实模型成绩。

## 本轮落地内容

- [规范 V2](BUSINESS_SPEC_V2.md)与[SOP V2](BUSINESS_SOP_V2.md)区分付款凭证、明确交易限制、账单范围、客户确认及操作员审批。同账单多扣款应完整处理；默认付款排序不是天然的退款资格限制。
- [32条审阅表](DYNAMIC_AGENT_GOLD_REVIEW.md)已同步。G08用付款凭证定位账单；G09只核查暂不退款；G12/G13处理同账单两笔多扣共98元，并保护其他账单。
- G04补独立收费依据；G10/G11限定月份及套餐；G19补历史退款来源、申请时间与业务时钟；G20–G24统一政策事实；G25/G31移除无依据升级。G28/G29正常交互与迟到旧审批攻击分开。
- D02历史部分退款续退、D06未清算执行隔离，不进入新版核心成绩；历史资产和原防护保留。
- 修复审批模拟器：空拒绝备注仍为空，缺失备注用中性文案；实际错误提案不能由读取金标的模拟器提前过滤。旧Runner显式拒收新格式，防止静默丢掉条件脚本和多笔预期。

## 新增环境契约

[版本化fixture](../data/tasks/dynamic-v2/gold-fixtures-v0.2.json)包含32套完整Billing世界、业务时钟、目录快照、客户条件脚本、审批与后续消息描述、明确授权范围、逐笔退款预期和事实引用。目录来源为[候选JSON v0.3.0](../data/eval-design/dynamic-v1/gold-candidates-v0.3.0.json)。

这不是旧版 `EvalTask`，也不是已接入的Agent任务。`agent_execution_ready=false`，`agent_metrics=null`。构建器使用本版32题的明确模板，不能把任意目录文字自动编译成可信答案；受控扩写需要另外登记算子和新版本。

独立金额规则同时核对单笔余额和整张账单超收额度。校验器还检查客户归属、对象关联、金额和币种、时间、政策正文、事实引用及目录完整性。固定Gold中的授权范围、审批决策和预期不能被一起改写以绕过检查。新增负向测试覆盖错目标、少退一笔、超额、重复、跨客户、时间错配、只读误写和审批脚本漂移。

## 参考操作的含义

[本地Billing参考报告](../analysis/dynamic-eval-v1/fixture-reference-v2/report.json)保存每题真实HTTP读取、退款请求、按幂等键独立核验，以及完整操作前后状态。验证的是账务环境能否表达正确结果：12题需要资金操作，共14笔；其余20题保持账务不变。G12/G13各退两笔4900分，G13上月多扣保持原样。

参考执行者使用作者定义的范围和独立规则直接操作测试Billing，因此并未验证Agent能否识别范围或获得授权。审批、条件式客户回答、后续消息调度、人工交接、回复事实评分、账单级原子额度保护和故障恢复均未在此执行，不能以“32/32环境通过”表述Agent成功率或安全率。

## 指定维度对照

[对照差异报告](../analysis/dynamic-eval-v1/controlled-pairs-v1.json)记录：

- G08/G09账务世界、时钟和根族一致，主因素为客户是否申请立即办理退款；授权、预期资金变化和回复要求据此联动。
- G12/G13用户输入、客户脚本、审批、后续交互、退款范围及目标明细一致；世界仅新增上月账单的一笔重复付款 `PAY-88101`。本案仍只退本月两笔。

这是两组具体对照的检查，不等于80条Synthetic Coverage或通用变异引擎已经完成验收。所有候选属于已曝光开发侧，未创建或查看Hidden题。

## 复现与下一环节

```bash
.venv/bin/python -m billingops.evaluation.business_v2 build \
  --catalog data/eval-design/dynamic-v1/gold-candidates-v0.3.0.json \
  --out /tmp/billingops-gold-fixtures-review.json
.venv/bin/python -m billingops.evaluation.business_v2 validate \
  /tmp/billingops-gold-fixtures-review.json
.venv/bin/python -m billingops.evaluation.business_v2 reference \
  /tmp/billingops-gold-fixtures-review.json --out /tmp/billingops-fixture-reference-review
.venv/bin/python -m pytest -q
```

输出文件和目录必须尚不存在；重跑另取名称，保留旧运行。当前正式记录以v0.2及reference-v2为准；v0.1/reference-v1保留首轮验收历史，不能用旧格式验证结果替代当前校验。

下一环节是把环境契约接入动态Agent评测：实现客户条件回答与后续消息调度、必要回复原子断言、V2评分和任务适配。新增多笔退款、仅核查等运行能力要与旧缺陷修复分别登记，再运行DeepSeek开发pilot和B0基线，保存badcase/goodcase证据。B0源码快照保留原样。业务修订授权、程序验收、逐题人审、真实模型评测分别记账；当前没有独立逐题人审标签，也没有新的成功率提升数字。
