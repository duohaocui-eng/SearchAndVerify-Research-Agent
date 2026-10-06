"""Portable review page. Source and model strings are escaped, never executed."""
import html
from pathlib import Path


def render(state, out):
    e=lambda value:html.escape(str(value),quote=True)
    cards=[];sources={}
    for child in state['children']:
        brief=child['result'];review=brief.get('review',{})
        review_rows={x['claim_id']:x for x in review.get('assessments',[])}
        findings=[]
        for i,f in enumerate(brief['findings']):
            sources.setdefault(f['record_id'],f)
            r=review_rows.get(str(i),{})
            findings.append(f'<li><b>{e(f["record_id"])} · {e(f["relation"])}</b><p>{e(f["interpretation"])}</p>'
                f'<p class="muted">复核：{e(r.get("verdict","未启用/不可用"))} {e(r.get("reason",""))}</p></li>')
        trace=[]
        for t in child.get('trace',[]):
            raw=t.get('raw_decision') or {};obs=t['observation']
            trace.append(f'<tr><td>{t["step"]}</td><td>{e(raw.get("action","model"))}</td><td>{e(raw.get("terms",raw.get("record_id","")))}</td>'
                         f'<td>{e(raw.get("reason",""))}</td><td>{e(obs.get("error",obs.get("result",{})))}</td></tr>')
        cards.append(f'<article><span class="tag">{e(child["id"])} · {e(child["status"])}</span><h2>{e(brief["question"])}</h2>'
            f'<p>{e(brief["answer"])}</p><ol>{"".join(findings)}</ol><h3>仍未解决</h3><ul>'+''.join(f'<li>{e(x)}</li>' for x in brief['limitations'])+
            f'</ul><details><summary>展开独立调查的行动与反馈</summary><div class="scroll"><table><tr><th>步</th><th>行动</th><th>检索/文献</th><th>理由</th><th>观察</th></tr>{"".join(trace)}</table></div></details></article>')
    parent=[]
    for t in state['trace']:
        raw=t.get('raw_decision') or {}
        parent.append(f'<li><b>{e(raw.get("action","model error"))}</b> · 依据 {e(", ".join(raw.get("basis",[])) or "初始目标")}'
                      f'<p>{e(raw.get("question",raw.get("answer","")))}</p><p class="muted">{e(raw.get("reason",t["observation"].get("error","")))}</p></li>')
    bibliography=[]
    for f in sources.values():
        # URLs originate only in the registered provider. Also constrain output href.
        url=f['url'] if f['url'].startswith('https://europepmc.org/article/') else '#'
        bibliography.append(f'<li><a href="{e(url)}">{e(f["record_id"])} · {e(f["title"])}</a><blockquote>{e(f["quote"])}</blockquote>'
                            f'<small>摘要 SHA256：{e(f["abstract_sha256"])}</small></li>')
    report=state.get('report') or {'answer':'调查未完成，保留缺口。','limitations':[]}
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Candidate 3 · 证据缺口调查</title>
<style>body{font:16px/1.7 system-ui,sans-serif;background:#f4f6f8;color:#172a39;margin:0}main{max-width:1100px;margin:auto;padding:32px 22px}header{background:#173e4b;color:white;padding:32px;border-radius:16px}h1{font-size:32px;line-height:1.3}h2{font-size:20px;line-height:1.5}article,section{background:white;border:1px solid #dae2e7;border-radius:12px;padding:24px;margin-top:22px}.muted,small{color:#536976}.tag{color:#176651;font-weight:650}blockquote{border-left:3px solid #69a999;padding-left:16px}details{border-top:1px solid #ddd;padding-top:14px}summary{cursor:pointer;font-weight:600}table{border-collapse:collapse;font-size:13px;min-width:850px}td,th{padding:10px;border:1px solid #ddd;vertical-align:top;max-width:330px;overflow-wrap:anywhere}.scroll{overflow:auto}a{color:#096d8a}small{overflow-wrap:anywhere}li{margin-bottom:12px}</style><main>'''
    page+=f'<header><div>Candidate 3 · 公开文献调查</div><h1>{e(state["goal"]["target"])}：从证据缺口到下一步问题</h1><p>{e(state["goal"]["question"])}</p><p>状态：{e(state["status"])} · {len(state["children"])} 个独立调查任务 · {len(sources)} 篇引文来源</p></header>'
    page+='<section><h2>如何读取这份结果</h2><p>引文已核对原文；解释与独立复核仍是模型判断。结果作为候选研究的旁路简报，保留待补证状态，不写入评分、淘汰或临床决策。</p></section>'
    page+=f'<section><h2>父任务如何改变计划</h2><ol>{"".join(parent)}</ol></section>'+''.join(cards)
    page+=f'<section><h2>本次研究结论</h2><p>{e(report["answer"])}</p><ul>'+''.join(f'<li>{e(x)}</li>' for x in report['limitations'])+'</ul></section>'
    page+=f'<section><h2>原文定位</h2><ol>{"".join(bibliography)}</ol><p class="muted">每篇仅展示首条短引文；其他判断可沿文献 ID 回查。仅阅读公开摘要，未读全文。</p></section></main></html>'
    Path(out).write_text(page,encoding='utf8')


def render_run(folder):
    import json
    state=json.loads((folder/'parent_state.json').read_text(encoding='utf8'))
    for child in state['children']:
        child['trace']=json.loads((folder/child['id']/'child_state.json').read_text(encoding='utf8'))['trace']
    if (folder/'baseline.json').exists():
        baseline=json.loads((folder/'baseline.json').read_text(encoding='utf8'))
        state['children']=[{'id':'fixed_retrieval','status':baseline['status'],
            'result':{'question':state['goal']['question'],**baseline['result']},'trace':[]}]
    render(state,folder/'research.html')
    if (folder/'baseline.json').exists():
        path=folder/'research.html'
        path.write_text(path.read_text(encoding='utf8').replace('1 个独立调查任务','固定检索基线，无独立调查循环'),encoding='utf8')
