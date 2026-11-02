"""A fake internal service (customers + orders) that the demo agent's tools call.

/admin/state lets the demo flip two switches:
  fail_refunds: the payment provider times out on every refund (drives the loop demo)
  version: 2 renames refund_order's `order_id` to `orderId` (drives the drift demo)
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI(title="Acme internal API (mock)")

STATE = {"version": 1, "fail_refunds": False}
CUSTOMERS: dict = {}
ORDERS: dict = {}


def seed():
    CUSTOMERS.clear()
    ORDERS.clear()
    CUSTOMERS["C42"] = {"id": "C42", "name": "Asha Rao", "plan": "pro"}
    ORDERS["A1001"] = {"id": "A1001", "customer_id": "C42", "amount": 40.0, "refunded": False}


seed()


def _order_key() -> str:
    return "order_id" if STATE["version"] == 1 else "orderId"


def tool_defs() -> list[dict]:
    k = _order_key()
    return [
        {"name": "get_customer", "description": "Look up a customer by id.",
         "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}},
                          "required": ["customer_id"]}},
        {"name": "refund_order", "description": "Refund an order, fully or partially.",
         "input_schema": {"type": "object",
                          "properties": {k: {"type": "string"}, "amount": {"type": "number"}},
                          "required": [k, "amount"]}},
        {"name": "delete_customer", "description": "Permanently delete a customer and their data.",
         "input_schema": {"type": "object", "properties": {"customer_id": {"type": "string"}},
                          "required": ["customer_id"]}},
    ]


@app.get("/schema")
def schema():
    return {"version": STATE["version"], "tools": tool_defs()}


@app.post("/tools/get_customer")
async def get_customer(req: Request):
    a = await req.json()
    c = CUSTOMERS.get(a.get("customer_id"))
    return c if c else JSONResponse(status_code=404, content={"error": "customer not found"})


@app.post("/tools/refund_order")
async def refund_order(req: Request):
    a = await req.json()
    k = _order_key()
    if k not in a:
        return JSONResponse(status_code=422, content={"error": f"missing field '{k}'"})
    if STATE["fail_refunds"]:
        return JSONResponse(status_code=503, content={"error": "payment provider timeout"})
    o = ORDERS.get(a[k])
    if not o:
        return JSONResponse(status_code=404, content={"error": "order not found"})
    o["refunded"] = True
    return {"order": o["id"], "refunded_amount": a.get("amount"), "status": "refunded"}


@app.post("/tools/delete_customer")
async def delete_customer(req: Request):
    a = await req.json()
    if CUSTOMERS.pop(a.get("customer_id"), None) is None:
        return JSONResponse(status_code=404, content={"error": "customer not found"})
    return {"deleted": a["customer_id"]}


@app.get("/admin/state")
def get_state():
    return {**STATE, "customers": sorted(CUSTOMERS), "orders": ORDERS}


@app.post("/admin/state")
async def set_state(req: Request):
    body = await req.json()
    if body.get("reset"):
        STATE.update(version=1, fail_refunds=False)
        seed()
    for k in ("version", "fail_refunds"):
        if k in body:
            STATE[k] = body[k]
    return get_state()
