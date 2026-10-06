from __future__ import annotations
import hashlib
import html
import json
from pathlib import Path
import re
import time
import os
import uuid
import urllib.parse
import urllib.request
from product.runtime import write


class LiteratureSource:
    """One registered read-only provider. Queries always retain entity binding."""
    def __init__(self, cache, *, offline=False, fault=False):
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.offline, self.fault = offline, fault
        self.requests = []
        self.next_start = 0

    def request(self, params):
        url = 'https://www.ebi.ac.uk/europepmc/webservices/rest/search?'+urllib.parse.urlencode(params)
        key = hashlib.sha256(url.encode()).hexdigest()
        path = self.cache/(key+'.json')
        record = {'url': url, 'key': key, 'cache_hit': path.exists(), 'at': time.time()}
        self.requests.append(record)
        if self.fault:
            raise RuntimeError('declared_source_fault')
        if path.exists():
            stored = json.loads(path.read_text(encoding='utf8'))
            if stored['url'] != url: raise ValueError('cache_binding_mismatch')
            return stored['data']
        if self.offline:
            raise RuntimeError('offline_cache_miss')
        time.sleep(max(0, self.next_start-time.monotonic()))
        self.next_start = time.monotonic()+1
        request = urllib.request.Request(url, headers={'User-Agent': 'Candidate3-ResearchPrototype/0.2'})
        # No retries; errors become observations, not hidden unbounded work.
        with urllib.request.urlopen(request, timeout=25) as response:
            data = json.load(response)
        # Independent investigations can retrieve the same URL concurrently.
        temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps({'url':url,'retrieved_at':time.time(),'data':data},ensure_ascii=False),encoding='utf8')
        os.replace(temporary,path)
        return data

    def search(self, target, terms, *, scope='all'):
        if scope not in {'all','focused'}:raise ValueError('unknown_search_scope')
        if not isinstance(terms, str) or not 2 <= len(terms) <= 200:
            raise ValueError('query_length')
        # Free research terms, but no provider field syntax or entity-OR escape.
        if not re.fullmatch(r'[\w\s\-]+', terms, re.UNICODE):
            raise ValueError('invalid_query_terms')
        entity = re.sub(r'[^A-Za-z0-9_-]', '', target)
        if entity != target or not entity: raise ValueError('invalid_target')
        query = f'"{entity}" AND ({terms})'
        if scope=='focused':query='TITLE_ABS:'+query+' NOT PUB_TYPE:review'
        data = self.request({'query': query, 'format': 'json', 'pageSize': 5, 'resultType': 'lite'})
        hits = []
        for item in data.get('resultList', {}).get('result', []):
            if item.get('source') not in {'MED', 'PMC', 'PPR'}: continue
            identifier = str(item.get('id', ''))
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,40}', identifier): continue
            hits.append({'id': item['source']+':'+identifier, 'title': html.unescape(item.get('title', '')),
                         'year': item.get('pubYear'), 'source': item['source'], 'source_id': identifier,
                         'url': f"https://europepmc.org/article/{item['source']}/{identifier}"})
        return {'query': query, 'scope':scope, 'hit_count': data.get('hitCount', 0), 'hits': hits}

    def read(self, hit):
        query = f'EXT_ID:{hit["source_id"]} AND SRC:{hit["source"]}'
        data = self.request({'query': query, 'format': 'json', 'pageSize': 1, 'resultType': 'core'})
        items = data.get('resultList', {}).get('result', [])
        if not items: raise RuntimeError('record_unavailable')
        item = items[0]
        if str(item.get('id')) != hit['source_id'] or item.get('source') != hit['source']:
            raise ValueError('record_identity_mismatch')
        abstract = html.unescape(re.sub('<[^>]+>', ' ', item.get('abstractText', '')))
        abstract = re.sub(r'\s+', ' ', abstract).strip()
        return {**hit, 'abstract': abstract[:16000], 'abstract_truncated': len(abstract)>16000,
                'publication_types': item.get('pubTypeList', {}).get('pubType', []),
                'doi': item.get('doi'), 'abstract_sha256': hashlib.sha256(abstract.encode()).hexdigest(),
                'scope': 'Public abstract only; no automatic scoring or semantic verification.'}
