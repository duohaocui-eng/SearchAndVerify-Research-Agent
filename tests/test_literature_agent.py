import copy
import json
import pytest
from literature_agent.agent import Investigator, ResearchDirector
from literature_agent.source import LiteratureSource
from product.planner import digest


class Source:
    def __init__(self): self.requests=[]
    def search(self,target,terms):
        self.requests.append({'target':target,'terms':terms})
        hits=[] if terms=='binding' else [{'id':'MED:1','title':'Synthetic uptake assay','source':'MED','source_id':'1','url':'https://example.invalid/1'}]
        return {'hits':hits,'hit_count':len(hits),'query':terms}
    def read(self,hit):
        return {**hit,'abstract':'Antibody entered the target cells. Surface binding alone did not establish uptake.',
                'abstract_sha256':'synthetic','abstract_truncated':False,'publication_types':['Synthetic fixture']}


class Client:
    def complete_json(self,prompt,*,purpose):
        c=json.loads(prompt.split('\n',1)[1]);s=c['state_id']
        if purpose=='literature_parent':
            children=c['child_results']
            if len(children)<2:
                return {'state_id':s,'action':'delegate','question':'Investigate uptake evidence' if not children else 'Check whether a binding-only control limits the uptake interpretation',
                        'reason':'New child findings expose a scope question' if children else 'Establish evidence',
                        'basis':[children[-1]['id']] if children else []}
            return {'state_id':s,'action':'finish','answer':'Synthetic investigation complete, interpretation remains unverified',
                    'limitations':['Synthetic fixture is not a real experiment'],'basis':[x['id'] for x in children]}
        if not c['queries']:
            return {'state_id':s,'action':'search_literature','terms':'binding','reason':'Initial hypothesis'}
        if len(c['queries'])==1:
            return {'state_id':s,'action':'search_literature','terms':'intracellular uptake','reason':'No hits; change retrieval hypothesis'}
        if not c['read_records']:
            return {'state_id':s,'action':'read_abstract','record_id':c['discovered_records'][0]['id'],'reason':'Inspect original abstract'}
        return {'state_id':s,'action':'report','answer':'Synthetic antibody entry is described, with scope limits',
                'findings':[{'record_id':'MED:1','quote':'Antibody entered the target cells.','relation':'direct','interpretation':'This fixture describes uptake.'}],
                'limitations':['Synthetic evidence; not medically validated']}


def test_child_independent_loop_changes_query_and_parent_changes_question(tmp_path):
    source=Source();parent=ResearchDirector({'target':'TARGET','question':'Investigate uptake evidence and limitations'},Client(),source,tmp_path)
    result=parent.run()
    assert result['status']=='completed' and len(result['children'])==2
    assert result['trace'][1]['raw_decision']['basis']==['child_1']
    questions=[c['result']['question'] for c in result['children']]
    assert questions[0]!=questions[1]
    for i in [1,2]:
        state=json.loads((tmp_path/f'child_{i}'/'child_state.json').read_text())
        assert state['steps']==4 and state['queries']==['binding','intracellular uptake']
        assert not state['trace'][0]['context']['read_records']
        assert all(digest(t['context'])==t['state_id'] for t in state['trace'])
        assert all('children' not in t['context'] for t in state['trace'])
    assert all(c['result']['findings'][0]['semantic_verdict']=='unverified_model_assessment' for c in result['children'])
    assert all(digest(t['context'])==t['state_id']==t['raw_decision']['state_id'] for t in result['trace'])


@pytest.mark.parametrize('mutation',['fabricated_quote','unread','duplicate','score','long_quote'])
def test_report_cannot_forge_source_or_gain_score_authority(tmp_path,mutation):
    child=Investigator({'target':'T','question':'Q'},Client(),Source(),tmp_path)
    child.state['read']['MED:1']=Source().read({'id':'MED:1','title':'Synthetic','url':'https://example.invalid/1'})
    raw={'state_id':'test','action':'report','answer':'Assessment','findings':[{'record_id':'MED:1','quote':'Antibody entered the target cells.','relation':'direct','interpretation':'Synthetic'}],'limitations':['Not verified']}
    if mutation=='fabricated_quote':raw['findings'][0]['quote']='Entirely made up phrase'
    if mutation=='unread':raw['findings'][0]['record_id']='MED:9'
    if mutation=='duplicate':raw['findings']*=2
    if mutation=='score':raw['score']=9
    if mutation=='long_quote':raw['findings'][0]['quote']='word '*26
    with pytest.raises(ValueError):child.validate_report(raw)
    assert child.state['result'] is None


def test_quoted_text_is_not_semantic_verification(tmp_path):
    child=Investigator({'target':'T','question':'Does uptake occur?'},Client(),Source(),tmp_path)
    child.state['read']['MED:1']=Source().read({'id':'MED:1','title':'Synthetic','url':'https://example.invalid/1'})
    raw={'state_id':'test','action':'report','answer':'A deliberately wrong interpretation for boundary testing',
         'findings':[{'record_id':'MED:1','quote':'Surface binding alone did not establish uptake.','relation':'direct','interpretation':'Wrong semantic interpretation'}],
         'limitations':['Interpretation is not independently verified']}
    result=child.validate_report(raw)
    assert result['findings'][0]['quote_verified']
    assert result['findings'][0]['semantic_verdict']=='unverified_model_assessment'
    assert 'no score' in result['authority']


