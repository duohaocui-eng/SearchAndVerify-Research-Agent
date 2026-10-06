"""Run formal screening, then a bounded main-agent advisory continuation."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
from product.cli import lock
from product.runtime import write
from product.budget import RatedClient
from research_product.runtime import ResearchAgent
from literature_agent.agent import LiteraturePaused
from literature_agent.bridge import gap_goal
from literature_agent.source import LiteratureSource
from evals.failure_driven_redesign.live_budget import BudgetLedger
from reintegration.runtime import MainResearchController


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--ledger', type=Path, required=True)
    p.add_argument('--target', default='GPC3')
    p.add_argument('--dimension', default='internalization')
    p.add_argument('--scenario', choices=['missing', 'exhausted'], default='missing')
    p.add_argument('--cache', type=Path, default=ROOT/'outputs/literature_cache')
    p.add_argument('--offline', action='store_true')
    p.add_argument('--source-fault', action='store_true')
    p.add_argument('--compare', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--pause-after-import', action='store_true')
    p.add_argument('--child-pause', type=int)
    a = p.parse_args()
    with lock(a.out/'.run.lock'):
        if (a.out/'main_checkpoint.json').exists() and not a.resume:
            raise ValueError('existing_run_not_overwritable')
        ledger = BudgetLedger(a.ledger)
        client = RatedClient(ledger, 'reintegration:'+str(a.out.resolve()), a.out,
                             max_calls=72, timeout=35, max_tokens=1600)
        with ledger.connect() as db:
            client.attempts = db.execute('SELECT count(*) FROM calls WHERE batch=?', (client.batch,)).fetchone()[0]
        if not client.is_configured():
            raise ValueError('model_not_configured')
        formal_folder = a.out/'formal'
        formal_state = None
        if not a.resume:
            agent = ResearchAgent(a.scenario, formal_folder)
            agent.run()
            formal_state = agent.state
        goal = gap_goal(formal_folder, a.target, a.dimension)
        formal_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in formal_folder.iterdir() if path.is_file()}
        binding = {'scenario': a.scenario, 'ledger': str(ledger.path.resolve()),
                   'model': client.model, 'base_url': client.base_url, 'max_tokens': client.max_tokens,
                   'max_calls': client.max_calls, 'batch': client.batch, 'offline': a.offline,
                   'source_fault': a.source_fault, 'cache': str(a.cache.resolve()),
                   'formal_files': formal_hashes}
        source = LiteratureSource(a.cache, offline=a.offline, fault=a.source_fault)
        controller = MainResearchController(formal_state, goal, client, source, a.out,
                        binding=binding, resume=a.resume, compare=a.compare,
                        pause_after_import=a.pause_after_import, child_pause=a.child_pause)
        paused = False
        try:
            controller.run()
        except LiteraturePaused:
            controller.save()
            paused = True
        after_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in formal_folder.iterdir() if path.is_file()}
        if after_hashes != formal_hashes:
            raise ValueError('formal_artifacts_changed')
        with ledger.connect() as db:
            cost = db.execute('SELECT coalesce(sum(charged),0) FROM calls WHERE batch=?', (client.batch,)).fetchone()[0]
        state = controller.state
        summary = {'phase': state['phase'], 'paused': paused, 'disposition': state['disposition'],
                   'epistemic_status': state['epistemic_status'], 'formal_unchanged': True,
                   'formal_digest': controller.formal_digest,
                   'model_calls': client.attempts, 'batch_cny_upper_bound': cost,
                   'ledger_summary': ledger.summary(), 'observation_count': len(state['observations']),
                   'main_decisions': len(state['decisions']),
                   'fallbacks': sum(x['decision_source'] != 'llm' for x in state['decisions'])}
        write(a.out/'summary.json', summary)
        from reintegration.viewer import render
        render(json.loads((a.out/'main_research.json').read_text(encoding='utf8')), a.out/'main.html')
        print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
