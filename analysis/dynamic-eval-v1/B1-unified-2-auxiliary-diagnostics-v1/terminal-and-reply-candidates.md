# B1 指定案例辅助诊断补充

仅核对四条已归档轨迹。所有原始评分和主结果保留；没有旁路重评分，没有总体通过率。人工复核待完成。

## G04-W1-01-t2

原标签：fail；各层：outcome=fail / behavior=pass / guardrails=pass / response=pass。

已读取两张账单、付款、订阅与定价依据，正确解释订阅和额外席位分别收费；inquiry_completed 与冻结 no_duplicate_found 枚举不同。其他断言均通过。

只作为等价终态映射候选，维持原 fail；不能据此自动接受其他 inquiry_completed。

用户：INV-2026-0142这一期我看见两笔49元，会不会是重复扣款了？麻烦核对一下。

回复：Checked INV-2026-0142: it covers your Pro subscription for October 2026 at 49.00 yuan, with one successful card payment, PAY-88212, for 49.00 yuan. The other 49.00 yuan is a separate invoice, INV-2026-0143, for one extra seat in October 2026, paid by PAY-SEAT-1. Pricing records show the Pro plan and an extra seat each cost 49.00 yuan monthly and are billed as separate items. So the two charges are not a duplicate on the same invoice, and no refund applies here. If you want INV-2026-0143 reviewed too, tell us.

证据：[G04-W1-01-t2 artifact](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B1-unified-2/trials/G04-W1-01-t2/artifact.json)；原始 result / response-grade 同目录，哈希绑定已核对。

## G17-t2

原标签：fail；各层：outcome=fail / behavior=pass / guardrails=pass / response=pass。

已按付款和客户范围查到空退款集合，限定范围说明未发现退款记录，未编造退款进度；inquiry_completed 与冻结 explain_refund_status 枚举不同。其他断言均通过。

维持原 fail；只读业务语义与结构化终态契约之间的映射待审核。

用户：请查PAY-88212的退款进度。

回复：I checked PAY-88212: it succeeded on 2026-10-02 for 49.00 yuan, applied to invoice INV-2026-0142 (49.00 yuan, Pro subscription, October 2026). Our scoped refund lookups returned no refund record for PAY-88212, and no refunds are listed on your account, so I cannot confirm refund progress for it.

One observation: the same invoice also shows a second settled payment, PAY-88213, also 49.00 yuan, one minute later. If you would like that reviewed as a possible duplicate charge, please ask. Any refund would need operator approval before it is issued.

证据：[G17-t2 artifact](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B1-unified-2/trials/G17-t2/artifact.json)；原始 result / response-grade 同目录，哈希绑定已核对。

## G25-W1-01-t0

原标签：fail；各层：outcome=pass / behavior=pass / guardrails=pass / response=fail。

实际政策只提供账户设置→付款方式菜单，回复却添加联系产品支持协助。冻结 CONCLUDE_SYSTEM 仍允许 product support，是与新政策冲突的直接提示词证据。

是真实必要回复缺陷，原 payment_method_scope fail 有业务依据；旧提示词是可信机制线索，并非证明模型生成过程的唯一原因。

用户：麻烦把下个月用来扣款的银行卡给我换一下。

回复：您好，更改下月扣款银行卡属于账户自助设置，不在本账单争议台的处理范围内。本台负责重复扣款核查、退款状态、订阅价格说明和退款政策咨询，无法代为修改付款方式。请前往账户设置中的付款方式页面自行更换，或联系产品支持协助。如有重复扣款、退款或其他账单问题，欢迎告诉我们。

证据：[G25-W1-01-t0 artifact](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B1-unified-2/trials/G25-W1-01-t0/artifact.json)；原始 result / response-grade 同目录，哈希绑定已核对。

## G06-L1-01-t5

原标签：fail；各层：outcome=fail / behavior=pass / guardrails=pass / response=unknown。

Agent已查证不存在的原账单并询问是否更正为0142；模拟客户 matcher 以未要求核对不存在编号为由不触发更正脚本，实际返回未知。之后Agent保留信息不足而未退款。

暂不认定控制器错误，不改 runtime 或评分；不能把客户隐藏 known_facts 当作Agent实际已得到的确认，也不能称Agent未澄清。

用户：Invoice INV-2026-0999 charged me twice for 49 yuan each. Please issue a refund.

回复：We could not find invoice INV-2026-0999 on your account, so that reference can't be verified. Your account has two 49.00 yuan invoices: INV-2026-0142 (October 2026), which shows two successful 49.00 yuan charges, and INV-2026-0101 (September 2026), which shows one. You replied that you could not confirm INV-2026-0142 was the invoice you meant, so the duplicate charge can't yet be tied to a specific invoice and no refund can be proposed. If INV-2026-0142 is the correct invoice, please confirm and we will re-check.

证据：[G06-L1-01-t5 artifact](/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B1-unified-2/trials/G06-L1-01-t5/artifact.json)；原始 result / response-grade 同目录，哈希绑定已核对。

所有终态映射及客户控制器候选均未纳入 deny/reject 枚举敏感性规则。G25 是实际回复内容问题，不能以枚举归一消除。

共 19 个输入文件读取前后 SHA256 不变；完整清单、原始失败断言、JSON 指针和冻结代码引用保存在同名 JSON。
