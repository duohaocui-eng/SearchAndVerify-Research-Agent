"""Read an unresolved product objective without mutating candidate evidence."""
import hashlib
import json


def gap_goal(folder, target, dimension):
    summary_path=folder/'summary.json'
    summary=json.loads(summary_path.read_text(encoding='utf8'))
    if summary['statuses'].get(target)!='needs_more':raise ValueError('candidate_not_needs_more')
    if [target,dimension] not in summary.get('deferred',[]):raise ValueError('objective_not_deferred')
    return {'target':target,
            'question':f'Investigate the unresolved {dimension} evidence for {target}. What public experimental evidence supports this property, what only supports a related property, and what remains uncertain?',
            'origin':{'run_id':summary['run_id'],'dimension':dimension,'candidate_status':'needs_more',
                      'summary_sha256':hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                      'environment':summary['environment']},
            'authority':'Sidecar research brief only. Original candidate status, evidence and contract remain unchanged.'}
