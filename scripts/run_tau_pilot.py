"""Run five pinned official Retail tasks with the official agent, not BillingOps.

Use benchmarks/tau2-bench/.venv/bin/python scripts/run_tau_pilot.py.
Secrets are read from BillingOps .env and passed only through the process environment.
"""
from pathlib import Path
import os
import json
import hashlib
import subprocess
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
REPO=ROOT/'benchmarks/tau2-bench'
PIN='b7ea9074c1cba482b30687fecdb5c8425fd6f619'
OUT=ROOT/'analysis/tau-retail-pilot'
load_dotenv(ROOT/'.env',override=False)
key=os.environ.get('BILLINGOPS_LLM_API_KEY')
if not key:raise RuntimeError('Configure BILLINGOPS_LLM_API_KEY in .env')
os.environ['OPENAI_API_KEY']=key
base=os.environ.get('BILLINGOPS_LLM_BASE_URL','https://api.deepseek.com/v1')
model='openai/'+os.environ.get('BILLINGOPS_LLM_MODEL','deepseek-flash')
if subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,text=True).strip()!=PIN:
    raise RuntimeError('The upstream pin changed')
if subprocess.check_output(['git','diff','--name-only'],cwd=REPO,text=True).strip():
    raise RuntimeError('Upstream tracked files were modified')
args={'api_base':base,'temperature':0,'max_tokens':4096,'timeout':90,'num_retries':1}
agent_args={**args,'parallel_tool_calls':False}
# The pinned tasks actually use DB + NL_ASSERTION. Configure their LLM judge
# explicitly, retaining tasks, policy, reference actions and grading code.
import tau2.evaluator.evaluator_nl_assertions as judge
judge.DEFAULT_LLM_NL_ASSERTIONS=model
judge.DEFAULT_LLM_NL_ASSERTIONS_ARGS={**args,'response_format':{'type':'json_object'}}
from tau2.cli import main

OUT.mkdir(exist_ok=True,parents=True)
task_file=REPO/'data/tau2/domains/retail/tasks.json'
tasks=[t for t in json.loads(task_file.read_text()) if t['id'] in ['0','1','2','3','4']]
manifest={'created_at':datetime.now(timezone.utc).isoformat(),'upstream_commit':PIN,
 'agent':'official LLMAgent (not BillingOps A/B)','domain':'retail','split':'base','task_ids':['0','1','2','3','4'],
 'sampling':'first five base IDs; convenience smoke subset, no representativeness claim',
 'agent_model':model,'user_model':model,'nl_judge_model':model,'agent_llm_args':agent_args,'user_llm_args':args,
 'judge_override':'Official default GPT-4.1 replaced by configured DeepSeek; exploratory results are not directly comparable with default-judge leaderboard scores.',
 'reward_basis':{t['id']:t['evaluation_criteria']['reward_basis'] for t in tasks},
 'tasks_file_sha256':hashlib.sha256(task_file.read_bytes()).hexdigest(),
 'policy_sha256':hashlib.sha256((REPO/'data/tau2/domains/retail/policy.md').read_bytes()).hexdigest(),
 'core_import_workarounds':['websockets==17.1','audioop-lts'],
 'seed':300,'max_steps':100,'timeout_seconds':1200,'trials_per_task':1,'max_concurrency':1,
 'cost_caveat':'Unknown LiteLLM model pricing may emit 0; retain usage and do not interpret missing pricing as free.',
 'output':str(REPO/'data/simulations/billingops-pilot-v1/results.json')}
with (OUT/'manifest.json').open('x') as f:json.dump(manifest,f,indent=2)
os.chdir(REPO)
sys.argv=['tau2','run','--domain','retail','--task-ids','0','1','2','3','4',
 '--agent-llm',model,'--user-llm',model,'--agent-llm-args',json.dumps(agent_args),'--user-llm-args',json.dumps(args),
 '--num-trials','1','--seed','300','--max-steps','100','--timeout','1200','--max-concurrency','1',
 '--max-retries','0','--save-to','billingops-pilot-v1','--log-level','ERROR','--verbose-logs','--llm-log-mode','all']
main()
