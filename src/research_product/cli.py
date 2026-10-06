from __future__ import annotations
import argparse
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
from product.cli import lock
from product.runtime import Paused, write
from research_product.runtime import ResearchAgent, SCENARIOS


def main():
    p = argparse.ArgumentParser(description='Explicit research plans; fixture observations, optional fresh model decisions.')
    p.add_argument('--scenario', choices=SCENARIOS, default='recover')
    p.add_argument('--mode', choices=['rule_plan', 'research', 'rolling'], default='rule_plan')
    p.add_argument('--context-mode', choices=['relevant', 'recency', 'full'], default='relevant')
    p.add_argument('--tools-mode', choices=['dynamic', 'static'], default='dynamic')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--ledger', type=Path)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--pause-after', type=int)
    a = p.parse_args()
    with lock(a.out/'.run.lock'):
        if (a.out/'summary.json').exists():
            raise ValueError('completed_output_not_overwritable')
        if (a.out/'checkpoint.json').exists() and not a.resume:
            raise ValueError('existing_run_requires_resume')
        client = None
        if a.mode in {'research', 'rolling'}:
            if not a.ledger:
                p.error('live model mode requires existing authorized --ledger')
            from evals.failure_driven_redesign.live_budget import BudgetLedger
            from product.budget import RatedClient
            ledger = BudgetLedger(a.ledger)
            client = RatedClient(ledger, 'research:'+str(a.out.resolve()), a.out,
                                 max_calls=120, timeout=35, max_tokens=900)
            with ledger.connect() as db:
                client.attempts = db.execute('SELECT count(*) FROM calls WHERE batch=?', (client.batch,)).fetchone()[0]
            if not client.is_configured():
                raise ValueError('model_not_configured')
        agent = ResearchAgent(a.scenario, a.out, mode=a.mode, client=client,
                              context_mode=a.context_mode, tools_mode=a.tools_mode, pause_after=a.pause_after)
        if a.resume:
            agent.restore()
        try:
            agent.run()
            status = 'completed'
        except Paused:
            status = 'paused'
        if client:
            write(a.out/'budget.json', ledger.summary())
        print(status, a.out, agent.state.budget.steps)


if __name__ == '__main__':
    main()
