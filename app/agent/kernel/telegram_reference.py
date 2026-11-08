"""Selected Telegram text: one state row, scoped pointer, untrusted projection."""
from __future__ import annotations

import hashlib
import html
import json
import secrets
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .references import ReferenceError

SOURCE_KEYS = ('kind', 'chat_id', 'message_id', 'thread_id', 'incoming_message_id', 'incoming_thread_id')
CONTEXT_KEYS = ('text', 'source', 'row_id', 'version', 'created_generation', 'reference_handle')


def context_copy(reply):
    return {key: reply[key] for key in CONTEXT_KEYS if key in reply}


def version(text, source):
    return hashlib.sha256(json.dumps([text, source], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


async def prepare(reply, lease, store):
    text = str(reply.get('text') or '')
    source = reply.get('source')
    if not isinstance(source, Mapping):
        return {'text': text}
    source = {key: source.get(key) for key in SOURCE_KEYS}
    pointer = {'source': source, 'row_id': secrets.token_urlsafe(18),
               'version': version(text, source), 'created_generation': lease.generation}
    handle = await store.put(owner=lease.owner, session_id=lease.session_id,
                             kind='telegram_reference', value=pointer, ttl_seconds=900)
    return {**pointer, 'text': text, 'reference_handle': handle.ref}


def preview(reply, *, unavailable=False):
    text = str(reply.get('text') or '')
    if not reply.get('source'):
        return html.escape(text[:2000], quote=False) + ('\n[truncated: 引用无可恢复来源]' if len(text) > 2000 else '')
    data: dict[str, Any] = {'untrusted_reference': True, 'text': text[:2000], 'truncated': len(text) > 2000}
    if unavailable:
        data.update(source=reply['source'], reference_unavailable=True)
    elif reply.get('reference_handle'):
        data.update(source=reply['source'], reference_handle=reply['reference_handle'],
                    read_method='session.result', path='/text', total=len(text))
    # Text cannot close the enclosing data tag or inject a forged sibling tag.
    return json.dumps(data, ensure_ascii=False).replace('<', '\\u003c').replace('>', '\\u003e')


async def project_history(messages, replies, *, current_handle, **scope):
    """Downgrade only retained Telegram projections, never durable/native history."""
    projected = list(messages)
    for reply in replies:
        handle = reply.get('reference_handle')
        if not handle or handle == current_handle:
            continue
        original = preview(reply)
        # 已持久化的旧预览仍可降级；不恢复旧工具入口。
        originals = (original, original.replace('"read_method": "session.result"', '"read_tool": "agent.read_result"'))
        users = {i: value for i, message in enumerate(projected) for value in originals
                 if message.role == 'user' and value in message.content}
        pages = {}
        for i, message in enumerate(projected):
            if message.role != 'tool' or message.tool_name != 'agent.read_result':
                continue
            try:
                page = json.loads(message.content)
            except (ValueError, TypeError):
                continue
            if (isinstance(page, dict) and page.get('untrusted_reference') is True
                    and page.get('result_handle') == handle and page.get('source') == reply.get('source')):
                pages[i] = page
        if not users and not pages:
            continue
        try:
            await resolve(handle, **scope)
        except ReferenceError:
            for i, value in users.items():
                projected[i] = replace(projected[i], content=projected[i].content.replace(value, preview(reply, unavailable=True)))
            for i, page in pages.items():
                page.pop('result_handle', None)
                page.pop('read_tool', None)
                page['reference_unavailable'] = True
                projected[i] = replace(projected[i], content=json.dumps(page, ensure_ascii=False, separators=(',', ':')))
    return tuple(projected)


async def resolve(handle, *, owner, session_id, reference_store, state_store):
    pointer = await reference_store.resolve(handle, owner=owner, session_id=session_id,
                                            expected_kind='telegram_reference')
    if not isinstance(pointer, dict) or set(pointer) != {'source', 'row_id', 'version', 'created_generation'}:
        raise ReferenceError('invalid reference pointer')
    state = await state_store.load(owner=owner, session_id=session_id)
    rows = [row.get('reply_context') for row in state.conversation if row.get('role') == 'user'
            and isinstance(row.get('reply_context'), Mapping)
            and row['reply_context'].get('row_id') == pointer['row_id']]
    if len(rows) != 1:
        raise ReferenceError('reference source unavailable')
    reply = rows[0]
    if (any(reply.get(key) != value for key, value in pointer.items())
            or reply.get('reference_handle') != handle
            or version(reply.get('text'), reply.get('source')) != pointer['version']):
        raise ReferenceError('reference source mismatch')
    return reply
