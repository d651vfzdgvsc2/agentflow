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

from flask import Flask, jsonify, request, send_file

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


def _new_run(scenario_id: str, task: str, dry_run: bool,
             forced_files: list[str] | None = None) -> tuple[str, Orchestrator]:
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
        forced_files=forced_files,
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
    forced_files = [str(f) for f in (data.get("target_files") or []) if str(f).strip()]
    if not task:
        return jsonify({"error": "请填写任务描述"}), 400
    try:
        run_id, orch = _new_run(scenario_id, task, dry_run, forced_files)
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
            "verifications": orch.bb.get("verifications") or [],
            "duplicates": orch.bb.get("duplicates"),
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


@app.route("/api/results")
def api_results():
    """列出本次运行产生的结果文件（供前端逐个给出直链下载）。"""
    run_id = request.args.get("run_id", "")
    with LOCK:
        run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "run_id 不存在"}), 404
    from core.package import result_files

    files = result_files(run["orch"].bb)
    return jsonify({"files": [{"i": k, "name": p.name, "size": p.stat().st_size}
                              for k, p in enumerate(files)]})


@app.route("/api/download")
def api_download():
    """下载某个结果文件（永远直接给 xlsx，绝不打包）。"""
    run_id = request.args.get("run_id", "")
    with LOCK:
        run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "run_id 不存在"}), 404
    from core.package import result_files

    files = result_files(run["orch"].bb)
    if not files:
        return jsonify({"error": "该任务没有写出文件，暂无可下载的结果"}), 404
    i = request.args.get("i")
    if i is not None:
        try:
            idx = int(i)
        except ValueError:
            return jsonify({"error": "i 必须是整数"}), 400
        if idx < 0 or idx >= len(files):
            return jsonify({"error": "文件序号越界"}), 404
        p = files[idx]
    elif len(files) == 1:
        p = files[0]
    else:
        return jsonify({"error": "有多个结果文件，请指定 i 分别下载"}), 409
    return send_file(str(p), as_attachment=True, download_name=p.name)


@app.route("/api/preview")
def api_preview():
    """预览某个已上传 Excel 的前几行（只允许 data 目录内的文件）。"""
    rel = request.args.get("path", "")
    data_root = config.DATA_DIR.resolve()
    p = (config.DATA_DIR / rel).resolve()
    try:
        p.relative_to(data_root)
    except ValueError:
        return jsonify({"error": "非法路径"}), 400
    if not p.exists() or p.suffix.lower() not in {".xlsx", ".xlsm"}:
        return jsonify({"error": "文件不存在或不是 Excel"}), 404
    from excel_ops import inspect_excel

    info = inspect_excel(str(p))
    if "error" in info:
        return jsonify(info), 400
    return jsonify({"file": p.name, "sheets": info["sheets"]})


@app.route("/api/autofill", methods=["POST"])
def api_autofill():
    """按基准表合并补齐：把对账的两张表合并成一张完整表（只补空、不改原表）。"""
    data = request.get_json(force=True) or {}
    run_id = data.get("run_id", "")
    with LOCK:
        run = RUNS.get(run_id)
    if not run:
        return jsonify({"error": "run_id 不存在"}), 404
    bb = run["orch"].bb
    diff = bb.get("diff")
    plan = bb.get("plan") or {}
    if diff:
        left_file = (diff.get("left") or {}).get("file")
        right_file = (diff.get("right") or {}).get("file")
        keys = diff.get("key_columns") or plan.get("key_columns")
        compare = diff.get("compare_columns") or plan.get("compare_columns")
    else:
        files = plan.get("target_files") or []
        if len(files) != 2:
            return jsonify({"error": "需要两张表才能按基准合并（当前没有可比对的差异结果）"}), 400
        left_file, right_file = files[0], files[1]
        keys = plan.get("key_columns")
        compare = plan.get("compare_columns")
    from tools.data_ops import merge_complete

    res = merge_complete(left_file, right_file, keys, compare)
    if "error" in res:
        return jsonify(res), 400
    bb.set("merged", res)
    return jsonify(res)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    files = request.files.getlist("file") + request.files.getlist("files")
    files = [f for f in files if f and f.filename]
    if not files:
        return jsonify({"error": "没有收到文件"}), 400
    # 目录名安全化：只取最后一段，避免 ../ 之类的路径逃逸
    folder = Path((request.form.get("folder") or "").strip()).name
    dest_dir = config.DATA_DIR / "上传" / folder if folder else config.DATA_DIR / "上传"
    dest_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = Path(f.filename).name
        if not name.lower().endswith((".xlsx", ".xlsm")):
            continue
        f.save(str(dest_dir / name))
        saved.append(name)
    if not saved:
        return jsonify({"error": "只支持 .xlsx / .xlsm 文件"}), 400
    rel_dir = f"上传/{folder}" if folder else "上传"
    return jsonify({
        "ok": True, "files": saved, "folder": folder, "rel_dir": rel_dir,
        # 兼容旧的单文件字段
        "file": saved[0], "rel": f"{rel_dir}/{saved[0]}",
        "path": str(dest_dir / saved[0]),
    })


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
    <div id="fileList"></div>
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
      <button id="btnMerge" class="ghost" disabled title="把对账的两张表按基准合并、补齐成一张完整表">按基准合并补齐</button>
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
    <h2>执行报告
      <span><span id="dlArea"></span>
      <button id="btnReport" class="ghost">刷新报告</button></span>
    </h2>
    <div class="scroll"><pre id="report">执行结束后自动显示。</pre></div>
  </div>
