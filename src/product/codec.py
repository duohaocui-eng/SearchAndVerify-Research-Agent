"""Typed JSON codec adapted from historical decision checkpoints; no pickle."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import hashlib
import json
from pathlib import Path
import time

from agent.planner import LLMPlanner
from agent.state import Budget, Candidate, RunState
from evidence_core.evidence_store import Conflict, EvidenceStore
from evidence_core.normalize import GeneIdentity
from evidence_core.registry import SourceRegistry, SourceSpec
from evidence_core.schema import EvidenceItem

VERSION = 'offline-decision-checkpoint-v1'
CLASSES = {c.__name__: c for c in (Budget, Candidate, RunState, Conflict,
                                  GeneIdentity, SourceSpec, EvidenceItem)}


class FrozenBudget(Budget):
    @property
    def elapsed(self):
        return self._checkpoint_elapsed


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _runtime():
    root = Path(__file__).resolve().parents[2]
    # A changed planner, schema, scorer or policy must not silently replay an old
    # checkpoint under different semantics. No source contents or secrets stored.
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / 'src').rglob('*.py'))}


def _encode(value):
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, EvidenceStore):
        return {'type': 'store', 'value': _encode(vars(value))}
    if is_dataclass(value):
        name = 'Budget' if isinstance(value, FrozenBudget) else type(value).__name__
        if name not in CLASSES:
            raise ValueError('unsupported_checkpoint_dataclass: ' + name)
        return {'type': name, 'value': _encode({f.name: getattr(value, f.name) for f in fields(value)})}
    if isinstance(value, dict):
        # Entry arrays preserve insertion order, including registry tie order.
        return {'type': 'dict', 'value': [[_encode(k), _encode(v)] for k, v in value.items()]}
    if isinstance(value, (list, tuple, set)):
        entries = [_encode(v) for v in value]
        if isinstance(value, set):
            entries.sort(key=lambda v: json.dumps(v, sort_keys=True))
        return {'type': type(value).__name__, 'value': entries}
    raise ValueError('unsupported_checkpoint_value: ' + type(value).__name__)


def _decode(value):
    if not isinstance(value, dict):
        if value is None or type(value) in (str, int, float, bool):
            return value
        raise ValueError('invalid_checkpoint_encoding')
    if set(value) != {'type', 'value'}:
        raise ValueError('invalid_checkpoint_encoding')
    kind, raw = value['type'], value['value']
    if kind == 'dict':
        result = {}
        for key, entry in raw:
            key = _decode(key)
            if key in result:
                raise ValueError('duplicate_checkpoint_key')
            result[key] = _decode(entry)
        return result
    if kind in {'list', 'tuple', 'set'}:
        return {'list': list, 'tuple': tuple, 'set': set}[kind](_decode(v) for v in raw)
    if kind == 'store':
        result = EvidenceStore()
        attributes = _decode(raw)
        if set(attributes) != set(vars(result)):
            raise ValueError('store_schema_changed')
        vars(result).update(attributes)
        return result
    if kind in CLASSES:
        attributes = _decode(raw)
        if set(attributes) != {f.name for f in fields(CLASSES[kind])}:
            raise ValueError('dataclass_schema_changed: ' + kind)
        return CLASSES[kind](**attributes)
    raise ValueError('unknown_checkpoint_type')

