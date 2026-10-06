from __future__ import annotations
import json
import os
import time
from pathlib import Path
from agent.loop import SearchAndVerifyAgent
from agent.report import render_markdown
from evidence_core.contract_writer import write_contract
from evidence_core.config import load_agent_config
from evidence_core.sources import build_registry
from evidence_core.sources.fixtures import FixtureSource
from evidence_core.http import SourceUnavailable
from .codec import _encode, _decode, _runtime
from .planner import BoundedPlanner, digest

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = {
    'hcc': {'cancer': 'hcc', 'targets': ['GPC3', 'ASGR1', 'AFP'], 'missing': []},
    'luad': {'cancer': 'luad', 'targets': ['CEACAM5', 'TACSTD2', 'SECRETX'], 'missing': []},
    'missing': {'cancer': 'hcc', 'targets': ['GPC3', 'ASGR1', 'AFP'], 'missing': ['internalization', 'delivery_precedent']},
}


def write(path, value):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf8')
    os.replace(temporary, path)


def version():
    paths = list((ROOT/'configs').rglob('*.yaml')) + list((ROOT/'data/fixtures').glob('*.json')) + list((ROOT/'data').glob('*.json')) + [ROOT/'evals/failure_driven_redesign/live_budget.py']
    return {**_runtime(), **{p.relative_to(ROOT).as_posix(): __import__('hashlib').sha256(p.read_bytes()).hexdigest() for p in paths}}


class Paused(RuntimeError):
    pass


class ScenarioSource(FixtureSource):
    def __init__(self, spec, scenario):
        super().__init__(spec); self.scenario = scenario

    def candidate_pool(self, config):
        return [r for r in super().candidate_pool(config) if r['symbol'] in self.scenario['targets']]

    def fetch(self, request):
        if request.dimension in self.scenario['missing']:
            raise SourceUnavailable('fixtures', 'Declared fault injection: this dimension is unavailable')
        return super().fetch(request)


class ProductAgent(SearchAndVerifyAgent):
    def __init__(self, scenario, out, *, mode='bounded', client=None, pause_after=None):
        if scenario not in SCENARIOS or mode not in {'rule', 'bounded'}:
            raise ValueError('unsupported_scenario_or_mode')
        self.scenario, self.out, self.mode, self.pause_after = scenario, Path(out), mode, pause_after
        self.out.mkdir(parents=True, exist_ok=True)
        spec = SCENARIOS[scenario]
        cfg = load_agent_config()
        cfg['planner'].update(mode='rule', allow_candidate_expansion=False)
        cfg['verification']['mode'] = 'rule'
        cfg['budget'].update(max_steps=100, max_source_calls=90, max_cost_units=200, max_seconds=1200)
        reg = build_registry()
        for sid,s in reg.specs.items():
            s.api['enabled'] = sid == 'fixtures'
        reg.fetchers = {'fixtures': ScenarioSource(reg.specs['fixtures'], spec)}
        super().__init__(spec['cancer'], agent_config=cfg, registry=reg, llm_client=client)
        self.planner = BoundedPlanner(reg, client, mode)
        self.binding = version()

    def seed_candidates(self):
        # Existing fixture identity normalization must remain offline even when
        # the planner makes a live API call. No public source HTTP is used here.
        old = os.environ.get('SNV_TEST_OFFLINE')
        os.environ['SNV_TEST_OFFLINE'] = '1'
        try:
            return super().seed_candidates()
        finally:
            if old is None: os.environ.pop('SNV_TEST_OFFLINE', None)
            else: os.environ['SNV_TEST_OFFLINE'] = old

    def checkpoint(self):
        payload = {'format': 'product-run-v1', 'version': self.binding,
            'scenario': self.scenario, 'mode': self.mode, 'elapsed': self.state.budget.elapsed,
            'state': _encode(self.state), 'trace': self.trace.records,
            'decisions': self.planner.decisions, 'feedback': self.planner.feedback}
        write(self.out/'checkpoint.json', {'payload': payload, 'sha256': digest(payload)})

    def restore(self):
        cp = json.loads((self.out/'checkpoint.json').read_text(encoding='utf8'))
        p = cp['payload']
        if digest(p) != cp['sha256'] or p['format'] != 'product-run-v1':
            raise ValueError('checkpoint_corrupt')
        if p['version'] != self.binding or (p['scenario'], p['mode']) != (self.scenario, self.mode):
            raise ValueError('checkpoint_version_or_config_changed')
        self.state = _decode(p['state'])
        self.state.budget.started_at = time.monotonic() - p['elapsed']
        self.trace.run_id = self.state.run_id
        self.trace.records = p['trace']
        self.planner.decisions, self.planner.feedback = p['decisions'], p['feedback']
        if self.state.phase != 'search_verify':
            raise ValueError('checkpoint_not_resumable')

    def after_step(self, result):
        self.planner.observe(self.state, result)
        self.checkpoint()
        if self.pause_after is not None and self.state.budget.steps >= self.pause_after:
            raise Paused('saved_after_complete_step')

    def run(self):
        result = super().run()
        result.trace.write(self.out/'trace.jsonl')
        write(self.out/'decisions.json', self.planner.decisions)
        write(self.out/'evidence.json', [e.to_dict() for e in result.state.store.all()])
        summary = result.summary()
        summary.update(scenario=self.scenario, mode=self.mode, environment='fixture',
            statuses={k:c.status for k,c in self.state.candidates.items()},
            delivery_missing={k:self.state.delivery_missing(k) for k in self.state.candidates},
            llm_decisions=sum(d['decision_source']=='llm' for d in self.planner.decisions),
            fallbacks=sum(d['decision_source']=='fallback' for d in self.planner.decisions),
            rule_divergences=sum(d['differs_from_rule'] for d in self.planner.decisions))
        write(self.out/'version.json', self.binding)
        write_contract(result.contract_rows, self.out/'target_evidence.csv', strict=True)
        (self.out/'report.md').write_text(render_markdown(result), encoding='utf8')
        write(self.out/'summary.json', summary)
        from .viewer import render
        render(self.out)
        return result

