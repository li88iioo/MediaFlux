"""下载隔离目录收尾：调用方必须持有现有guangya-organize跨进程写锁。

不读取过滤后的Scoped视图，不移除全局保护；先验证整棵自有空树，再逐级
回收。写意图持久化为failed，只有确认成功才改completed；不确定写入不得重放。
"""
from __future__ import annotations

from typing import Any

from app import database as db
from app.repositories import download_requests as download_requests_repo
from app.modules.organize_delete_audit import (
    DeleteCandidate,
    execute_recycle_bin_delete,
)
from app.repositories.download_staging import (
    is_current_staging_confirmation,
    list_staging_cleanup_requests,
    same_staging_identity,
    staging_confirmation_summary,
    unresolved_staging_confirmations,
    update_staging_cleanup,
)


class _RetainDirectory(RuntimeError):
    def __init__(self, kind: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.provider_write_not_started = True


def cleanup_report() -> dict[str, Any]:
    return {
        'cleaned': 0, 'scanned': 0, 'candidates': 0, 'protected': 0,
        'not_empty': 0, 'unavailable': 0, 'unsupported': 0,
        'scan_failures': 0, 'delete_failures': 0, 'reasons': [],
        '_cleaned_roots': [], '_retained_roots': [], '_retryable': False, '_blocked': False,
    }


def _reason(report: dict, kind: str, message: str) -> None:
    report[kind] = int(report.get(kind) or 0) + 1
    if message not in report['reasons'] and len(report['reasons']) < 8:
        report['reasons'].append(message)


def _eligible(row: dict, *, allow_running: bool = False, owned: bool = False) -> bool:
    if (
        int(row.get('gy_isolated') or 0) != 1
        or row.get('gy_status') != 'completed'
        or row.get('status') not in ('completed', 'submitted', 'downloading', 'manual_review')
        or row.get('targets') not in ('guangya', 'both')
        or str(row.get('organize_status') or '') in ('resubmitted', 'cleared', 'failed', 'stopped')
        or row.get('attention_cleared_at')
    ):
        return False
    phases = ('completed', 'requires_manual', 'running', 'starting', 'queued') if allow_running else ('completed',)
    if row.get('organize_status') not in phases or int(row.get('organize_started') or 0) < 0:
        return False
    allowed = ('failed',) if owned else ('', 'pending', 'retained', 'skipped')
    return str(row.get('gy_staging_cleanup_status') or '') in allowed


def _version(info) -> tuple[str, int]:
    etag = str(getattr(info, 'etag', '') or '')
    try:
        updated_at = max(0, int(getattr(info, 'updated_at', 0) or 0))
    except (TypeError, ValueError):
        updated_at = 0
    if not etag and not updated_at:
        raise _RetainDirectory('unavailable', '目录缺少有效版本信息，已保留')
    return etag, updated_at


def _identity(client, id_: str, parent: str, name: str):
    current = client.file_info(id_)
    if (
        current is None or getattr(current, 'is_dir', None) is not True
        or str(getattr(current, 'file_id', '') or '') != id_
        or str(getattr(current, 'parent_id', '') or '') != parent
        or str(getattr(current, 'name', '') or '') != name
    ):
        raise _RetainDirectory('unavailable', '目录身份与原请求记录不一致，已保留')
    members = client.list_dir(parent)
    matches = [item for item in members if str(getattr(item, 'file_id', '') or '') == id_]
    if (len(matches) != 1 or getattr(matches[0], 'is_dir', None) is not True
            or str(getattr(matches[0], 'name', '') or '') != name):
        # 回收站详情可能仍可读取，父成员验证避免把移走/已回收的ID再次delete。
        raise _RetainDirectory('unavailable', '目录已不在原父目录中，未重放清理操作')
    _version(current)
    return current


def _empty_tree(client, row: dict, protected_ids: set[str], report: dict) -> list[tuple[str, str, str]]:
    nodes: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    entries = 0

    def scan(id_: str, parent: str, name: str, depth: int) -> None:
        nonlocal entries
        if id_ in seen or depth > 64 or len(seen) >= 1000:
            raise _RetainDirectory('scan_failures', '目录扫描出现循环或超过安全上限，未执行清理')
        if id_ in protected_ids:
            raise _RetainDirectory('protected', '目录属于永久来源、归档根或其他下载范围，已保留')
        seen.add(id_)
        _identity(client, id_, parent, name)
        files = client.list_dir(id_)
        report['scanned'] += 1
        entries += len(files)
        if entries > 10000:
            raise _RetainDirectory('scan_failures', '目录条目超过安全上限，未执行清理')
        if any(getattr(item, 'is_dir', None) is not True for item in files):
            raise _RetainDirectory('not_empty', '下载隔离目录仍有媒体、伴随或其他文件，已保留')
        for item in files:
            child_id = str(getattr(item, 'file_id', '') or '').strip()
            child_name = str(getattr(item, 'name', '') or '')
            if not child_id or not child_name:
                raise _RetainDirectory('scan_failures', '目录扫描信息不完整，未执行清理')
            scan(child_id, id_, child_name, depth + 1)
        nodes.append((id_, parent, name))

    scan(str(row['gy_target_dir']), str(row['gy_staging_parent_dir']), str(row['gy_staging_name']), 0)
    report['candidates'] += len(nodes)
    return nodes


def _verify_selected_scope(client, root_id: str, scope_ids: frozenset[str] | None) -> None:
    """复核原选来源仍覆盖该隔离根；直属parent不变不代表仍在所选子树内。"""
    if scope_ids is None:
        return  # 自动收尾以请求持久化的隔离根/直属parent为授权范围。
    current = root_id
    seen: set[str] = set()
    while current:
        if current == '0':
            if current in scope_ids:
                return
            break
        if current in seen or len(seen) >= 64:
            raise _RetainDirectory('unavailable', '所选来源的目录父链异常，已停止清理')
        seen.add(current)
        info = client.file_info(current)
        if info is None or getattr(info, 'is_dir', None) is not True:
            break
        if current in scope_ids:
            return
        parent = str(getattr(info, 'parent_id', '') or '0')
        matches = [item for item in client.list_dir(parent)
                   if str(getattr(item, 'file_id', '') or '') == current]
        if (len(matches) != 1 or getattr(matches[0], 'is_dir', None) is not True
                or str(getattr(matches[0], 'name', '') or '') != str(getattr(info, 'name', '') or '')):
            break
        current = parent
    raise _RetainDirectory('unavailable', '下载目录已移出本次选择的来源范围，未执行清理')


def _current_permanent_roots(snapshot: frozenset[str]) -> set[str]:
    """配置保存不持整理锁；每次写前重读当前永久根，与原快照取并集。"""
    from app import config
    from app.modules.organize_sources import normalize_organize_sources

    try:
        sources, error = normalize_organize_sources(config.get('GY_ORGANIZE_SOURCE_DIRS', ''))
        target = config.get('GY_ORGANIZE_TARGET_DIR', '0')
    except Exception as exc:
        raise _RetainDirectory('protected', '当前永久来源或归档配置无法读取，已停止清理') from exc
    if error or not isinstance(target, (str, int)) or isinstance(target, bool):
        raise _RetainDirectory('protected', '当前永久来源或归档配置无效，已停止清理')
    return set(snapshot) | {'0', (target or '0').strip()} | {item['id'] for item in sources}


def cleanup_download_staging(
    client, request_ids: list[int], *, source_ids: set[str], protected_ids: set[str],
    enabled: bool = True, allow_running: bool = False,
    scope_ids: set[str] | None = None,
    expected_identities: dict[int, dict] | None = None,
) -> dict[str, Any]:
    """只清理明确请求与来源根的交集；不从名称/时间猜测请求归属。"""
    report = cleanup_report()
    original_protected = frozenset(value for value in protected_ids)
    selected_scope = frozenset(value for value in scope_ids) if scope_ids is not None else None
    for request_id in dict.fromkeys(value for value in request_ids):
        stored = download_requests_repo.get_download_request(request_id)
        if stored is None:
            continue
        row = dict(stored)
        root_id = str(row.get('gy_target_dir') or '').strip()
        if not root_id or root_id not in source_ids or row.get('gy_staging_cleanup_status') == 'completed':
            continue
        report['_retained_roots'].append(root_id)
        authority = expected_identities.get(request_id) if expected_identities is not None else None
        if expected_identities is not None:
            if not isinstance(authority, dict) or not same_staging_identity(row, authority):
                report['_blocked'] = True
                _reason(report, 'protected', '请求已偏离持久收尾身份，不能将新绑定作为删除授权')
                continue
            row['_confirmation_id'] = authority.get('_confirmation_id')
            if row['_confirmation_id'] and not is_current_staging_confirmation(row['_confirmation_id'], root_id):
                report['_blocked'] = True
                _reason(report, 'protected', '原确认已被替代或失效，停止自动收尾')
                continue
        if not _eligible(row, allow_running=allow_running):
            _reason(report, 'protected', '下载仍活动、整理尚未收口或上次清理结果待核验，已保留')
            continue
        if root_id in original_protected or root_id == '0':
            _reason(report, 'protected', '永久来源或归档根目录不能作为下载隔离目录删除')
            continue
        write_in_flight = False
        try:
            live_protected = _current_permanent_roots(original_protected)
            if root_id in live_protected:
                raise _RetainDirectory('protected', '隔离根已成为永久来源或归档根，已停止清理')
            owners = list_staging_cleanup_requests(source_id=root_id)
            if len(owners) != 1 or int(owners[0]['id']) != request_id:
                raise _RetainDirectory('protected', '隔离目录归属不唯一，已保留')
            if unresolved_staging_confirmations(root_id):
                raise _RetainDirectory('protected', '下载目录仍有待确认或失败的整理事项，已保留')
            if not enabled:
                summary = staging_confirmation_summary(root_id)
                # 只有全部成功确认可消除历史假告警；已查到的真实残留不能改成策略跳过。
                if summary['total'] and summary['completed'] == summary['total']:
                    message = '按策略保留隔离目录（clean_empty 已关闭）；未检查或删除目录'
                    if update_staging_cleanup(row, status='skipped', error=message):
                        _reason(report, 'policy_retained', message)
                    else:
                        report['_blocked'] = True
                        _reason(report, 'protected', '存在尚未复核的清理告警或未知删除，关闭策略不覆盖原状态')
                continue
            delete_empty = getattr(client, 'delete_empty_directory', None)
            capability = getattr(client, 'supports_guarded_empty_directory_delete', None)
            if capability is None:
                capability = getattr(client, 'supports_atomic_empty_directory_delete', None)
            if not callable(delete_empty) or capability is not True:
                raise _RetainDirectory('unsupported', '云盘接口不支持安全空目录清理')
            other_staging = set(download_requests_repo.list_protected_guangya_staging_ids()) - {root_id}
            _verify_selected_scope(client, root_id, selected_scope)
            nodes = _empty_tree(client, row, live_protected | other_staging, report)
            # 即使下载行被别处改回 retained，未知审计也不能被后台重放。
            with db.get_conn() as conn:
                unknown = conn.execute(
                    "SELECT 1 FROM organize_delete_audit WHERE trigger='download_staging_cleanup' "
                    "AND status IN ('pending','failed') AND file_id IN (" + ','.join('?' for _ in nodes) + ') LIMIT 1',
                    [node[0] for node in nodes],
                ).fetchone()
            if unknown:
                raise _RetainDirectory('protected', '存在结果未知的目录删除审计，停止自动收尾')
            latest = download_requests_repo.get_download_request(request_id)
            if (
                latest is None or not same_staging_identity(dict(latest), row)
                or not _eligible(dict(latest), allow_running=allow_running)
                or unresolved_staging_confirmations(root_id)
            ):
                raise _RetainDirectory('unavailable', '下载请求或待确认状态已变化，未执行清理')
            directory_chain = {id_: (parent, name) for id_, parent, name in nodes}

            def validate_write(current_id, *, expected_version=None, chain=directory_chain,
                               expected_row=row, current_request_id=request_id, current_root=root_id):
                if expected_row.get('_confirmation_id') and not is_current_staging_confirmation(
                    expected_row['_confirmation_id'], current_root,
                ):
                    raise _RetainDirectory('protected', '原确认在清理准备期间被替代或失效，已停止')
                _verify_selected_scope(client, current_root, selected_scope)
                ancestor = current_id
                chain_seen: set[str] = set()
                while ancestor in chain:
                    if ancestor in chain_seen:
                        raise _RetainDirectory('unavailable', '目录父链出现循环，已停止清理')
                    chain_seen.add(ancestor)
                    ancestor_parent, ancestor_name = chain[ancestor]
                    _identity(client, ancestor, ancestor_parent, ancestor_name)
                    ancestor = ancestor_parent
                owners = list_staging_cleanup_requests(source_id=current_root)
                if len(owners) != 1 or int(owners[0]['id']) != current_request_id:
                    raise _RetainDirectory('protected', '隔离目录归属在清理准备时变为不唯一，已停止')
                latest = download_requests_repo.get_download_request(current_request_id)
                if (
                    latest is None or not same_staging_identity(dict(latest), expected_row)
                    or not _eligible(dict(latest), allow_running=allow_running,
                                     owned=expected_row.get('gy_staging_cleanup_status') == 'failed')
                    or unresolved_staging_confirmations(current_root)
                    or current_id in (set(download_requests_repo.list_protected_guangya_staging_ids()) - {current_root})
                ):
                    raise _RetainDirectory('unavailable', '下载请求或待确认状态已变化，已停止后续清理')
                parent, name = chain[current_id]
                current = _identity(client, current_id, parent, name)
                if client.list_dir(current_id):
                    raise _RetainDirectory('not_empty', '目录出现新文件，已停止后续清理')
                version = _version(current)
                if expected_version is not None and version != expected_version:
                    raise _RetainDirectory('unavailable', '目录版本在清理准备期间已变化，未执行删除')
                # 这是最终 provider 前的配置检查；只检查本次自有树内祖先，
                # 不能把树外的永久来源父目录误当成禁止删除其下载子目录。
                current_protected = _current_permanent_roots(original_protected)
                ancestor = current_id
                while ancestor in chain:
                    if ancestor in current_protected:
                        raise _RetainDirectory('protected', '目录或其自有祖先已成为永久来源或归档根，已停止')
                    ancestor = chain[ancestor][0]
                return version

            for id_, parent, name in nodes:
                etag, updated_at = validate_write(id_)
                # 每个节点单独进入不可重放写边界；前序 success 审计不是当前节点未知写。
                if not update_staging_cleanup(row, status='failed', error='清理写入已开始，结果未确认前禁止重复删除'):
                    raise _RetainDirectory('unavailable', '下载目录状态已变化，未执行清理')
                row['gy_staging_cleanup_status'] = 'failed'

                def delete_current(current_id=id_, current_etag=etag, current_time=updated_at,
                                   operation=delete_empty, validate=validate_write):
                    nonlocal write_in_flight
                    # 审计pending落盘也可能耗时；在真正provider调用前再次检查全部权限/状态。
                    try:
                        validate(current_id, expected_version=(current_etag, current_time))
                    except _RetainDirectory:
                        raise
                    except Exception as exc:
                        # provider 尚未调用：把只读错误准确记录为 blocked，而不是 unknown delete。
                        raise _RetainDirectory(
                            'scan_failures', '删除前读取失败，尚未调用云盘删除，将退避复核', retryable=True,
                        ) from exc
                    write_in_flight = True
                    result = operation(current_id, expected_etag=current_etag, expected_updated_at=current_time)
                    if result is False:
                        raise RuntimeError('云盘未确认空目录回收结果')
                    return result

                execute_recycle_bin_delete(
                    client, trigger='download_staging_cleanup',
                    reason='下载整理完成后清理已核验的自有空隔离目录',
                    candidate=DeleteCandidate(file_id=id_, name=name, parent_id=parent),
                    safe_failure_message='目录回收结果未确认，已停止自动重试，请先核对云盘',
                    delete_operation=delete_current,
                )
                # 只有 provider + success 审计均返回成功，才能结束当前未知写区间。
                write_in_flight = False
                report['cleaned'] += 1
                if id_ != root_id:
                    checkpoint = '已核验回收部分空目录，剩余目录待继续复核'
                    if not update_staging_cleanup(row, status='retained', error=checkpoint):
                        raise _RetainDirectory('unavailable', '前序目录已回收但请求发生变化，已停止后续清理')
                    row['gy_staging_cleanup_status'] = 'retained'
                    row['gy_staging_cleanup_error'] = checkpoint
            if not update_staging_cleanup(row, status='completed'):
                raise _RetainDirectory('unavailable', '目录已回收但请求记录发生变化，未覆盖新状态')
            report['_cleaned_roots'].append(root_id)
            report['_retained_roots'].remove(root_id)
        except _RetainDirectory as exc:
            _reason(report, exc.kind, str(exc))
            report['_retryable'] = exc.retryable and not write_in_flight
            report['_blocked'] = write_in_flight or (not exc.retryable and exc.kind != 'not_empty')
            if not write_in_flight:
                update_staging_cleanup(row, status='retained', error=str(exc))
        except Exception:  # noqa: BLE001 - SDK写入边界：任何不明结果都必须保留且不重放。
            report['_retryable'] = not write_in_flight
            report['_blocked'] = write_in_flight
            kind = 'delete_failures' if write_in_flight else 'scan_failures'
            message = ('目录回收结果未确认，已停止自动重试，请先核对云盘'
                       if write_in_flight else ('剩余目录读取或校验失败；已确认成功的目录回收不会重放'
                             if report['cleaned'] else '目录读取或清理记录校验失败，未执行删除'))
            _reason(report, kind, message)
            update_staging_cleanup(row, status='failed' if write_in_flight else 'retained', error=message)
    return report


def staging_roots_in_source(client, rows: list[dict], source_id: str) -> list[dict]:
    """按精确目录ID与当前父链限定手动来源，禁止按名称/前缀扩大删除范围。"""
    parents: dict[str, str] = {}
    result = []
    for row in rows:
        current = str(row.get('gy_target_dir') or '')
        if current == source_id:
            result.append(row)
            continue
        current = str(row.get('gy_staging_parent_dir') or '')
        seen: set[str] = set()
        while current:
            if current == source_id:
                result.append(row)
                break
            if current == '0':
                break
            if current in seen or len(seen) >= 64:
                raise RuntimeError('下载目录父链不完整或超过安全上限，已停止清理')
            seen.add(current)
            if current not in parents:
                info = client.file_info(current)
                if info is None or not bool(getattr(info, 'is_dir', False)):
                    break
                parents[current] = str(getattr(info, 'parent_id', '') or '')
            current = parents[current]
    return result


def source_inside_protected_staging(client, source_id: str, protected: set[str],
                                    *, info_cache: dict | None = None) -> bool:
    """直接选中活动隔离根内部的子目录，也不能绕过下载写保护。"""
    if not protected:
        return False
    if not source_id:
        raise RuntimeError('来源目录身份无法确认，已停止清理')
    current = source_id
    seen: set[str] = set()
    while current:
        if current in protected:
            return True
        if current == '0':
            return False
        if current in seen or len(seen) >= 64:
            raise RuntimeError('来源目录父链异常，已停止清理')
        seen.add(current)
        if info_cache is None:
            info = client.file_info(current)
        else:
            if current not in info_cache:
                info_cache[current] = client.file_info(current)
            info = info_cache[current]
        if info is None or not bool(getattr(info, 'is_dir', False)):
            raise RuntimeError('来源目录身份无法确认，已停止清理')
        current = str(getattr(info, 'parent_id', '') or '')
        if not current:
            raise RuntimeError('来源目录父链不完整，已停止清理')
    return False


def assert_guangya_download_delete_allowed(client, item) -> None:
    """在手动删除的 Writer 回调内检查下载占用；不改变自动整理的自有清理权限。"""
    from app.modules.directory_scrape_errors import DirectoryScrapeConflictError

    try:
        requests = list_staging_cleanup_requests(active_only=True)
        if not requests:
            return
        info_cache = {str(item.file_id): item} if item.is_dir else {}
        for row in requests:
            root_id = str(row['gy_target_dir'])
            inside = str(item.file_id) == root_id or source_inside_protected_staging(
                client, str(item.file_id if item.is_dir else item.parent_id), {root_id},
                info_cache=info_cache,
            )
            contains = not inside and item.is_dir and source_inside_protected_staging(
                client, root_id, {str(item.file_id)}, info_cache=info_cache,
            )
            if inside or contains:
                raise DirectoryScrapeConflictError(
                    f"此项目或其目录正被下载请求 #{row['id']} 使用，请等待下载和整理结束后再删除"
                )
        # 父链读取可能耗时，期间新入队确认/新认领下载不能沿用旧的空闲结论。
        before = {(row['id'], row['gy_target_dir']) for row in requests}
        after = {(row['id'], row['gy_target_dir'])
                 for row in list_staging_cleanup_requests(active_only=True)}
        if before != after:
            raise DirectoryScrapeConflictError('下载任务占用已变化，未删除项目，请重新确认')
    except DirectoryScrapeConflictError:
        raise
    except Exception as exc:  # 安全状态/云盘父链读取失败，不执行破坏性操作。
        raise DirectoryScrapeConflictError(
            '暂时无法确认目录与下载任务的关联，已取消删除，请稍后重试'
        ) from exc


def merge_cleanup_report(target: dict, other: dict) -> None:
    for key in ('cleaned', 'scanned', 'candidates', 'protected', 'not_empty', 'unavailable',
                'unsupported', 'scan_failures', 'delete_failures'):
        target[key] = int(target.get(key) or 0) + max(0, int(other.get(key) or 0))
    for reason in other.get('reasons', []):
        if reason not in target['reasons'] and len(target['reasons']) < 8:
            target['reasons'].append(reason)
