"""Provider 请求的模型消息预算与安全压缩。"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any, NoReturn

RESULT_PATH_MAX = 1_000


def estimated_tokens(value: object) -> int:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            text = str(value)
    ascii_chars = sum(1 for char in text if ord(char) < 128)
    return max(1, (ascii_chars + 3) // 4 + len(text) - ascii_chars)


def conservative_tokens(value: object) -> int:
    """UTF-8 byte proxy, not an exact tokenizer or final wire-size guarantee."""
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str,
    )
    return max(1, len(text.encode("utf-8")))


def decode_tool_content(content: str) -> dict[str, Any]:
    """读取规范 JSON；只在恢复旧历史时迁移旧的多行引用附录。"""
    text = (content or "")
    body, separator, suffix = text.partition("\nopaque_refs=")
    if text.startswith("opaque_refs="):
        body, separator, suffix = "{}", "legacy", text.removeprefix("opaque_refs=")
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        value = {"summary": body}
    payload = value if isinstance(value, dict) else {"data": value}
    if separator:
        for line in ("opaque_refs=" + suffix).splitlines():
            key, _, raw = line.partition("=")
            if key in {"opaque_refs", "reference_arguments", "candidate_numbers", "recommended_ingest_arguments", "recommended_download_arguments"}:
                try:
                    payload[key] = json.loads(raw)
                except ValueError:
                    continue
    return payload


def _invalid_result() -> NoReturn:
    from app.agent.errors import ToolPipelineError

    raise ToolPipelineError("结果续取元数据无效，不能将不完整证据当作完整结果；请重新观察。", code="invalid_tool_result")


def _validate_result_metadata(payload: dict[str, Any], *, result_page: bool) -> None:
    """Validate only trusted envelopes, never infer protocol identity from JSON shape."""
    required = {"path", "kind", "total", "offset", "returned", "has_more", "next_offset"}
    scalars = ("int", "float", "bool", "NoneType")
    kinds = {"dict": "object", "list": "array", "str": "string"}

    def pointer(value: Any) -> bool:
        return (isinstance(value, str) and len(value) <= RESULT_PATH_MAX
                and (not value or value.startswith("/")) and not re.search(r"~(?![01])", value))

    def counts(row: dict[str, Any]) -> bool:
        return (all(type(row.get(key)) is int and row[key] >= 0 for key in ("total", "offset", "returned"))
                and row["returned"] <= max(0, row["total"] - row["offset"]))

    rows = payload.get("truncation", [])
    if (not isinstance(rows, list) or ("truncated" in payload and type(payload["truncated"]) is not bool)
            or bool(rows) != bool(payload.get("truncated"))):
        _invalid_result()
    seen = set()
    for row in rows:
        if (not isinstance(row, dict) or not required <= row.keys() or row.keys() - required - {"scope"}
                or not pointer(row.get("path")) or not counts(row)):
            _invalid_result()
        if row["path"] in seen or row["kind"] not in (*kinds, *kinds.values(), "scalar", *scalars):
            _invalid_result()
        seen.add(row["path"])
        subtree = row.get("scope") == "subtree"
        if ("scope" in row and not subtree or row["has_more"] is not True
                or type(row["next_offset"]) is not int or row["offset"] > row["total"]
                or row["next_offset"] != row["offset"] + (0 if subtree else row["returned"])
                or not subtree and row["next_offset"] >= row["total"]
                or row["kind"] in ("scalar", *scalars) and row["total"] != 1):
            _invalid_result()
    if not result_page or payload.get("ok") is False:
        return
    page = payload.get("data")
    handle = payload.get("result_handle")
    if (not isinstance(handle, str) or not 1 <= len(handle) <= 200 or payload.get("read_tool") != "agent.read_result"
            or not isinstance(page, dict) or page.keys() != required | {"items"}
            or not pointer(page.get("path")) or not counts(page)):
        _invalid_result()
    kind, items = page["kind"], page["items"]
    if kind not in (*kinds, *scalars) or not isinstance(items, str if kind == "str" else list):
        _invalid_result()
    if kind == "dict":
        keys = []
        for item in items:
            if not isinstance(item, dict) or item.keys() != {"key", "value"} or not isinstance(item["key"], str):
                _invalid_result()
            keys.append(item["key"])
        if len(set(keys)) != len(keys):
            _invalid_result()
    end = page["offset"] + len(items)
    if (len(items) != page["returned"] or page["has_more"] is not (end < page["total"])
            or page["next_offset"] != (end if end < page["total"] else None)
            or end < page["total"] and (not items or type(page["next_offset"]) is not int)
            or kind in scalars and (page["total"] != 1 or any(type(item).__name__ != kind for item in items))):
        _invalid_result()
    for row in rows:
        if page["path"] and row["path"] != page["path"] and not row["path"].startswith(page["path"] + "/"):
            _invalid_result()
        if row["path"] == page["path"] and (
            any(row[key] != page[key] for key in ("total", "offset", "returned"))
            or row["kind"] not in (kind, kinds.get(kind, "scalar"))
        ):
            _invalid_result()


def compact_tool_content(
    content: str, *, maximum: int, result_page: bool = False, trusted: bool = False, tool_name: str = "",
) -> str:
    """Budget a source or a canonical projection, retaining original snapshot coordinates.

    Raw tool DTOs/arguments use the default. Only executor-produced projections and
    persisted tool messages use trusted=True; result_page comes from the executor's
    tool_name, not any field inside content. No model-visible magic marker grants trust.
    """
    payload = decode_tool_content(content)
    # 仅恢复旧持久历史中的分页协议；已无对应可执行工具，不按业务字段推断协议身份。
    result_page = result_page or tool_name in ("agent.read_result", "agent__read_result")
    protocol_keys = {"result_handle", "read_tool", "read_method", "truncated", "truncation"}
    if trusted:
        _validate_result_metadata(payload, result_page=result_page)
    elif result_page:
        _invalid_result()
    collisions = {} if trusted else {key: payload[key] for key in protocol_keys & payload.keys()}
    if len(content) <= maximum and not collisions:
        return content
    protected = {"ok", "status", "code", "result_handle", "read_tool", "read_method"}
    prior = {row["path"]: dict(row) for row in payload.get("truncation", [])} if trusted else {}
    omitted = dict(prior)
    # Reader identity comes from the executor/message tool_name, never business fields.
    page = payload.get("data") if result_page and payload.get("ok") is not False else None
    root = page["path"] if page is not None else ""

    def dumps(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    def source_path(path: str) -> str:
        if page is None:
            return path
        tail = path.removeprefix("/data/items")
        if not tail or page["kind"] == "str":
            return root
        index, separator, rest = tail[1:].partition("/")
        if page["kind"] == "dict":
            key = page["items"][int(index)]["key"]
            return root + "/" + key.replace("~", "~0").replace("/", "~1") + rest.removeprefix("value")
        prefix = "/" + str(page["offset"] + int(index)) if page["kind"] == "list" else ""
        return root + prefix + ("/" + rest if separator else "")

    def branches(node: Any, path: str = ""):
        if not isinstance(node, (dict, list)):
            return
        for key, value in (node.items() if isinstance(node, dict) else enumerate(node)):
            if not path and key in protected | {"truncated", "truncation"}:
                continue
            wrapper = page is not None and page["kind"] == "dict" and path.startswith("/data/items/") and path.count("/") == 3
            if wrapper and key == "key":
                continue
            child = path + "/" + str(key).replace("~", "~0").replace("/", "~1")
            # key/value is a display wrapper, not a source object.
            if not (page is not None and page["kind"] == "dict" and path == "/data/items"):
                yield node, key, child, value
            yield from branches(value, child)

    def fields():
        if page is not None:
            yield page, "items", "/data/items", page["items"]
            yield from branches(page["items"], "/data/items")
        else:
            yield from branches(payload)

    def measure(value: Any) -> tuple[str, int]:
        return ("object", len(value)) if isinstance(value, dict) else ("array", len(value)) if isinstance(value, list) else ("string", len(value)) if isinstance(value, str) else ("scalar", 1)

    originals = {source_path(path): measure(value) for _, _, path, value in fields()}
    root_members = payload.keys() - (protocol_keys if trusted else set())
    if trusted and page is None:
        # Before root coverage exists, displaced members remain in child obligations.
        # Union direct keys, not child totals: descendants/visible parents count once.
        # An existing root record below remains authoritative after subtree merging.
        root_members.update(path.split("/", 2)[1].replace("~1", "/").replace("~0", "~")
                            for path in prior if path)
    originals[root] = (page["kind"], page["total"]) if page is not None else ("object", len(root_members))
    originals.update(("/" + key, measure(value)) for key, value in collisions.items())
    for path, row in omitted.items():
        originals[path] = (row["kind"], row["total"])

    def covers(ancestor: str, child: str) -> bool:
        return ancestor == child or not ancestor or child.startswith(ancestor + "/")

    def record(path: str, returned: int, *, subtree: bool = False) -> dict[str, Any]:
        kind, total = originals[path]
        offset = prior.get(path, {}).get("offset", page["offset"] if page is not None and path == root else 0)
        row = {"path": path, "kind": kind, "total": total, "offset": offset,
               "returned": returned, "has_more": True, "next_offset": offset if subtree else offset + returned}
        if subtree:
            row["scope"] = "subtree"
        return row

    # Business protocol-name collisions stay at their original snapshot pointers.
    # Omit them explicitly from the view; do not overwrite the frozen source or wrap data.
    for key in collisions:
        payload.pop(key, None)
        omitted["/" + key] = record("/" + key, 0, subtree=True)

    def fail_budget() -> NoReturn:
        from app.agent.errors import ToolPipelineError

        raise ToolPipelineError("结果的原子键或续取元数据超过上下文预算，无法安全分页；请缩小原查询。", code="context_budget_exceeded")

    def encode() -> str:
        current = {source_path(path): measure(value)[1] for _, _, path, value in fields()}
        current[root] = len(page["items"]) if page is not None else len(payload.keys() - {"result_handle", "read_tool", "read_method", "truncated", "truncation"})
        for path in list(omitted):
            ancestor = path
            while len(ancestor) > RESULT_PATH_MAX:
                ancestor = ancestor.rpartition("/")[0]
            if ancestor != path:
                del omitted[path]
                omitted[ancestor] = record(ancestor, current.get(ancestor, 0), subtree=True)
        # An ancestor plus a descendant is one subtree obligation, never summed counts.
        for path in sorted(list(omitted), key=len):
            if path not in omitted:
                continue
            descendants = [child for child in omitted if child != path and covers(path, child)]
            if descendants or omitted[path].get("scope") == "subtree":
                omitted[path] = record(path, current.get(path, 0), subtree=True)
                for child in descendants:
                    del omitted[child]
        if len(omitted) > 4 or len(dumps(list(omitted.values()))) > max(180, maximum // 3):
            paths = list(omitted)
            ancestor = paths[0]
            while not all(covers(ancestor, path) for path in paths):
                ancestor = ancestor.rpartition("/")[0]
            while ancestor not in originals:
                ancestor = ancestor.rpartition("/")[0]
            omitted.clear()
            omitted[ancestor] = record(ancestor, current.get(ancestor, 0), subtree=True)
        if omitted:
            # Compact JSON alone can fit: truncation denotes loss, not re-encoding.
            payload["truncated"] = True
            payload["truncation"] = list(omitted.values())
        if page is not None:
            count = len(page["items"])
            end = page["offset"] + count
            page.update(returned=count, has_more=end < page["total"], next_offset=end if end < page["total"] else None)
        return dumps(payload)

    encoded = encode()
    while len(encoded) > maximum:
        choices = []
        for parent, key, path, value in fields():
            if isinstance(value, (dict, list, str)) and len(value) > (48 if isinstance(value, str) else 1):
                priority = 1 if isinstance(value, str) else -1 if isinstance(value, dict) else 0
                choices.append((priority, len(dumps(value)), parent, key, path, value))
        minimum_string = 48
        if not choices and page is not None:
            minimum_string = 1
            choices = [(1, len(dumps(value)), parent, key, path, value)
                       for parent, key, path, value in fields() if isinstance(value, str) and len(value) > 1]
        if not choices:
            # Atomic page keys/coordinates cannot be shortened without changing identity.
            if page is not None:
                break
            omitted.clear()
            payload = {key: value for key, value in payload.items() if key in protected}
            omitted[root] = record(root, 0, subtree=True)
            encoded = encode()
            break
        _, _, parent, key, path, value = max(choices, key=lambda item: item[:2])
        source = source_path(path)
        if page is not None and len(source) > RESULT_PATH_MAX:
            fail_budget()
        retained = max(minimum_string if isinstance(value, str) else 1, len(value) // 2)
        replacement = dict(list(value.items())[:retained]) if isinstance(value, dict) else value[:retained]
        if isinstance(parent, list):
            assert isinstance(key, int)
            parent[key] = replacement
        else:
            parent[key] = replacement
        previous = omitted.get(source, {})
        omitted[source] = record(source, retained, subtree=previous.get("scope") == "subtree")
        encoded = encode()
    if len(encoded) > maximum:
        fail_budget()
    _validate_result_metadata(payload, result_page=result_page)
    return encoded


def _compact_chain(messages: Sequence[Any], *, max_tool_chars: int) -> list[Any]:
    result = []
    for message in messages:
        if message.role == "tool":
            if message.tool_name in {"agent.run_code", "agent__run_code"}:
                # 终端事实由输出文件承接，不走业务快照截断；超窗仍按原整组预算处理。
                result.append(message)
                continue
            content = compact_tool_content(
                message.content, maximum=max_tool_chars, trusted=True,
                tool_name=message.tool_name,
            )
            if content != message.content:
                message = replace(message, content=content)
        result.append(message)
    return result


def archive_native_output(
    message: Any, *, output_available: Callable[[str], bool] | None,
    preview_chars: int | None = None,
) -> Any:
    """生成可回读的终端投影；最新未消费输出保留有界首尾，历史仍归档正文。"""
    if (output_available is None or message.role != "tool"
            or message.tool_name not in {"agent.run_code", "agent__run_code"}
            or message.effect_plan_id or message.completion_receipt_id):
        return message
    try:
        payload = json.loads(message.content)
    except (ValueError, TypeError):
        return message
    if (not isinstance(payload, dict) or payload.get("approval")
            or payload.get("status") == "approval_required" or payload.get("truncated")
            or payload.get("truncation")):
        return message
    data = payload.get("data")
    if (not isinstance(data, dict) or type(data.get("exit_code")) is not int
            or type(data.get("timed_out")) is not bool or data.get("working_directory") != "/work"
            or not isinstance(data.get("sdk_calls"), list)
            or any(type(data.get(f"{stream}_truncated")) is not bool for stream in ("stdout", "stderr"))):
        return message
    stdout_path, stderr_path = data.get("stdout_path"), data.get("stderr_path")
    if not isinstance(stdout_path, str) or not isinstance(stderr_path, str):
        return message
    if (not re.fullmatch(r"/output/[0-9a-f]{32}\.stdout", stdout_path)
            or stderr_path != stdout_path.removesuffix(".stdout") + ".stderr"
            or not all(output_available(path) for path in (stdout_path, stderr_path))):
        return message
    if payload.get("output_archived") is True and "stdout" not in data and "stderr" not in data:
        return message
    if not all(isinstance(data.get(stream), str) for stream in ("stdout", "stderr")):
        return message
    if preview_chars is None:
        for stream in ("stdout", "stderr"):
            data.pop(stream)
        payload.pop("result_handle", None)
        payload.pop("read_tool", None)
        payload.pop("read_method", None)
        payload["output_archived"] = True
    else:
        if all(len(data[stream]) <= preview_chars for stream in ("stdout", "stderr")):
            return message
        for stream in ("stdout", "stderr"):
            text = data[stream]
            if len(text) <= preview_chars:
                continue
            gap = f"\n[中间输出已省略；完整输出见 {data[stream + '_path']}]\n"
            head = (preview_chars - len(gap)) // 2
            data[stream] = text[:head] + gap + text[-(preview_chars - len(gap) - head):]
            data[stream + "_truncated"] = True
    payload["details_omitted"] = True
    archived = replace(message, content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return archived if estimated_tokens(archived.to_dict()) < estimated_tokens(message.to_dict()) else message


def _compact_legacy_history(messages: Sequence[Any]) -> list[Any]:
    return [
        replace(message, content=compact_tool_content(message.content, maximum=800, trusted=True))
        if message.role == "tool" and message.tool_name == "agent.capabilities" and len(message.content) > 4_000
        else message for message in messages
    ]


def _native_history(messages: Sequence[Any], *, reserved_ids: set[str] | None = None) -> list[Any]:
    """保留原生工具问答角色；历史调用只是证据，不是重新执行的授权。"""
    result = []
    pending: dict[str, str] = {}
    reserved = set(reserved_ids or ()) | {
        call.call_id for message in messages if message.provider_state is not None
        for call in message.tool_calls
    }
    for index, message in enumerate(messages):
        if message.role == "assistant" and message.tool_calls:
            calls = []
            for position, call in enumerate(message.tool_calls):
                # 旧历史去重ID；有原生私有状态的调用保留供应商关联ID，不能改写签名链。
                identifier = call.call_id
                if message.provider_state is None:
                    identifier = f"history_{index}_{position}"
                    while identifier in reserved:
                        identifier += "_"
                    reserved.add(identifier)
                pending[call.call_id] = identifier
                calls.append(replace(call, call_id=identifier))
            result.append(replace(message, tool_calls=tuple(calls)))
        elif message.role == "tool":
            identifier = pending.pop(message.tool_call_id, "")
            if identifier:
                result.append(replace(message, tool_call_id=identifier))
            else:
                # 仅迁移旧80条截断留下的孤立观察，不能伪造已执行的调用参数。
                result.append(replace(message, role="user", tool_call_id="", tool_name="",
                                      content="历史观察（原调用已缺失，不是当前查询或执行授权）：\n" + message.content))
        else:
            result.append(message)
    return result


def protocol_history_groups(
    messages: Sequence[Any], *, history_end: int,
):
    """Select whole user groups by native identity only, before any budget loss."""
    split_at = max(0, min(history_end, len(messages)))
    history = list(messages[:split_at])
    current = list(messages[split_at:])
    current_counts = Counter(call.call_id for message in current for call in message.tool_calls)
    current_native_ids = {call.call_id for message in current if message.provider_state is not None
                          for call in message.tool_calls}
    if any(current_counts[identifier] > 1 for identifier in current_native_ids):
        from app.agent.errors import ToolPipelineError

        raise ToolPipelineError(
            "模型复用了原生状态关联标识，无法安全续传；已保留真实工具结果，请重新发起查询",
            code="provider_state_conflict",
        )
    groups: list[list[Any]] = []
    for message in history:
        if message.role == "user" or not groups:
            groups.append([])
        groups[-1].append(message)
    # Native state can bind provider call IDs. Do not rewrite those IDs to
    # deduplicate history; omit an entire older user turn on a collision.
    reserved_ids = {call.call_id for message in current for call in message.tool_calls}
    unique_groups: list[list[Any]] = []
    for group in reversed(groups):
        native_ids = {call.call_id for message in group if message.provider_state is not None
                      for call in message.tool_calls}
        counts = Counter(call.call_id for message in group for call in message.tool_calls)
        if native_ids & reserved_ids or any(counts[identifier] > 1 for identifier in native_ids):
            continue
        reserved_ids.update(native_ids)
        unique_groups.append(group)
    return list(reversed(unique_groups)), current, reserved_ids


def bounded_model_messages(
    messages: Sequence[Any],
    *,
    history_end: int,
    consumed_end: int | None = None,
    tool_definitions: Sequence[Mapping[str, Any]],
    system_prompt: str,
    context_window_tokens: int,
    output_tokens: int,
    conservative: bool = False,
    output_available: Callable[[str], bool] | None = None,
) -> tuple[Any, ...]:
    """保留最新完整回合，裁剪旧历史，并硬性阻止超窗口 Provider 请求。"""
    groups, current, reserved_ids = protocol_history_groups(
        messages, history_end=history_end,
    )
    history = _native_history(
        [message for group in groups for message in group], reserved_ids=reserved_ids)
    # 先统一协议身份；后续只改变正文视图或淘汰整组，不重排原生签名链。
    offset = 0
    for index, group in enumerate(groups):
        groups[index] = history[offset:offset + len(group)]
        offset += len(group)
    estimate = conservative_tokens if conservative else estimated_tokens
    message_budget = max(
        0,
        context_window_tokens
        - estimate(system_prompt)
        - estimate(tool_definitions)
        - output_tokens
        - 512,
    )

    def cost(items: Sequence[Any]) -> int:
        return sum(8 + estimate(item.to_dict()) for item in items)

    total_cost = cost(history) + cost(current)
    if total_cost <= message_budget:
        # 已结束回合的大段终端输出不随每次追问重复发送。只替换确有完整
        # 工作区文件的正文；原话、终答、小结果和当前回合仍全文保留。
        history = [
            archive_native_output(message, output_available=output_available)
            if len(message.content) > 16_384 else message
            for message in history
        ]
        return tuple(history + current)

    # 仅超窗才按时间顺序归档已消费结果；最新批次先保留正文。
    split_at = max(0, min(history_end, len(messages)))
    consumed = split_at if consumed_end is None else max(split_at, min(consumed_end, len(messages)))
    for group in (*groups, current):
        eligible = consumed - split_at if group is current else len(group)
        for index in range(eligible):
            original = group[index]
            archived = archive_native_output(original, output_available=output_available)
            saving = cost([original]) - cost([archived])
            if saving <= 0:
                continue
            group[index] = archived
            total_cost -= saving
            if total_cost <= message_budget:
                return tuple([message for history_group in groups for message in history_group] + current)

    groups = [_compact_legacy_history(group) for group in groups]
    current_cost = cost(current)
    for maximum in (1_200, 400):
        if current_cost <= message_budget:
            break
        current = _compact_chain(current, max_tool_chars=maximum)
        current_cost = cost(current)
    # 当前链本身仍超窗时，给最新终端结果保留首尾及完整路径，允许下一轮读文件恢复。
    # 复用同一文件/回执校验；原消息与历史归档策略保持不变，文件不可用时不丢正文。
    originals = current
    for maximum in (2_048, 512):
        if current_cost <= message_budget:
            break
        current = [
            archive_native_output(message, output_available=output_available, preview_chars=maximum)
            if index >= consumed - split_at else message
            for index, message in enumerate(originals)
        ]
        current_cost = cost(current)
    if current_cost > message_budget:
        from app.agent.errors import ToolPipelineError

        raise ToolPipelineError(
            "当前工具链超过模型上下文上限，请缩小查询范围后重试",
            code="context_budget_exceeded",
        )
    remaining = max(0, message_budget - current_cost)
    history_view = [message for group in groups for message in group]
    if cost(history_view) <= remaining:
        return tuple(history_view + current)

    kept: list[list[Any]] = []
    for group in reversed(groups):
        candidate, candidate_cost = group, cost(group)
        for maximum in (1_200, 400):
            if candidate_cost <= remaining or kept:
                break
            from app.agent.errors import ToolPipelineError

            try:
                candidate = _compact_chain(group, max_tool_chars=maximum)
            except ToolPipelineError as exc:
                if exc.code != "context_budget_exceeded":
                    raise
                break  # 不可压缩的旧整组留在存储中，不阻塞当前请求。
            candidate_cost = cost(candidate)
        if candidate_cost > remaining:
            # 历史整组超出窗口时停止装载，不让旧调用锁死新问题。
            # Session 的历史索引保留遗漏标记；历史本身不授予写操作权限。
            break
        kept.append(candidate)
        remaining -= candidate_cost
    return tuple([message for group in reversed(kept) for message in group] + current)
