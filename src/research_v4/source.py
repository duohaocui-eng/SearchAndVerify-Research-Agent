"""Registered Europe PMC OA XML, bounded section retrieval, literal span binding."""
from __future__ import annotations
from collections import Counter
import hashlib
import html
import json
import math
from pathlib import Path
import re
import time
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from literature_agent.source import LiteratureSource
from product.runtime import write
from evals.failure_driven_redesign.live_budget import NoRedirect


def tokens(text):
    return [x.lower() for x in re.findall(r'[A-Za-z0-9]+', text) if len(x) > 2 and x.lower() not in
            {'the', 'and', 'for', 'with', 'that', 'from', 'this', 'were', 'was', 'are', 'not', 'into', 'how', 'what'}]


def rank(passages, query, limit=6):
    """Small in-document BM25; scores measure term relevance, never truth."""
    terms = set(tokens(query)); docs = [Counter(tokens(p['text'])) for p in passages]
    avg = sum(sum(d.values()) for d in docs)/max(1, len(docs)); scored = []
    for i, (p, counts) in enumerate(zip(passages, docs)):
        score = 0.
        for term in terms:
            n = counts[term]
            df = sum(term in d for d in docs)
            idf = math.log(1+(len(docs)-df+.5)/(df+.5))
            score += idf*n*2.2/(n+1.2*(.25+.75*sum(counts.values())/max(1, avg)))
        scored.append((score, i, p))
    return [dict(p, retrieval_score=round(score, 4)) for score, _, p in sorted(scored, key=lambda x: (-x[0], x[1]))[:limit]]


def parse_document(raw, record):
    text = raw.decode('utf8')
    if '\x00' in text or '<!ENTITY' in text.upper():
        raise ValueError('unsafe_xml_entity')
    root = ET.fromstring(text)
    ids = {e.attrib.get('pub-id-type'): ''.join(e.itertext()).strip() for e in root.findall('./front/article-meta/article-id')}
    if ids.get('pmc', ids.get('pmcid', '')).removeprefix('PMC') != record['pmcid'].removeprefix('PMC'):
        raise ValueError('fulltext_identity_mismatch')
    if ids.get('pmid') and record['source'] == 'MED' and ids['pmid'] != record['source_id']:
        raise ValueError('fulltext_pubmed_mismatch')
    sha = hashlib.sha256(raw).hexdigest(); passages = []
    def visit(element, headings):
        if element.tag == 'sec':
            title = element.find('title')
            headings = headings+[(' '.join(title.itertext()).strip() if title is not None else element.get('sec-type', 'Section'))]
        if element.tag == 'p':
            paragraph = ' '.join(''.join(element.itertext()).split())
            # Preserve normalized paragraph coordinates; long paragraphs are chunked.
            start = 0
            while start < len(paragraph):
                end = min(len(paragraph), start+2200)
                if end < len(paragraph): end = paragraph.rfind(' ', start+1000, end) if ' ' in paragraph[start+1000:end] else end
                span = paragraph[start:end]
                if span:
                    passages.append({'passage_id': record['id']+':'+sha[:12]+':p'+str(len(passages)+1),
                        'record_id': record['id'], 'section': ' / '.join(headings) or 'Body',
                        'paragraph_sha256': hashlib.sha256(paragraph.encode()).hexdigest(),
                        'start': start, 'end': end, 'text': span, 'document_sha256': sha,
                        'source_scope': 'open_full_text', 'url': 'https://europepmc.org/articles/'+record['pmcid']})
                start = end+1 if end < len(paragraph) and paragraph[end] == ' ' else end
            return
        for child in element:
            if child.tag not in {'ref-list', 'table-wrap', 'supplementary-material'}: visit(child, headings)
    body = root.find('body')
    if body is None: raise ValueError('fulltext_body_unavailable')
    visit(body, [])
    if not passages: raise ValueError('fulltext_no_readable_paragraphs')
    licenses = [' '.join(x.itertext()).strip() for x in root.findall('./front/article-meta/permissions/license')]
    return {'record_id': record['id'], 'pmcid': record['pmcid'], 'document_sha256': sha,
            'license_text': licenses, 'passages': passages,
            'limitations': ['Only structured body paragraphs are indexed; long paragraphs are chunked. Tables, supplements, images and reference lists are excluded.',
                            'Document retrieval and literal spans do not verify semantic or biological claims.']}


