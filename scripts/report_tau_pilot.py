"""Publish an evidence-linked smoke report; preserve upstream outputs verbatim."""
import json
import html
import shutil
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SRC=ROOT/'benchmarks/tau2-bench/data/simulations/billingops-pilot-v1'
OUT=ROOT/'analysis/tau-retail-pilot'
x=json.loads((SRC/'results.json').read_text())
shutil.copyfile(SRC/'results.json',OUT/'results.json')
shutil.copyfile('/tmp/billingops-tau-pilot.log',OUT/'run.log')
rows=[]
for s in x['simulations']:
    inp=out=cache=0
    for m in s['messages']:
        usage=(m.get('raw_data') or {}).get('usage') or {}
        inp+=usage.get('prompt_tokens',0);out+=usage.get('completion_tokens',0)
        cache+=usage.get('prompt_cache_hit_tokens',0)
    # Configured prices, not provider billing. NL judging has separate debug usage
    # and is deliberately excluded from this conversation-only estimate.
    price=((inp-cache)*0.30+cache*0.006+out*1.20)/1_000_000
    rows.append({'task_id':s['task_id'],'reward':s['reward_info']['reward'],'breakdown':s['reward_info'].get('reward_breakdown'),
        'termination':s['termination_reason'],'messages':len(s['messages']),'duration_s':s['duration'],
        'conversation_input_tokens':inp,'conversation_cached_tokens':cache,'conversation_output_tokens':out,'conversation_cost_estimate_usd':price})
with (OUT/'summary.json').open('x') as f:json.dump(rows,f,indent=2)
md='''# τ Retail：5 题接入试跑

固定 upstream `b7ea9074c1cba482b30687fecdb5c8425fd6f619`（1.0.1），使用官方 LLMAgent、官方用户模拟器、原任务与政策。Agent 和用户均为 `deepseek-flash`；NL judge 也显式改用该模型。**这是独立环境接入试跑，不是 BillingOps 的成绩，也不与默认 judge 的排行榜直接比较。**

选择 base 前5题 0–4，每题1次；seed300、temperature0、max_steps100、concurrency1。5题中4题reward=1；规模和选题方式不支持能力或稳定性推断。

实际 tasks.json 的 reward_basis 是 `DB + NL_ASSERTION`，与当前 docs/evaluation.md 所说 Retail 默认 `DB + COMMUNICATE` 不同。以固定版本任务和实际 reward_breakdown 为准；0/1题没有自然语言断言，该项为缺省通过。评分代码与任务均未改。

| 任务 | DB | NL | 总分 | 消息数 |
|---|---:|---:|---:|---:|
'''
for r in rows:md+=f"| {r['task_id']} | {r['breakdown']['DB']} | {r['breakdown']['NL_ASSERTION']} | {r['reward']} | {r['messages']} |\n"
md+='''
## 失败案例 3：用户模拟器在确认后立即终止

消息序号从0开始：

- 消息13：`get_order_details` 找到 `#W4776164` 中蓝色 S / cotton / v-neck 的 T-Shirt。
- 消息16：Agent 找到紫色 S / polyester / v-neck 替代项，计算差价 $0.05，并请求确认。
- 消息17：模拟用户同意修改并扣差价，但在同一消息输出 `###STOP###`。
- `termination_reason=user_stop`，没有后续 Agent 回合，也没有执行 `modify_pending_order_items`；DB=0，NL=1。

可确认机制是用户模拟器提前结束，使执行机会消失；不能仅凭总分0就归因为Agent不会调用修改工具。保留0分和完整轨迹，不事后删除此题。这里的文本来自模型生成的合成用户。

迁移到 BillingOps 的启示：把“同意执行”和“任务已完成”分开；用户模拟器必须等到写入及验证后才结束。未来扩展多轮用户模拟器时，增加批准后立即STOP的独立故障测试。当前 BillingOps 的确定性 ApprovalSimulator 不使用文本STOP，所以本次未改它。

## 可复现与限制

运行入口 `scripts/run_tau_pilot.py`，须使用独立的 `benchmarks/tau2-bench/.venv/bin/python`。核心安装另需 `websockets==17.1`、`audioop-lts==0.2.2` 才能在此 Python3.13 环境导入；未安装语音/RL训练框架，未修改 upstream 文件。

[完整输出](results.json)、[运行配置](manifest.json)、[逐题资源](summary.json)、[日志](run.log)均保留。原始SDK费用字段为0是 LiteLLM没有识别模型价格，**不是免费**。summary里的费用用BillingOps配置单价估算，只含Agent+用户对话，不含NL judge；不与BillingOps只算业务Agent的费用直接比较。

下一步不宜直接扩大到全量：先固定更可靠的用户模拟器配置，检查批准后结束的协议，再在新批次重跑。若要测BillingOps，应另做Retail域适配和公平的A流程；本轮未把官方LLMAgent称作自己的Agent。
'''
(OUT/'report.md').write_text(md)
s=x['simulations'][3]
parts=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>τ Retail · 接入试跑</title><style>body{max-width:1000px;margin:40px auto;font:16px system-ui;line-height:1.7;background:#f4f7fa;color:#172e48}section,article{background:white;border:1px solid #dce4ec;border-radius:12px;padding:22px;margin:18px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{color:#1760ae}</style><h1>τ Retail · 4 / 5</h1><p>官方Agent + DeepSeek · 独立接入试跑，非 BillingOps 成绩</p><section><h2>失败案例 3：用户确认后，模拟器提前 STOP</h2><p>DB=0、NL=1。轨迹在用户授权后结束，未给Agent执行机会。需审查用户模拟器，不能直接断言是Agent能力失败。</p><a href="report.md">报告</a> · <a href="manifest.json">固定配置</a> · <a href="results.json">原始输出</a> · <a href="summary.json">资源与分项</a></section>']
for i,m in enumerate(s['messages']):
    text=m.get('content') or json.dumps(m.get('tool_calls'),ensure_ascii=False,indent=2)
    parts.append(f'<article id="message-{i}"><strong>消息 {i} · {html.escape(m["role"])}</strong><pre>{html.escape(text)}</pre></article>')
parts.append('</html>');(OUT/'index.html').write_text('\n'.join(parts))
print(OUT/'report.md')
