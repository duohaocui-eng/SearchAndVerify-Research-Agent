import copy
import hashlib
import json
import pytest
from research_v4.source import parse_document, rank, OpenSource
from research_v4.agent import DocumentResearcher
from research_v4.runtime import OpenResearchController
from literature_agent.agent import LiteraturePaused
from agent.state import RunState, Candidate
from product.codec import _encode

XML = b'<article><front><article-meta><article-id pub-id-type="pmc">123</article-id><article-id pub-id-type="pmid">1</article-id></article-meta></front><body><sec><title>Methods</title><p>The antibody entered cells in an assay. This is a synthetic example, not clinical evidence.</p></sec><sec><title>Limitations</title><p>Binding alone does not establish internalization.</p></sec></body></article>'
RECORD = {'id': 'MED:1', 'pmcid': 'PMC123', 'source': 'MED', 'source_id': '1', 'is_open_access': True}


class Source:
    def __init__(self, fault=False): self.requests = []; self.fault = fault
    def search(self, target, terms, scope='all'):
        self.requests.append({'kind': 'search'})
        return {'hit_count': 2, 'hits': [{'id': 'MED:'+str(i), 'source': 'MED', 'source_id': str(i),
                                        'title': 'Synthetic assay', 'url': 'https://example.invalid/'+str(i)} for i in [1, 2]]}
    def read(self, hit):
        self.requests.append({'kind': 'read', 'id': hit['id']})
        if self.fault and hit['id'] == 'MED:2': raise RuntimeError('declared_failure')
        text = 'A synthetic antibody assay was described, without detailed methods in the abstract.'
        return {**hit, 'abstract': text, 'abstract_sha256': hashlib.sha256(text.encode()).hexdigest(),
                'pmcid': 'PMC123', 'is_open_access': True, 'publication_types': ['Synthetic fixture']}
    def locate(self, record, query):
        self.requests.append({'kind': 'locate', 'id': record['id']})
        doc = parse_document(XML, RECORD)
        return {'passages': doc['passages'], 'total_passages': len(doc['passages']), 'limitations': doc['limitations']}


class Client:
    def complete_json(self, prompt, *, purpose):
        c = json.loads(prompt.split('\n', 1)[1]); sid = c['state_id']
        if purpose == 'main_replanning':
            return {'state_id': sid, 'action': 'request_verification' if c['observations'] else 'investigate',
                'objective': 'Check synthetic assay controls against the full methods.', 'reason': 'Fixture tests plumbing, not model quality.',
                'basis': [c['observations'][0]['id']] if c['observations'] else []}
        if not c['discovered_records']: ops = [{'action': 'search', 'terms': 'uptake', 'scope': 'focused'}]
        elif not c['read_metadata']: ops = [{'action': 'read', 'record_id': h['id']} for h in c['discovered_records'][:c['max_operations_per_decision']]]
        elif not any(p['source_scope'] == 'open_full_text' for p in c['visible_passages']):
            ops = [{'action': 'locate', 'record_id': c['read_metadata'][0]['id'], 'query': 'antibody cells assay'}]
        else:
            p = next(p for p in c['visible_passages'] if 'The antibody entered cells' in p['text'])
            return {'state_id': sid, 'action': 'report', 'answer': 'Synthetic cell assay described; no clinical inference.',
                'findings': [{'passage_id': p['passage_id'], 'quote': 'The antibody entered cells in an assay.',
                    'claim': 'Synthetic assay with no clinical authority.', 'scope': 'cell', 'relation': 'direct'}],
                'limitations': ['Synthetic fixture is not biological evidence.'], 'next_questions': ['Which controls are missing?']}
        return {'state_id': sid, 'action': 'tools', 'operations': ops, 'reason': 'Exercise a bounded operation.'}


def make(tmp_path, **kwargs):
    return DocumentResearcher({'target': 'TARGET', 'question': 'Which uptake methods and controls are reported?'},
                              Client(), Source(), tmp_path, **kwargs)


