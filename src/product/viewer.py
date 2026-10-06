"""Offline trace viewer; only JSON already written by the product runtime."""
from __future__ import annotations
import html
import json
from pathlib import Path


def render(out):
    out=Path(out)
    decisions=json.loads((out/'decisions.json').read_text(encoding='utf8'))
    summary=json.loads((out/'summary.json').read_text(encoding='utf8'))
    payload=json.dumps({'summary':summary,'decisions':decisions},ensure_ascii=False).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Candidate 3 · 证据研究闭环</title>
<style>body{font:16px system-ui;margin:0;background:#101824;color:#e5edf5}main{max-width:1200px;margin:auto;padding:32px}h1{font-size:30px}p{color:#aabbd0;line-height:1.7}.tag{color:#64debe}button,select{padding:10px 18px;background:#203249;color:inherit;border:1px solid #4c6688;border-radius:8px;margin:6px}section{background:#172537;padding:20px;border-radius:12px;min-width:0}article{display:grid;grid-template-columns:1fr 1fr;gap:16px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px/1.6 ui-monospace,monospace}h2{font-size:18px;color:#78dec9}#summary{padding:16px;border-left:3px solid #78dec9}footer{margin-top:24px;color:#aabbd0}@media(max-width:800px){article{grid-template-columns:1fr}main{padding:16px}}</style>
<main><span class="tag">EVIDENCE ACQUISITION & VERIFICATION</span><h1>一次决策，怎样改变下一步研究</h1>
<p>环境是明确标注的 fixture；此页展示本次运行实际保存的决策，不会重新调用模型。用于解释机制，不是临床建议或公开数据准确率证明。</p>
<div id="summary"></div><nav><button id="prev">← 上一步</button><select id="step" aria-label="选择决策步骤"></select><button id="next">下一步 →</button></nav>
<article><section><h2>1 · Goal / State → 有限 Context</h2><pre id="context"></pre></section>
<section><h2>2 · Decision → 受约束的 Action</h2><pre id="decision"></pre></section>
<section><h2>3 · Runtime → Observation</h2><pre id="observation"></pre></section>
<section><h2>4 · 更新状态 → 下一次 Context</h2><pre id="nextcontext"></pre></section></article>
<footer>查阅同目录 report.md、target_evidence.csv、evidence.json、trace.jsonl、decisions.json、version.json。Rule 步与 LLM 步分别标注；只有多个合法选项才调用模型。</footer></main>
<script id="data" type="application/json">PAYLOAD</script><script>
const data=JSON.parse(document.getElementById('data').textContent);const ds=data.decisions;let i=0;
const el=id=>document.getElementById(id);const show=(id,x)=>el(id).textContent=JSON.stringify(x,null,2);
el('summary').textContent=`${data.summary.scenario} · ${data.summary.mode} | ${data.summary.llm_decisions} 次模型选择 | ${data.summary.rule_divergences} 次偏离 Rule 首选 | ${data.summary.fallbacks} 次回退 | 终止：${data.summary.stop_reason}`;
ds.forEach((d,j)=>{let o=document.createElement('option');o.value=j;o.textContent=`${j+1} · ${d.decision_source} · ${d.action.kind} ${d.action.target||''} ${d.action.dimension||''}`;el('step').appendChild(o)});
function draw(){const d=ds[i];if(!d)return;el('step').value=i;show('context',d.context);show('decision',{source:d.decision_source,raw:d.raw_decision,executed_action:d.action,error:d.error,state_binding:d.state_id});show('observation',d.observation||{terminal:true});show('nextcontext',ds[i+1]?{last_observation:ds[i+1].context.last_observation,candidates:ds[i+1].context.candidates,options:ds[i+1].context.options}:{terminal_statuses:data.summary.statuses,unresolved:data.summary.delivery_missing});el('prev').disabled=i===0;el('next').disabled=i===ds.length-1}
el('step').onchange=e=>{i=Number(e.target.value);draw()};el('prev').onclick=()=>{i--;draw()};el('next').onclick=()=>{i++;draw()};draw();
</script></html>'''
    (out/'demo.html').write_text(page.replace('PAYLOAD',payload),encoding='utf8')
