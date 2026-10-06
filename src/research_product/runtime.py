"""Research-plan prototype on the unchanged stable product execution loop."""
from __future__ import annotations
import copy
import hashlib
import json
from agent.actions import ActionResult, FETCH_EVIDENCE, SWITCH_SOURCE
from evidence_core.http import SourceUnavailable
from evidence_core.sources.fixtures import FixtureSource
from product.runtime import ProductAgent, SCENARIOS as BASE_SCENARIOS, ROOT, write, version
from product.planner import digest
from product.codec import _encode
from .planner import ResearchPlanner, RECOVERABLE

SCENARIOS = {**copy.deepcopy(BASE_SCENARIOS),
    'recover': {**copy.deepcopy(BASE_SCENARIOS['hcc']), 'missing': ['internalization']},
    'exhausted': {**copy.deepcopy(BASE_SCENARIOS['hcc']), 'missing': ['internalization'],
                  'archive_missing': ['internalization']}}
SCENARIOS['missing']['archive_missing'] = ['internalization', 'delivery_precedent']


def research_version():
    return {**version(), **{p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in (ROOT/'configs/research_methods').glob('*.json')}}


class RouteSource(FixtureSource):
    def __init__(self, spec, scenario, archive=False):
        super().__init__(spec)
        self.scenario, self.archive = scenario, archive

    def candidate_pool(self, config):
        if self.archive:
            return []
        return [r for r in super().candidate_pool(config) if r['symbol'] in self.scenario['targets']]

    def fetch(self, request):
        missing = self.scenario.get('archive_missing' if self.archive else 'missing', [])
        if request.dimension in missing:
            raise SourceUnavailable(self.spec.source_id, 'Declared route fault injection: dimension unavailable')
        observations = super().fetch(request)
        # Same records and independence group, not manufactured corroboration.
        for obs in observations:
            obs.context['retrieval_route'] = self.spec.source_id
        return observations