def test_fulltext_identity_section_and_quote_positions():
    doc = parse_document(XML, RECORD); p = doc['passages'][0]
    assert p['section'] == 'Methods' and p['start'] == 0 and p['end'] == len(p['text'])
    assert p['document_sha256'] == hashlib.sha256(XML).hexdigest()
    assert len(doc['passages']) == 2
    with pytest.raises(ValueError, match='identity'): parse_document(XML, {**RECORD, 'pmcid': 'PMC999'})
    with pytest.raises(ValueError, match='pubmed'): parse_document(XML, {**RECORD, 'source_id': '999'})


@pytest.mark.parametrize('raw', [b'<!DOCTYPE a [<!ENTITY x SYSTEM "file:///secret">]>'+XML,
                               b'<!ENTITY x "boom">'+XML, b'\x00'+XML])
def test_unsafe_xml_cannot_expand_entities(raw):
    with pytest.raises(ValueError, match='unsafe_xml'): parse_document(raw, RECORD)


def test_only_open_access_records_can_be_requested(tmp_path):
    source = OpenSource(tmp_path, offline=True)
    with pytest.raises(ValueError, match='not_available'): source.fulltext({**RECORD, 'is_open_access': False})
    with pytest.raises(ValueError, match='not_available'): source.fulltext({**RECORD, 'pmcid': '../../secret'})
    with pytest.raises(RuntimeError, match='cache_miss'): source.fulltext(RECORD)


def test_batch_reads_quote_binding_and_literal_not_semantic_verification(tmp_path):
    r = make(tmp_path); result = r.run()
    assert r.state['status'] == 'completed'
    assert len(r.state['trace'][1]['raw_decision']['operations']) == 2
    f = result['findings'][0]
    assert f['section'] == 'Methods' and f['source_scope'] == 'open_full_text'
    assert f['semantic_verdict'] == 'unverified_model_assessment'
    assert f['quote_start'] == 0 and f['quote_end'] == len(f['quote'])


@pytest.mark.parametrize('change', ['unread', 'invented_quote', 'score', 'clinical_scope'])
def test_report_rejects_unbound_or_extra_authority_fields(tmp_path, change):
    r = make(tmp_path); r.run(); raw = copy.deepcopy(r.state['trace'][-1]['raw_decision'])
    if change == 'unread': raw['findings'][0]['passage_id'] = 'unread'
    if change == 'invented_quote': raw['findings'][0]['quote'] = 'An invented result that never happened'
    if change == 'score': raw['score'] = 10
    if change == 'clinical_scope': raw['findings'][0]['scope'] = 'verified_clinical_fact'
    with pytest.raises(ValueError): r.report(raw)


def test_failed_batch_stops_before_unexecuted_operation(tmp_path):
    r = make(tmp_path); r.source = Source(fault=True)
    r.run()
    event = r.state['trace'][1]['observation']
    assert not event['ok'] and len(event['outcomes']) == 2
    assert 'MED:1' in r.state['records'] and 'MED:2' not in r.state['records']
    # Still reported only actually read passages; failure did not create negative evidence.
    assert r.state['result']['findings'][0]['record_id'] == 'MED:1'


def test_successful_work_reuse_and_recall_of_omitted_passage(tmp_path):
    r = make(tmp_path); r.run(); count = len(r.source.requests)
    result = r.execute_op({'action': 'read', 'record_id': 'MED:1'})
    assert result['reused'] and len(r.source.requests) == count
    template = next(iter(r.state['passages'].values()))
    for i in range(20):
        p = dict(template, passage_id='synthetic_'+str(i), text='unrelated passage '+str(i))
        r.state['passages'][p['passage_id']] = p
    r.execute_op({'action': 'recall', 'passage_ids': ['synthetic_19']})
    assert 'synthetic_19' in {p['passage_id'] for p in r.context()['visible_passages']}
    assert len(r.context()['passage_index']) == len(r.state['passages'])


def test_compaction_preserves_negation_inside_selected_paragraph():
    text = 'Internalization assay ' + 'method context '*40 + 'was NOT performed in human cells.'
    p = {'text': text, 'passage_id': 'p'}
    assert 'NOT performed' not in text[:120]  # Counterexample to naive prefix compression.
    assert 'NOT performed' in rank([p], 'internalization', 1)[0]['text']