class OpenSource(LiteratureSource):
    def __init__(self, cache, *, offline=False, fault=False, fulltext_fault=False):
        super().__init__(cache, offline=offline, fault=fault)
        self.fulltext_fault = fulltext_fault
        self.documents = {}

    def read(self, hit):
        data = self.request({'query': f'EXT_ID:{hit["source_id"]} AND SRC:{hit["source"]}',
                             'format': 'json', 'pageSize': 1, 'resultType': 'core'})
        if not data.get('resultList', {}).get('result'): raise RuntimeError('record_unavailable')
        item = data['resultList']['result'][0]
        if str(item.get('id')) != hit['source_id'] or item.get('source') != hit['source']:
            raise ValueError('record_identity_mismatch')
        abstract = ' '.join(html.unescape(re.sub('<[^>]+>', ' ', item.get('abstractText', ''))).split())
        return {**hit, 'abstract': abstract[:16000], 'abstract_truncated': len(abstract) > 16000,
                'abstract_sha256': hashlib.sha256(abstract.encode()).hexdigest(),
                'publication_types': item.get('pubTypeList', {}).get('pubType', []),
                'pmcid': item.get('pmcid'), 'is_open_access': item.get('isOpenAccess') == 'Y'}

    def fulltext(self, record):
        if not record.get('is_open_access') or not re.fullmatch(r'PMC[0-9]+', record.get('pmcid') or ''):
            raise ValueError('open_fulltext_not_available')
        key = record['pmcid']
        if key in self.documents: return self.documents[key]
        path = self.cache/(key+'.xml.json')
        url = 'https://www.ebi.ac.uk/europepmc/webservices/rest/'+key+'/fullTextXML'
        self.requests.append({'kind': 'fulltext', 'url': url, 'cache_hit': path.exists(), 'at': time.time()})
        if self.fault or self.fulltext_fault: raise RuntimeError('declared_fulltext_fault')
        if path.exists():
            cached = json.loads(path.read_text(encoding='utf8'))
            if cached['url'] != url: raise ValueError('fulltext_cache_binding')
            raw = cached['xml'].encode()
            if hashlib.sha256(raw).hexdigest() != cached['sha256']: raise ValueError('fulltext_cache_corrupt')
        else:
            if self.offline: raise RuntimeError('offline_fulltext_cache_miss')
            time.sleep(max(0, self.next_start-time.monotonic())); self.next_start = time.monotonic()+1
            request = urllib.request.Request(url, headers={'User-Agent': 'Candidate3-OpenResearch/0.4'})
            with urllib.request.build_opener(NoRedirect).open(request, timeout=25) as response:
                raw = response.read(5_000_001)
            if len(raw) > 5_000_000: raise ValueError('fulltext_size_limit')
            parse_document(raw, record)  # Validate identity and XML before caching.
            temporary = path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
            temporary.write_text(json.dumps({'url': url, 'retrieved_at': time.time(), 'xml': raw.decode('utf8'),
                         'sha256': hashlib.sha256(raw).hexdigest()}, ensure_ascii=False), encoding='utf8')
            temporary.replace(path)
        if len(raw) > 5_000_000: raise ValueError('fulltext_size_limit')
        document = parse_document(raw, record)
        self.documents[key] = document
        return document

    def locate(self, record, query, limit=6):
        if not isinstance(query, str) or not 2 <= len(query) <= 240: raise ValueError('passage_query_length')
        doc = self.fulltext(record)
        return {'record_id': record['id'], 'document_sha256': doc['document_sha256'],
                'passages': rank(doc['passages'], query, limit), 'query': query,
                'total_passages': len(doc['passages']), 'limitations': doc['limitations']}
