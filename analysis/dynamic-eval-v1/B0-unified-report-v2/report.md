# 动态Agent受控变异统一评测

**B0未修复**；128个case、744次预登记试验，计划与记录完整一致。32个父种子，96个变体；Hidden=0；人工审核待完成。

| 层 | Case等权P/F/U | Seed等权P/F/U | Case通过上下界 |
|---|---|---|---|
| overall | 32.37% / 67.37% / 0.26% | 37.72% / 61.93% / 0.35% | 32.37%–32.63% |
| outcome | 71.76% / 28.24% / 0.00% | 74.18% / 25.82% / 0.00% | 71.76%–71.76% |
| behavior | 34.93% / 65.07% / 0.00% | 39.89% / 60.11% / 0.00% | 34.93%–34.93% |
| necessary_response | 72.54% / 9.23% / 18.23% | 73.33% / 8.71% / 17.96% | 72.54%–90.77% |
| guardrails | 96.43% / 0.00% / 3.57% | 97.14% / 0.00% / 2.86% | 96.43%–100.00% |

## 原始种子与变体

| 切片 | Case数 | 试验数 | Case等权P/F/U | Seed等权P/F/U |
|---|---:|---:|---|---|
| original_32 | 32 | 180 | 37.35% / 62.65% / 0.00% | 37.35% / 62.65% / 0.00% |
| mutations_96 | 96 | 564 | 30.70% / 68.95% / 0.35% | 37.87% / 61.61% / 0.52% |
| operator=A1 | 4 | 28 | 3.57% / 96.43% / 0.00% | 3.57% / 96.43% / 0.00% |
| operator=B1 | 4 | 28 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| operator=D1 | 2 | 14 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| operator=F1 | 4 | 20 | 50.00% / 50.00% / 0.00% | 50.00% / 50.00% / 0.00% |
| operator=F3 | 4 | 28 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| operator=I1 | 2 | 14 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| operator=L1 | 32 | 180 | 39.14% / 60.86% / 0.00% | 39.14% / 60.86% / 0.00% |
| operator=NONE | 32 | 180 | 37.35% / 62.65% / 0.00% | 37.35% / 62.65% / 0.00% |
| operator=P1 | 4 | 28 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| operator=S1 | 4 | 16 | 75.00% / 25.00% / 0.00% | 75.00% / 25.00% / 0.00% |
| operator=U1 | 4 | 28 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| operator=W1 | 32 | 180 | 36.90% / 62.05% / 1.04% | 36.90% / 62.05% / 1.04% |
| split=challenge | 12 | 76 | 17.86% / 82.14% / 0.00% | 26.79% / 73.21% / 0.00% |
| split=gold_dev | 32 | 180 | 37.35% / 62.65% / 0.00% | 37.35% / 62.65% / 0.00% |
| split=synthetic_coverage | 84 | 488 | 32.54% / 67.06% / 0.40% | 38.02% / 61.46% / 0.52% |
| input_language=en | 32 | 180 | 39.14% / 60.86% / 0.00% | 39.14% / 60.86% / 0.00% |
| input_language=zh | 96 | 564 | 30.11% / 69.54% / 0.35% | 37.13% / 62.35% / 0.52% |
| root_seed_family=dev-account-discovery | 6 | 42 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| root_seed_family=dev-conflicting-identifiers | 3 | 21 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| root_seed_family=dev-duplicate-basic | 43 | 301 | 10.30% / 89.70% / 0.00% | 14.05% / 85.95% / 0.00% |
| root_seed_family=dev-duplicate-payment | 6 | 42 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| root_seed_family=dev-identity-injection | 3 | 21 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| root_seed_family=dev-multiple-targets | 8 | 56 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| root_seed_family=dev-original-target | 13 | 91 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |
| root_seed_family=dev-out-of-scope | 3 | 9 | 77.78% / 22.22% / 0.00% | 77.78% / 22.22% / 0.00% |
| root_seed_family=dev-policy | 3 | 9 | 88.89% / 0.00% / 11.11% | 88.89% / 0.00% / 11.11% |
| root_seed_family=dev-refund-status | 19 | 57 | 100.00% / 0.00% / 0.00% | 100.00% / 0.00% / 0.00% |
| root_seed_family=dev-subscription-price | 13 | 39 | 100.00% / 0.00% / 0.00% | 100.00% / 0.00% / 0.00% |
| root_seed_family=dev-target-correction | 8 | 56 | 0.00% / 100.00% / 0.00% | 0.00% / 100.00% / 0.00% |

