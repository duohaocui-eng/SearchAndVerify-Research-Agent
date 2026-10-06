from __future__ import annotations
import argparse
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from product.cli import lock
from product.runtime import write
from product.budget import RatedClient
from evals.failure_driven_redesign.live_budget import BudgetLedger
from literature_agent.agent import ResearchDirector,Investigator,LiteraturePaused
from literature_agent.bridge import gap_goal
from literature_agent.source import LiteratureSource


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--target',required=True)
    p.add_argument('--question')
    p.add_argument('--from-run',type=Path)
    p.add_argument('--dimension',default='internalization')
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--ledger',type=Path,required=True)
    p.add_argument('--cache',type=Path,default=ROOT/'outputs/literature_cache')
    p.add_argument('--offline',action='store_true')
    p.add_argument('--source-fault',action='store_true')
    p.add_argument('--review',action='store_true')
    p.add_argument('--mode',choices=['director','single','fixed'],default='director')
    p.add_argument('--fixed-scope',choices=['all','focused'],default='all')
    p.add_argument('--resume',action='store_true')
    p.add_argument('--pause-after',type=int)
    a=p.parse_args()
    if (a.resume or a.pause_after) and a.mode!='director':p.error('checkpoint resume is supported for director mode')
    if bool(a.question)==bool(a.from_run):p.error('specify exactly one of --question or --from-run')
    goal=gap_goal(a.from_run,a.target,a.dimension) if a.from_run else {'target':a.target,'question':a.question}
    with lock(a.out/'.run.lock'):
        if (a.out/'run_binding.json').exists() and not a.resume:raise ValueError('existing_run_not_overwritable')
        ledger=BudgetLedger(a.ledger)
        before=ledger.summary()
        client=RatedClient(ledger,'literature:'+str(a.out.resolve()),a.out,max_calls=40,timeout=35,max_tokens=1600)
        with ledger.connect() as db:
            client.attempts=db.execute('SELECT count(*) FROM calls WHERE batch=?',(client.batch,)).fetchone()[0]
        if not client.is_configured():raise ValueError('model_not_configured')
        binding={'goal':goal,'ledger':str(a.ledger.resolve()),'cache':str(a.cache.resolve()),
                 'model':client.model,'base_url':client.base_url,'review':a.review,'offline':a.offline,'source_fault':a.source_fault,'mode':a.mode,'fixed_scope':a.fixed_scope}
        if a.resume:
            import json
            if json.loads((a.out/'run_binding.json').read_text(encoding='utf8'))!=binding:raise ValueError('run_binding_mismatch')
        else:write(a.out/'run_binding.json',binding)
        source=LiteratureSource(a.cache,offline=a.offline,fault=a.source_fault)
        if a.resume and (a.out/'source_requests.json').exists():
            source.requests=json.loads((a.out/'source_requests.json').read_text(encoding='utf8'))
        director=ResearchDirector(goal,client,source,a.out,review=a.review,resume=a.resume,pause_after=a.pause_after)
        try:
            if a.mode=='director':state=director.run()
            elif a.mode=='single':
                child=Investigator({'task_id':'child_1',**goal},client,source,a.out/'child_1')
                brief=child.run()
                state={'goal':goal,'status':child.state['status'],'children':[{'id':'child_1','status':child.state['status'],'result':brief}],
                       'trace':[],'report':{'answer':brief['answer'],'limitations':brief['limitations']},'version':director.version}
                write(a.out/'parent_state.json',state);write(a.out/'source_requests.json',source.requests)
            else:
                from literature_agent.comparison import fixed_baseline
                base=fixed_baseline(goal,client,source,a.out,scope=a.fixed_scope)
                state={'goal':goal,'status':base['status'],'children':[],'trace':[],'report':base['result'],'version':director.version}
                write(a.out/'parent_state.json',state)
        except LiteraturePaused:
            director.save();print('paused',a.out);return
        with ledger.connect() as db:
            batch_cost=db.execute('SELECT coalesce(sum(charged),0) FROM calls WHERE batch=?',(client.batch,)).fetchone()[0]
        summary={'status':state['status'],'children':len(state['children']),
                 'model_calls':client.attempts,'source_calls':len(source.requests),
                 'network_requests':sum(not x['cache_hit'] and not a.source_fault for x in source.requests),
                 'quoted_findings':len(base['result']['findings']) if a.mode=='fixed' else sum(len(x['result']['findings']) for x in state['children']),
                 'parent_feedback_replans':sum(bool(x['raw_decision']) and x['raw_decision'].get('action')=='delegate' and bool(x['raw_decision'].get('basis')) for x in state['trace']),
                 'budget_before':before,'budget_after':ledger.summary(),
                 'ledger_delta_cny_upper_bound':ledger.summary()['cny_upper_bound']-before['cny_upper_bound'],
                 'batch_cny_upper_bound':batch_cost}
        write(a.out/'summary.json',summary)
        from literature_agent.viewer import render_run
        render_run(a.out)
        print(summary)


if __name__=='__main__':main()