class ResearchAgent(ProductAgent):
    def __init__(self, scenario, out, *, mode='rule_plan', client=None,
                 context_mode='relevant', tools_mode='dynamic', pause_after=None):
        if scenario not in SCENARIOS or mode not in {'research', 'rule_plan', 'rolling'}:
            raise ValueError('unsupported_research_configuration')
        if context_mode not in {'relevant', 'recency', 'full'} or tools_mode not in {'dynamic', 'static'}:
            raise ValueError('unsupported_working_set')
        spec = SCENARIOS[scenario]
        super().__init__(spec['cancer'], out, mode='rule', client=client, pause_after=pause_after)
        self.scenario, self.mode = scenario, mode
        self.context_mode, self.tools_mode = context_mode, tools_mode
        self.registry.fetchers['fixtures'] = RouteSource(self.registry.specs['fixtures'], spec)
        archive = copy.deepcopy(self.registry.specs['fixtures'])
        archive.source_id = 'fixtures_archive'
        archive.name = 'Alternate route to SAME fixture records'
        archive.provides = sorted(RECOVERABLE)
        # Available to the capability executor; excluded from generic Rule routing.
        archive.api['enabled'] = mode == 'rolling'
        self.registry.specs[archive.source_id] = archive
        self.registry.fetchers[archive.source_id] = RouteSource(archive, spec, True)
        self.planner = ResearchPlanner(self.registry, client, mode, context_mode, tools_mode)
        self.binding = research_version()
        self.execution_rejections = []

    def execute(self, action):
        if self.mode == 'rolling':
            return super().execute(action)
        if action.kind in {'load_method', 'defer_gap', FETCH_EVIDENCE, SWITCH_SOURCE}:
            offers = {(o['target'], o['dimension']): o['methods'] for o in self.planner.opportunities(self.state)}
            pair = (action.target, action.dimension)
            goal = next((g for g in self.planner.current_plan if g['status'] == 'pending'), None)
            error = None
            if not goal or (goal['target'], goal['dimension']) != pair or goal['method'] not in offers.get(pair, []):
                error = 'objective_no_longer_executable'
            elif self.state.candidates[action.target].source_calls >= self.state.budget.per_candidate_max_calls:
                error = 'candidate_budget_exhausted'
            elif action.kind == 'load_method':
                if action.payload != {'method': goal['method']}:
                    error = 'method_binding_mismatch'
                else:
                    loaded = self.planner.methods.load(goal['method'], {k: goal[k] for k in ('target', 'dimension', 'method')}, self.state.budget.steps)
                    return ActionResult(action, True, detail='Capability loaded: '+loaded['id'], payload=copy.deepcopy(loaded))
            elif action.kind == 'defer_gap':
                if goal['method'] != 'defer':
                    error = 'invalid_defer'
                else:
                    self.planner.deferred.append([action.target, action.dimension])
                    return ActionResult(action, True, detail='Unresolved gap retained; no evidence or negative score created')
            else:
                active = self.planner.methods.active
                if self.tools_mode == 'static':
                    definition = self.planner.methods.definition(goal['method'])
                elif not active or active['objective'] != {k: goal[k] for k in ('target', 'dimension', 'method')}:
                    definition = None
                else:
                    definition = active['definition']
                if not definition or action.source_id not in definition['sources'] or action.dimension not in definition['dimensions']:
                    error = 'capability_not_loaded_or_out_of_scope'
                elif not self.registry.specs[action.source_id].can_provide(action.dimension):
                    error = 'source_contract_mismatch'
            if error:
                self.execution_rejections.append({'step': self.state.budget.steps, 'error': error, 'action': action.to_dict()})
                return ActionResult(action, False, detail=error)
        return super().execute(action)

    def checkpoint(self):
        payload = {'format': 'product-run-v1', 'version': self.binding,
                   'scenario': self.scenario, 'mode': self.mode, 'elapsed': self.state.budget.elapsed,
                   'state': _encode(self.state), 'trace': self.trace.records,
                   'decisions': self.planner.decisions, 'feedback': self.planner.feedback,
                   'research': self.planner.export(),
                   'research_config': [self.context_mode, self.tools_mode],
                   'model_budget_binding': self.model_budget_binding(),
                   'execution_rejections': self.execution_rejections}
        write(self.out/'checkpoint.json', {'payload': payload, 'sha256': digest(payload)})

    def _progress_fingerprint(self):
        # Recording a previously unresolved objective is real progress; loading
        # capabilities alone is not, so a load/reload loop still stops.
        return super()._progress_fingerprint() + (len(self.planner.deferred),)

    def restore(self):
        cp = json.loads((self.out/'checkpoint.json').read_text(encoding='utf8'))
        if cp['payload'].get('research_config') != [self.context_mode, self.tools_mode]:
            raise ValueError('research_config_changed')
        if cp['payload'].get('model_budget_binding') != self.model_budget_binding():
            raise ValueError('model_budget_binding_changed')
        super().restore()
        self.planner.restore_research(cp['payload']['research'])
        self.execution_rejections = cp['payload']['execution_rejections']

    def model_budget_binding(self):
        client = self.planner.client
        if client is None or not hasattr(client, 'ledger'):
            return None
        return {'ledger': str(client.ledger.path.resolve()), 'batch': client.batch,
                'max_calls': client.max_calls, 'model': client.model, 'max_tokens': client.max_tokens}

    def run(self):
        result = super().run()
        self.planner.methods.finish(self.state.budget.steps, 'run_terminated')
        for goal in self.planner.current_plan:
            if goal['status'] == 'pending':
                goal['status'] = 'not_executed_at_termination'
        write(self.out/'research.json', self.planner.export())
        summary = json.loads((self.out/'summary.json').read_text(encoding='utf8'))
        events = self.planner.plan_events
        summary.update(plan_revisions=len(events), llm_plans=sum(e['decision_source']=='llm' for e in events),
                       plan_fallbacks=sum(e['decision_source']=='fallback' for e in events),
                       failure_replans=sum(e['trigger']=='action_failure' for e in events),
                       deferred=self.planner.deferred, execution_rejections=self.execution_rejections,
                       context_mode=self.context_mode, tools_mode=self.tools_mode)
        write(self.out/'summary.json', summary)
        from .viewer import render
        render(self.out)
        return result