</div>

<div id="modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:99;align-items:center;justify-content:center">
  <div style="background:#fff;border:3px solid #000;max-width:1000px;width:80vw;max-height:82vh;overflow:auto;padding:16px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
      <b id="mtitle" style="font-size:18px"></b>
      <button class="ghost" type="button" onclick="closeModal()">关闭</button>
    </div>
    <div id="mbody"></div>
  </div>
</div>

<script>
let runId=null, timer=null, t0=0, running=false, uploaded=[], mergedInfo=null;
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
  $('btnPlan').disabled=true; $('dlArea').innerHTML=''; $('btnMerge').disabled=true; mergedInfo=null;
  $('status').textContent='规划中…'; $('status').className='tag';
  t0=Date.now(); running=true; $('timer').textContent='0s';
  $('steps').innerHTML='—'; $('agents').innerHTML='—'; $('diff').innerHTML='—'; $('report').textContent='执行结束后自动显示…';
  try{
    const r=await fetch('/api/plan',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({task,scenario:$('scenario').value,dry_run:false,target_files:uploaded})});
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
async function loadResults(){
  if(!runId)return;
  try{
    const r=await fetch('/api/results?run_id='+encodeURIComponent(runId));
    const d=await r.json();
    const area=$('dlArea');
    if(!d.files||!d.files.length){area.innerHTML='';return;}
    if(d.files.length===1){
      area.innerHTML='<a style="border:2px solid #000;padding:6px 12px;text-decoration:none;font-weight:800;color:#000" href="/api/download?run_id='+encodeURIComponent(runId)+'">下载 '+esc(d.files[0].name)+'</a>';
    }else{
      area.innerHTML='下载结果文件：'+d.files.map(f=>'<a style="margin:0 8px" href="/api/download?run_id='+encodeURIComponent(runId)+'&i='+f.i+'">'+esc(f.name)+'</a>').join('');
    }
  }catch(e){}
}
$('btnMerge').onclick=async()=>{
  if(!runId)return;
  const btn=$('btnMerge'); btn.disabled=true; btn.textContent='合并中…';
  try{
    const r=await fetch('/api/autofill',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({run_id:runId})});
    const d=await r.json();
    if(d.error){alert(d.error);return;}
    mergedInfo=d; loadResults();
    const dh=$('diff'); dh.innerHTML=(dh.innerHTML==='—'?'':dh.innerHTML)+renderMerge(mergedInfo);
  }catch(e){alert(e);}
  finally{btn.textContent='按基准合并补齐';}
};

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
  uploaded=[];
  renderFileList();
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
    const label = sid==='clean' ? '要清洗的表格' : '报价表';
    area.innerHTML=
      '<label>选择文件夹（整个目录上传）</label>'+
      '<input type="file" id="dirS" webkitdirectory directory multiple style="font-size:15px">'+
      '<label style="margin-top:6px">或选择文件（可多选）</label>'+
      '<input type="file" id="fileS" accept=".xlsx,.xlsm" multiple style="font-size:15px">'+
      '<div class="row" style="margin-top:8px"><span id="upmsg" style="font-size:15px">选好即自动上传并锁定处理范围</span></div>';
    $('dirS').onchange=()=>uploadPicked($('dirS').files,true);
    $('fileS').onchange=()=>uploadPicked($('fileS').files,false);
  }
}

async function uploadPicked(files,isDir){
  if(!files||!files.length)return;
  const folder=isDir?((files[0].webkitRelativePath||'').split('/')[0]||'上传批次'):'';
  const fd=new FormData();
  for(const f of files) fd.append('file',f);
  if(folder) fd.append('folder',folder);
  $('upmsg').textContent='上传中…';
  const r=await fetch('/api/upload',{method:'POST',body:fd});
  const d=await r.json();
  if(d.error){$('upmsg').textContent=d.error;return;}
  uploaded=d.files.map(n=>d.rel_dir+'/'+n);
  renderFileList();
  $('upmsg').innerHTML='已上传 '+d.files.length+' 个文件（已锁定，只处理这些）：<br>'
    +d.files.map(n=>'· '+esc(n)).join('<br>');
  const sid=$('scenario').value;
  const scope=d.files.length===1?(d.rel_dir+'/'+d.files[0]):d.rel_dir;
  if(sid==='clean'){
    $('task').value='检查 '+scope+'，找出重复记录与缺失字段并生成清洗报告。';
  }else{
    $('task').value='把 '+scope+' 按最新市场行情完成填报，并检查结果。';
  }
}

