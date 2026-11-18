"""运维操作网页后端：聚合两台代理状态、转发真实诊断、切换模拟后端模式。"""
from __future__ import annotations

import asyncio
import os

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

PROXY_A = os.environ.get("PROXY_A_URL", "http://proxy-a:8000")
PROXY_B = os.environ.get("PROXY_B_URL", "http://proxy-b:8000")
BACKEND = os.environ.get("BACKEND_URL", "http://backend:8000")
HTTP_TIMEOUT = float(os.environ.get("WEB_HTTP_TIMEOUT", "6"))

app = FastAPI(title="设备诊断熔断演练台")
PROXIES = {"a": PROXY_A, "b": PROXY_B}


class DiagnoseBody(BaseModel):
    device_id: str
    proxy: str = "a"


class ConcurrentBody(BaseModel):
    device_id: str
    proxy: str = "a"
    count: int = Field(3, ge=2, le=10)
    delay: float = Field(0.0, ge=0.0, le=10.0)


class ModeBody(BaseModel):
    mode: str


async def _get(url: str) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.get(url)
            return resp.status_code, resp.json()
    except httpx.HTTPError as exc:
        return 503, {"ok": False, "error": "UPSTREAM_UNREACHABLE", "message": str(exc)}


async def _post(url: str, json: dict | None = None) -> tuple[int, dict]:
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            resp = await client.post(url, json=json)
            return resp.status_code, resp.json()
    except httpx.HTTPError as exc:
        return 503, {"ok": False, "error": "UPSTREAM_UNREACHABLE", "message": str(exc)}


@app.get("/health")
async def health() -> dict:
    """供容器健康检查使用：只反映网页进程本身存活。"""
    return {"service": "web", "ok": True}


@app.get("/api/overview")
async def overview() -> dict:
    proxies: dict[str, dict] = {}
    for name, base in PROXIES.items():
        health_code, health = await _get(f"{base}/health")
        cb_code, cb = await _get(f"{base}/cb/status")
        proxies[name] = {
            "base": base,
            "health": health,
            "health_http": health_code,
            "cb": cb,
            "cb_http": cb_code,
        }
    _, backend_mode = await _get(f"{BACKEND}/admin/mode")

    # 两台代理读写同一份 Redis 状态；任一可用即可作为全局视图。
    available = [p["cb"] for p in proxies.values() if p["cb_http"] == 200]
    shared = available[0] if available else {"state": "UNKNOWN"}
    # 若两台代理视图不一致（例如一台 Redis 连接异常），页面要如实提示
    snapshots = {
        (cb.get("state"), cb.get("generation"), cb.get("probe_owner"))
        for cb in available
    }
    return {
        "proxies": proxies,
        "backend_mode": backend_mode,
        "shared_cb": shared,
        "shared_sources": [
            name for name, p in proxies.items() if p["cb_http"] == 200
        ],
        "views_diverged": len(snapshots) > 1,
    }


@app.post("/api/diagnose")
async def diagnose(body: DiagnoseBody) -> dict:
    base = PROXIES.get(body.proxy)
    if base is None:
        return {"ok": False, "error": f"未知代理：{body.proxy}（应为 a 或 b）"}
    code, payload = await _get(f"{base}/diagnose/{body.device_id}")
    return {"http_status": code, **payload}


@app.post("/api/diagnose/concurrent")
async def diagnose_concurrent(body: ConcurrentBody) -> dict:
    """同一代理同时发起多条诊断，用于观测“一份探测权只放行一条真实请求”。

    delay 只用于让探测在途时间变长（正常模式下也可稳定观察归属）；
    它经由代理透传给模拟后端，不改变熔断语义。
    """
    base = PROXIES.get(body.proxy)
    if base is None:
        return {"ok": False, "error": f"未知代理：{body.proxy}（应为 a 或 b）"}
    query = f"?delay={body.delay}" if body.delay > 0 else ""
    results = await asyncio.gather(
        *[
            _get(f"{base}/diagnose/{body.device_id}{query}")
            for _ in range(body.count)
        ]
    )
    return {
        "proxy": body.proxy,
        "count": body.count,
        "delay": body.delay,
        "results": [{"http_status": code, **payload} for code, payload in results],
    }


@app.post("/api/backend/mode")
async def backend_mode(body: ModeBody) -> dict:
    code, payload = await _post(f"{BACKEND}/admin/mode", {"mode": body.mode})
    return {"http_status": code, **payload}


@app.post("/api/reset")
async def reset() -> dict:
    results = {}
    for name, base in PROXIES.items():
        code, payload = await _post(f"{base}/cb/reset")
        results[name] = {"http_status": code, **payload}
    return {"results": results}


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _PAGE


