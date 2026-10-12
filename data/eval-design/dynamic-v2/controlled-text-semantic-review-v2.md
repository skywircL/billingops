# 受控文本变异独立会话语义复审 v2

**64条全部通过助手语义审查；独立人工复核仍为0。** 本次对象为`controlled-text-rewrites-v2.json`，SHA256：`112b1c56aa02ab0bffa5d95cacd6f85e15615003a4adb4126ee451a074a14f00`。

逐对象比对确认：相较v1，仅G22-L1-01与G30-L1-01两处请求译文发生变化，其余62条及所有控制字段不变。

| 修订 | 复审理由 |
|---|---|
| G22-L1-01 | `Could I get the extra 10 yuan back?`保留口语间接索补，避免额外加入明确的compensate分类词。 |
| G30-L1-01 | `The supervisor`保留原文未指明隶属关系的主管，不新增“客户上级”。 |

[v1首次审查](controlled-text-semantic-review-v1.json)的62pass/2fail原样保存；[v2逐条结果](controlled-text-semantic-review-v2.json)供入库校验按源哈希与variant_id核对。五条负向敏感性反例单独保存且不进入64条评测任务，不计作API或人工校准结果。

审查者：`assistant_independent_session_not_human`。英文范围仅为初始请求、条件回答及后续消息，环境和客户未知回答仍可能使用中文。同族变体均属已曝光开发数据。
