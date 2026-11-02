"""A real LLM agent whose tool calls all go through Haltwise.

    export ANTHROPIC_API_KEY=...   (or OPENAI_API_KEY=...)
    python agent/llm_agent.py "Refund order A1001 for customer C42. Keep retrying until it works."

Tool definitions come from the gateway (GET /v1/tools), so after a drift fix is approved
the model sees the new contract on its next step. Gateway decisions (killed, held,
quarantined) are returned to the model as tool results, which is how it learns to stop.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

import httpx

GW = os.environ.get("HALTWISE_URL", "http://127.0.0.1:8000")
MAX_STEPS = 25
SYSTEM = "You are a customer-support operations agent. Use the tools to complete the task."


def gateway_tools() -> list[dict]:
    return httpx.get(f"{GW}/v1/tools", timeout=10).json()["tools"]


def run_tool(run_id: str, name: str, args: dict) -> tuple[str, bool]:
    """Returns (text for the model, stop?)."""
    r = httpx.post(f"{GW}/v1/runs/{run_id}/tools/{name}", json={"args": args},
                   headers={"x-agent-id": "llm-agent"}, timeout=30)
    body = r.json()
    if r.status_code == 202:
        aid = body["approval_id"]
        print(f"  held for approval {aid}: open {GW} to decide")
        while (status := httpx.get(f"{GW}/v1/approvals/{aid}", timeout=10).json()["status"]) == "pending":
            time.sleep(1)
        if status == "approved":
            r = httpx.post(f"{GW}/v1/runs/{run_id}/tools/{name}", json={"args": args},
                           headers={"x-agent-id": "llm-agent", "x-approval-id": aid}, timeout=30)
            body = r.json()
        else:
            return json.dumps({"error": "a human denied this action"}), False
    stop = r.status_code == 409 and body.get("error") == "run_killed"
    print(f"  {name}({json.dumps(args)}) -> {r.status_code} {json.dumps(body)[:160]}")
    return json.dumps(body), stop


def anthropic_loop(task: str, run_id: str):
    import anthropic  # pip install anthropic
    client = anthropic.Anthropic()
    model = os.environ.get("HALTWISE_MODEL", "claude-sonnet-4-5")
    messages = [{"role": "user", "content": task}]
    for _ in range(MAX_STEPS):
        tools = [{"name": t["function"]["name"], "description": t["function"]["description"],
                  "input_schema": t["function"]["parameters"]} for t in gateway_tools()]
        resp = client.messages.create(model=model, max_tokens=1024, system=SYSTEM, tools=tools, messages=messages)
        messages.append({"role": "assistant", "content": resp.content})
        calls = [b for b in resp.content if b.type == "tool_use"]
        if not calls:
            print("agent:", "".join(b.text for b in resp.content if b.type == "text"))
            return
        results, stop = [], False
        for c in calls:
            text, s = run_tool(run_id, c.name, c.input)
            results.append({"type": "tool_result", "tool_use_id": c.id, "content": text})
            stop = stop or s
        messages.append({"role": "user", "content": results})
        if stop:
            print("Haltwise killed this run.")
            return


def openai_loop(task: str, run_id: str):
    from openai import OpenAI  # pip install openai
    client = OpenAI()
    model = os.environ.get("HALTWISE_MODEL", "gpt-4.1-mini")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": task}]
    for _ in range(MAX_STEPS):
        resp = client.chat.completions.create(model=model, messages=messages, tools=gateway_tools())
        msg = resp.choices[0].message
        messages.append(msg)
        if not msg.tool_calls:
            print("agent:", msg.content)
            return
        stop = False
        for c in msg.tool_calls:
            text, s = run_tool(run_id, c.function.name, json.loads(c.function.arguments or "{}"))
            messages.append({"role": "tool", "tool_call_id": c.id, "content": text})
            stop = stop or s
        if stop:
            print("Haltwise killed this run.")
            return


if __name__ == "__main__":
    task = " ".join(sys.argv[1:]) or "Refund order A1001 ($40) for customer C42. Keep retrying until it works."
    run_id = f"llm-{uuid.uuid4().hex[:6]}"
    print(f"run {run_id}: {task}")
    if os.environ.get("ANTHROPIC_API_KEY"):
        anthropic_loop(task, run_id)
    elif os.environ.get("OPENAI_API_KEY"):
        openai_loop(task, run_id)
    else:
        sys.exit("Set ANTHROPIC_API_KEY or OPENAI_API_KEY (or use agent/demo.py, which needs no key).")
