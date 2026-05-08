"""Deterministic, compact JSON serialization helpers.

The public ``stable_json_dump`` function returns a deterministic JSON string for
supported input trees by pre-normalizing sets/frozensets and delegating final
serialization to ``json.dumps``.

>>> stable_json_dump({"b": 1, "a": {"d": 4, "c": 3}})
'{"a":{"c":3,"d":4},"b":1}'
>>> stable_json_dump([{"z", "a"}])
'[["a","z"]]'
>>> stable_json_dump({"root": [{"flags": (None, True), "inner": {frozenset({2, 1}), frozenset({3})}}]})
'{"root":[{"flags":[null,true],"inner":[[1,2],[3]]}]}'
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["stable_json_dump"]


def _normalize(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj

    if isinstance(obj, dict):
        return {key: _normalize(value) for key, value in obj.items()}

    if isinstance(obj, (list, tuple)):
        return [_normalize(item) for item in obj]

    if isinstance(obj, (set, frozenset)):
        normalized_items = [_normalize(item) for item in obj]
        return sorted(normalized_items, key=repr)

    return obj


def stable_json_dump(obj: Any) -> str:
    normalized = _normalize(obj)
    return json.dumps(normalized, sort_keys=True, separators=(",", ":"))