def test_resume_keeps_read_prefix_and_config_binding(tmp_path):
    r = make(tmp_path, pause_after=2)
    with pytest.raises(LiteraturePaused): r.run()
    prefix = copy.deepcopy(r.state['trace'])
    resumed = make(tmp_path, resume=True); resumed.run()
    assert resumed.state['trace'][:2] == prefix
    assert sum(x.get('kind') == 'read' for x in resumed.source.requests) == 2
    with pytest.raises(ValueError, match='binding'): make(tmp_path, resume=True, config={'compact': False})


def test_main_reintegration_preserves_nonempty_formal_state(tmp_path):
    state = RunState(cancer_id='synthetic', cancer_config={}, agent_config={})
    state.candidates['TARGET'] = Candidate(symbol='TARGET', identity=None, status='needs_more')
    state.notes = ['Existing verified pipeline output must not change.']; before = _encode(state)
    goal = {'target': 'TARGET', 'question': 'Investigate uptake and its controls.',
            'origin': {'run_id': state.run_id, 'dimension': 'internalization'}}
    c = OpenResearchController(state, goal, Client(), Source(), tmp_path, profile='combined')
    result = c.run()
    assert _encode(state) == before and result['phase'] == 'completed'
    assert result['observations'][0]['provenance_refs'][0]['source_scope'] == 'open_full_text'
    assert result['verification_queue'][0]['status'] == 'pending_human_verification'


def test_cached_fulltext_detects_tampering_and_identity_mismatch(tmp_path):
    source = OpenSource(tmp_path, offline=True)
    cached = {'url': 'https://www.ebi.ac.uk/europepmc/webservices/rest/PMC123/fullTextXML',
              'xml': XML.decode(), 'sha256': hashlib.sha256(XML).hexdigest()}
    path = tmp_path/'PMC123.xml.json'
    path.write_text(json.dumps({**cached, 'xml': XML.decode().replace('Methods', 'Forged')}), encoding='utf8')
    with pytest.raises(ValueError, match='cache_corrupt'): source.fulltext(RECORD)
    path.write_text(json.dumps(cached), encoding='utf8')
    with pytest.raises(ValueError, match='pubmed'): source.fulltext({**RECORD, 'source_id': '2'})
    assert source.fulltext(RECORD)['record_id'] == 'MED:1'


def test_failure_consumes_tool_allowance_but_never_becomes_success_memo(tmp_path):
    r = make(tmp_path); r.source = Source(fault=True)
    r.execute_op({'action': 'search', 'terms': 'uptake', 'scope': 'focused'})
    for _ in range(r.config['max_reads']):
        with pytest.raises(RuntimeError): r.execute_op({'action': 'read', 'record_id': 'MED:2'})
    with pytest.raises(ValueError, match='read_limit'): r.execute_op({'action': 'read', 'record_id': 'MED:2'})
    assert not r.state['records'] and not r.state['passages']


def test_batch_failure_skips_later_operation_and_forbidden_write(tmp_path):
    r = make(tmp_path); r.source = Source(fault=True)
    class FailingClient(Client):
        def complete_json(self, prompt, *, purpose):
            c = json.loads(prompt.split('\n', 1)[1])
            if len(c['read_metadata']) == 0 and c['discovered_records']:
                return {'state_id': c['state_id'], 'action': 'tools', 'reason': 'Test partial failure.',
                        'operations': [{'action': 'read', 'record_id': 'MED:2'}, {'action': 'read', 'record_id': 'MED:1'}]}
            return super().complete_json(prompt, purpose=purpose)
    r.client = FailingClient(); result = r.run()
    assert r.state['trace'][1]['observation']['skipped_operations'] == 1
    assert not any(x.get('id') == 'MED:1' for x in r.source.requests)
    assert not result['findings']
    with pytest.raises(ValueError, match='operation_schema'):
        r.execute_op({'action': 'write_evidence', 'score': 1})


