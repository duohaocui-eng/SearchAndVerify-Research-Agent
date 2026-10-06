"""Standalone main-state/plan comparison, without publishing full abstracts."""
import copy
import html
import json
from pathlib import Path
from product.planner import digest


def public_state(state):
    result = copy.deepcopy(state)
    # Contexts duplicate every quote. Preserve binding, show observations once.
    for event in result['decisions']:
        event['context_sha256'] = digest(event.pop('context'))
        baseline = event.get('counterfactual_without_observation')
        if baseline:
            baseline['context_sha256'] = digest(baseline.pop('context'))
    seen = set()
    for observation in result['observations']:
        for ref in observation['provenance_refs']:
            if ref['record_id'] in seen:
                ref['quote_sha256'] = digest(ref.pop('quote'))
                ref['quote_display'] = '此来源短引文见首次出现；原始逐字引文已审计。'
            seen.add(ref['record_id'])
    return result


def render(state, path):
    e = lambda value: html.escape(str(value))
    s = public_state(state) if state.get('decisions') and 'context' in state['decisions'][0] else state
    cards = []
    for d in s['decisions']:
        p = d['plan']; base = d.get('counterfactual_without_observation')
        body = f'<p class="action">{e(p["action"])}</p><p>{e(p["objective"])}</p><p class="muted">{e(p["reason"])}</p><p>依据：{e(", ".join(p["basis"]) or "尚无观察")}</p>'
        baseline = ''
        if base:
            b = base['plan']
            baseline = f'<div><h3>无 observation 对照 · 未执行</h3><p class="action">{e(b["action"])}</p><p>{e(b["objective"])}</p><p class="muted">{e(b["reason"])}</p></div>'
        cards.append(f'<section><h2>主计划第 {d["revision"]} 轮</h2><div class="compare"><div><h3>实际执行的研究动作</h3>{body}</div>{baseline}</div><details><summary>状态绑定与决策来源</summary><pre>{e(json.dumps({k:d[k] for k in ["context_sha256","decision_source","observation_ids"] if k in d},ensure_ascii=False,indent=2))}</pre></details></section>')
    observations = []
    for o in s['observations']:
        refs = []
        for ref in o['provenance_refs']:
            # Provider URLs are fixed by source.py; escape even if malformed local data.
            url = ref['url'] if ref['url'].startswith('https://europepmc.org/article/') else '#'
            refs.append(f'<article><h3><a href="{e(url)}">{e(ref["record_id"])}</a> · {e(ref["child_id"])}</h3><blockquote>{e(ref.get("quote", ref.get("quote_display", "")))}</blockquote><p>{e(ref["interpretation"])}</p><small>{e(ref["relation"])} · 模型判断未核证 · SHA256 {e(ref["abstract_sha256"])}</small></article>')
        observations.append(f'<section><h2>主状态中的观察</h2><p>{e(o["id"])} · {e(o["parent_status"])}</p><p class="badge">ADVISORY / UNVERIFIED · 正式 gap 仍 unknown</p>'+''.join(refs)+'<h3>限制与未解问题</h3><ul>'+''.join(f'<li>{e(x)}</li>' for x in o['limitations'])+'</ul><details><summary>绑定的来源与 Child 状态</summary><pre>'+e(json.dumps({'origin':o['origin'],'children':o['children'],'version_sha256':o['version_sha256']},ensure_ascii=False,indent=2))+'</pre></details></section>')
    queue = ''.join(f'<article><h3>{e(task["status"])}</h3><p>{e(task["objective"])}</p><p>这是待人工核证任务，尚未执行，不代表问题已经解决。</p></article>' for task in s['verification_queue'])
    page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>主研究闭环与对照</title><style>body{margin:0;background:#edf3f3;color:#14313c;font:16px/1.7 system-ui}main{max-width:1120px;margin:auto;padding:32px 24px}header{background:#163e49;color:white;padding:30px;border-radius:14px}h1{font-size:32px}h2{font-size:23px}h3{font-size:17px}section{background:white;margin:22px 0;padding:25px;border:1px solid #cfdddf;border-radius:12px}.compare{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:28px}.action{font-weight:750;color:#087460}.muted,small{color:#536a73}article{border-top:1px solid #dce7e8;padding:12px 0}.badge{background:#eff5d9;padding:10px}blockquote{border-left:3px solid #66a899;margin:12px 0;padding-left:16px}a{color:#08716d}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}details{margin:16px 0}footer{padding:25px}</style><main>'
    goal = s['goal']
    page += f'<header><p>MAIN → LITERATURE → OBSERVATION → MAIN</p><h1>{e(goal["target"])} · {e(goal["origin"]["dimension"])} 研究闭环</h1><p>正式核证为 missing fixture / Rule；文献与研究规划为实时模型决定，公开摘要或已有来源缓存。</p><p>终态：{e(s["phase"])} / {e(s["disposition"])} · 正式状态未改变：{e(s.get("formal_unchanged", True))}</p></header>'
    page += ''.join(cards)+''.join(observations)+'<section><h2>真实执行产物：核证队列</h2>'+(queue or '<p>本次没有生成待人工核证任务；见主计划终止理由。</p>')+'</section><footer>只证明研究控制的反馈链；不证明医学准确率或更优决策。对照除 observations 字段外使用相同上下文、动作与剩余预算。完整摘要和模型请求保留在本地。</footer></main></html>'
    Path(path).write_text(page, encoding='utf8')
