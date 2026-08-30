"""The operator dashboard -- one self-contained HTML page.

Lab managers do not buy a REST API; they buy the screen that shows them what
the agents are doing and a big red button that stops it. This page is that
screen: live device states, the durable task board, open incidents and
quarantines, running jobs, the event stream, and the e-stop.

Engineering constraints, deliberately strict:

  - ONE file, served from memory by the gateway itself at ``GET /``. No build
    toolchain, no npm, no CDN -- it must render inside an air-gapped lab
    network exactly as it renders anywhere else.
  - The page shell carries NO data. Every byte of lab state is fetched by the
    browser through the same authenticated ``POST /tools/*`` endpoints agents
    use -- the dashboard has no privileged side door, so it cannot become one.
  - The API key lives in a JavaScript variable for the lifetime of the tab.
    It is never written to localStorage, sessionStorage, cookies, or the URL.
  - The event feed uses ``fetch``-streamed SSE, not ``EventSource``, because
    EventSource cannot send an Authorization header.
"""

from __future__ import annotations

DASHBOARD_CSP = ("default-src 'none'; style-src 'unsafe-inline'; "
                 "script-src 'unsafe-inline'; connect-src 'self'; "
                 "img-src data:; base-uri 'none'; form-action 'none'; "
                 "frame-ancestors 'none'")

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LabAIAgent — Operator Console</title>
<style>
  :root{
    --bg:#0d1117; --panel:#161b22; --panel2:#1c2129; --line:#2d333b;
    --text:#e6edf3; --dim:#8b949e; --accent:#58a6ff; --ok:#3fb950;
    --warn:#d29922; --bad:#f85149; --purple:#bc8cff;
    font-size:14px;
  }
  *{box-sizing:border-box; margin:0}
  body{background:var(--bg); color:var(--text);
       font:1rem/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
       padding:0 0 40px}
  header{display:flex; align-items:center; gap:14px; padding:12px 20px;
         background:var(--panel); border-bottom:1px solid var(--line);
         position:sticky; top:0; z-index:5}
  header h1{font-size:1.05rem; font-weight:600; letter-spacing:.4px}
  header h1 span{color:var(--accent)}
  .dot{width:9px;height:9px;border-radius:50%;background:var(--dim);
       display:inline-block; margin-right:5px}
  .dot.ok{background:var(--ok); box-shadow:0 0 6px var(--ok)}
  .dot.bad{background:var(--bad); box-shadow:0 0 6px var(--bad)}
  #labname{color:var(--dim)}
  .spacer{flex:1}
  input[type=password]{background:var(--panel2); border:1px solid var(--line);
       color:var(--text); padding:6px 10px; border-radius:6px; width:210px}
  button{background:var(--panel2); border:1px solid var(--line); color:var(--text);
       padding:6px 14px; border-radius:6px; cursor:pointer}
  button:hover{border-color:var(--accent)}
  #estop{background:#3d1113; border:1px solid var(--bad); color:#ffb3ae;
       font-weight:700; letter-spacing:1px}
  #estop:hover{background:var(--bad); color:#fff}
  main{display:grid; grid-template-columns:repeat(auto-fit,minmax(340px,1fr));
       gap:14px; padding:16px 20px; max-width:1700px; margin:0 auto}
  section{background:var(--panel); border:1px solid var(--line);
       border-radius:10px; overflow:hidden; min-height:120px}
  section.wide{grid-column:1/-1}
  h2{font-size:.8rem; text-transform:uppercase; letter-spacing:1.2px;
     color:var(--dim); padding:10px 14px; border-bottom:1px solid var(--line);
     display:flex; justify-content:space-between}
  h2 small{font-weight:400; text-transform:none; letter-spacing:0}
  .body{padding:10px 14px; max-height:420px; overflow:auto}
  .card{background:var(--panel2); border:1px solid var(--line); border-radius:8px;
        padding:9px 12px; margin-bottom:8px}
  .row{display:flex; justify-content:space-between; gap:8px; align-items:baseline}
  .id{font-weight:700}
  .dim{color:var(--dim); font-size:.85rem}
  .state{font-size:.75rem; padding:2px 9px; border-radius:20px;
         border:1px solid var(--line); text-transform:uppercase;
         letter-spacing:.5px}
  .state.idle{color:var(--ok); border-color:var(--ok)}
  .state.busy{color:var(--warn); border-color:var(--warn)}
  .state.error,.state.estopped,.state.failed{color:var(--bad); border-color:var(--bad)}
  .state.disconnected{color:var(--dim)}
  .state.running,.state.in_progress{color:var(--accent); border-color:var(--accent)}
  .state.done,.state.succeeded{color:var(--ok); border-color:var(--ok)}
  .state.pending,.state.queued{color:var(--purple); border-color:var(--purple)}
  .state.cancelled{color:var(--dim)}
  #feed{font-size:.82rem}
  #feed div{padding:3px 0; border-bottom:1px dashed #21262d; white-space:nowrap;
            overflow:hidden; text-overflow:ellipsis}
  #feed .t{color:var(--dim); margin-right:8px}
  #feed .e{color:var(--accent); margin-right:8px}
  .banner{display:none; background:#3d1113; color:#ffb3ae; text-align:center;
          padding:8px; font-weight:700; letter-spacing:2px;
          border-bottom:1px solid var(--bad)}
  .empty{color:var(--dim); font-style:italic; padding:8px 2px}
  .kpis{display:flex; gap:10px; flex-wrap:wrap; padding:12px 14px}
  .kpi{background:var(--panel2); border:1px solid var(--line); border-radius:8px;
       padding:8px 16px; text-align:center; min-width:110px}
  .kpi b{display:block; font-size:1.5rem}
  .kpi span{color:var(--dim); font-size:.75rem; text-transform:uppercase;
            letter-spacing:1px}
  footer{color:var(--dim); text-align:center; font-size:.78rem; padding-top:18px}
