from __future__ import annotations
import copy
import json
from pathlib import Path
from product.planner import digest

ROOT=Path(__file__).resolve().parents[2]

class Methods:
    def __init__(self):
        self.paths={p.stem:p for p in (ROOT/'configs/research_methods').glob('*.json')}
        self.active=None
        self.events=[]

    def catalog(self):
        return [{'id':k,'description':json.loads(p.read_text(encoding='utf-8-sig'))['description']} for k,p in sorted(self.paths.items())]

    def definition(self, name):
        if name not in self.paths:raise ValueError('unknown_method')
        return json.loads(self.paths[name].read_text(encoding='utf-8-sig'))

    def load(self, name, objective, step):
        if name not in self.paths:raise ValueError('unknown_method')
        data=json.loads(self.paths[name].read_text(encoding='utf-8-sig'))
        if objective['dimension'] not in data['dimensions']:raise ValueError('method_dimension_mismatch')
        if self.active:
            self.events.append({'event':'unload','step':step,'id':self.active['id'],'objective':self.active['objective']})
        self.active={'id':name,'sha256':digest(data),'definition':data,'objective':copy.deepcopy(objective),'loaded_at':step}
        self.events.append({'event':'load','step':step,**copy.deepcopy(self.active)})
        return self.active

    def finish(self, step, status):
        if self.active:
            self.events.append({'event':'result','step':step,'id':self.active['id'],'objective':self.active['objective'],'status':status})
            self.events.append({'event':'unload','step':step,'id':self.active['id']})
            self.active=None


def working_set(evidence, history, focus, *, mode='relevant', max_records=6):
    """Select detached records with source pointers, never summarize into facts.

    Relevant failures are pinned even when older than the recent window. The
    recency ablation uses the same number of record slots.
    """
    pairs={(x['target'],x['dimension']) for x in focus}
    targets={t for t,d in pairs}
    records=[]
    for e in evidence:
        records.append({'id':e['evidence_id'],'kind':'evidence','target':e['target'],'dimension':e['dimension'],
            'claim':e['claim'][:600],'verification':e['verification'],'source_ref':e['source_ref'],
            'source_id':e['source_id'],'score':e['score']})
    for h in history:
        a=h['action']
        records.append({'id':h['id'],'kind':'observation','target':a.get('target',''),'dimension':a.get('dimension',''),
            'ok':h['ok'],'source_id':a.get('source_id',''),'detail':h['detail'][:600]})
    if mode=='recency':selected=records[-max_records:]
    elif mode=='full':selected=records
    else:
        ranked=sorted(enumerate(records),key=lambda x:(
            (x[1]['target'],x[1]['dimension']) in pairs,
            x[1]['kind']=='observation' and x[1].get('ok') is False,
            x[1]['target'] in targets,x[0]),reverse=True)
        selected=[x[1] for x in ranked[:max_records]]
    return copy.deepcopy({'records':selected,'available_records':len(records),'selected_records':len(selected),
        'selection':mode,'raw_pointer':'evidence.json / research.json:history'})
