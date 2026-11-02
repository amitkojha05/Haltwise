"""Haltwise gateway.

Every agent tool call goes through POST /v1/runs/{run_id}/tools/{tool}.
Checks run in this order, and a call that fails any of them never reaches the tool:

  1. run state        - a killed run stays killed
  2. contract         - quarantined tools are blocked; old argument names are shimmed
  3. budget           - max calls and max cost per run
  4. loop detection   - same tool + same args + same result N times = runaway loop
  5. circuit breaker  - N consecutive failures on a tool opens its circuit
  6. action policy    - allow / deny / hold for human approval
  7. schema check     - arguments must match the approved contract

Every decision is written to the audit log.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .contract import (BREAKING, apply_mapping, diff_tool, propose_fix, severity,
                       to_openai_tools, validate_args)

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("HALTWISE_CONFIG", ROOT / "haltwise.yaml"))
AUDIT_PATH = Path(os.environ.get("HALTWISE_AUDIT", ROOT / "audit.log.jsonl"))
DASHBOARD = Path(__file__).resolve().parent / "dashboard.html"


def _sig(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


class State:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.upstream = os.environ.get("HALTWISE_UPSTREAM", cfg["upstream"]).rstrip("/")
        self.reset()

    def reset(self):
        self.tools: dict[str, dict] = {}        # approved contracts
        self.mappings: dict[str, dict] = {}     # tool -> {old_arg: new_arg}
        self.quarantine: dict[str, dict] = {}   # tool -> drift record + proposal
        self.runs: dict[str, dict] = {}
        self.circuits: dict[str, dict] = {}
        self.approvals: dict[str, dict] = {}
        self.events: list[dict] = []

    def event(self, kind: str, **data):
        ev = {"ts": round(time.time(), 3), "kind": kind, **data}
        self.events.append(ev)
        self.events = self.events[-200:]
        with AUDIT_PATH.open("a") as f:
            f.write(json.dumps(ev) + "\n")
        return ev

    def run(self, run_id: str, agent: str) -> dict:
        if run_id not in self.runs:
            self.runs[run_id] = {"run_id": run_id, "agent": agent, "calls": 0, "cost_usd": 0.0,
                                 "status": "running", "reason": None, "started": time.time(),
                                 "last_sig": None, "last_result": None, "repeats": 0}
            self.event("run_started", run_id=run_id, agent=agent)
        return self.runs[run_id]

    def circuit(self, tool: str) -> dict:
        return self.circuits.setdefault(tool, {"state": "closed", "failures": 0, "opened_at": None})


S: State  # set in lifespan


def load_config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text())


async def fetch_upstream_schema() -> dict[str, dict]:
    async with httpx.AsyncClient(timeout=5) as c:
        r = await c.get(f"{S.upstream}/schema")
        r.raise_for_status()
        return {t["name"]: t for t in r.json()["tools"]}


async def load_baseline(retries: int = 40):
    for _ in range(retries):
        try:
            S.tools = await fetch_upstream_schema()
            S.event("contracts_loaded", tools=sorted(S.tools))
            return
        except Exception:
            await asyncio.sleep(0.5)
    S.event("contracts_unavailable", upstream=S.upstream)


async def check_drift() -> list[dict]:
    """Compare live upstream schemas with approved contracts; quarantine breaking changes."""
    try:
        live = await fetch_upstream_schema()
    except Exception as e:
        return [S.event("drift_check_failed", error=str(e))]
    found = []
    for name, approved in S.tools.items():
        changes = diff_tool(approved, live.get(name))
        if not changes:
            continue
        live_hash = _sig(live.get(name))
        q = S.quarantine.get(name)
        if q and q["live_hash"] == live_hash:
            continue  # already reported this exact change
        sev = severity(changes)
        record = {"tool": name, "severity": sev, "changes": changes, "live_hash": live_hash,
                  "detected_at": time.time(), "proposal": propose_fix(approved, live.get(name), changes)}
        if sev == BREAKING:
            S.quarantine[name] = record
            found.append(S.event("drift_breaking", tool=name, changes=[c["detail"] for c in changes],
                                 proposal=record["proposal"]["summary"]))
        else:
            S.tools[name] = live[name]  # non-breaking: adopt and just note it
            found.append(S.event("drift_compatible", tool=name, changes=[c["detail"] for c in changes]))
    return found


async def drift_poller():
    every = S.cfg.get("drift", {}).get("poll_seconds", 3)
    while True:
        await asyncio.sleep(every)
        if S.tools:
            await check_drift()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global S
    S = State(load_config())
    await load_baseline()
    task = asyncio.create_task(drift_poller())
    yield
    task.cancel()


app = FastAPI(title="Haltwise gateway", lifespan=lifespan)


def _kill(run: dict, reason: str, detail: str) -> JSONResponse:
    run["status"], run["reason"] = "killed", reason
    S.event("run_killed", run_id=run["run_id"], reason=reason, detail=detail)
    return JSONResponse(status_code=409, content={"error": "run_killed", "reason": reason, "detail": detail})


def _deny(status: int, error: str, run: dict, tool: str, args: dict, detail, **extra) -> JSONResponse:
    S.event("call_blocked", run_id=run["run_id"], tool=tool, args=args, error=error, detail=detail)
    return JSONResponse(status_code=status, content={"error": error, "detail": detail, **extra})


def _match(rule: dict, tool: str, args: dict) -> bool:
    if rule.get("tool", "*") not in ("*", tool):
        return False
    w = rule.get("when")
    if not w:
        return True
    v = args.get(w["arg"])
    if v is None:
        return False
    op, target = w.get("op", "eq"), w["value"]
    try:
        return {"eq": v == target, "gt": float(v) > float(target),
                "gte": float(v) >= float(target), "lt": float(v) < float(target)}[op]
    except (TypeError, ValueError):
        return False


def evaluate_policy(tool: str, args: dict) -> dict:
    for rule in S.cfg.get("policies", []):
        if _match(rule, tool, args):
            return rule
    return {"action": "allow"}


@app.post("/v1/runs/{run_id}/tools/{tool}")
async def call_tool(run_id: str, tool: str, request: Request,
                    x_agent_id: str = Header("demo-agent"),
                    x_approval_id: str | None = Header(None)):
    body = await request.json() if await request.body() else {}
    args = body.get("args", {}) if isinstance(body, dict) else {}
    run = S.run(run_id, x_agent_id)
    cfg = S.cfg

    # 1. run state
    if run["status"] == "killed":
        return JSONResponse(status_code=409, content={"error": "run_killed", "reason": run["reason"]})
    if tool not in S.tools:
        return _deny(404, "unknown_tool", run, tool, args, f"'{tool}' is not an approved tool")

    # 2. contract quarantine + argument shim
    if tool in S.quarantine:
        q = S.quarantine[tool]
        return _deny(409, "tool_quarantined", run, tool, args,
                     "upstream contract changed in a breaking way; waiting for approval of the proposed fix",
                     changes=[c["detail"] for c in q["changes"]], proposal=q["proposal"]["summary"])
    args, shimmed = apply_mapping(args, S.mappings.get(tool))

    # 3. budget
    b = cfg["budgets"]["per_run"]
    cost = float(cfg.get("tool_costs_usd", {}).get(tool, cfg.get("default_tool_cost_usd", 0.01)))
    if run["calls"] + 1 > b["max_calls"]:
        return _kill(run, "budget_calls", f"run would exceed {b['max_calls']} tool calls")
    if run["cost_usd"] + cost > b["max_cost_usd"] + 1e-9:
        return _kill(run, "budget_cost", f"run would exceed ${b['max_cost_usd']:.2f}")

    # 4. loop detection: identical call that keeps getting the identical result
    sig = _sig(tool, args)
    max_rep = cfg["loop"]["max_identical_calls"]
    if sig == run["last_sig"] and run["repeats"] >= max_rep:
        return _kill(run, "loop_detected",
                     f"{tool} called {run['repeats']} times with identical arguments and identical results")

    # 5. circuit breaker
    c = S.circuit(tool)
    cb = cfg["circuit"]
    if c["state"] == "open":
        if time.time() - c["opened_at"] < cb["cooldown_seconds"]:
            return _deny(503, "circuit_open", run, tool, args,
                         f"{tool} failed {c['failures']} times in a row; circuit open")
        c["state"] = "half_open"
        S.event("circuit_half_open", tool=tool)

    # 6. action policy
    rule = evaluate_policy(tool, args)
    if rule["action"] == "deny":
        return _deny(403, "policy_denied", run, tool, args, rule.get("reason", "denied by policy"))
    if rule["action"] == "require_approval":
        appr = S.approvals.get(x_approval_id or "")
        if not appr or appr["sig"] != _sig(run_id, tool, args):
            aid = uuid.uuid4().hex[:8]
            S.approvals[aid] = {"id": aid, "run_id": run_id, "tool": tool, "args": args,
                                "reason": rule.get("reason", "requires approval"),
                                "status": "pending", "sig": _sig(run_id, tool, args), "created": time.time()}
            S.event("approval_requested", approval_id=aid, run_id=run_id, tool=tool, args=args,
                    reason=S.approvals[aid]["reason"])
            return JSONResponse(status_code=202, content={"status": "pending_approval", "approval_id": aid,
                                                          "reason": S.approvals[aid]["reason"]})
        if appr["status"] == "denied":
            return _deny(403, "approval_denied", run, tool, args, "a human denied this action")
        if appr["status"] != "approved":
            return JSONResponse(status_code=202, content={"status": "pending_approval",
                                                          "approval_id": appr["id"]})
        if appr.get("used"):
            return _deny(403, "approval_used", run, tool, args, "approval already used")
        appr["used"] = True

    # 7. schema check against the approved contract
    errors = validate_args(S.tools[tool].get("input_schema", {}), args)
    if errors:
        return _deny(422, "contract_violation", run, tool, args, errors)

    # forward
    t0 = time.time()
    try:
        async with httpx.AsyncClient(timeout=cfg.get("upstream_timeout_seconds", 5)) as client:
            r = await client.post(f"{S.upstream}/tools/{tool}", json=args)
        status, result = r.status_code, (r.json() if r.content else None)
    except Exception as e:
        status, result = 502, {"error": f"upstream unreachable: {e}"}
    ms = round((time.time() - t0) * 1000, 1)

    run["calls"] += 1
    run["cost_usd"] = round(run["cost_usd"] + cost, 4)
    rsig = _sig(status, result)
    if sig == run["last_sig"] and rsig == run["last_result"]:
        run["repeats"] += 1
    else:
        run["repeats"] = 1
    run["last_sig"], run["last_result"] = sig, rsig

    ok = status < 400
    if ok:
        if c["state"] != "closed":
            S.event("circuit_closed", tool=tool)
        c.update(state="closed", failures=0, opened_at=None)
    else:
        c["failures"] += 1
        if c["state"] == "half_open" or c["failures"] >= cb["failure_threshold"]:
            c.update(state="open", opened_at=time.time())
            S.event("circuit_opened", tool=tool, failures=c["failures"])

    S.event("call", run_id=run_id, tool=tool, args=args, status=status, ok=ok, ms=ms,
            cost_usd=cost, shimmed=shimmed or None)
    return JSONResponse(status_code=status, content={"result": result, "haltwise": {
        "run_calls": run["calls"], "run_cost_usd": run["cost_usd"], "shimmed": shimmed}})


@app.get("/v1/tools")
async def list_tools():
    live = {k: v for k, v in S.tools.items() if k not in S.quarantine}
    return {"tools": to_openai_tools(live), "quarantined": sorted(S.quarantine)}


@app.get("/v1/approvals/{aid}")
async def get_approval(aid: str):
    if aid not in S.approvals:
        raise HTTPException(404)
    return S.approvals[aid]


@app.post("/v1/approvals/{aid}/{decision}")
async def decide(aid: str, decision: str):
    if aid not in S.approvals or decision not in ("approve", "deny"):
        raise HTTPException(404)
    a = S.approvals[aid]
    a["status"] = "approved" if decision == "approve" else "denied"
    S.event(f"approval_{a['status']}", approval_id=aid, tool=a["tool"], run_id=a["run_id"])
    return a


@app.post("/v1/drift/check")
async def drift_check():
    return {"events": await check_drift(), "quarantined": sorted(S.quarantine)}


@app.post("/v1/drift/{tool}/approve")
async def approve_fix(tool: str):
    q = S.quarantine.pop(tool, None)
    if not q:
        raise HTTPException(404, "no pending drift for this tool")
    p = q["proposal"]
    if p["new_definition"] is None:
        S.tools.pop(tool, None)
    else:
        S.tools[tool] = p["new_definition"]
        S.mappings[tool] = p["mapping"]
    S.circuits.pop(tool, None)
    S.event("drift_fix_approved", tool=tool, mapping=p["mapping"])
    return {"tool": tool, "mapping": p["mapping"], "definition": p["new_definition"]}


@app.post("/v1/admin/reset")
async def reset():
    S.reset()
    await load_baseline(retries=10)
    return {"ok": True, "tools": sorted(S.tools)}


@app.get("/v1/state")
async def state():
    return {
        "budgets": S.cfg["budgets"]["per_run"],
        "runs": sorted(({k: v for k, v in r.items() if k not in ("last_sig", "last_result")}
                        for r in S.runs.values()), key=lambda r: -r["started"]),
        "approvals": sorted(S.approvals.values(), key=lambda a: -a["created"]),
        "quarantine": list(S.quarantine.values()),
        "circuits": S.circuits,
        "tools": sorted(S.tools),
        "events": S.events[-60:][::-1],
    }


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD.read_text(encoding="utf-8")
