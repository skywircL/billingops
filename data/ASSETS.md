# 评测资产分类

机器可读版本：[assets_manifest.json](assets_manifest.json)。分类规则：

- **exposed（已暴露）**：参与过调优、复盘、回归或任何被开发侧阅读过详细结果的资产。保留原始分数与产物；可作为 Gold / Coverage / Regression / Canary 的素材；**不能**作为验收证据。
- **sealed（封存）**：由独立出题上下文按 [业务规范](../docs/BUSINESS_SPEC.md)、[能力矩阵](../docs/CAPABILITY_MATRIX.md) 与 [任务契约](../docs/EVAL_TASK_CONTRACT.md) 编写，冻结后只跑正式验收；开发侧只看结构摘要。详细结果一旦用于调优，相关任务转为 exposed。
- **dev（开发）**：契约版新任务，供校准与迭代。

| 资产 | 任务数 | 角色 | 分类 |
|---|---:|---|---|
| `dev_cases.json` | 20 | 开发种子集 | exposed |
| `heldout_cases.json` | 100 | v2 预注册 held-out；A 99 / B 96 的确认性结果保留 | exposed |
| `challenge_cases_v1.json` | 8 | v2 后手写挑战集 | exposed |
| `v3_validation.json` | 12 | v3 修复后分支验证 | exposed |
| `v3_legacy_regression.json` | 6 | v2 失败回归 | exposed |
| `replays/` | — | 真实与替身录制 | exposed |
| `tasks/legacy-*.json` | 146 | 上述集合的契约只读适配（`tasks adapt-legacy` 生成，参考路径为占位） | exposed |
| `tasks/sealed-v1.json` | 48 | 封存验收集 v1（12 族 × 4，独立上下文按规范编写，2026-09-21 冻结）；首轮族级结果用于修复，2026-09-22 起转为已暴露（见 manifest） | exposed |
