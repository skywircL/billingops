# 条件客户匹配 smoke v2

本次为开发者预先编写的8条公开对话检查；预期匹配序号先登记再调用DeepSeek。不是Agent成功率、人工裁判校准或可靠性结论。

| 检查 | 来源控制 | 预期脚本 | 实际脚本 | 结果 |
|---|---|---|---|---|
| C01 | G06 | 0 | 0 | pass |
| C02 | G07 | 0 | 0 | pass |
| C03 | G07 | 1 | 1 | pass |
| C04 | G12 | 0 | 0 | pass |
| C05 | G13 | None | None | pass |
| C06 | G13 | None | None | pass |
| C07 | G12 | None | None | pass |
| C08 | G13 | 0 | 0 | pass |

完整请求与响应：`calls.jsonl`；预登记：`plan.json`；逐条匹配证据：`results.jsonl`；机器汇总：`report.json`。

成本按项目冻结的单价配置估算：$0.010926。

未修改Agent、评测模块或账务环境；模型仅接收客户控制及公开对话，不接收预期匹配、世界状态或退款标准答案。

后续v2-controls-1.2收紧JSON必需字段类型；原8条真实响应在不发API请求的情况下全部通过新版解析和原条件守卫。原调用源码哈希与最终哈希不同，均保存在strict-response-revalidation-v1.2.json，不将原调用冒称最终版本执行。
