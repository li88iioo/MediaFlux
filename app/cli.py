"""MediaFlux 生产运行入口。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import webbrowser
from pathlib import Path
from typing import Sequence
from urllib.error import URLError
from urllib.request import urlopen

import uvicorn

from app.defaults import DEFAULT_WEB_PORT
from app.runtime_paths import RuntimePaths, configure_runtime_paths, get_runtime_paths
from app.version import BuildInfo


def build_parser() -> argparse.ArgumentParser:
    """构建稳定的命令行参数解析器。"""
    parser = argparse.ArgumentParser(prog="mediaflux")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--host", default=None)
    start.add_argument("--port", type=int, default=None)
    start.add_argument("--data-dir", default=None)

    status = sub.add_parser("status")
    status.add_argument("--port", type=int, default=None)
    open_web = sub.add_parser("open")
    open_web.add_argument("--port", type=int, default=None)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--source", action="append", type=Path, default=[])
    doctor.add_argument("--target", action="append", type=Path, default=[])
    doctor.add_argument("--host", default=None)
    doctor.add_argument("--port", type=int, default=None)
    doctor.add_argument(
        "--strict-startup",
        action="store_true",
        help="将监听端口不可绑定视为启动错误（供服务管理器 ExecStartPre 使用）",
    )

    version = sub.add_parser("version")
    version.add_argument("--json", action="store_true")

    support_bundle = sub.add_parser("support-bundle")
    support_bundle.add_argument("--output", type=Path, default=None)
    support_bundle.add_argument("--log-lines", type=int, default=300)

    backup = sub.add_parser("backup")
    backup_sub = backup.add_subparsers(dest="backup_command", required=True)
    backup_create = backup_sub.add_parser("create")
    backup_create.add_argument("--output", type=Path, default=None)
    backup_create.add_argument("--reason", default="manual")
    backup_verify = backup_sub.add_parser("verify")
    backup_verify.add_argument("archive", type=Path)
    backup_verify.add_argument("--json", action="store_true")
    backup_restore = backup_sub.add_parser("restore")
    backup_restore.add_argument("archive", type=Path)

    update = sub.add_parser("update")
    update_sub = update.add_subparsers(dest="update_command", required=True)
    update_check = update_sub.add_parser("check")
    update_check.add_argument("--json", action="store_true")
    update_check.add_argument("--include-prerelease", action="store_true")
    return parser


def _enforce_packaged_production_mode() -> None:
    """容器运行模式下强制使用 production，不能被开发环境文件降级。"""
    if os.getenv("MEDIAFLUX_CONTAINER", "").strip().lower() in {"1", "true", "yes", "on"}:
        os.environ["APP_ENV"] = "production"


def _configure_start_paths(data_dir: str | None) -> None:
    """在导入路径消费者前配置 CLI 指定的运行目录。"""
    if data_dir is None:
        return

    root = Path(data_dir).expanduser()
    environment = dict(os.environ)
    environment["MEDIAFLUX_DATA_DIR"] = str(root)
    environment.setdefault("MEDIAFLUX_CONFIG_DIR", str(root / "config"))
    environment.setdefault("MEDIAFLUX_CACHE_DIR", str(root / "cache"))
    environment.setdefault("MEDIAFLUX_LOG_DIR", str(root / "logs"))
    environment.setdefault("MEDIAFLUX_STRM_DIR", str(root / "strm-data"))
    paths = RuntimePaths.from_environment(environment)
    paths.ensure_writable_dirs()
    configure_runtime_paths(paths)


def _start(host: str | None, port: int | None, data_dir: str | None) -> int:
    _enforce_packaged_production_mode()
    _configure_start_paths(data_dir)

    # 启动、pending restore 与整个 Uvicorn 生命周期共享同一把独占锁。
    # 必须在 config/first_run/app.main 首次读取 user.env 前恢复，否则会把
    # 恢复前的端口、首启状态或会话密钥冻结到当前进程。
    from app.modules.backup import recover_pending_restore, runtime_lifecycle_guard

    paths = get_runtime_paths()
    with runtime_lifecycle_guard(paths):
        recovered = recover_pending_restore(paths, lifecycle_lock_held=True)

        # 仅在运行目录配置和恢复完成后导入路径/配置消费者。
        from app import config
        from app.logger import get_logger

        if recovered:
            config.reload_after_restore()
            from app.modules import first_run

            first_run.refresh_startup_state_after_restore()

        # 在任何配置读取或首次运行检查前安装脱敏 handler，避免启动早期异常旁路。
        get_logger(__name__)

        external_host = os.getenv("WEB_HOST", "")
        requested_host = host if host is not None else (external_host or None)
        from app.modules.first_run import (
            UnsafeFirstRunBindingError,
            resolve_bind_host,
        )

        try:
            effective_host = resolve_bind_host(requested_host)
        except UnsafeFirstRunBindingError as exc:
            print(f"MediaFlux refused unsafe first-run binding: {exc}", file=sys.stderr)
            return 2

        from app.main import app as web_app

        effective_port = port if port is not None else config.flask_port()
        pid_file = paths.data_dir / "mediaflux.pid"
        try:
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            pid_file.write_text(str(os.getpid()), encoding="utf-8")
        except OSError:
            pass

        try:
            uvicorn.run(
                web_app,
                host=effective_host,
                port=effective_port,
                workers=1,
                reload=False,
                log_level="info",
                log_config=None,
                access_log=False,
            )
        finally:
            # Uvicorn 在进入 lifespan 前失败或测试替换 uvicorn.run 时，也必须
            # 释放应用构造期的启动预留锁；正常 shutdown 后该回调是幂等的。
            release_guard = getattr(
                getattr(web_app, "state", None),
                "release_startup_lifecycle_guard",
                None,
            )
            if callable(release_guard):
                release_guard()
            try:
                if (
                    pid_file.is_file()
                    and pid_file.read_text(encoding="utf-8").strip() == str(os.getpid())
                ):
                    pid_file.unlink(missing_ok=True)
            except OSError:
                pass
        return 0


def _status(port: int | None = None) -> int:
    """按当前运行配置探测本机 HTTP 健康检查端点。"""
    from app.runtime_paths import get_runtime_paths

    try:
        paths = get_runtime_paths()
        _, effective_port, runtime_config_check = _resolve_doctor_endpoint(paths, None, port)
    except (OSError, ValueError):
        runtime_config_check = object()
        effective_port = DEFAULT_WEB_PORT

    if runtime_config_check is not None:
        # 保持旧版 status 在损坏配置下仍探测默认端口；doctor 负责详细报告配置错误。
        effective_port = DEFAULT_WEB_PORT

    try:
        with urlopen(f"http://127.0.0.1:{effective_port}/healthz", timeout=1) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, json.JSONDecodeError):
        print("MediaFlux health check failed")
        return 1

    if isinstance(payload, dict) and payload.get("status") == "ok":
        print("MediaFlux is healthy")
        return 0

    print("MediaFlux health check failed")
    return 1


def _open_web(port: int | None = None) -> int:
    """使用当前配置端口打开本机控制台。"""
    from app.runtime_paths import get_runtime_paths

    try:
        paths = get_runtime_paths()
        _, effective_port, runtime_config_check = _resolve_doctor_endpoint(paths, None, port)
    except (OSError, ValueError):
        runtime_config_check = object()
        effective_port = DEFAULT_WEB_PORT
    if runtime_config_check is not None:
        effective_port = DEFAULT_WEB_PORT
    url = f"http://127.0.0.1:{effective_port}/"
    if not webbrowser.open(url, new=2):
        print(url)
        return 1
    print(url)
    return 0


def _resolve_doctor_endpoint(
    paths: RuntimePaths,
    host: str | None,
    port: int | None,
) -> tuple[str, int, object | None]:
    """只读解析 doctor 端点：CLI、环境、已有 user.env、默认值。"""
    from app.modules.runtime_diagnostics import DiagnosticCheck

    resolved_host = host if host is not None else os.environ.get("WEB_HOST") or None
    resolved_port = port
    raw_environment_port = None if port is not None else os.environ.get("WEB_PORT")
    if raw_environment_port:
        try:
            resolved_port = int(raw_environment_port)
        except ValueError:
            return (
                resolved_host or "0.0.0.0",
                DEFAULT_WEB_PORT,
                DiagnosticCheck(
                    "runtime_config",
                    "error",
                    "当前执行身份无法解析 WEB_PORT 环境变量。",
                    "将 WEB_PORT 设置为 1 到 65535 的整数。",
                ),
            )

    values: dict[str, str] = {}
    if (resolved_host is None or resolved_port is None) and paths.env_file.exists():
        try:
            from app.env_file import read_env_text

            values = read_env_text(paths.env_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            return (
                resolved_host or "0.0.0.0",
                resolved_port or DEFAULT_WEB_PORT,
                DiagnosticCheck(
                    "runtime_config",
                    "error",
                    f"当前执行身份无法读取运行配置：{getattr(exc, 'strerror', None) or exc}。",
                    "检查已有 user.env 的读取权限和格式。",
                ),
            )

    if resolved_host is None:
        resolved_host = values.get("WEB_HOST") or "0.0.0.0"
    if resolved_port is None:
        raw_file_port = values.get("WEB_PORT")
        if raw_file_port:
            try:
                resolved_port = int(raw_file_port)
            except ValueError:
                return (
                    resolved_host,
                    DEFAULT_WEB_PORT,
                    DiagnosticCheck(
                        "runtime_config",
                        "error",
                        "当前执行身份无法解析 user.env 中的 WEB_PORT。",
                        "将 WEB_PORT 设置为 1 到 65535 的整数。",
                    ),
                )
        else:
            resolved_port = DEFAULT_WEB_PORT

    if not 1 <= resolved_port <= 65535:
        return (
            resolved_host,
            DEFAULT_WEB_PORT,
            DiagnosticCheck(
                "runtime_config",
                "error",
                "当前执行身份解析到无效的 WEB_PORT。",
                "将 WEB_PORT 设置为 1 到 65535 的整数。",
            ),
        )
    return resolved_host, resolved_port, None


def _emit_doctor_checks(as_json: bool, checks: list[dict[str, str]]) -> int:
    """输出稳定的 doctor 检查数组，并返回适合进程退出的状态码。"""
    if as_json:
        print(json.dumps(checks, ensure_ascii=False, sort_keys=True))
    else:
        errors = sum(check["status"] == "error" for check in checks)
        warnings = sum(check["status"] == "warning" for check in checks)
        successes = sum(check["status"] == "ok" for check in checks)
        print(f"MediaFlux doctor: {successes} 项正常，{warnings} 项警告，{errors} 项错误")
        for check in checks:
            print(f"[{check['status']}] {check['key']}: {check['message']}")
            if check["suggestion"]:
                print(f"  建议：{check['suggestion']}")
    return 1 if any(check["status"] == "error" for check in checks) else 0


def _doctor(
    as_json: bool,
    source_paths: Sequence[Path] = (),
    target_paths: Sequence[Path] = (),
    host: str | None = None,
    port: int | None = None,
    strict_startup: bool = False,
) -> int:
    """运行安装与权限诊断，不自动修改任何系统设置。"""
    from app.runtime_paths import get_runtime_paths

    try:
        paths = get_runtime_paths()
    except (ValueError, OSError) as exc:
        from app.modules.runtime_diagnostics import DiagnosticCheck

        check = DiagnosticCheck(
            "runtime_config",
            "error",
            f"当前执行身份无法解析运行路径：{exc}。",
            "检查运行目录相关环境变量是否为有效绝对路径。",
        )
        return _emit_doctor_checks(as_json, [check.as_dict()])

    effective_host, effective_port, runtime_config_check = _resolve_doctor_endpoint(paths, host, port)

    # RuntimePaths 和端口均确定后才导入诊断模块。
    from app.modules.runtime_diagnostics import DiagnosticReport, run_diagnostics

    report = run_diagnostics(
        paths,
        source_paths=tuple(source_paths),
        target_paths=tuple(target_paths),
        host=effective_host,
        port=effective_port,
    )
    if strict_startup:
        report = DiagnosticReport(tuple(
            type(check)(
                check.key,
                "error" if check.key == "default_service_port" and check.status == "warning" else check.status,
                (
                    f"启动前检查失败：{check.message}"
                    if check.key == "default_service_port" and check.status == "warning"
                    else check.message
                ),
                check.suggestion,
            )
            for check in report.checks
        ))
    if runtime_config_check is not None:
        report = DiagnosticReport((*report.checks, runtime_config_check))
    return _emit_doctor_checks(as_json, report.as_dict()["checks"])


def _version(as_json: bool) -> int:
    info = BuildInfo.current()
    if as_json:
        print(json.dumps(info.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        print(f"{info.name} {info.version}")
    return 0


def _support_bundle(output: Path | None, log_lines: int) -> int:
    """生成仅保存在本机、默认脱敏的支持诊断包。"""
    from app.modules.support_bundle import SupportBundleError, create_support_bundle
    from app.runtime_paths import get_runtime_paths

    try:
        paths = get_runtime_paths()
        host, port, runtime_config_check = _resolve_doctor_endpoint(paths, None, None)
        destination = create_support_bundle(
            paths,
            output=output,
            host=host,
            port=port,
            runtime_config_check=runtime_config_check,
            log_lines=log_lines,
        )
    except (OSError, ValueError, SupportBundleError) as exc:
        print(f"MediaFlux support bundle failed: {exc}", file=sys.stderr)
        return 2
    print(destination)
    return 0


def _backup_command(args: argparse.Namespace) -> int:
    """执行本地备份操作；恢复默认拒绝对运行中的服务写入。"""
    from app.modules.backup import BackupError, create_backup, restore_backup, verify_backup
    from app.runtime_paths import get_runtime_paths

    try:
        paths = get_runtime_paths()
        if args.backup_command == "create":
            destination = create_backup(paths, output=args.output, reason=args.reason)
            print(destination)
            return 0
        if args.backup_command == "verify":
            manifest = verify_backup(args.archive)
            if args.json:
                print(json.dumps(manifest.as_dict(), ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"MediaFlux backup verified: {len(manifest.entries)} files, "
                    f"schema {manifest.as_dict().get('database_schema_version', 0)}"
                )
            return 0
        if args.backup_command == "restore":
            manifest = restore_backup(paths, args.archive)
            print(
                f"MediaFlux backup restored: {len(manifest.entries)} files. "
                "Restart the service and run mediaflux doctor."
            )
            return 0
    except (OSError, ValueError, BackupError) as exc:
        print(f"MediaFlux backup failed: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"unsupported backup command: {args.backup_command}")


def _update_command(args: argparse.Namespace) -> int:
    from app.modules.update_check import UpdateCheckError, check_for_updates

    if args.update_command != "check":
        raise AssertionError(f"unsupported update command: {args.update_command}")
    try:
        result = check_for_updates(include_prerelease=args.include_prerelease)
    except UpdateCheckError as exc:
        print(f"MediaFlux update check failed: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(result.as_dict(), ensure_ascii=False, sort_keys=True))
    elif result.update_available:
        target = result.recommended_asset_name or "发布页"
        print(
            f"MediaFlux update available: {result.current_version} -> "
            f"{result.latest_version} ({target})"
        )
        if result.release_url:
            print(result.release_url)
    else:
        print(f"MediaFlux is up to date: {result.current_version}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """运行 MediaFlux CLI 并返回适合进程退出的状态码。"""
    args = build_parser().parse_args(argv)
    if args.command == "start":
        return _start(args.host, args.port, args.data_dir)
    if args.command == "status":
        return _status(args.port)
    if args.command == "open":
        return _open_web(args.port)
    if args.command == "doctor":
        return _doctor(
            args.json,
            args.source,
            args.target,
            args.host,
            args.port,
            args.strict_startup,
        )
    if args.command == "version":
        return _version(args.json)
    if args.command == "support-bundle":
        return _support_bundle(args.output, args.log_lines)
    if args.command == "backup":
        return _backup_command(args)
    if args.command == "update":
        return _update_command(args)
    raise AssertionError(f"unsupported command: {args.command}")
