"""Scripted demo agent: plays the three failure scenarios through the Haltwise gateway.

    python agent/demo.py loop
    python agent/demo.py unsafe            # waits for you to click Approve/Deny in the dashboard
    python agent/demo.py drift             # waits for you to click "Approve fix" in the dashboard
    python agent/demo.py all --auto        # no clicks: decisions are made by the script (for tests)

The agent is deliberately naive: it does what a buggy or over-eager LLM agent would do.
Haltwise is what stops it. For a real LLM agent, see agent/llm_agent.py.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import httpx

GW = os.environ.get("HALTWISE_URL", "http://127.0.0.1:8000")
MOCK = os.environ.get("MOCK_URL", "http://127.0.0.1:9000")
AGENT = "support-bot"

BOLD, DIM, RED, GRN, YEL, CYN, END = "\033[1m", "\033[2m", "\033[31m", "\033[32m", "\033[33m", "\033[36m", "\033[0m"


def say(color, tag, msg):
    print(f"{color}{BOLD}{tag:<10}{END} {msg}", flush=True)


def call(run_id: str, tool: str, args: dict, approval_id: str | None = None) -> httpx.Response:
    headers = {"x-agent-id": AGENT}
    if approval_id:
        headers["x-approval-id"] = approval_id
    return httpx.post(f"{GW}/v1/runs/{run_id}/tools/{tool}", json={"args": args}, headers=headers, timeout=10)


def mock_admin(**state):
    httpx.post(f"{MOCK}/admin/state", json=state, timeout=5).raise_for_status()


def wait_decision(aid: str, auto: str | None, timeout: float = 180) -> str:
    if auto:
        httpx.post(f"{GW}/v1/approvals/{aid}/{auto}", timeout=5)
    else:
        say(YEL, "waiting", f"open {GW} and Approve or Deny request {aid}")
    t0 = time.time()
    while time.time() - t0 < timeout:
        status = httpx.get(f"{GW}/v1/approvals/{aid}", timeout=5).json()["status"]
        if status != "pending":
            return status
        time.sleep(0.5)
    return "timeout"


def scenario_loop(auto: bool) -> bool:
    print(f"\n{CYN}{BOLD}== Demo 1: runaway retry loop =={END}")
    print(f"{DIM}Task: 'refund order A1001, keep retrying until it succeeds'. The payment provider is down.{END}")
    mock_admin(fail_refunds=True)
    run = f"loop-{int(time.time())}"
    for attempt in range(1, 50):
        r = call(run, "refund_order", {"order_id": "A1001", "amount": 40})
        body = r.json()
        if r.status_code == 409 and body.get("error") == "run_killed":
            say(RED, "KILLED", f"Haltwise stopped the run after {attempt - 1} calls: {body['reason']}"
                               f" ({body.get('detail', '')})")
            mock_admin(fail_refunds=False)
            return body["reason"] == "loop_detected"
        if r.status_code == 503 and body.get("error") == "circuit_open":
            say(YEL, "blocked", f"attempt {attempt}: circuit open for refund_order, agent retries anyway")
            continue
        say(DIM, "agent", f"attempt {attempt}: refund_order -> {r.status_code} {body.get('result')}")
        time.sleep(0.2)
    mock_admin(fail_refunds=False)
    say(RED, "FAIL", "loop was never stopped")
    return False


def scenario_unsafe(auto: bool) -> bool:
    print(f"\n{CYN}{BOLD}== Demo 2: unsafe action =={END}")
    print(f"{DIM}Task: 'customer C42 asked to close their account'. Agent decides to delete the customer.{END}")
    run = f"unsafe-{int(time.time())}"
    r = call(run, "get_customer", {"customer_id": "C42"})
    say(DIM, "agent", f"get_customer -> {r.status_code} {r.json().get('result')}")
    r = call(run, "delete_customer", {"customer_id": "C42"})
    if r.status_code != 202:
        say(RED, "FAIL", f"expected the delete to be held, got {r.status_code} {r.text}")
        return False
    aid = r.json()["approval_id"]
    say(YEL, "HELD", f"delete_customer held for human approval ({r.json()['reason']}), request {aid}")
    decision = wait_decision(aid, "deny" if auto else None)
    if decision == "denied":
        r = call(run, "delete_customer", {"customer_id": "C42"}, approval_id=aid)
        say(GRN, "SAFE", f"human denied it; agent got {r.status_code} {r.json().get('error')}; customer data intact")
        return r.status_code == 403
    if decision == "approved":
        r = call(run, "delete_customer", {"customer_id": "C42"}, approval_id=aid)
        say(GRN, "APPROVED", f"human approved; delete ran -> {r.status_code} {r.json().get('result')}")
        mock_admin(reset=True)
        return r.status_code == 200
    say(RED, "TIMEOUT", "no decision made")
    return False


def scenario_drift(auto: bool) -> bool:
    print(f"\n{CYN}{BOLD}== Demo 3: tool contract drift =={END}")
    print(f"{DIM}A teammate ships a change to the orders service: 'order_id' becomes 'orderId'.{END}")
    run = f"drift-{int(time.time())}"
    mock_admin(version=2, fail_refunds=False)
    say(DIM, "deploy", "orders service v2 is live")
    ev = httpx.post(f"{GW}/v1/drift/check", timeout=10).json()
    if "refund_order" not in ev["quarantined"]:
        say(RED, "FAIL", f"drift not detected: {ev}")
        return False
    q = next(x for x in httpx.get(f"{GW}/v1/state", timeout=5).json()["quarantine"] if x["tool"] == "refund_order")
    say(YEL, "DRIFT", f"refund_order: {q['severity']} change - " + "; ".join(c["detail"] for c in q["changes"]))
    say(YEL, "PROPOSAL", q["proposal"]["summary"])

    r = call(run, "refund_order", {"order_id": "A1001", "amount": 40})
    say(YEL, "blocked", f"agent still uses the old contract -> {r.status_code} {r.json().get('error')} "
                        f"(instead of a silent wrong call)")

    if auto:
        httpx.post(f"{GW}/v1/drift/refund_order/approve", timeout=5)
    else:
        say(YEL, "waiting", f"open {GW} and click 'Approve fix' for refund_order")
    t0 = time.time()
    while time.time() - t0 < 180:
        if "refund_order" not in httpx.get(f"{GW}/v1/tools", timeout=5).json()["quarantined"]:
            break
        time.sleep(0.5)
    else:
        say(RED, "TIMEOUT", "fix not approved")
        return False

    r = call(run, "refund_order", {"order_id": "A1001", "amount": 40})
    shim = r.json().get("haltwise", {}).get("shimmed")
    say(GRN, "HEALED", f"same agent, old argument names -> {r.status_code} {r.json().get('result')} (shim: {shim})")
    tools = httpx.get(f"{GW}/v1/tools", timeout=5).json()["tools"]
    params = next(t for t in tools if t["function"]["name"] == "refund_order")["function"]["parameters"]
    say(GRN, "UPDATED", f"agents that refetch tools now get: {sorted(params['properties'])}")
    return r.status_code == 200


def main():
    p = argparse.ArgumentParser()
    p.add_argument("scenario", choices=["loop", "unsafe", "drift", "all"])
    p.add_argument("--auto", action="store_true", help="make approval decisions automatically")
    a = p.parse_args()
    if os.name == "nt":
        os.system("")  # turn on ANSI colours in the Windows console

    mock_admin(reset=True)
    httpx.post(f"{GW}/v1/admin/reset", timeout=10).raise_for_status()
    runs = {"loop": scenario_loop, "unsafe": scenario_unsafe, "drift": scenario_drift}
    names = list(runs) if a.scenario == "all" else [a.scenario]
    results = {n: runs[n](a.auto) for n in names}
    print()
    for n, ok in results.items():
        say(GRN if ok else RED, "PASS" if ok else "FAIL", n)
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