_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>设备诊断熔断演练台</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: "PingFang SC","Microsoft YaHei",system-ui,sans-serif;
         background:#0f1420; color:#e6e9ef; }
  header { padding:20px 28px; background:#161d2e; border-bottom:1px solid #243049; }
  header h1 { margin:0; font-size:20px; }
  header p { margin:6px 0 0; color:#8b96ad; font-size:13px; }
  main { display:grid; grid-template-columns: 390px 1fr; gap:18px; padding:22px 28px; }
  .card { background:#161d2e; border:1px solid #243049; border-radius:12px; padding:18px; }
  .card h2 { margin:0 0 14px; font-size:15px; color:#9db2d8; }
  label { display:block; font-size:13px; color:#aeb8cc; margin:10px 0 6px; }
  input, select { width:100%; padding:10px 12px; border-radius:8px;
        border:1px solid #2c3a57; background:#0f1420; color:#e6e9ef; font-size:14px; }
  button { cursor:pointer; border:0; border-radius:8px; padding:10px 16px;
        font-size:14px; font-weight:600; margin-top:12px; }
  button:disabled { opacity:.55; cursor:not-allowed; }
  .primary { background:#3b82f6; color:#fff; width:100%; }
  .primary:hover { background:#2f6fe0; }
  .row { display:flex; gap:10px; }
  .row button { flex:1; }
  .normal { background:#16a34a; color:#fff; }
  .timeout { background:#dc2626; color:#fff; }
  .ghost { background:#2c3a57; color:#cdd6ea; width:100%; }
  .badge { display:inline-block; padding:3px 10px; border-radius:999px; font-size:12px; font-weight:700; }
  .b-CLOSED { background:#14532d; color:#86efac; }
  .b-OPEN { background:#7f1d1d; color:#fca5a5; }
  .b-HALF_OPEN { background:#78350f; color:#fcd34d; }
  .b-UNKNOWN { background:#374151; color:#d1d5db; }
  .kv { display:grid; grid-template-columns:130px 1fr; gap:6px 10px; font-size:13px; margin-top:8px; }
  .kv .k { color:#8b96ad; }
  .proxy-box { border:1px solid #243049; border-radius:10px; padding:12px; margin-top:10px; }
  .proxy-box h3 { margin:0 0 6px; font-size:13px; display:flex; justify-content:space-between; gap:8px; }
  .ok { color:#4ade80; } .bad { color:#f87171; } .warn { color:#fbbf24; }
  pre { background:#0b1020; border:1px solid #243049; border-radius:10px;
        padding:14px; font-size:12.5px; overflow:auto; max-height:40vh; white-space:pre-wrap; }
  .meta { font-size:12px; color:#8b96ad; margin:4px 0 10px; }
  .pill { font-size:11px; padding:2px 8px; border-radius:6px; background:#243049; color:#aeb8cc; }
  .result { border:1px solid #243049; border-radius:10px; padding:12px; margin-top:10px; font-size:13px; }
  .result h3 { margin:0 0 8px; font-size:13px; display:flex; justify-content:space-between; gap:8px; }
  .result.allowed { border-color:#166534; }
  .result.rejected { border-color:#7f1d1d; }
  .alert { border:1px solid #92400e; background:#3b2206; color:#fcd34d;
           border-radius:10px; padding:10px 12px; font-size:13px; margin-bottom:10px; }
  ul.checks { margin:6px 0 0 18px; padding:0; color:#cdd6ea; }
  details summary { cursor:pointer; color:#8b96ad; font-size:12px; margin-top:10px; }
  @media (max-width: 980px){ main{grid-template-columns:1fr;} }
</style>
</head>
<body>
<header>
  <h1>设备诊断 · 双代理熔断器演练台</h1>
  <p>两台代理共享 Redis 熔断状态：合计连续 3 次超时即熔断，冷却 5 秒后半开；每份探测租约全局只放行一条真实请求，探测成功才恢复。</p>
</header>
<main>
  <section>
    <div class="card">
      <h2>① 模拟后端模式</h2>
      <div class="row">
        <button class="normal" id="btnNormal" onclick="setMode('normal')">切到正常</button>
        <button class="timeout" id="btnTimeout" onclick="setMode('timeout')">切到超时</button>
      </div>
      <p class="meta" id="backendMode">当前模式：读取中…</p>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>② 发起真实诊断</h2>
      <label>设备号</label>
      <input id="deviceId" value="DEV-10086" placeholder="例如 DEV-10086">
      <label>入口代理（两代理共享同一熔断器）</label>
      <select id="proxySel"><option value="a">代理 A (proxy-a)</option><option value="b">代理 B (proxy-b)</option></select>
      <button class="primary" id="btnDiagnose" onclick="diagnose()">发起诊断</button>
      <button class="ghost" id="btnBurst" onclick="diagnoseBurst()">同代理并发 3 次（观察探测归属）</button>
      <button class="ghost" id="btnReset" onclick="resetCb()">复位熔断器（管理操作）</button>
    </div>
  </section>
  <section>
    <div class="card">
      <h2>③ 共享熔断状态与代理 <span class="pill">每 1 秒自动刷新</span></h2>
      <div id="cbPanel">加载中…</div>
    </div>
    <div class="card" style="margin-top:16px">
      <h2>④ 真实诊断响应</h2>
      <p class="meta" id="respMeta">尚未发起请求</p>
      <div id="respCards"></div>
      <details><summary>原始响应 JSON</summary><pre id="respBox">{}</pre></details>
    </div>
  </section>
</main>
<script>
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const badge = state => `<span class="badge b-${esc(state || 'UNKNOWN')}">${esc(state || 'UNKNOWN')}</span>`;
const pct = v => (v === undefined || v === null) ? '—' : String(v);
function leaseText(cb){
  if (!cb.probe_in_progress) return '<span class="warn">无有效探测租约</span>';
  const rem = cb.probe_lease_remaining_ms ? (cb.probe_lease_remaining_ms/1000).toFixed(1)+' s' : '—';
  return `<b>${esc(cb.probe_owner)}</b>（剩余 ${rem}）`;
}

async function refresh(){
  let d;
  try { d = await (await fetch('/api/overview')).json(); }
  catch (e) { document.getElementById('cbPanel').textContent = '读取概览失败：' + e; return; }
  const cb = d.shared_cb || {};
  const remain = cb.cooldown_remaining_ms ? (cb.cooldown_remaining_ms/1000).toFixed(1)+' s' : '—';
  const threshold = cb.failure_threshold ?? 3;
  let html = '';
  if (d.views_diverged) {
    html += `<div class="alert">两台代理读到不同的熔断视图，请检查 Redis 连通性。</div>`;
  }
  if (!d.shared_sources || d.shared_sources.length === 0) {
    html += `<div class="alert">两台代理都无法读取共享熔断状态（Redis 可能不可用），诊断请求会被明确拒绝。</div>`;
  }
  html += `<div class="kv" style="grid-template-columns:150px 1fr">
      <span class="k">全局熔断状态</span><span>${badge(cb.state)}</span>
      <span class="k">连续超时</span><span><b>${pct(cb.failures)}</b> / ${esc(threshold)}</span>
      <span class="k">冷却剩余</span><span>${remain}</span>
      <span class="k">代数 generation</span><span>${pct(cb.generation)}</span>
      <span class="k">当前探测权</span><span>${leaseText(cb)}</span>
      <span class="k">共享状态来源</span><span>${esc((d.shared_sources||[]).join(' / ') || '无')}</span>
    </div>`;
  for (const [name, p] of Object.entries(d.proxies || {})) {
    const redisOk = p.health?.redis_ok;
    const state = p.cb?.state || 'UNKNOWN';
    html += `<div class="proxy-box">
      <h3><span>代理 ${name.toUpperCase()}（${esc(p.base)}）</span>
        <span class="${redisOk ? 'ok':'bad'}">Redis ${redisOk ? '连通' : '不可用'}</span></h3>
      <div class="kv">
        <span class="k">熔断视图</span><span>${badge(state)}</span>
        <span class="k">连续超时</span><span>${pct(p.cb?.failures)}</span>
        <span class="k">探测归属</span><span>${p.cb?.probe_in_progress ? esc(p.cb?.probe_owner) : '—'}</span>
        <span class="k">健康检查 HTTP</span><span>${p.health_http}</span>
        <span class="k">状态接口 HTTP</span><span>${p.cb_http}</span>
        <span class="k">错误信息</span><span>${esc(p.cb?.error || p.health?.error || '无')}</span>
      </div></div>`;
  }
  document.getElementById('cbPanel').innerHTML = html;
  const mode = d.backend_mode?.mode;
  document.getElementById('backendMode').innerHTML =
    '当前模式：<b class="'+(mode==='timeout'?'bad':'ok')+'">'+esc(mode || '未知')+'</b>';
}

function renderResults(items){
  return items.map((d, i) => {
    const rejected = d.rejected === true;
    const cls = rejected ? 'rejected' : 'allowed';
    const title = d.title
      ? esc(d.title)
      : (rejected ? `被拒绝 · ${esc(d.reason)}` : (d.ok ? '诊断成功' : '后端失败'));
    let rows = `
      <div class="kv">
        <span class="k">HTTP / 代理</span><span>${esc(d.http_status ?? '—')} · ${esc(d.proxy || '—')}</span>
        <span class="k">request_id</span><span>${esc(d.request_id || '—')}</span>
        <span class="k">是否探测</span><span>${d.is_probe ? '是' : '否'}</span>
        <span class="k">熔断状态</span><span>${badge(d.cb_state || d.cb?.state || 'UNKNOWN')} · generation ${pct(d.cb?.generation)}</span>
        <span class="k">连续超时</span><span>${pct(d.cb?.failures)}</span>`;
    if (!rejected) {
      rows += `<span class="k">后端耗时</span><span>${pct(d.backend_latency_ms)} ms</span>
        <span class="k">结果被采纳</span><span>${d.cb_result_applied === false ? '<span class="warn">否（迟到结果已忽略）</span>' : '是'}</span>`;
    }
    rows += `</div>`;
    if (d.message) rows += `<p class="meta">${esc(d.message)}</p>`;
    const b = d.backend;
    if (b && b.ok) {
      const res = b.results || {};
      rows += `<div class="meta">设备 ${esc(b.device_id)} · 诊断时间 ${esc(b.diagnosed_at)}</div>
        <div class="kv">
          <span class="k">CPU 温度</span><span>${pct(res.cpu_temp_c)} °C</span>
          <span class="k">内存占用</span><span>${pct(res.memory_used_pct)} %</span>
          <span class="k">磁盘占用</span><span>${pct(res.disk_used_pct)} %</span>
          <span class="k">自检项</span><span>${esc((res.checks||[]).join('、') || '—')}</span>
        </div>`;
    } else if (b && !b.ok) {
      rows += `<p class="meta bad">后端返回：${esc(b.error || '未知错误')} — ${esc(b.message || '')}</p>`;
    }
    const label = items.length > 1 ? `#${i+1} ` : '';
    return `<div class="result ${cls}"><h3><span>${label}${title}</span><span class="pill">${esc(d.http_status ?? '')}</span></h3>${rows}</div>`;
  }).join('');
}

function showResponse(meta, payload, items){
  document.getElementById('respMeta').textContent = meta;
  document.getElementById('respCards').innerHTML = renderResults(items);
  document.getElementById('respBox').textContent = JSON.stringify(payload, null, 2);
}

async function setMode(mode){
  const d = await (await fetch('/api/backend/mode', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode})})).json();
  showResponse('后端模式切换 → HTTP ' + d.http_status, d, [{
    http_status: d.http_status, ok: d.ok, title: d.ok ? '后端模式已切换' : '模式切换失败',
    message: d.error || ('当前模式 ' + (d.mode || mode)),
    backend: null, rejected: false, cb: {},
  }]);
  refresh();
}

async function diagnose(){
  const body = {device_id: document.getElementById('deviceId').value || 'DEV-0000',
                proxy: document.getElementById('proxySel').value};
  const t0 = performance.now();
  const d = await (await fetch('/api/diagnose', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json();
  const ms = Math.round(performance.now()-t0);
  showResponse(`入口 proxy-${body.proxy} · HTTP ${d.http_status} · 耗时 ${ms} ms · request_id ${d.request_id || '-'}`, d, [d]);
  refresh();
}

async function diagnoseBurst(){
  const body = {device_id: document.getElementById('deviceId').value || 'DEV-0000',
                proxy: document.getElementById('proxySel').value, count: 3, delay: 1.0};
  const t0 = performance.now();
  const d = await (await fetch('/api/diagnose/concurrent', {method:'POST',
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})).json();
  const ms = Math.round(performance.now()-t0);
  const allowed = (d.results || []).filter(r => r.rejected !== true).length;
  showResponse(`代理 ${body.proxy.toUpperCase()} 并发 3 次（探测在途 1s） · 总耗时 ${ms} ms · 真实放行 ${allowed} 条`, d, d.results || []);
  refresh();
}

async function resetCb(){
  const d = await (await fetch('/api/reset', {method:'POST'})).json();
  showResponse('熔断器已复位', d, [{http_status: 200, ok: true, title: '熔断器已复位',
    message: '已清空共享熔断状态与探测租约', rejected: false, cb: {}}]);
  refresh();
}
refresh(); setInterval(refresh, 1000);
</script>
</body>
</html>
"""
