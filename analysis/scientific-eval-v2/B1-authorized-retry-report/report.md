# V2 评测指标补充报告

输入：`/Users/skywircl/Documents/ChatGPT/billingops/runs/dynamic-v2-B1-unified-2`。128 个 case，744/744 个预登记 trial。
原始评分、轨迹和版本全部保留。本报告新增观测口径，不代表当前 Agent 成绩或 Agent 改进收益。

实际资源统计包含 753 次执行；重试合并遵守原已授权登记，原中断目录保持中断身份。

发布门禁：**inconclusive**，release_permitted=False。

## 结果与统计区间

Pass@k / Pass^k 按固定 k 和 case 等权汇总；交换性/IID 未获证明时只解释为重复试验的描述性组合统计。
unknown 敏感性上下界与根族 bootstrap 区间分开。开发集区间不能证明生产泛化；缺失试验时不输出正式总体。

```json
{
  "status": "complete_registered_plan",
  "aggregates": {
    "case_equal": {
      "1": {
        "eligible_tasks": 128,
        "ineligible_tasks": 0,
        "pass_at_k": {
          "lower": 0.8020833333333334,
          "upper": 0.8374255952380952
        },
        "pass_pow_k": {
          "lower": 0.8020833333333334,
          "upper": 0.8374255952380952
        }
      },
      "3": {
        "eligible_tasks": 128,
        "ineligible_tasks": 0,
        "pass_at_k": {
          "lower": 0.9015625,
          "upper": 0.903125
        },
        "pass_pow_k": {
          "lower": 0.6705357142857142,
          "upper": 0.7569196428571429
        }
      }
    },
    "root_family_equal": {
      "1": {
        "eligible_tasks": 128,
        "ineligible_tasks": 0,
        "root_families": 12,
        "unresolved_family_tasks": [],
        "status": "available",
        "pass_at_k": {
          "lower": 0.8616726514905829,
          "upper": 0.9228480476185495
        },
        "pass_pow_k": {
          "lower": 0.8616726514905829,
          "upper": 0.9228480476185495
        }
      },
      "3": {
        "eligible_tasks": 128,
        "ineligible_tasks": 0,
        "root_families": 12,
        "unresolved_family_tasks": [],
        "status": "available",
        "pass_at_k": {
          "lower": 0.9712868280570239,
          "upper": 0.9733932326343587
        },
        "pass_pow_k": {
          "lower": 0.7001496922090557,
          "upper": 0.848626046705912
        }
      }
    }
  },
  "bootstrap": {
    "method": "root_family_percentile_cluster_bootstrap",
    "confidence_level": 0.95,
    "iterations": 2000,
    "seed": 20261012,
    "minimum_root_families": 8,
    "interpretation": "Exploratory development-family resampling. Intervals for lower and upper identification-bound estimands are separate; these are not production confidence guarantees.",
    "by_k": {
      "1": {
        "status": "exploratory",
        "eligible_tasks": 128,
        "root_families": 12,
        "intervals": {
          "case_equal": {
            "pass_at_k": {
              "lower_endpoint_ci95": [
                0.7241032557791015,
                0.9355474386724387
              ],
              "upper_endpoint_ci95": [
                0.7600069527402087,
                0.9706972789115647
              ]
            },
            "pass_pow_k": {
              "lower_endpoint_ci95": [
                0.7241032557791015,
                0.9355474386724387
              ],
              "upper_endpoint_ci95": [
                0.7600069527402087,
                0.9706972789115647
              ]
            }
          },
          "root_family_equal": {
            "pass_at_k": {
              "lower_endpoint_ci95": [
                0.7843423040619338,
                0.9312073586744638
              ],
              "upper_endpoint_ci95": [
                0.8572019215512943,
                0.9737535612535613
              ]
            },
            "pass_pow_k": {
              "lower_endpoint_ci95": [
                0.7843423040619338,
                0.9312073586744638
              ],
              "upper_endpoint_ci95": [
                0.8572019215512943,
                0.9737535612535613
              ]
            }
          }
        },
        "degenerate": false
      },
      "3": {
        "status": "exploratory",
        "eligible_tasks": 128,
        "root_families": 12,
        "intervals": {
          "case_equal": {
            "pass_at_k": {
              "lower_endpoint_ci95": [
                0.8104768762343647,
                0.998886827458256
              ],
              "upper_endpoint_ci95": [
                0.8125547235023042,
                1.0
              ]
            },
            "pass_pow_k": {
              "lower_endpoint_ci95": [
                0.5584357026392647,
                0.830898498664456
              ],
              "upper_endpoint_ci95": [
                0.658139841962986,
                0.9180980392156863
              ]
            }
          },
          "root_family_equal": {
            "pass_at_k": {
              "lower_endpoint_ci95": [
                0.9240814994496528,
                0.9984126984126984
              ],
              "upper_endpoint_ci95": [
                0.9249319631816572,
                1.0
              ]
            },
            "pass_pow_k": {
              "lower_endpoint_ci95": [
                0.5366598910065862,
                0.8438301081228712
              ],
              "upper_endpoint_ci95": [
                0.7435036203638943,
                0.9344747831303063
              ]
            }
          }
        },
        "degenerate": false
      }
    }
  }
}
```

