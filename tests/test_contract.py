from haltwise.contract import apply_mapping, diff_tool, propose_fix, severity, validate_args


def tool(props, required, desc="Refund an order."):
    return {"name": "refund_order", "description": desc,
            "input_schema": {"type": "object", "properties": props, "required": required}}


V1 = tool({"order_id": {"type": "string"}, "amount": {"type": "number"}}, ["order_id", "amount"])
V2 = tool({"orderId": {"type": "string"}, "amount": {"type": "number"}}, ["orderId", "amount"])


def test_no_change():
    assert diff_tool(V1, V1) == []


def test_rename_is_breaking_and_mapped():
    ch = diff_tool(V1, V2)
    assert severity(ch) == "breaking"
    p = propose_fix(V1, V2, ch)
    assert p["mapping"] == {"order_id": "orderId"}
    assert p["confidence"] == "high"


def test_optional_field_is_compatible():
    v = tool({**V1["input_schema"]["properties"], "note": {"type": "string"}}, ["order_id", "amount"])
    assert severity(diff_tool(V1, v)) == "compatible"


def test_type_change_is_breaking():
    v = tool({"order_id": {"type": "integer"}, "amount": {"type": "number"}}, ["order_id", "amount"])
    assert severity(diff_tool(V1, v)) == "breaking"


def test_description_change_is_warning():
    assert severity(diff_tool(V1, tool(V1["input_schema"]["properties"], ["order_id", "amount"], "x"))) == "warning"


def test_removed_tool():
    assert severity(diff_tool(V1, None)) == "breaking"


def test_shim_and_validation():
    args, shimmed = apply_mapping({"order_id": "A1", "amount": 5}, {"order_id": "orderId"})
    assert args == {"orderId": "A1", "amount": 5} and shimmed == ["order_id->orderId"]
    assert validate_args(V2["input_schema"], args) == []
    assert validate_args(V2["input_schema"], {"amount": 5}) == ["missing required parameter 'orderId'"]