</style>
</head>
<body>
<div class="banner" id="estopBanner">EMERGENCY STOP LATCHED — HUMAN RESET REQUIRED</div>
<header>
  <h1><span>Lab</span>AIAgent</h1>
  <span id="labname"><span class="dot" id="connDot"></span>not connected</span>
  <div class="spacer"></div>
  <input type="password" id="apikey" placeholder="API key (blank if loopback)"
         autocomplete="off">
  <button id="connect">Connect</button>
  <button id="estop">E-STOP</button>
</header>

<main>
  <section class="wide"><h2>Lab at a glance</h2><div class="kpis" id="kpis"></div></section>
  <section><h2>Instruments <small id="devCount"></small></h2><div class="body" id="devices"></div></section>
  <section><h2>Task board <small>durable — survives restarts</small></h2><div class="body" id="tasks"></div></section>
  <section><h2>Incidents &amp; quarantines</h2><div class="body" id="incidents"></div></section>
  <section><h2>Jobs</h2><div class="body" id="jobs"></div></section>
  <section class="wide"><h2>Live events <small id="feedState">stream off</small></h2>
    <div class="body" id="feed"></div></section>
</main>
<footer>LabAIAgent operator console — every action on this page goes through the
same authenticated, audited tool endpoints the agents use.</footer>

<script>
"use strict";
let KEY = "";                     // in-memory only, never persisted
let timer = null, feedAbort = null;