def test_budget_exhaustion_never_synthesizes_findings(tmp_path):
    from evals.failure_driven_redesign.live_budget import BudgetStop
    class EmptyClient:
        def complete_json(self, prompt, *, purpose): raise BudgetStop('explicit_test_limit')
    r = make(tmp_path); r.client = EmptyClient(); result = r.run()
    assert r.state['status'] == 'budget_stopped' and not result['findings']
    assert not r.source.requests


@pytest.mark.parametrize('compact', [True, False])
def test_recall_actually_changes_visible_context_after_eviction(tmp_path, compact):
    r = make(tmp_path, config={'compact':compact}); r.run()
    first = next(iter(r.state['passages']))
    template = r.state['passages'][first]
    for i in range(25): r.state['passages']['later_'+str(i)] = dict(template, passage_id='later_'+str(i), text='Unrelated later item '+str(i))
    r.state['active'] = []
    r.execute_op({'action':'recall','passage_ids':[first]})
    assert r.context()['visible_passages'][0]['passage_id'] == first


def test_long_paragraph_chunking_does_not_drop_nonspace_characters():
    text = 'A'*4500
    raw = XML.replace(b'The antibody entered cells in an assay. This is a synthetic example, not clinical evidence.',text.encode())
    passages=parse_document(raw,RECORD)['passages'][:-1]
    assert ''.join(p['text'] for p in passages) == text


def test_fixed_retrieval_history_and_budget_stop(tmp_path):
    from research_v4.runtime import FixedResearcher
    from product.planner import digest
    from evals.failure_driven_redesign.live_budget import BudgetStop
    class EmptyClient:
        calls=0
        def complete_json(self, prompt, *, purpose):
            self.calls+=1; raise BudgetStop('explicit_test_limit')
    r = FixedResearcher({'target':'TARGET','question':'Which uptake assay?'},EmptyClient(),Source(),tmp_path)
    r.run()
    assert r.state['status']=='budget_stopped' and r.client.calls==1
    assert all(t['state_id']==digest(t['context']) for t in r.state['trace'])
    assert r.state['trace'][0]['context']['history']['reads']==[]


@pytest.mark.parametrize('tamper', [False, True])
def test_followup_inherits_discovered_catalog_only_with_same_origin(tmp_path, tamper):
    from product.planner import digest
    state=RunState(cancer_id='synthetic',cancer_config={},agent_config={})
    state.candidates['TARGET']=Candidate(symbol='TARGET',identity=None,status='needs_more')
    goal={'target':'TARGET','question':'Investigate the uptake assay.', 'origin':{'run_id':state.run_id,'dimension':'internalization'}}
    controller=OpenResearchController(state,goal,Client(),Source(),tmp_path,profile='compact',pause_after_import=True)
    with pytest.raises(LiteraturePaused):controller.run()
    controller.pause_after_import=False
    context=controller.context()
    event={'plan':{'state_id':digest(context),'action':'investigate','objective':'Read MED:2 and compare the earlier assay.',
           'reason':'Known record needs a focused follow-up.','basis':[controller.state['observations'][0]['id']]}}
    controller.execute_plan(event)
    if tamper:
        path=tmp_path/'investigation_1/researcher.json';saved=json.loads(path.read_text(encoding='utf8'))
        saved['payload']['goal']['origin']['run_id']='another_run'
        saved['sha256']=digest(saved['payload']);path.write_text(json.dumps(saved),encoding='utf8')
        with pytest.raises(ValueError,match='prior_catalog_binding'):controller.finish_pending()
    else:
        controller.finish_pending()
        saved=json.loads((tmp_path/'investigation_2/researcher.json').read_text(encoding='utf8'))['payload']['state']
        first=saved['trace'][0]['context']
        assert {h['id'] for h in first['discovered_records']}=={'MED:1','MED:2'}
        assert not first['read_metadata'] and not first['visible_passages']
        assert saved['trace'][0]['raw_decision']['operations'][0]['action']=='read'
        assert saved['inherited_catalog'][0]['investigation_id']=='investigation_1'
