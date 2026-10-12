# 表达裁判校准标注（response-judge-v1）

40 条回复、113 条原子断言。请在 `labels.json` 里为每条断言填 `human`：`true`（断言对这条回复成立）、`false`（不成立）、`unknown`（凭回复与证据无法判断）。`sheet.md` 是同一内容的可读版本。

规则：
- 只看回复原文和 `items.json` 里的证据提示，不看裁判的判定（裁判判定保存在 `judge-verdicts.json`，标注完成前不要打开）。
- 断言是关于"回复说了什么"，不是"好的回复该说什么"。回复没提到的主题：对 must 断言标 `false`，对 must_not 断言标 `false`。
- `split` 已按种子分好：`tune` 用于调整裁判提示词，`validate` 固定用于报告一致率。不要改动。
- 标注完成后运行：`python -m billingops.evaluation judge calibrate --judged <judge.jsonl 或合并文件> --labels analysis/response-judge-v1/labels.json`。

单标注者参考标签；报告时写计数，不写小数点后两位。
