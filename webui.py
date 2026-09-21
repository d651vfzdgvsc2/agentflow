"""WebUI：多 Agent 协同的面板。

流程（两步式，贴合"人在环审批"）：
  1) POST /api/plan    → 只跑 Planner，返回计划与预估（用户先看）
  2) POST /api/run     → 后台线程执行 检索→执行→校验→报告
  3) GET  /api/progress→ 轮询实时轨迹、各 Agent 成本、问题清单
  4) GET  /api/report  → 取最终报告

启动：python webui.py  → 浏览器打开 http://127.0.0.1:8000
"""
from __future__ import annotations

import sys
import threading
import traceback
from pathlib import Path

from flask import Flask, jsonify, request

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import config  # noqa: E402
from agents.base import AppConfig  # noqa: E402
from core.budget import TokenBudget  # noqa: E402
from core.llm import build_client  # noqa: E402
from core.orchestrator import Orchestrator  # noqa: E402
from scenarios import list_scenarios, load_scenario  # noqa: E402

app = Flask(__name__)

RUNS: dict[str, dict] = {}
LOCK = threading.Lock()
# 串行执行：兼容层依赖全局 SCENARIO，避免并发运行不同场景时互相覆盖
EXEC_LOCK = threading.Lock()
MAX_RUNS = 20


def _new_run(scenario_id: str, task: str, dry_run: bool) -> tuple[str, Orchestrator]:
    scenario = load_scenario(scenario_id)
    budget_cfg = scenario.budget or {}
    budget = TokenBudget(
        max_total_tokens=int(budget_cfg.get("max_total_tokens", 800_000)),
        price_in_per_m=float(budget_cfg.get("price_in_per_m", 2.0)),
        price_out_per_m=float(budget_cfg.get("price_out_per_m", 8.0)),
    )
    llm = build_client(
        api_key=config.get_deepseek_api_key(),
        base_url=config.DEEPSEEK_BASE_URL,
        model=config.DEEPSEEK_MODEL,
        budget=budget,
    )

    import time

    run_id = time.strftime("%Y%m%d_%H%M%S") + "_" + str(len(RUNS))

    def on_event(step: dict) -> None:
        with LOCK:
            RUNS[run_id]["steps"].append(step)

    orch = Orchestrator(
        task=task, scenario=scenario, llm=llm,
        app_config=AppConfig(dry_run=dry_run),
        on_event=on_event, run_id=run_id,
    )
    with LOCK:
        if len(RUNS) >= MAX_RUNS:
            finished = [k for k, v in RUNS.items()
                        if v["status"] in ("done", "cancelled", "budget_exceeded", "error")]
            for k in finished:
                RUNS.pop(k, None)
        RUNS[run_id] = {
            "orch": orch, "task": task, "scenario": scenario.to_dict(),
            "status": "created", "steps": [], "result": None, "error": None,
            "dry_run": dry_run,
        }
    return run_id, orch


# ----------------------------------------------------------------------
@app.route("/api/scenarios")
def api_scenarios():
    return jsonify({"scenarios": list_scenarios(), "default": config.DEFAULT_SCENARIO})


@app.route("/api/plan", methods=["POST"])
def api_plan():
    data = request.get_json(force=True) or {}
    task = (data.get("task") or "").strip()
    scenario_id = data.get("scenario") or config.DEFAULT_SCENARIO
    dry_run = bool(data.get("dry_run"))
    if not task:
        return jsonify({"error": "请填写任务描述"}), 400
    try:
        run_id, orch = _new_run(scenario_id, task, dry_run)
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"初始化失败: {e}"}), 500

    try:
        plan = orch.plan_only()
        with LOCK:
            RUNS[run_id]["status"] = "planned"
        return jsonify({"run_id": run_id, "plan": plan, "usage": orch.budget.summary()})
    except Exception as e:  # noqa: BLE001
        with LOCK:
            RUNS[run_id]["status"] = "error"
            RUNS[run_id]["error"] = str(e)
        return jsonify({"error": f"规划失败: {e}", "run_id": run_id}), 500