function headers(){
  const h = {"Content-Type":"application/json"};
  if (KEY) h["Authorization"] = "Bearer " + KEY;
  return h;
}
async function tool(name, args){
  const r = await fetch("/tools/"+name, {method:"POST", headers:headers(),
                                         body:JSON.stringify(args||{})});
  if (r.status === 401) throw new Error("unauthorized");
  return r.json();
}
function el(id){ return document.getElementById(id); }
function esc(s){ const d=document.createElement("div");
                 d.textContent=String(s??"");
                 // innerHTML escapes <>& but NOT quotes; some esc() output is
                 // interpolated into attribute values, so escape both quotes
                 // too (defence against a malicious device id/state string).
                 return d.innerHTML.replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
function state(s){
  s = String(s??"");
  // class attribute gets a WHITELISTED token only, never a raw string
  const cls = /^[a-z_]+$/.test(s) ? s : "unknown";
  return '<span class="state '+cls+'">'+esc(s)+"</span>"; }

async function refresh(){
  try{
    const health = await (await fetch("/health",{headers:headers()})).json();
    el("connDot").className = "dot ok";
    el("labname").innerHTML = '<span class="dot ok"></span>'+esc(health.lab||"lab");
    el("estopBanner").style.display = health.emergency_stop ? "block":"none";

    const dv = await tool("list_devices",{});
    if (dv.ok){
      const devs = dv.result.devices;
      el("devCount").textContent = devs.length + " connected";
      el("devices").innerHTML = devs.map(d =>
        '<div class="card"><div class="row"><span class="id">'+esc(d.id)+
        "</span>"+state(d.state)+'</div><div class="dim">'+esc(d.vendor)+" "+
        esc(d.model)+" · "+esc(d.category)+(d.simulated?" · SIMULATED":"")+
        "</div></div>").join("") || '<div class="empty">no devices</div>';
      const byState = {};
      devs.forEach(d => byState[d.state]=(byState[d.state]||0)+1);
      kpis(devs.length, byState, health.emergency_stop);
    }
    const tk = await tool("lab_tasks",{action:"list"});
    if (tk.ok){
      const t = tk.result;
      el("tasks").innerHTML = (t.tasks||[]).slice(0,30).map(x =>
        '<div class="card"><div class="row"><span>'+esc(x.title)+"</span>"+
        state(x.status)+'</div><div class="dim">#'+esc(x.id)+" · priority "+
        esc(x.priority)+(x.note ? " · "+esc(x.note):"")+"</div></div>"
      ).join("") || '<div class="empty">task board empty</div>';
      const inc = (t.open_incidents||[]).map(i =>
        '<div class="card"><div class="row"><span class="id">'+esc(i.device)+
        "</span>"+state("error")+'</div><div class="dim">'+esc(i.summary||i.error||"")+
        '</div><div class="dim">incident '+esc(i.id)+
        " — human resolve_incident required</div></div>").join("");
      const q = (t.quarantined_devices||[]).map(d =>
        '<div class="card"><div class="row"><span class="id">'+esc(d.device||d)+
        "</span>"+state("estopped")+'</div><div class="dim">quarantined</div></div>'
      ).join("");
      el("incidents").innerHTML = (inc+q) ||
        '<div class="empty">no open incidents — good</div>';
    } else {
      el("tasks").innerHTML = '<div class="empty">'+esc(tk.message||"memory off")+"</div>";
      el("incidents").innerHTML = '<div class="empty">—</div>';
    }
    const jb = await tool("list_jobs",{limit:15});
    if (jb.ok){
      el("jobs").innerHTML = (jb.result.jobs||[]).map(j =>
        '<div class="card"><div class="row"><span>'+esc(j.label)+"</span>"+
        state(j.state)+'</div><div class="dim">'+esc(j.job_id)+" · "+
        esc(j.actor||"local")+" · "+(j.elapsed_s!=null? j.elapsed_s+"s":"")+
        (j.error? " · "+esc(j.error):"")+"</div></div>").join("")
        || '<div class="empty">no jobs yet</div>';
    }
  }catch(err){
    el("connDot").className = "dot bad";
    el("labname").innerHTML = '<span class="dot bad"></span>'+
      (String(err.message).includes("unauthorized") ? "bad API key" : "unreachable");
  }
}

function kpis(nDev, byState, estop){
  const k = [["instruments", nDev],
             ["idle", byState.idle||0],
             ["busy", byState.busy||0],
             ["error", (byState.error||0)+(byState.estopped||0)],
             ["e-stop", estop ? "LATCHED":"clear"]];
  el("kpis").innerHTML = k.map(([n,v]) =>
    '<div class="kpi"><b>'+esc(v)+"</b><span>"+esc(n)+"</span></div>").join("");
}

async function startFeed(){
  if (feedAbort) feedAbort.abort();
  feedAbort = new AbortController();
  el("feedState").textContent = "connecting…";
  try{
    const resp = await fetch("/events",{headers:headers(), signal:feedAbort.signal});
    if (!resp.ok || !resp.body){ el("feedState").textContent="stream unavailable"; return; }
    el("feedState").textContent = "live";
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for(;;){
      const {done, value} = await reader.read();
      if (done) break;
      buf += dec.decode(value, {stream:true});
      let idx;
      while((idx = buf.indexOf("\n\n")) >= 0){
        const frame = buf.slice(0, idx); buf = buf.slice(idx+2);
        const data = frame.split("\n").filter(l=>l.startsWith("data:"))
                          .map(l=>l.slice(5).trim()).join("");
        if (!data) continue;
        try{ addFeed(JSON.parse(data)); }catch(e){}
      }
    }
  }catch(e){ /* aborted or dropped */ }
  el("feedState").textContent = "stream off";
}
function addFeed(ev){
  const f = el("feed");
  const d = document.createElement("div");
  const t = new Date().toLocaleTimeString();
  const name = ev.event || ev.type || "event";
  const rest = Object.entries(ev).filter(([k]) =>
      !["event","type","timestamp"].includes(k))
    .map(([k,v]) => k+"="+(typeof v==="object"? JSON.stringify(v): v))
    .join(" ").slice(0,220);
  d.innerHTML = '<span class="t">'+esc(t)+'</span><span class="e">'+
                esc(name)+"</span>"+esc(rest);
  f.prepend(d);
  while (f.childNodes.length > 200) f.removeChild(f.lastChild);
}

el("connect").onclick = () => {
  KEY = el("apikey").value.trim();
  if (timer) clearInterval(timer);
  refresh();
  timer = setInterval(refresh, 4000);
  startFeed();
};
el("estop").onclick = async () => {
  const reason = prompt("EMERGENCY STOP every instrument.\nReason (recorded in the audit trail):");
  if (reason === null) return;
  const out = await tool("emergency_stop",{reason: reason || "dashboard e-stop"});
  if (out.ok) refresh();
  else alert("E-stop call failed: " + (out.message||out.error));
};
// Auto-connect for the loopback / no-key case.
el("connect").click();
</script>
</body>
</html>
"""

__all__ = ["DASHBOARD_HTML", "DASHBOARD_CSP"]