statistics_by_operator 单独列出原始题（NONE）及每种扰动；总体分数代表预登记的混合开发分布。

## 过程与澄清

工具响应成功与语义正确分别计量；未有独立逐调用标签时语义正确率为 unknown。重复读取、提案作废、重新调查只作为候选诊断。

```json
{
  "tool_transport": {
    "numerator": 4117,
    "denominator": 4209,
    "known_denominator": 4209,
    "unknown": 0,
    "unknown_denominator_trials": 0,
    "rate": 0.9781420765027322,
    "lower_bound": 0.9781420765027322,
    "upper_bound": 0.9781420765027322,
    "weighting": "micro_by_registered_opportunity; not case_equal_task_success"
  },
  "tool_semantic_correctness": {
    "numerator": 0,
    "denominator": 4209,
    "known_denominator": 4209,
    "unknown": 4209,
    "unknown_denominator_trials": 0,
    "rate": null,
    "lower_bound": 0.0,
    "upper_bound": 1.0,
    "weighting": "micro_by_registered_opportunity; not case_equal_task_success"
  },
  "necessary_evidence": {
    "numerator": 3580,
    "denominator": 3594,
    "known_denominator": 3594,
    "unknown": 0,
    "unknown_denominator_trials": 0,
    "rate": 0.9961046188091264,
    "lower_bound": 0.9961046188091264,
    "upper_bound": 0.9961046188091264,
    "weighting": "micro_by_registered_opportunity; not case_equal_task_success"
  },
  "clarification": {
    "missing_information_ask_rate": {
      "numerator": 113,
      "denominator": 133,
      "known_denominator": 133,
      "unknown": 18,
      "unknown_denominator_trials": 0,
      "rate": null,
      "lower_bound": 0.849624060150376,
      "upper_bound": 0.9849624060150376,
      "weighting": "micro_by_registered_opportunity; not case_equal_task_success"
    },
    "premature_action_rate": {
      "numerator": 0,
      "denominator": 133,
      "known_denominator": 133,
      "unknown": 0,
      "unknown_denominator_trials": 0,
      "rate": 0.0,
      "lower_bound": 0.0,
      "upper_bound": 0.0,
      "weighting": "micro_by_registered_opportunity; not case_equal_task_success"
    },
    "effective_rounds": {
      "known": 743,
      "unknown": 1,
      "mean": 0.10228802153432032,
      "p50": 0.0,
      "p95": 1.0,
      "min": 0,
      "max": 1
    },
    "repeated_questions": {
      "known": 744,
      "unknown": 0,
      "mean": 0,
      "p50": 0.0,
      "p95": 0.0,
      "min": 0,
      "max": 0
    },
    "repeated_question_rate": {
      "numerator": 0,
      "denominator": 151,
      "known_denominator": 151,
      "unknown": 0,
      "unknown_denominator_trials": 0,
      "rate": 0.0,
      "lower_bound": 0.0,
      "upper_bound": 0.0,
      "weighting": "micro_by_registered_opportunity; not case_equal_task_success",
      "measurement": "lexical_repeat_candidate_only",
      "semantic_repeat_rate": null
    }
  }
}
```

## 资源与校准

完整 JSON 同时提供所有执行、成功且安全检查明确通过的试验：各角色调用数、tokens、模型估算费用、工具费用和 wall-time 的均值/P50/P95及缺失数。历史工具费用缺少明确报价时保持 unknown。

裁判抽样：110 条 / 3356 条原子判定。人工校准状态：`pending`。

将 human-review-template.json 分别交给独立复核者填写；高风险判定需要两名复核者。模板不含裁判标签。身份声明只是一项记录，发布还需要可信流程确认身份与独立性。

release-protocol-proposal.json 仅是待审的阈值提案，不是本历史运行的预登记，也不代表发布批准。现有输入没有未曝光 Hidden 验收证明。

## 文件

- [完整指标](report.json)
- [独立人工复核模板](human-review-template.json)
- [校准绑定与分层抽样](calibration-packet.json)
- [校准报告](calibration-report.json)
- [发布阻断理由](release-gate.json)
- [下轮协议提案](release-protocol-proposal.json)
