"""Tool-contract handling: schema diffs, fix proposals, argument shims.

Everything here is deterministic. No LLM decides whether a call is allowed;
the proposal is rule-based and always waits for a human to approve it.
"""
from __future__ import annotations

from typing import Any

BREAKING = "breaking"
WARNING = "warning"
COMPATIBLE = "compatible"
_RANK = {COMPATIBLE: 0, WARNING: 1, BREAKING: 2}


def _norm(name: str) -> str:
    """order_id, orderId, Order-ID -> orderid"""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def diff_tool(old: dict, new: dict | None) -> list[dict]:
    """Compare two tool definitions ({name, description, input_schema})."""
    if new is None:
        return [{"kind": "tool_removed", "field": None, "severity": BREAKING,
                 "detail": f"tool '{old['name']}' no longer exists upstream"}]

    changes: list[dict] = []
    if old.get("description") != new.get("description"):
        changes.append({"kind": "description_changed", "field": None, "severity": WARNING,
                        "detail": "description changed (can change how the model picks the tool)"})

    o, n = old.get("input_schema", {}), new.get("input_schema", {})
    op, np_ = o.get("properties", {}), n.get("properties", {})
    oreq, nreq = set(o.get("required", [])), set(n.get("required", []))

    for f in op:
        if f not in np_:
            changes.append({"kind": "field_removed", "field": f, "severity": BREAKING,
                            "detail": f"parameter '{f}' was removed"})
    for f in np_:
        if f not in op:
            if f in nreq:
                changes.append({"kind": "required_field_added", "field": f, "severity": BREAKING,
                                "detail": f"new required parameter '{f}'"})
            else:
                changes.append({"kind": "optional_field_added", "field": f, "severity": COMPATIBLE,
                                "detail": f"new optional parameter '{f}'"})
    for f in op:
        if f in np_:
            if op[f].get("type") != np_[f].get("type"):
                changes.append({"kind": "type_changed", "field": f, "severity": BREAKING,
                                "detail": f"'{f}' type {op[f].get('type')} -> {np_[f].get('type')}"})
            if f not in oreq and f in nreq:
                changes.append({"kind": "became_required", "field": f, "severity": BREAKING,
                                "detail": f"'{f}' is now required"})
    return changes


def severity(changes: list[dict]) -> str:
    if not changes:
        return COMPATIBLE
    return max((c["severity"] for c in changes), key=lambda s: _RANK[s])


def propose_fix(old: dict, new: dict | None, changes: list[dict]) -> dict:
    """Draft a fix: the new definition plus a rename map for old argument names."""
    if new is None:
        return {"mapping": {}, "new_definition": None, "unresolved": ["tool removed"],
                "confidence": "needs_review",
                "summary": "Tool was removed upstream. Remove it from the agent or point it elsewhere."}

    removed = [c["field"] for c in changes if c["kind"] == "field_removed"]
    added = [c["field"] for c in changes if c["kind"] in ("required_field_added", "optional_field_added")]
    new_props = new.get("input_schema", {}).get("properties", {})
    old_props = old.get("input_schema", {}).get("properties", {})

    mapping: dict[str, str] = {}
    for r in removed:
        same = [a for a in added if _norm(a) == _norm(r) and a not in mapping.values()]
        if len(same) == 1:
            mapping[r] = same[0]
    # One leftover removed + one leftover added of the same type: likely a rename too.
    left_r = [r for r in removed if r not in mapping]
    left_a = [a for a in added if a not in mapping.values()]
    if len(left_r) == 1 and len(left_a) == 1 and \
            old_props.get(left_r[0], {}).get("type") == new_props.get(left_a[0], {}).get("type"):
        mapping[left_r[0]] = left_a[0]

    unresolved = [c["detail"] for c in changes
                  if c["severity"] == BREAKING and c.get("field") not in mapping
                  and c.get("field") not in mapping.values()]
    renames = ", ".join(f"{k} -> {v}" for k, v in mapping.items()) or "none"
    return {
        "mapping": mapping,
        "new_definition": new,
        "unresolved": unresolved,
        "confidence": "high" if not unresolved else "needs_review",
        "summary": f"Adopt the new definition. Renamed parameters: {renames}."
                   + (" Unresolved: " + "; ".join(unresolved) if unresolved else ""),
    }


def apply_mapping(args: dict, mapping: dict | None) -> tuple[dict, list[str]]:
    """Translate old argument names so agents built on the old contract keep working."""
    if not mapping:
        return args, []
    out, shimmed = {}, []
    for k, v in args.items():
        if k in mapping and mapping[k] not in args:
            out[mapping[k]] = v
            shimmed.append(f"{k}->{mapping[k]}")
        else:
            out[k] = v
    return out, shimmed


def validate_args(schema: dict, args: dict) -> list[str]:
    props = schema.get("properties", {})
    errors = [f"missing required parameter '{f}'" for f in schema.get("required", []) if f not in args]
    errors += [f"unknown parameter '{k}'" for k in args if k not in props]
    return errors


def to_openai_tools(defs: dict[str, dict]) -> list[dict[str, Any]]:
    return [{"type": "function",
             "function": {"name": d["name"], "description": d.get("description", ""),
                          "parameters": d.get("input_schema", {"type": "object", "properties": {}})}}
            for d in defs.values()]