@app.route("/api/run", methods=["POST"])
def api_run():
    data = request.get_json(force=True) or {}
    run_id = data.get("run_id")
    use_reference = bool(data.get("use_reference"))
    with LOCK:
        run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "run_id 不存在"}), 404
    if run["status"] not in ("planned", "created"):
        return jsonify({"error": f"当前状态不可执行: {run['status']}"}), 400

    orch: Orchestrator = run["orch"]
    plan = dict(orch.bb.get("plan") or {})
    plan["_use_reference"] = use_reference

    def worker() -> None:
        with LOCK:
            RUNS[run_id]["status"] = "running"
        try:
            with EXEC_LOCK:
                result = orch.run(preset_plan=plan)
            with LOCK:
                RUNS[run_id]["status"] = result.status
                RUNS[run_id]["result"] = {
                    "status": result.status, "summary": result.summary,
                    "report_path": result.report_path, "usage": result.usage,
                    "findings": result.findings, "run_dir": result.run_dir,
                }
        except Exception as e:  # noqa: BLE001
            with LOCK:
                RUNS[run_id]["status"] = "error"
                RUNS[run_id]["error"] = f"{e}\n{traceback.format_exc()}"
        finally:
            with LOCK:
                RUNS[run_id]["usage"] = orch.budget.summary()

    threading.Thread(target=worker, daemon=True).start()
    return jsonify({"run_id": run_id, "status": "running"})


@app.route("/api/progress")
def api_progress():
    run_id = request.args.get("run_id", "")
    with LOCK:
        run = RUNS.get(run_id)
        if not run:
            return jsonify({"error": "run_id 不存在"}), 404
        orch: Orchestrator = run["orch"]
        payload = {
            "status": run["status"],
            "steps": run["steps"][-200:],
            "plan": orch.bb.get("plan") or {},
            "approval": orch.bb.get("approval") or {},
            "usage": orch.budget.summary(),
            "findings": orch.bb.get("findings") or [],
            "diff": orch.bb.get("diff"),
            "writes": orch.bb.get("writes") or [],
            "error": run.get("error"),
            "result": run.get("result"),
        }
    return jsonify(payload)


@app.route("/api/report")
def api_report():
    run_id = request.args.get("run_id", "")
    with LOCK:
        run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "run_id 不存在"}), 404
    report = run["orch"].bb.get("report") or {}
    path = report.get("path") if isinstance(report, dict) else None
    if not path or not Path(path).exists():
        return jsonify({"error": "报告尚未生成"}), 404
    return jsonify({"path": path, "markdown": Path(path).read_text(encoding="utf-8")})


