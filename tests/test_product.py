import json
from pathlib import Path
import pytest
from product.runtime import ProductAgent, Paused
from product.planner import digest

class Client:
    def __init__(self, corrupt=None): self.corrupt=corrupt
    def is_configured(self): return True
    def complete_json(self,prompt,**kw):
        context=json.loads(prompt.split('\n',1)[1])
        if self.corrupt=='error': raise RuntimeError('simulated_transport')
        result={'state_id':context['state_id'],'option_id':context['options'][-1]['id'],'reason':'Select a listed evidence gap'}
        if self.corrupt=='stale': result['state_id']='old'
        if self.corrupt=='id': result['option_id']='999'
        if self.corrupt=='score': result['score']=10
        return result
    def dump_calls(self): return []

def signature(result):
    return {'status':{k:c.status for k,c in result.state.candidates.items()},
        'rows':result.contract_rows,
        'calls':result.state.budget.source_calls,
        'actions':[r['result']['action'] for r in result.trace.records if 'result' in r]}

@pytest.mark.parametrize('scene',['hcc','luad','missing'])
def test_resume_same_observations_and_actions(tmp_path,scene):
    full=ProductAgent(scene,tmp_path/'full',mode='rule').run()
    partial=ProductAgent(scene,tmp_path/'resumed',mode='rule',pause_after=4)
    with pytest.raises(Paused): partial.run()
    resumed=ProductAgent(scene,tmp_path/'resumed',mode='rule')
    resumed.restore()
    assert resumed.state.budget.steps==4
    assert resumed.state.budget.source_calls==partial.state.budget.source_calls
    assert signature(resumed.run())==signature(full)

@pytest.mark.parametrize('error',['stale','id','score','error'])
def test_invalid_proposal_falls_back_without_injecting_evidence(tmp_path,error):
    a=ProductAgent('hcc',tmp_path,client=Client(error))
    a.probe();a.seed_candidates()
    chosen=a.planner.plan(a.state)
    assert a.planner.decisions[-1]['decision_source']=='fallback'
    assert chosen.to_dict()==a.planner.rule.plan(a.state).to_dict()
    assert not a.state.store.all()

def test_live_choice_changes_execution_and_feedback(tmp_path):
    a=ProductAgent('missing',tmp_path,client=Client())
    r=a.run()
    assert any(d['differs_from_rule'] for d in a.planner.decisions)
    assert any(d['decision_source']=='llm' for d in a.planner.decisions)
    for prior,nxt in zip(a.planner.decisions,a.planner.decisions[1:]):
        assert nxt['context']['last_observation']==prior['observation']
    assert all(c.status!='completed' for c in r.state.candidates.values())
    assert r.state.candidates['GPC3'].status!='killed'

def test_corrupt_checkpoint_rejected(tmp_path):
    a=ProductAgent('hcc',tmp_path,mode='rule',pause_after=2)
    with pytest.raises(Paused):a.run()
    cp=json.loads((tmp_path/'checkpoint.json').read_text(encoding='utf8'))
    cp['payload']['elapsed']=0
    (tmp_path/'checkpoint.json').write_text(json.dumps(cp))
    with pytest.raises(ValueError,match='corrupt'):ProductAgent('hcc',tmp_path,mode='rule').restore()

def test_code_binding_rejected(tmp_path):
    a=ProductAgent('hcc',tmp_path,mode='rule',pause_after=2)
    with pytest.raises(Paused):a.run()
    b=ProductAgent('hcc',tmp_path,mode='rule');b.binding={'changed':True}
    with pytest.raises(ValueError,match='version'):b.restore()


def test_historical_context_is_detached_from_future_state(tmp_path):
    a=ProductAgent('missing',tmp_path,client=Client())
    a.run()
    for decision in a.planner.decisions:
        assert digest(decision['context'])==decision['state_id']
    assert all(not c['failed_dimensions'] for c in a.planner.decisions[0]['context']['candidates'])

def test_output_directory_lock_is_exclusive(tmp_path):
    from product.cli import lock
    with lock(tmp_path/'run.lock'):
        with pytest.raises(OSError):
            with lock(tmp_path/'run.lock'):pass

def test_resume_preserves_nonzero_elapsed_budget(tmp_path):
    a=ProductAgent('hcc',tmp_path,mode='rule',pause_after=2)
    a.state.budget.started_at-=40
    with pytest.raises(Paused):a.run()
    b=ProductAgent('hcc',tmp_path,mode='rule');b.restore()
    assert 40<=b.state.budget.elapsed<60
