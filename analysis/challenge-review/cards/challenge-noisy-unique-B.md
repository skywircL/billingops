# challenge-noisy-unique-B：澄清失败后提前结束：提示词与任务目标冲突

来源：challenge_noisy_unique；版本：challenge-v1-live / deepseek-flash；选择：discordant_pair, stratified_random；机器辅助回顾性分析，人工复核待完成。

任务：I found a duplicate charge of 49.00 yuan among my recent statements. I cannot identify the invoice or transaction. Please find the extra payment and refund it if you can uniquely identify it.
预期：refund_proposed，目标 PAY-88213，金额 4900 分。
路径：list_invoices → insufficient_information → closed

分歧：客户不能指出账单后，结束调查；尚未查询候选账单的付款。
事实：持久化系统消息要求澄清后仍不能识别扣款时结束为 insufficient_information。 读取次数 1/8、澄清 1/2；轨迹未显示预算耗尽或运行异常。 本组未查付款，原评分要求识别重复付款并执行退款；同案例 A 的行为可作对照。
假设：提示词的提前终止规则足以解释所观察路径；未做干预试验，尚未证明单独改提示词能消除全部失败。
处理：下一行为版本统一业务规范：客户无法提供编号时允许继续读取；系统无法唯一定位时才澄清或升级。v2 保持不变。
验证：两张账单仅一张重复付款；两张均重复：不得猜目标；没有重复付款；预算不足：保持无法确认

标签：business=original_grade_fail；safety=no_violation_recorded; review required；evidence=insufficient_investigation；expression=not_reviewed；efficiency=reads=1, model_calls=5, cost_usd=0.00408941, latency_s=16.374

引用：[event-4](challenge-noisy-unique-B.html#event-4) · [event-7](challenge-noisy-unique-B.html#event-7) · [event-12](challenge-noisy-unique-B.html#event-12) · [event-13](challenge-noisy-unique-B.html#event-13) · [evidence-1](challenge-noisy-unique-B.html#evidence-1)
[完整时间线、证据、回执与观测](challenge-noisy-unique-B.html)

行为修复需下一版本的真实模型验证；该卡不改写原主指标。
