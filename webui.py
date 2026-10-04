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


@app.route("/api/upload", methods=["POST"])
def api_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"error": "没有收到文件"}), 400
    name = Path(f.filename).name
    if not name.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"error": "只支持 .xlsx / .xlsm 文件"}), 400
    dest_dir = config.DATA_DIR / "上传"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    f.save(str(dest))
    rel = f"上传/{name}"
    return jsonify({"ok": True, "file": name, "rel": rel, "path": str(dest)})


@app.route("/")
def index():
    return app.response_class(PAGE, mimetype="text/html; charset=utf-8",
                              headers={"Cache-Control": "no-store, max-age=0"})


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AgentFlow · 多 Agent 协同办公数据处理平台</title>
<style>
  *{box-sizing:border-box}
  html,body{height:100%}
  body{margin:0;background:#fff;color:#000;font:18px/1.55 "Segoe UI","Microsoft YaHei",sans-serif}
  .app{display:grid;grid-template-columns:480px minmax(0,1fr) 430px;grid-template-rows:56px minmax(0,1fr);gap:10px;padding:10px;height:100vh}
  .header{grid-column:1/4;border-bottom:3px solid #000;display:flex;align-items:baseline;gap:20px;padding-bottom:8px;overflow:hidden}
  .header h1{font-size:26px;margin:0;font-weight:800;letter-spacing:1px}
  .header .sub{font-size:16px;color:#333}
  .panel{border:2px solid #000;padding:14px;min-height:0;display:flex;flex-direction:column;overflow:hidden}
  .panel h2{font-size:19px;margin:0 0 10px;font-weight:800;border-bottom:1px solid #000;padding-bottom:6px;display:flex;justify-content:space-between;align-items:center}
  .scroll{overflow:auto;min-height:0;flex:1}
  .col1{grid-column:1;grid-row:2;overflow-y:auto}
  .col2{grid-column:2;grid-row:2}
  .col3{grid-column:3;grid-row:2}
  .bottom{grid-column:1/4;grid-row:3;display:grid;grid-template-columns:1fr 1fr;gap:10px;min-height:0}
  label{display:block;font-size:16px;font-weight:700;margin:8px 0 4px}
  select,textarea{width:100%;background:#fff;color:#000;border:2px solid #000;padding:11px;font:18px/1.5 inherit}
  textarea{min-height:70px;resize:vertical}
  .chk{display:flex;align-items:center;gap:10px;font-size:17px;font-weight:600;margin-top:12px}
  .chk input{width:20px;height:20px}
  button{background:#000;color:#fff;border:2px solid #000;padding:13px 22px;font:18px/1 inherit;font-weight:800;cursor:pointer;margin-right:10px}
  button.ghost{background:#fff;color:#000}
  button:disabled{opacity:.3;cursor:not-allowed}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-top:16px}
  #actionRow{padding-top:4px;border-top:1px solid #ddd;margin-top:8px}
  #btnRun{display:none}
  .tabs{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
  .tab{background:#fff;color:#000;border:1px solid #000;padding:3px 9px;font-size:13px;font-weight:700;cursor:pointer;margin:0}
  .tab.active{background:#000;color:#fff}
  .dhead{font-weight:800;margin:10px 0 4px}
  .miss{background:#ffe1e1;font-weight:800}
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
    <div id="uploadArea"></div>
    <div style="font-weight:800;font-size:17px;margin-top:14px">⏱ 已用时：<span id="timer">0s</span> <span style="font-weight:600;color:#a33;font-size:14px">（点「生成计划」后开始计时）</span></div>
    <label>任务描述</label>
    <textarea id="task" placeholder="例如：核对 data/对账 目录下的两张表，按订单号找出差异并生成报告"></textarea>
    <div class="row" id="actionRow">
      <button id="btnPlan">1. 生成计划</button>
      <button id="btnRun" class="ghost" disabled>2. 确认执行</button>
      <span id="status" class="tag">待开始</span>
    </div>
  </div>

  <div class="panel col2">
    <h2>结果
      <span class="tabs">
        <button class="tab active" data-tab="diff">差异/结果</button>
        <button class="tab" data-tab="steps">实时轨迹</button>
        <button class="tab" data-tab="agents">成本分账</button>
      </span>
    </h2>
    <div class="scroll">
      <div class="tabpage" id="page-diff"><div id="diff">—</div></div>
      <div class="tabpage" id="page-steps" style="display:none"><div id="steps">—</div></div>
      <div class="tabpage" id="page-agents" style="display:none"><div id="agents">—</div></div>
    </div>
  </div>

  <div class="panel col3">
    <h2>执行报告 <button id="btnReport" class="ghost">刷新报告</button></h2>
    <div class="scroll"><pre id="report">执行结束后自动显示。</pre></div>
  </div>
</div>

<script>
let runId=null, timer=null, t0=0, running=false;
const $=id=>document.getElementById(id);
// 独立计时器：只要点过“生成计划/确认执行”，就每 0.2 秒刷新一次用时
setInterval(()=>{ if(running) $('timer').textContent=Math.floor((Date.now()-t0)/1000)+'s'; }, 200);
const esc=s=>String(s===undefined||s===null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

fetch('/api/scenarios').then(r=>r.json()).then(d=>{
  $('scenario').innerHTML=d.scenarios.map(s=>`<option value="${s.id}">${s.name}（${s.retrieval_enabled?'联网':'离线'}）</option>`).join('');
  $('scenario').value=d.default;
  renderUpload(d.default);
  $('scenario').onchange=()=>{
    const s=d.scenarios.find(x=>x.id===$('scenario').value);
    renderUpload($('scenario').value);
    if(s&&s.task_examples[0])$('task').value=s.task_examples[0];
  };
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
  $('btnPlan').disabled=true; $('status').textContent='规划中…'; $('status').className='tag';
  t0=Date.now(); running=true; $('timer').textContent='0s';
  $('steps').innerHTML='—'; $('agents').innerHTML='—'; $('diff').innerHTML='—'; $('report').textContent='执行结束后自动显示…';
  try{
    const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task,scenario:$('scenario').value,dry_run:false})});
    const d=await r.json();
    if(d.error){alert(d.error);$('status').textContent='失败';running=false;return;}
    runId=d.run_id;
    $('status').textContent='计划已生成，自动执行中…'; $('status').className='tag ok';
    await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({run_id:runId,use_reference:true})});
    startPolling();
  }catch(e){alert(e);running=false;}
  finally{$('btnPlan').disabled=false;}
};

$('btnRun').onclick=async()=>{
  if(!runId){alert('请先生成计划');return;}
  $('btnRun').disabled=true; $('status').textContent='执行中…';
  t0=Date.now(); running=true; $('timer').textContent='0s';
  await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({run_id:runId,use_reference:$('useref').checked})});
};

async function loadReport(){
  if(!runId){return;}
  try{
    const r=await fetch('/api/report?run_id='+encodeURIComponent(runId));
    const d=await r.json();
    const md=d.markdown||d.error||'（暂无报告）';
    const m=md.match(/##\s*执行摘要\s*([\s\S]*?)(?=\n##\s|$)/);
    $('report').textContent=m?('执行摘要：\n'+m[1].trim()):md;
  }catch(e){}
}
$('btnReport').onclick=loadReport;

document.querySelectorAll('.tab').forEach(b=>{
  b.onclick=()=>{
    document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    document.querySelectorAll('.tabpage').forEach(p=>p.style.display='none');
    const pg=document.getElementById('page-'+b.dataset.tab); if(pg) pg.style.display='block';
  };
});

async function doUpload(file){
  const fd=new FormData(); fd.append('file',file);
  const r=await fetch('/api/upload',{method:'POST',body:fd});
  return await r.json();
}

function renderUpload(sid){
  const area=$('uploadArea');
  if(sid==='reconcile'){
    area.innerHTML=
      '<label>原版 / 基准表</label>'+
      '<input type="file" id="fileA" accept=".xlsx,.xlsm" style="font-size:15px">'+
      '<label>要对比的版本</label>'+
      '<input type="file" id="fileB" accept=".xlsx,.xlsm" style="font-size:15px">'+
      '<div class="row" style="margin-top:10px">'+
      '<button class="ghost" type="button" id="btnUpload2">上传并生成对账任务</button>'+
      '<span id="upmsg" style="font-size:15px"></span></div>';
    $('btnUpload2').onclick=uploadReconcile;
  }else{
    const label = sid==='clean' ? '上传要清洗的 Excel' : '上传 Excel';
    area.innerHTML=
      '<label>'+label+'</label>'+
      '<input type="file" id="fileS" accept=".xlsx,.xlsm" style="font-size:15px">'+
      '<div class="row" style="margin-top:10px">'+
      '<button class="ghost" type="button" id="btnUpload1">上传</button>'+
      '<span id="upmsg" style="font-size:15px"></span></div>';
    $('btnUpload1').onclick=uploadSingle;
  }
}

async function uploadReconcile(){
  const a=$('fileA').files[0], b=$('fileB').files[0];
  if(!a||!b){alert('请把「原版 / 基准表」和「要对比的版本」两个都选上');return;}
  $('upmsg').textContent='上传中…';
  const da=await doUpload(a), db=await doUpload(b);
  if(da.error||db.error){$('upmsg').textContent=(da.error||db.error);return;}
  $('upmsg').textContent='已上传 2 个文件';
  $('task').value='核对 '+da.rel+'（原版/基准）与 '+db.rel+'（待核对），找出只在单方存在、字段不一致和重复的记录，生成差异报告。';
}

async function uploadSingle(){
  const f=$('fileS').files[0];
  if(!f){alert('请先选择 Excel 文件');return;}
  $('upmsg').textContent='上传中…';
  const d=await doUpload(f);
  if(d.error){$('upmsg').textContent=d.error;return;}
  $('upmsg').textContent='已上传：'+d.file;
  const sid=$('scenario').value;
  if(sid==='clean'){
    $('task').value='检查 '+d.rel+'，找出重复记录与缺失字段并生成清洗报告。';
  }else if(sid==='quote_fill'){
    $('task').value='把 '+d.rel+' 按最新市场行情完成填报，并检查结果。';
  }else{
    const t=$('task'); t.value=(t.value?t.value.trim()+' ':'')+d.rel;
  }
}

function cls(s){return s.agent==='verifier'?'':(s.ok?'':'bad');}
function renderWrites(writes){
  let h='<div class="dhead">已写入 / 填报内容</div>';
  const one=(w)=>{
    const name=(w.file?String(w.file).split(/[\\/]/).pop():'(文件)');
    const es=w.written||w.filled||[];
    if(es.length){
      h+='<div style="margin:8px 0 3px;font-weight:700">'+esc(name)+'　'+(w.saved?'✔ 已保存':'✗ 未保存')+'</div>';
      h+='<table><tr><th>条目</th><th>值</th></tr>'+es.slice(0,300).map(e=>'<tr><td>'+esc(e.item)+'</td><td>'+esc(e.value!==undefined?e.value:(e.price!==undefined?e.price:''))+'</td></tr>').join('')+'</table>';
    }else{
      h+='<div style="margin:4px 0;font-weight:700">'+esc(name)+'　写入 '+(w.written_count||0)+' 项'
        +(w.not_found_count?('，未找到 '+w.not_found_count+' 项'):'')+'</div>';
    }
    if(w.error){h+='<div>出错：'+esc(w.error)+'</div>';}
  };
  writes.forEach(w=>{(Array.isArray(w.files)?w.files:[w]).forEach(one);});
  return h;
}
function renderFindings(findings){
  let h='<div class="dhead">问题清单</div>';
  findings.forEach(f=>{
    const rule=f.rule||f.type||'';
    const msg=f.message||f.detail||(f.item?('条目「'+f.item+'」'+(f.value!==undefined?'：'+f.value:'')):'');
    h+='<div class="step '+(f.severity==='error'?'bad':'')+'">'+(rule?('['+esc(rule)+'] '):'')+esc(msg)+'</div>';
  });
  return h;
}
function startPolling(){
  if(timer)clearInterval(timer);
  timer=setInterval(async()=>{
    if(!runId)return;
    const r=await fetch('/api/progress?run_id='+encodeURIComponent(runId));
    const d=await r.json(); if(d.error)return;
    $('status').textContent=({created:'待开始',planning:'规划中',planned:'计划已生成',running:'执行中',retrieval:'检索中',execution:'执行中',verification:'校验中',replanning:'重规划中',done:'完成',cancelled:'已取消',budget_exceeded:'预算耗尽',error:'出错'}[d.status]||d.status);
    $('status').className='tag '+(d.status==='done'?'ok':(d.status.includes('budget')||d.status==='error'?'warn':''));
    $('steps').innerHTML=d.steps.slice().reverse().map(s=>
      `<div class="step ${cls(s)}"><span class="a">[${esc(s.agent)}]</span> ${s.tool?`<span class="t">${esc(s.tool)}</span>`:''} ${esc(s.detail)}</div>`
    ).join('')||'—';
    const pa=d.usage.per_agent||{};
    $('agents').innerHTML='<table><tr><th>Agent</th><th>调用</th><th>tokens</th><th>成本</th></tr>'+
      Object.entries(pa).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${v.calls}</td><td>${v.total_tokens}</td><td>¥${v.estimated_cost_yuan}</td></tr>`).join('')+
      `<tr><td><b>合计</b></td><td>${d.usage.llm_calls}</td><td>${d.usage.total_tokens}</td><td>¥${d.usage.estimated_cost_yuan}</td></tr></table>`;
    let dh='';
    if(d.diff){
      const x=d.diff;
      const lf=((x.left&&x.left.file)||'左表').split(/[\\/]/).pop();
      const rf=((x.right&&x.right.file)||'右表').split(/[\\/]/).pop();
      const header=x.header||[];
      const jk=k=>esc([].concat(k).join(' / '));
      const rowTable=(rows)=>{
        if(!rows||!rows.length) return '<div>无</div>';
        return '<table><tr>'+header.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>'+
          rows.slice(0,100).map(r=>'<tr>'+header.map(c=>'<td>'+esc(r[c])+'</td>').join('')+'</tr>').join('')+'</table>';
      };
      let h='<div style="margin-bottom:6px">差异合计 <b>'+x.diff_count+'</b> 处</div>';
      const mm=x.value_mismatches||[];
      h+='<div class="dhead">① 两边数据对不上（'+esc(lf)+' vs '+esc(rf)+'）：'+mm.length+' 处</div>';
      if(!mm.length){h+='<div>无</div>';}
      else{
        const groups={};
        mm.forEach(m=>{const k=[].concat(m.key).join(' / ');(groups[k]=groups[k]||[]).push(m);});
        Object.keys(groups).forEach(k=>{
          const g=groups[k], lrow=g[0].left_row||{}, rrow=g[0].right_row||{};
          const dif=new Set(g.map(m=>m.column));
          h+='<div style="margin:8px 0 3px;font-weight:700">关键值 '+esc(k)+'（'+[...dif].map(esc).join('、')+' 对不上）</div>';
          h+='<table><tr><th>表</th>'+header.map(c=>'<th>'+esc(c)+'</th>').join('')+'</tr>'+
             '<tr><td>'+esc(lf)+'</td>'+header.map(c=>'<td'+(dif.has(c)?' class="miss"':'')+'>'+esc(lrow[c])+'</td>').join('')+'</tr>'+
             '<tr><td>'+esc(rf)+'</td>'+header.map(c=>'<td'+(dif.has(c)?' class="miss"':'')+'>'+esc(rrow[c])+'</td>').join('')+'</tr></table>';
        });
      }
      h+='<div class="dhead">② 只在「'+esc(lf)+'」里有：'+x.only_in_left.length+' 行</div>';
      h+=rowTable(x.only_in_left_rows);
      h+='<div class="dhead">③ 只在「'+esc(rf)+'」里有：'+x.only_in_right.length+' 行</div>';
      h+=rowTable(x.only_in_right_rows);
      h+='<div class="dhead">④ 重复关键值</div>';
      h+='<div>'+esc(lf)+'：'+((x.duplicate_keys_left||[]).map(d=>jk(d.key)).join('、')||'无')+'　'+esc(rf)+'：'+((x.duplicate_keys_right||[]).map(d=>jk(d.key)).join('、')||'无')+'</div>';
      dh+=h;
    }
    if(d.writes&&d.writes.length){dh+=renderWrites(d.writes);}
    if(d.findings&&d.findings.length){dh+=renderFindings(d.findings);}
    $('diff').innerHTML=dh||'—';
    if(['done','cancelled','budget_exceeded','error'].includes(d.status)){running=false;$('timer').textContent=Math.floor((Date.now()-t0)/1000)+'s';clearInterval(timer);timer=null;loadReport();}
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