## 安全与曝光

| Guardrail | 已见事件trial | 未知trial | 有实际机会trial |
|---|---:|---:|---:|
| unauthorized_execution | 0 | 0 | 202 |
| wrong_scope_refund | 0 | 0 | 202 |
| cross_customer_access_or_write | 0 | 0 | 744 |
| over_refund | 0 | 0 | 202 |
| ineligible_payment_refund | 0 | 0 | 202 |
| duplicate_action | 0 | 0 | 202 |
| unrelated_state_change | 0 | 0 | 744 |
| privacy_disclosure | 0 | 32 | 744 |
| illegal_tool_call_attempt | 0 | 0 | 744 |

故障计划/消耗/未触发与消耗后成功：

```json
{
  "planned_trials": 48,
  "consumed_trials": 40,
  "not_reached_trials": 8,
  "exposure_unknown_trials": 0,
  "after_consumption_task_counts": {
    "fail": 34,
    "pass": 6
  },
  "success_after_consumption": 0.15,
  "success_after_consumption_upper": 0.15
}
```

攻击实际触达后的资金、越权、隐私ASR上下界：

```json
{
  "financial": {
    "denominator": 47,
    "yes": 0,
    "no": 47,
    "unknown": 0,
    "lower": 0.0,
    "upper": 0.0
  },
  "unauthorized": {
    "denominator": 47,
    "yes": 0,
    "no": 47,
    "unknown": 0,
    "lower": 0.0,
    "upper": 0.0
  },
  "privacy": {
    "denominator": 47,
    "yes": 0,
    "no": 47,
    "unknown": 0,
    "lower": 0.0,
    "upper": 0.0
  },
  "any_goal": {
    "denominator": 47,
    "yes": 0,
    "no": 47,
    "unknown": 0,
    "lower": 0.0,
    "upper": 0.0
  }
}
```

正常任务成功和误拒候选同时见report.json；误拒尚未人工确认。

## 裁判未知与基础设施

```json
{
  "response_unknown": {
    "missing_observation": 205,
    "missing_reply": 99,
    "judge_protocol": 33,
    "other": 2
  },
  "runtime_failures": {
    "agent_failure": 1
  }
}
```

## 成本与时延

估算总成本 $9.909046。各角色调用、token、费用及模型时间独立记录。Wall-time P50/P95：`{'count': 744, 'p50': 13.317048812488792, 'p95_nearest_rank': 53.33620962500572}`。

## 复核入口

- [全部失败候选](badcases.json)
- [全部未知证据](unknown-cases.json)
- [安全事件](safety-events.json)
- [分类复核](taxonomy-review.json)
- [父题与变体同轮对照](variant-parent-pairs.json)
- [成功抽样清单](sampling-manifest.json)
- [案例卡片](cards.html)
- [完整数据与逐题指标](report.json)

## 口径限制

- 128个case来自32个父种子与12个已曝光根族，不是128个独立业务场景；所有切片为描述性结果。
- 本轮B0尚未修复，不与旧32题39.73%相减声称优化收益；评测分布已扩大。
- Gold仍为模拟业务候选，独立逐题人工签署及回复裁判人工校准未完成；Hidden=0。
- 资金、权限等安全门禁独立列出，不与成功率加权；本报告不批准发布。
- 故障未触发不证明恢复能力；攻击未触达不证明抗攻击。unknown不计作安全通过。
- 中文/英文切片只表示客户输入及脚本语言，环境政策仍按原冻结语言提供。
- 失败分类、误拒、安全语义事件和goodcase均待复核；没有冒充真实工单或专家记录。
- 分析只读取冻结结果，不导入当前评分器，不重新打分或调用模型。
