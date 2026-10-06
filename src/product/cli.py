"""Run from repository root: python -m product via PYTHONPATH, or this file."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from product.runtime import ProductAgent, Paused, SCENARIOS, write

@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as fh:
        fh.seek(0); fh.write(b'0'); fh.flush(); fh.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: yield
        finally:
            fh.seek(0)
            if os.name == 'nt': msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(fh, fcntl.LOCK_UN)


def main():
    p=argparse.ArgumentParser(description='Candidate 3 bounded research demo. Environment is offline fixture; --live uses fresh paid model decisions.')
    p.add_argument('--scenario', choices=SCENARIOS, default='hcc')
    p.add_argument('--out', type=Path, required=True)
    p.add_argument('--live', action='store_true')
    p.add_argument('--ledger', type=Path)
    p.add_argument('--resume', action='store_true')
    p.add_argument('--pause-after', type=int)
    a=p.parse_args()
    with lock(a.out/'.run.lock'):
        if (a.out/'summary.json').exists(): raise ValueError('completed_output_not_overwritable')
        if (a.out/'checkpoint.json').exists() and not a.resume: raise ValueError('existing_run_requires_resume')
        client=None
        if a.live:
            if not a.ledger: p.error('--live requires an explicitly authorized existing --ledger')
            from evals.failure_driven_redesign.live_budget import BudgetLedger
            from product.budget import RatedClient
            ledger=BudgetLedger(a.ledger)
            client=RatedClient(ledger, 'product:'+str(a.out.resolve()), a.out, max_calls=100, timeout=35, max_tokens=384)
            if not client.is_configured(): raise ValueError('DEEPSEEK_API_KEY_missing')
        agent=ProductAgent(a.scenario,a.out,mode='bounded' if a.live else 'rule',client=client,pause_after=a.pause_after)
        if a.resume: agent.restore()
        try:
            agent.run(); status='completed'
        except Paused:
            status='paused'
        if client: write(a.out/'budget.json', ledger.summary())
        print(json.dumps({'status':status,'out':str(a.out),'steps':agent.state.budget.steps},ensure_ascii=False))

if __name__=='__main__': main()
