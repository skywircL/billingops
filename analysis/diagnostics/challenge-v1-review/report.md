# 独立挑战集案例复盘

来源：challenge-v1-live。配对分布：{'both_pass': 5, 'a_only': 1, 'b_only': 2, 'both_fail': 0}；复核 8 对。

这是 v2 之后新增的探索性挑战集，受公开评测方法启发；不是官方 benchmark 成绩。未调整 Agent 提示词或策略。人工复核待完成。

分支组合用于发现机制，不代表随机业务分布，不能用其失败比例估计线上失败率。每个 trial 的应用状态、Billing 终态和摘要随运行保存，重评分报告列出实际覆盖率。

模型服务在 temperature=0 时仍可能变化。这里只运行一次，不声称稳定性已量化；追踪开启后的时延也不能直接与未追踪的 v2 比较。

## 已发现的机制

- [challenge-ambiguous-target-A](cards/challenge-ambiguous-target-A.html)：目标仍有歧义，却选中一笔符合金额规则的退款。
- [challenge-noisy-unique-B](cards/challenge-noisy-unique-B.html)：澄清失败后提前结束：提示词与任务目标冲突。
- [challenge-price-credit-wording-A](cards/challenge-price-credit-wording-A.html)：要求退还涨价差额，却仅解释订阅。

详见 index.html、cards/*.md、rescore/report.md。新增挑战保留在待修复集合，不加入必须通过的回归集。

## 全部差异案例对

| 案例 | A 原评分 / 结论 | B 原评分 / 结论 |
|---|---|---|
| challenge-ambiguous-target | [False / refund_proposed](cards/challenge-ambiguous-target-A.html) | [True / insufficient_information](cards/challenge-ambiguous-target-B.html) |
| challenge-noisy-unique | [True / refund_proposed](cards/challenge-noisy-unique-A.html) | [False / insufficient_information](cards/challenge-noisy-unique-B.html) |
| challenge-price-credit-wording | [False / explain_subscription](cards/challenge-price-credit-wording-A.html) | [True / escalate](cards/challenge-price-credit-wording-B.html) |
