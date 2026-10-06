import copy
import json
import pytest
from agent.actions import Action, FETCH_EVIDENCE, SWITCH_SOURCE
from product.runtime import Paused
from product.planner import digest
from research_product.runtime import ResearchAgent, SCENARIOS
from research_product.context import working_set


class PlanClient:
    def __init__(self, corrupt=None):
        self.corrupt = corrupt

    def is_configured(self): return True
    def dump_calls(self): return []

    def complete_json(self, prompt, **kwargs):
        c = json.loads(prompt.split('\n', 1)[1])
        offers = list(reversed(c['opportunities']))[:3]
        result = {'state_id': c['state_id'], 'strategy': 'recover_evidence',
                  'reason': 'Choose offered goals using observed capability limits',
                  'basis': [c['latest_observation']['id']] if c['latest_observation'] else [],
                  'objectives': [{k: o[k] for k in ['target', 'dimension']} | {'method': o['methods'][0]} for o in offers]}
        if self.corrupt == 'score': result['score'] = 10
        if self.corrupt == 'stale': result['state_id'] = 'old'
        if self.corrupt == 'basis': result['basis'] = ['invented_observation']
        if self.corrupt == 'objective': result['objectives'][0]['target'] = 'invented_target'
        if self.corrupt == 'error': raise RuntimeError('simulated_transport')
        return result


def signature(r):
    return ({k: c.status for k, c in r.state.candidates.items()}, r.contract_rows,
            [(e.target, e.dimension, e.claim, e.verification, e.independence_group) for e in r.state.store.all()],
            r.state.budget.source_calls)


@pytest.mark.parametrize('scene', SCENARIOS)
def test_pause_in_active_capability_preserves_plan_and_evidence(tmp_path, scene):
    full = ResearchAgent(scene, tmp_path/'full').run()
    p = ResearchAgent(scene, tmp_path/'resume', pause_after=3)
    with pytest.raises(Paused): p.run()
    assert p.planner.methods.active
    r = ResearchAgent(scene, tmp_path/'resume')
    r.restore()
    assert r.planner.export() == p.planner.export()
    assert signature(r.run()) == signature(full)


@pytest.mark.parametrize('error', ['score', 'stale', 'basis', 'objective', 'error'])
def test_bad_plan_never_injects_state(tmp_path, error):
    a = ResearchAgent('recover', tmp_path, mode='research', client=PlanClient(error))
    a.seed_candidates()
    action = a.planner.plan(a.state)
    assert a.planner.plan_events[-1]['decision_source'] == 'fallback'
    assert action.kind == 'load_method'
    assert not a.state.store.all()


def test_observation_changes_executable_plan_and_preserves_provenance(tmp_path):
    a = ResearchAgent('recover', tmp_path, mode='research', client=PlanClient())
    a.run()
    assert any(e['trigger'] == 'action_failure' and any(g['method'] == 'recover' for g in e['plan']) for e in a.planner.plan_events)
    assert any(d['action']['kind'] == SWITCH_SOURCE for d in a.planner.decisions)
    for event in a.planner.plan_events:
        assert digest(event['context']) == event['state_id']
    for decision in a.planner.decisions:
        assert digest(decision['context']) == decision['state_id']
    assert not a.execution_rejections
    recovered = [e for e in a.state.store.all() if e.context.get('retrieval_route') == 'fixtures_archive']
    assert recovered
    assert all('archive' not in e.independence_group for e in recovered)
    assert all(e.verification == 'verified' for e in recovered)


def test_capability_gate_blocks_unloaded_cross_goal_and_unsupported_tool(tmp_path):
    a = ResearchAgent('recover', tmp_path)
    a.seed_candidates()
    load = a.planner.plan(a.state)
    goal = a.planner.current_plan[0]
    fetch = Action(FETCH_EVIDENCE, 'probe', goal['target'], goal['dimension'], 'fixtures')
    assert not a.execute(fetch).ok
    assert a.state.budget.source_calls == 0
    assert a.execute(load).ok
    fetch.source_id = 'fixtures_archive'
    assert not a.execute(fetch).ok
    fetch.source_id = 'fixtures'
    fetch.target = 'unknown'
    assert not a.execute(fetch).ok
    assert not a.state.store.all()


def test_recovery_unavailable_is_uncertainty_not_negative(tmp_path):
    a = ResearchAgent('exhausted', tmp_path)
    a.run()
    assert a.state.candidates['GPC3'].status == 'needs_more'
    assert not a.state.store.for_target('GPC3', 'internalization')
    assert ['GPC3', 'internalization'] in a.planner.deferred
    assert not a.execution_rejections


def test_dynamic_and_static_capabilities_have_equal_evidence(tmp_path):
    a = ResearchAgent('recover', tmp_path/'dynamic').run()
    b = ResearchAgent('recover', tmp_path/'static', tools_mode='static').run()
    assert signature(a) == signature(b)


def test_related_old_failure_survives_noise_without_mutating_store():
    history = [{'id': 'critical', 'action': {'target': 'T', 'dimension': 'internalization'}, 'ok': False, 'detail': 'Primary failed'}]
    history += [{'id': f'noise{i}', 'action': {'target': 'OTHER', 'dimension': 'safety'}, 'ok': True, 'detail': 'irrelevant'*500} for i in range(200)]
    original = copy.deepcopy(history)
    focus = [{'target': 'T', 'dimension': 'internalization'}]
    relevant = working_set([], history, focus)
    recent = working_set([], history, focus, mode='recency')
    assert 'critical' in {r['id'] for r in relevant['records']}
    assert 'critical' not in {r['id'] for r in recent['records']}
    assert relevant['selected_records'] == recent['selected_records'] == 6
    assert len(json.dumps(relevant).encode()) < 7000
    assert history == original


def test_resume_rejects_context_configuration_change(tmp_path):
    a = ResearchAgent('recover', tmp_path, pause_after=3)
    with pytest.raises(Paused): a.run()
    with pytest.raises(ValueError, match='config_changed'):
        ResearchAgent('recover', tmp_path, context_mode='recency').restore()