@pytest.mark.parametrize('terms',['uptake) OR OTHER (','uptake:OTHER','x" OR OTHER "'])
def test_query_cannot_escape_entity_binding(tmp_path,terms):
    source=LiteratureSource(tmp_path,offline=True)
    source.request=lambda params: {'resultList':{'result':[]}}
    with pytest.raises(ValueError):source.search('TARGET',terms)


def test_unknown_action_is_blocked_and_source_budget_is_finite(tmp_path):
    child=Investigator({'target':'T','question':'Q'},Client(),Source(),tmp_path)
    with pytest.raises(ValueError):child.execute({'action':'write_score','score':10})
    child.state['queries']=['a','b','c']
    with pytest.raises(ValueError,match='search_budget'):
        child.execute({'state_id':'x','action':'search_literature','terms':'uptake','reason':'r'})


def test_nested_checkpoint_restores_without_redelegating_or_rereading(tmp_path):
    from literature_agent.agent import LiteraturePaused
    goal={'target':'TARGET','question':'Investigate uptake evidence and limitations'}
    source=Source()
    with pytest.raises(LiteraturePaused):
        ResearchDirector(goal,Client(),source,tmp_path,pause_after=3).run()
    parent=json.loads((tmp_path/'parent_state.json').read_text())
    before=json.loads((tmp_path/'child_1'/'child_state.json').read_text())
    assert parent['pending']['identifier']=='child_1' and len(before['read'])==1
    state=ResearchDirector(goal,Client(),source,tmp_path,resume=True).run()
    after=json.loads((tmp_path/'child_1'/'child_state.json').read_text())
    assert after['trace'][:3]==before['trace'] and state['status']=='completed'
    assert len(state['trace'])==3 and len(source.requests)==4
    with pytest.raises(ValueError,match='parent_checkpoint_binding'):
        ResearchDirector({**goal,'question':'Changed question'},Client(),source,tmp_path,resume=True)


def test_gap_bridge_requires_existing_unresolved_objective_and_preserves_input(tmp_path):
    from literature_agent.bridge import gap_goal
    path=tmp_path/'summary.json'
    payload={'statuses':{'T':'needs_more','K':'killed'},'deferred':[['T','internalization']],
             'run_id':'fixture','environment':'explicitly synthetic'}
    path.write_text(json.dumps(payload));before=path.read_bytes()
    goal=gap_goal(tmp_path,'T','internalization')
    assert goal['origin']['dimension']=='internalization' and path.read_bytes()==before
    for target,dimension in [('K','internalization'),('T','unlisted')]:
        with pytest.raises(ValueError):gap_goal(tmp_path,target,dimension)


def test_challenge_cannot_return_duplicate_or_unbound_claims(tmp_path):
    from literature_agent.comparison import challenge
    class Bad:
        def complete_json(self,prompt,*,purpose):
            c=json.loads(prompt.split('\n',1)[1])
            return {'state_id':c['state_id'],'assessments':[{'claim_id':'99','verdict':'supported_within_abstract','reason':'bad'}],'limitations':['x']}
    result=challenge({'question':'q','findings':[{'record_id':'1','interpretation':'claim','relation':'direct'}]},
                    [{'id':'1','title':'t','abstract':'text','abstract_sha256':'x'}],Bad(),tmp_path)
    assert result['status']=='unavailable' and not result['assessments']


def test_viewer_escapes_source_and_model_instructions(tmp_path):
    from literature_agent.viewer import render
    malicious='<script>alert(1)</script>'
    state={'goal':{'target':malicious,'question':'q'},'children':[],'trace':[],'status':'completed','report':{'answer':malicious,'limitations':[malicious]}}
    render(state,tmp_path/'view.html');page=(tmp_path/'view.html').read_text(encoding='utf8')
    assert malicious not in page and '&lt;script&gt;' in page


def test_focused_scope_changes_query_without_losing_target_binding(tmp_path):
    source=LiteratureSource(tmp_path,offline=True);queries=[]
    def request(params):
        queries.append(params['query']);return {'resultList':{'result':[]}}
    source.request=request
    source.search('CEACAM5','internalization',scope='all')
    source.search('CEACAM5','internalization',scope='focused')
    assert queries==['"CEACAM5" AND (internalization)','TITLE_ABS:"CEACAM5" AND (internalization) NOT PUB_TYPE:review']
    with pytest.raises(ValueError):source.search('CEACAM5','internalization',scope='arbitrary')


def test_scope_switch_is_not_a_duplicate_but_consumes_same_total_search_budget(tmp_path):
    source=LiteratureSource(tmp_path/'cache',offline=True)
    source.request=lambda params:{'resultList':{'result':[]}}
    child=Investigator({'target':'T','question':'q'},Client(),source,tmp_path/'child')
    action={'state_id':'x','action':'search_literature','terms':'uptake','reason':'observe'}
    child.execute(action);child.execute({**action,'scope':'focused'})
    assert len(child.state['queries'])==2
    with pytest.raises(ValueError,match='duplicate_query'):child.execute({**action,'scope':'focused'})
