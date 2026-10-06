from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[2]; sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
from product.cli import lock
from product.runtime import write
from product.budget import RatedClient
from research_product.runtime import ResearchAgent
from literature_agent.agent import LiteraturePaused
from literature_agent.bridge import gap_goal
from research_v4.source import OpenSource
from research_v4.agent import DocumentResearcher
from research_v4.runtime import PROFILES, FixedResearcher, OpenResearchController
from evals.failure_driven_redesign.live_budget import BudgetLedger

QUESTION = 'Which experimental systems, internalization measurements and controls are described for this target? Distinguish direct cellular uptake from related properties and identify what remains unknown.'


def main():
    p = argparse.ArgumentParser(); p.add_argument('--out', type=Path, required=True); p.add_argument('--ledger', type=Path, required=True)
    p.add_argument('--target', default='GPC3'); p.add_argument('--profile', choices=PROFILES, default='stepwise')
    p.add_argument('--question', default=QUESTION); p.add_argument('--mode', choices=['standalone', 'main'], default='main')
    p.add_argument('--cache', type=Path, default=ROOT/'outputs/literature_cache')
    p.add_argument('--source-fault', action='store_true'); p.add_argument('--fulltext-fault', action='store_true')
    p.add_argument('--offline', action='store_true'); p.add_argument('--resume', action='store_true')
    p.add_argument('--pause-after', type=int); p.add_argument('--pause-after-import', action='store_true')
    a = p.parse_args()
    with lock(a.out/'.run.lock'):
        binding_path = a.out/'binding.json'
        if binding_path.exists() and not a.resume: raise ValueError('existing_run')
        ledger = BudgetLedger(a.ledger)
        client = RatedClient(ledger, 'research_v4:'+str(a.out.resolve()), a.out, max_calls=44, max_tokens=2400, timeout=40)
        with ledger.connect() as db: client.attempts = db.execute('SELECT count(*) FROM calls WHERE batch=?', (client.batch,)).fetchone()[0]
        if not client.is_configured(): raise ValueError('model_not_configured')
        source = OpenSource(a.cache, offline=a.offline, fault=a.source_fault, fulltext_fault=a.fulltext_fault)
        binding = {'target': a.target, 'question': a.question, 'mode': a.mode, 'profile': a.profile,
            'source_fault': a.source_fault, 'fulltext_fault': a.fulltext_fault, 'offline': a.offline,
            'cache': str(a.cache.resolve()), 'model': client.model, 'max_tokens': client.max_tokens,
            'max_calls': client.max_calls, 'ledger': str(ledger.path.resolve()), 'batch': client.batch}
        if a.resume:
            if json.loads(binding_path.read_text(encoding='utf8')) != binding: raise ValueError('run_binding_changed')
        else: write(binding_path, binding)
        paused = False
        if a.mode == 'main':
            formal_folder = a.out/'formal'; formal = None
            if not a.resume:
                agent = ResearchAgent('missing', formal_folder); agent.run(); formal = agent.state
            goal = gap_goal(formal_folder, a.target, 'internalization')
            hashes = {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in formal_folder.iterdir() if x.is_file()}
            controller = OpenResearchController(formal, goal, client, source, a.out, profile=a.profile,
                binding={**binding, 'formal_files': hashes}, resume=a.resume, child_pause=a.pause_after,
                pause_after_import=a.pause_after_import)
            try: controller.run()
            except LiteraturePaused: controller.save(); paused = True
            assert hashes == {x.name: hashlib.sha256(x.read_bytes()).hexdigest() for x in formal_folder.iterdir() if x.is_file()}
            status = controller.state['phase']; observations = controller.state['observations']
            findings = [f for o in observations for f in o['provenance_refs']]
        else:
            goal = {'target': a.target, 'question': a.question}
            cls = FixedResearcher if a.profile == 'fixed' else DocumentResearcher
            researcher = cls(goal, client, source, a.out, config=PROFILES[a.profile], resume=a.resume, pause_after=a.pause_after)
            try: researcher.run()
            except LiteraturePaused: researcher.save(); paused = True
            status = researcher.state['status']; findings = (researcher.state['result'] or {}).get('findings', [])
        with ledger.connect() as db:
            cost = db.execute('SELECT coalesce(sum(charged),0) FROM calls WHERE batch=?', (client.batch,)).fetchone()[0]
        summary = {'status': status, 'paused': paused, 'profile': a.profile, 'mode': a.mode,
            'model_calls': client.attempts, 'batch_cny_upper_bound': cost, 'findings': len(findings),
            'fulltext_findings': sum(f.get('source_scope') == 'open_full_text' for f in findings),
            'source_requests': len(source.requests), 'formal_unchanged': a.mode == 'main', 'ledger': ledger.summary()}
        write(a.out/'summary.json', summary); print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__': main()
