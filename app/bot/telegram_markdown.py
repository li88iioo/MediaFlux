"""将 Agent 的 Markdown 安全投影为 Telegram 支持的 HTML。

模型输出不能直接交给 Telegram 的 MarkdownV2：不完整的流式片段、路径中的
反斜杠和业务文本里的标点很容易导致整条消息解析失败。本模块只生成 Telegram
明确支持的少量 HTML 标签，所有模型原文先转义，再按块级/行内语法恢复样式。
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

_MAX_INLINE_DEPTH = 4
_ESCAPABLE = frozenset(r"\`*{}[]()#+-.!_|>~")
_FENCE_RE = re.compile(r"^\s{0,3}(`{3,}|~{3,})([^`]*)$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_UNORDERED_RE = re.compile(r"^(?P<indent>\s{0,8})[-+*]\s+(?P<body>.+)$")
_ORDERED_RE = re.compile(r"^(?P<indent>\s{0,8})(?P<number>\d+)[.)]\s+(?P<body>.+)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_TABLE_SEPARATOR_RE = re.compile(r"^:?-{3,}:?$")
_THEMATIC_RE = re.compile(
    r"^\s{0,3}(?:(?:\*\s*){3,}|(?:-\s*){3,}|(?:_\s*){3,})$"
)


def _escape(value: object, *, quote: bool = False) -> str:
    return html.escape(str(value or ""), quote=quote)


def _safe_link(raw_url: str) -> str | None:
    value = html.unescape(str(raw_url or "")).strip().strip("<>")
    if not value or any(character in value for character in ("\x00", "\r", "\n")):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    scheme = parsed.scheme.casefold()
    if scheme in {"http", "https"} and parsed.netloc:
        return value
    if scheme == "mailto" and parsed.path:
        return value
    return None


def _markdown_link_at(source: str, start: int) -> tuple[bool, str, str, int] | None:
    image = source.startswith("![", start)
    if not image and not source.startswith("[", start):
        return None
    label_start = start + (2 if image else 1)
    label_end = source.find("](", label_start)
    if label_end < 0 or "\n" in source[label_start:label_end]:
        return None

    cursor = label_end + 2
    nesting = 0
    escaped = False
    while cursor < len(source):
        character = source[cursor]
        if escaped:
            escaped = False
        elif character == "\\":
            escaped = True
        elif character == "(":
            nesting += 1
        elif character == ")":
            if nesting == 0:
                break
            nesting -= 1
        cursor += 1
    if cursor >= len(source):
        return None

    destination = source[label_end + 2 : cursor].strip()
    # Markdown 允许链接后附标题；Telegram 只需要真正的目标地址。
    match = re.match(r'^(?:<([^>]+)>|([^\s]+))(?:\s+["\'].*["\'])?$', destination)
    if match is None:
        return None
    return (
        image,
        source[label_start:label_end],
        match.group(1) or match.group(2) or "",
        cursor + 1,
    )


def _marker_end(source: str, marker: str, start: int) -> int:
    cursor = start
    while True:
        cursor = source.find(marker, cursor)
        if cursor < 0:
            return -1
        backslashes = 0
        check = cursor - 1
        while check >= 0 and source[check] == "\\":
            backslashes += 1
            check -= 1
        if backslashes % 2 == 0:
            return cursor
        cursor += len(marker)


def _render_inline(source: str, *, depth: int = 0, allow_links: bool = True) -> str:
    if not source:
        return ""
    if depth > _MAX_INLINE_DEPTH:
        return _escape(source)

    output: list[str] = []
    index = 0
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""

        if character == "\\" and following in _ESCAPABLE:
            output.append(_escape(following))
            index += 2
            continue

        if character == "`":
            marker_length = 1
            while (
                index + marker_length < len(source)
                and source[index + marker_length] == "`"
            ):
                marker_length += 1
            marker = "`" * marker_length
            end = _marker_end(source, marker, index + marker_length)
            if end >= 0:
                content = source[index + marker_length : end]
                if content.startswith(" ") and content.endswith(" ") and content.strip():
                    content = content[1:-1]
                output.append(f"<code>{_escape(content)}</code>")
                index = end + marker_length
                continue

        if allow_links and (character == "[" or source.startswith("![", index)):
            link = _markdown_link_at(source, index)
            if link is not None:
                image, label, raw_url, end = link
                safe_url = _safe_link(raw_url)
                label_html = _render_inline(label, depth=depth + 1, allow_links=False)
                if image:
                    label_html = f"🖼 {label_html or '图片'}"
                if safe_url is None:
                    output.append(label_html)
                else:
                    output.append(
                        f'<a href="{_escape(safe_url, quote=True)}">{label_html}</a>'
                    )
                index = end
                continue

        marker = ""
        if source.startswith("**", index):
            marker = "**"
        elif source.startswith("__", index):
            marker = "__"
        if marker:
            previous = source[index - 1] if index else ""
            can_open = bool(following) and not following.isspace()
            if marker == "__" and previous.isalnum():
                can_open = False
            end = _marker_end(source, marker, index + 2) if can_open else -1
            if end > index + 2:
                output.append(
                    "<b>"
                    + _render_inline(source[index + 2 : end], depth=depth + 1)
                    + "</b>"
                )
                index = end + 2
                continue

        if source.startswith("~~", index):
            end = _marker_end(source, "~~", index + 2)
            if end > index + 2:
                output.append(
                    "<s>"
                    + _render_inline(source[index + 2 : end], depth=depth + 1)
                    + "</s>"
                )
                index = end + 2
                continue

        if character in {"*", "_"}:
            previous = source[index - 1] if index else ""
            can_open = bool(following) and not following.isspace()
            if character == "_" and previous.isalnum():
                can_open = False
            end = _marker_end(source, character, index + 1) if can_open else -1
            if end > index + 1:
                output.append(
                    "<i>"
                    + _render_inline(source[index + 1 : end], depth=depth + 1)
                    + "</i>"
                )
                index = end + 1
                continue

        if character == "<":
            match = re.match(r"<(https?://[^>]+|mailto:[^>]+)>", source[index:], re.IGNORECASE)
            if match is not None:
                safe_url = _safe_link(match.group(1))
                if safe_url is not None:
                    escaped_url = _escape(safe_url, quote=True)
                    output.append(f'<a href="{escaped_url}">{_escape(safe_url)}</a>')
                    index += len(match.group(0))
                    continue

        # 未匹配的 Markdown 标记和原始 HTML 都作为普通文本显示。
        output.append(_escape(character))
        index += 1

    return "".join(output)


def _split_table_row(value: str) -> list[str]:
    line = value.strip().removeprefix("|").removesuffix("|")
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for character in line:
        if escaped:
            current.append(character)
            escaped = False
            continue
        if character == "\\":
            escaped = True
            current.append(character)
            continue
        if character == "`":
            in_code = not in_code
        if character == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
    cells.append("".join(current).strip())
    return cells


def _table_at(lines: list[str], index: int) -> tuple[list[str], int] | None:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return None
    header = _split_table_row(lines[index])
    separators = _split_table_row(lines[index + 1])
    if (
        len(header) < 2
        or len(header) != len(separators)
        or not all(
            _TABLE_SEPARATOR_RE.fullmatch(cell.replace(" ", ""))
            for cell in separators
        )
    ):
        return None

    rows = [header]
    cursor = index + 2
    while cursor < len(lines) and lines[cursor].strip() and "|" in lines[cursor]:
        cells = _split_table_row(lines[cursor])
        if len(cells) != len(header):
            break
        rows.append(cells)
        cursor += 1

    rendered = ["<b>" + " · ".join(_render_inline(cell) for cell in rows[0]) + "</b>"]
    rendered.extend(" · ".join(_render_inline(cell) for cell in row) for row in rows[1:])
    return rendered, cursor


def render_telegram_markdown(value: object) -> str:
    """返回可直接配合 Telegram ``parse_mode=HTML`` 使用的安全文本。

    该函数也接受尚未闭合的流式 Markdown；未闭合标记按普通文本显示，因此每次
    草稿更新都会得到标签平衡、可被 Telegram 接受的 HTML。
    """

    source = str(value or "").replace("\x00", "")
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    output: list[str] = []
    index = 0

    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if not stripped:
            if output and output[-1] != "":
                output.append("")
            index += 1
            continue

        fence = _FENCE_RE.match(raw)
        if fence is not None:
            marker = fence.group(1)
            body: list[str] = []
            index += 1
            while index < len(lines) and not lines[index].lstrip().startswith(marker):
                body.append(lines[index])
                index += 1
            if index < len(lines):
                index += 1
            output.append(f"<pre><code>{_escape(chr(10).join(body))}</code></pre>")
            continue

        table = _table_at(lines, index)
        if table is not None:
            rendered_rows, index = table
            output.extend(rendered_rows)
            continue

        heading = _HEADING_RE.match(raw)
        if heading is not None:
            output.append(f"<b>{_render_inline(heading.group(2))}</b>")
            index += 1
            continue

        if index + 1 < len(lines) and re.fullmatch(r"\s*(?:={3,}|-{3,})\s*", lines[index + 1]):
            output.append(f"<b>{_render_inline(stripped)}</b>")
            index += 2
            continue

        if _THEMATIC_RE.fullmatch(raw):
            output.append("────────")
            index += 1
            continue

        quote = _QUOTE_RE.match(raw)
        if quote is not None:
            quoted: list[str] = []
            while index < len(lines):
                current = _QUOTE_RE.match(lines[index])
                if current is None:
                    break
                quoted.append(_render_inline(current.group(1)))
                index += 1
            output.append("<blockquote>" + "\n".join(quoted) + "</blockquote>")
            continue

        unordered = _UNORDERED_RE.match(raw)
        if unordered is not None:
            body = unordered.group("body")
            task = re.match(r"^\[([ xX])\]\s+(.*)$", body)
            prefix = "•"
            if task is not None:
                prefix = "☑" if task.group(1).casefold() == "x" else "☐"
                body = task.group(2)
            indent = "　" * min(3, len(unordered.group("indent")) // 2)
            output.append(f"{indent}{prefix} {_render_inline(body)}")
            index += 1
            continue

        ordered = _ORDERED_RE.match(raw)
        if ordered is not None:
            indent = "　" * min(3, len(ordered.group("indent")) // 2)
            output.append(
                f"{indent}{ordered.group('number')}. {_render_inline(ordered.group('body'))}"
            )
            index += 1
            continue

        output.append(_render_inline(raw.rstrip()))
        index += 1

    while output and output[-1] == "":
        output.pop()
    return "\n".join(output)