@app.route("/")
def index():
    return app.response_class(PAGE, mimetype="text/html; charset=utf-8")


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AgentFlow · 多 Agent 协同办公数据处理平台</title>
<style>
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:#fff;color:#000;font:18px/1.55 "Segoe UI","Microsoft YaHei",sans-serif}
  .app{display:grid;grid-template-columns:460px minmax(0,1fr) 420px;grid-template-rows:66px minmax(0,1fr) 250px;gap:10px;padding:10px;height:100vh}
  .header{grid-column:1/4;border-bottom:3px solid #000;display:flex;align-items:baseline;gap:20px;padding-bottom:8px;overflow:hidden}
  .header h1{font-size:26px;margin:0;font-weight:800;letter-spacing:1px}
  .header .sub{font-size:16px;color:#333}
  .panel{border:2px solid #000;padding:14px;min-height:0;display:flex;flex-direction:column;overflow:hidden}
  .panel h2{font-size:19px;margin:0 0 10px;font-weight:800;border-bottom:1px solid #000;padding-bottom:6px;display:flex;justify-content:space-between;align-items:center}
  .scroll{overflow:auto;min-height:0;flex:1}
  .col1{grid-column:1;grid-row:2}
  .col2{grid-column:2;grid-row:2}
  .col3{grid-column:3;grid-row:2}
  .bottom{grid-column:1/4;grid-row:3;display:grid;grid-template-columns:1fr 1fr;gap:10px;min-height:0}
  label{display:block;font-size:16px;font-weight:700;margin:14px 0 6px}
  select,textarea{width:100%;background:#fff;color:#000;border:2px solid #000;padding:11px;font:18px/1.5 inherit}
  textarea{min-height:120px;resize:vertical}
  .chk{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:600;margin-top:12px}
  .chk input{width:20px;height:20px}
  button{background:#000;color:#fff;border:2px solid #000;padding:13px 22px;font:18px/1 inherit;font-weight:800;cursor:pointer;margin-right:10px}
  button.ghost{background:#fff;color:#000}
  button:disabled{opacity:.3;cursor:not-allowed}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
  .tag{border:2px solid #000;padding:5px 14px;font-size:16px;font-weight:600}
  .tag.ok,.tag.warn{font-weight:900;background:#000;color:#fff}
  pre{white-space:pre-wrap;word-break:break-word;background:#fff;border:2px solid #000;padding:12px;margin:0;font:16px/1.55 "Consolas","Microsoft YaHei",monospace}
  .step{padding:8px 12px;border-left:5px solid #bbb;margin:5px 0;font:16px/1.5 "Consolas","Microsoft YaHei",monospace;white-space:pre-wrap;word-break:break-word}
  .step .a{font-weight:900}
  .step .t{font-weight:700;text-decoration:underline}
  .step.bad{border-left-color:#000;background:#eee}
  table{width:100%;border-collapse:collapse;font-size:16px}
  th,td{border:1px solid #000;padding:7px 9px;text-align:left}
  th{font-weight:800;background:#f0f0f0}
  @media(max-width:1200px){.app{grid-template-columns:1fr;grid-template-rows:auto;height:auto}.col1,.col2,.col3,.bottom{grid-column:1}.bottom{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <h1>AgentFlow</h1>
    <div class="sub">多 Agent 协同办公数据处理平台　|　Planner → Retrieval → Executor → Verifier → Reporter</div>
  </div>

  <div class="panel col1">
    <h2>任务</h2>
    <label>场景模板</label>
    <select id="scenario"></select>
    <label>任务描述</label>
    <textarea id="task" placeholder="例如：核对 data/对账 目录下的两张表，按订单号找出差异并生成报告"></textarea>
    <label class="chk"><input type="checkbox" id="dryrun"> 只读试跑（禁止写文件）</label>
    <label class="chk"><input type="checkbox" id="useref"> 允许使用内置参考数据</label>
    <div class="row">
      <button id="btnPlan">1. 生成计划</button>
      <button id="btnRun" class="ghost" disabled>2. 确认执行</button>
      <span id="status" class="tag">待开始</span>
    </div>
    <h2 style="margin-top:16px">执行计划</h2>
    <div class="scroll"><pre id="plan">—</pre></div>
  </div>

  <div class="panel col2">
    <h2>实时轨迹</h2>
    <div class="scroll" id="steps">—</div>
  </div>

  <div class="panel col3">
    <h2>成本分账</h2>
    <div class="scroll" id="agents">—</div>
    <h2 style="margin-top:14px">问题清单</h2>
    <div class="scroll" id="findings">—</div>
  </div>

  <div class="bottom">
    <div class="panel">
      <h2>差异 / 结果</h2>
      <div class="scroll" id="diff">—</div>
    </div>
    <div class="panel">
      <h2>执行报告 <button id="btnReport" class="ghost">查看报告</button></h2>
      <div class="scroll"><pre id="report">执行结束后可查看。</pre></div>
    </div>
  </div>
</div>

<script>
let runId=null, timer=null;
const $=id=>document.getElementById(id);
const esc=s=>String(s===undefined||s===null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

fetch('/api/scenarios').then(r=>r.json()).then(d=>{
  $('scenario').innerHTML=d.scenarios.map(s=>`<option value="${s.id}">${s.name}（${s.retrieval_enabled?'联网':'离线'}）</option>`).join('');
  $('scenario').value=d.default;
  $('scenario').onchange=()=>{const s=d.scenarios.find(x=>x.id===$('scenario').value); if(s&&s.task_examples[0])$('task').value=s.task_examples[0];};
  const s=d.scenarios.find(x=>x.id===d.default); if(s&&s.task_examples[0])$('task').value=s.task_examples[0];
});

// 连接模式下可挂在一次已存在的运行上，实时观看
const presetRun=new URLSearchParams(location.search).get('run');
if(presetRun){
  runId=presetRun;
  $('btnPlan').disabled=true; $('btnRun').disabled=true;
  $('status').textContent='已连接运行 '+presetRun;
  startPolling();
}

$('btnPlan').onclick=async()=>{
  const task=$('task').value.trim(); if(!task){alert('请填写任务');return;}
  $('btnPlan').disabled=true; $('btnRun').disabled=true; $('status').textContent='规划中…'; $('status').className='tag';
  $('steps').innerHTML='—'; $('agents').innerHTML='—'; $('findings').innerHTML='—'; $('diff').innerHTML='—'; $('report').textContent='—';
  try{
    const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task,scenario:$('scenario').value,dry_run:$('dryrun').checked})});
    const d=await r.json();
    if(d.error){alert(d.error);$('status').textContent='失败';return;}
    runId=d.run_id; $('plan').textContent=JSON.stringify(d.plan,null,2);
    $('btnRun').disabled=false; $('status').textContent='计划已生成，待确认'; $('status').className='tag ok';
    startPolling();
  }catch(e){alert(e);}
  finally{$('btnPlan').disabled=false;}
};

$('btnRun').onclick=async()=>{
  if(!runId){alert('请先生成计划');return;}
  $('btnRun').disabled=true; $('status').textContent='执行中…';
  await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({run_id:runId,use_reference:$('useref').checked})});
};

$('btnReport').onclick=async()=>{
  if(!runId){return;}
  const r=await fetch('/api/report?run_id='+encodeURIComponent(runId));
  const d=await r.json();
  $('report').textContent=d.markdown||d.error||'—';
};

function cls(s){return s.agent==='verifier'?'':(s.ok?'':'bad');}
function startPolling(){
  if(timer)clearInterval(timer);
  timer=setInterval(async()=>{
    if(!runId)return;
    const r=await fetch('/api/progress?run_id='+encodeURIComponent(runId));
    const d=await r.json(); if(d.error)return;
    $('status').textContent=d.status;
    $('status').className='tag '+(d.status==='done'?'ok':(d.status.includes('budget')||d.status==='error'?'warn':''));
    if(d.plan && Object.keys(d.plan).length){ $('plan').textContent=JSON.stringify(d.plan,null,2); }
    $('steps').innerHTML=d.steps.slice().reverse().map(s=>
      `<div class="step ${cls(s)}"><span class="a">[${esc(s.agent)}]</span> ${s.tool?`<span class="t">${esc(s.tool)}</span>`:''} ${esc(s.detail)}</div>`
    ).join('')||'—';
    const pa=d.usage.per_agent||{};
    $('agents').innerHTML='<table><tr><th>Agent</th><th>调用</th><th>tokens</th><th>成本</th></tr>'+
      Object.entries(pa).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${v.calls}</td><td>${v.total_tokens}</td><td>¥${v.estimated_cost_yuan}</td></tr>`).join('')+
      `<tr><td><b>合计</b></td><td>${d.usage.llm_calls}</td><td>${d.usage.total_tokens}</td><td>¥${d.usage.estimated_cost_yuan}</td></tr></table>`;
    $('findings').innerHTML=(d.findings||[]).map(f=>
      `<div class="step ${f.severity==='error'?'bad':''}"><span class="${f.severity==='error'?'err':(f.severity==='warning'?'warn':'ok')}">[${esc(f.rule)}]</span> ${esc(f.message)}</div>`
    ).join('')||'—';
    if(d.diff){
      const x=d.diff;
      $('diff').innerHTML=`<div>差异合计 <b>${x.diff_count}</b>　仅左 ${x.only_in_left.length}　仅右 ${x.only_in_right.length}　字段不一致 ${x.value_mismatches.length}</div>`+
        (x.value_mismatches.length?('<table><tr><th>关键值</th><th>字段</th><th>左</th><th>右</th></tr>'+
          x.value_mismatches.slice(0,20).map(m=>`<tr><td>${esc(m.key)}</td><td>${esc(m.column)}</td><td>${esc(m.left)}</td><td>${esc(m.right)}</td></tr>`).join('')+'</table>'):'');
    }
    if(['done','cancelled','budget_exceeded','error'].includes(d.status)){clearInterval(timer);timer=null;$('btnRun').disabled=true;}
  },1000);
}
</script>
</body>
</html>
"""


def main() -> None:
    host = "127.0.0.1"
    print(f"AgentFlow WebUI: http://{host}:8000")
    app.run(host=host, port=8000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
