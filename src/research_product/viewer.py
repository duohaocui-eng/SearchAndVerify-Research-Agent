"""Self-contained causal plan timeline; text-only rendering of untrusted data."""
import json
from pathlib import Path


def render(out):
    out = Path(out)
    data = {k: json.loads((out/(k+'.json')).read_text(encoding='utf8')) for k in ['research', 'summary', 'decisions']}
    encoded = json.dumps(data, ensure_ascii=False).replace('<', '\\u003c').replace('&', '\\u0026')
    html = '''<!doctype html><meta charset="utf-8"><title>Candidate 3 · Research plan trace</title>
<style>body{font:16px system-ui;margin:36px auto;max-width:1100px;background:#f3f5f7;color:#182532}h1{font-size:28px}article,details{background:white;padding:18px;margin:12px 0;border-radius:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:13px monospace}button{padding:10px;margin:8px;cursor:pointer}summary{cursor:pointer}.tag{color:#256f68}</style>
<h1>Candidate 3 · 观察如何改变计划</h1><p>环境：fixture 回放与显式故障注入。实时模型决策见 decision_source；archive 与主路由共享原记录，不能当独立证据。</p>
<p><a href="demo.html">逐步 Action / Observation</a> · <a href="report.md">研究报告</a> · <a href="research.json">原始计划状态</a></p>
<details><summary>结果 / 费用与限制</summary><pre id="result"></pre></details><div id="plans"></div>
<script id="data" type="application/json">DATA</script><script>
const d=JSON.parse(document.getElementById('data').textContent);document.getElementById('result').textContent=JSON.stringify(d.summary,null,2);
for(const e of d.research.plan_events){const card=document.createElement('article');const title=document.createElement('h2');title.textContent='Plan '+e.revision+' · step '+e.step+' · '+e.trigger+' · '+e.decision_source;card.append(title);
for(const [label,value] of [['触发观察',e.context.latest_observation],['模型计划与依据',e.raw_decision],['实际采用的目标',e.plan],['此计划之后的执行',d.decisions.filter(x=>x.context.revision===e.revision).map(x=>({action:x.action,observation:x.observation}))],['完整 Context',e.context]]){const det=document.createElement('details');const s=document.createElement('summary');s.textContent=label;det.append(s);const pre=document.createElement('pre');pre.textContent=JSON.stringify(value,null,2);det.append(pre);card.append(det)}document.getElementById('plans').append(card)}
</script>'''.replace('DATA', encoded)
    (out/'research.html').write_text(html, encoding='utf8')
