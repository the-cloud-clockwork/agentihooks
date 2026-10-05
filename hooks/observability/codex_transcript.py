import json


def _content(item: dict) -> list[dict]:
    kind = item.get("type")
    if kind == "message":
        return [{"type": "text", "text": block["text"]} for block in item.get("content", []) if "text" in block]
    if kind in ("function_call", "custom_tool_call"):
        arguments = item.get("arguments", item.get("input", ""))
        if kind == "function_call":
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                pass
        return [{"type": "tool_use", "id": item.get("call_id", ""), "name": item.get("name", ""), "input": arguments}]
    if kind in ("function_call_output", "custom_tool_call_output"):
        return [{"type": "tool_result", "tool_use_id": item.get("call_id", ""), "content": item.get("output", "")}]
    return []


def normalize_entries(records: list[dict]) -> list[dict]:
    entries = []
    model = ""
    generation = None
    last_total = None
    for index, record in enumerate(records):
        item = record.get("payload", {})
        if record.get("type") == "turn_context":
            model = item.get("model", model)
        elif record.get("type") == "event_msg" and item.get("type") == "token_count" and generation is not None:
            info = item.get("info") or {}
            usage = info.get("last_token_usage") or {}
            total = info.get("total_token_usage")
            if usage and (total is None or total != last_total):
                cached = usage.get("cached_input_tokens", 0)
                generation["usage"] = {
                    "input_tokens": max(0, usage.get("input_tokens", 0) - cached),
                    "cache_read_input_tokens": cached,
                    "output_tokens": usage.get("output_tokens", 0),
                }
                last_total = total
        elif record.get("type") == "response_item":
            content = _content(item)
            if not content or item.get("role") in ("system", "developer"):
                continue
            role = item.get("role", "user" if item.get("type", "").endswith("_output") else "assistant")
            message = {"role": role, "content": content}
            if role == "assistant":
                message.update(id=item.get("id") or f"codex-{index}", model=model)
                generation = message
            entries.append(
                {"type": role, "uuid": f"codex-{index}", "timestamp": record.get("timestamp", ""), "message": message}
            )
    return entries