async function uploadReconcile(){
  const a=$('fileA').files[0], b=$('fileB').files[0];
  if(!a||!b){alert('请把「原版 / 基准表」和「要对比的版本」两个都选上');return;}
  $('upmsg').textContent='上传中…';
  const da=await doUpload(a), db=await doUpload(b);
  if(da.error||db.error){$('upmsg').textContent=(da.error||db.error);return;}
  $('upmsg').textContent='已上传 2 个文件';
  uploaded=[da.rel,db.rel];
  renderFileList();
  $('task').value='核对 '+da.rel+'（原版/基准）与 '+db.rel+'（待核对），找出只在单方存在、字段不一致和重复的记录，生成差异报告。';
}

function renderFileList(){
  const box=$('fileList');
  if(!uploaded.length){box.innerHTML='';return;}
  box.innerHTML='<div style="margin-top:8px;font-weight:800">已上传文件（点文件名预览）：</div>'
    +uploaded.map((p,i)=>'<div class="step" data-i="'+i+'" style="cursor:pointer;padding:6px 10px">📄 '+esc(p.split('/').pop())+' <span style="color:#555;font-size:14px">'+esc(p)+'</span></div>').join('');
  box.querySelectorAll('[data-i]').forEach(el=>{el.onclick=()=>previewFile(uploaded[+el.dataset.i]);});
}
async function previewFile(rel){
  openModal(rel.split('/').pop());
  $('mbody').innerHTML='加载中…';
  try{
    const r=await fetch('/api/preview?path='+encodeURIComponent(rel));
    const d=await r.json();
    if(d.error){$('mbody').textContent=d.error;return;}
    let h='';
    (d.sheets||[]).forEach(s=>{
      h+='<div class="dhead">工作表：'+esc(s.name)+'（共 '+s.max_row+' 行）</div>';
      h+='<table><tr>'+((s.header||[]).map(c=>'<th>'+esc(c)+'</th>').join(''))+'</tr>'
        +((s.preview||[]).map(row=>'<tr>'+((s.header||[]).map((c,i)=>'<td>'+esc(row[i])+'</td>')).join('')+'</tr>').join(''))
        +'</table>';
    });
    $('mbody').innerHTML=h||'（空表）';
  }catch(e){$('mbody').textContent='预览失败：'+e;}
}
function openModal(title){$('mtitle').textContent='预览：'+title;$('modal').style.display='flex';}
function closeModal(){$('modal').style.display='none';}

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
function renderMerge(m){
  if(!m||m.status!=='ok')return '';
  let h='<div class="dhead">按基准合并补齐（已生成完整表）</div>';
  h+='<div>合并后共 <b>'+m.rows+'</b> 行：基准表 '+m.base_rows+' 行，另追加 '+(m.added_from_right||0)+' 行（仅待核对表）；补全空字段 '+(m.filled_count||0)+' 处。</div>';
  h+='<div style="margin:4px 0">输出文件：<b>'+esc(m.output_name)+'</b>（右上角「下载结果文件」可取）</div>';
  if(m.filled&&m.filled.length){
    h+='<table><tr><th>关键值</th><th>补全列</th><th>从另一表补入的值</th></tr>'
      +m.filled.slice(0,100).map(f=>'<tr><td>'+esc([].concat(f.key).join(' / '))+'</td><td>'+esc(f.column)+'</td><td>'+esc(f.value)+'</td></tr>').join('')+'</table>';
  }else{
    h+='<div>（没有可补的空字段）</div>';
  }
  if(m.added_keys&&m.added_keys.length){
    h+='<div class="dhead">追加的键（仅待核对表存在）</div><div>'+m.added_keys.map(k=>esc([].concat(k).join(' / '))).join('、')+'</div>';
  }
  return h;
}
function renderDuplicates(dup){
  let h='<div class="dhead">重复记录（按关键列判重）</div>';
  const groups=(dup&&dup.duplicate_groups)||[];
  if(!groups.length){h+='<div>未发现重复记录</div>';return h;}
  h+='<table><tr><th>关键值</th><th>重复所在行</th></tr>'
    +groups.map(g=>'<tr><td>'+esc([].concat(g.key).join(' / '))+'</td><td>'+esc((g.rows||[]).join(', '))+'</td></tr>').join('')
    +'</table>';
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
    if(d.duplicates&&$('scenario').value==='clean'){dh+=renderDuplicates(d.duplicates);}
    if(d.findings&&d.findings.length){dh+=renderFindings(d.findings);}
    if(mergedInfo){dh+=renderMerge(mergedInfo);}
    $('diff').innerHTML=dh||'—';
    if(['done','cancelled','budget_exceeded','error'].includes(d.status)){
      running=false;$('timer').textContent=Math.floor((Date.now()-t0)/1000)+'s';clearInterval(timer);timer=null;loadReport();
      if(['done','budget_exceeded'].includes(d.status))loadResults();
      if(d.diff)$('btnMerge').disabled=false;
    }
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
