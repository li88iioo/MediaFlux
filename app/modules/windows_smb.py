"""Windows 原生 SMB / UNC 网络共享连接与凭据管理。

支持在 Windows 服务（LocalSystem 或普通账户）与桌面会话中，
通过系统底层 mpr.dll (WNetAddConnection2W) 自动建立网络认证连接，
实现 NAS 共享路径开箱即用，无需用户手动修改 Windows 系统服务或组策略配置。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

from app.logger import get_logger

logger = get_logger(__name__)

# Windows 网络错误码对照
WINERROR_DESCRIPTIONS: dict[int, str] = {
    5: "访问被拒绝：NAS 共享权限不足，请配置有效的访问用户名和密码",
    53: "找不到网络路径：请检查 NAS 的 IP 或主机名是否正确，以及 NAS 是否已开机并连入局域网",
    64: "指定的网络名不再可用：与 NAS 的网络连接已中断",
    67: "找不到网络名：NAS 上的共享文件夹名称不存在或未启用 SMB 共享",
    85: "本地设备名已在使用中",
    86: "网络密码无效：NAS 访问密码错误",
    1202: "设备已记住该连接",
    1208: "发生扩展错误：无法连接到指定的 NAS 共享",
    1219: "凭据冲突：当前会话已使用其他用户名建立了到该 NAS 的连接，请使用统一账号或重启服务",
    1244: "需要身份验证：未提供有效的凭据或操作未被授权",
    1326: "登录失败：NAS 用户名或密码错误",
    1327: "用户帐户受限：该账号存在登录限制（如禁止空密码或网络登录受限）",
    1328: "登录失败：当前不在允许的登录时间段内",
    1329: "登录失败：该用户无法从此工作站登录",
    1330: "登录失败：该 NAS 账号的密码已过期，请在 NAS 管理后台修改密码",
    1331: "登录失败：该 NAS 账号已被停用或禁用。请在 NAS 用户管理后台启用该账号，或更换其他有效账号",
    1385: "登录失败：该账号未被授予通过网络访问此计算机的权限",
    1907: "登录失败：用户在首次登录前必须更改密码",
    1909: "登录失败：该 NAS 账号由于多次输入错误密码已被锁定，请在 NAS 控制台解锁",
    2242: "用户密码已在 NAS 上过期",
}

# 判定为「需要身份验证（Guest 未启用/无有效凭据/账号受限）」的错误码集合
AUTH_REQUIRED_WINERRORS: tuple[int, ...] = (
    5,
    86,
    1244,
    1326,
    1327,
    1328,
    1329,
    1330,
    1331,
    1385,
    1907,
    1909,
)


def _find_explorer_pid() -> int | None:
    """通过 CreateToolhelp32Snapshot 查找 explorer.exe 进程 PID（纯 ctypes，无 subprocess）。

    仅在 Windows 平台有效；在非 Windows 或查找失败时返回 None。
    """
    if not is_windows():
        return None

    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        CreateToolhelp32Snapshot = kernel32.CreateToolhelp32Snapshot
        CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]

        Process32FirstW = kernel32.Process32FirstW
        Process32FirstW.restype = wintypes.BOOL
        Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

        Process32NextW = kernel32.Process32NextW
        Process32NextW.restype = wintypes.BOOL
        Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]

        CloseHandle = kernel32.CloseHandle
        CloseHandle.restype = wintypes.BOOL
        CloseHandle.argtypes = [wintypes.HANDLE]

        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value  # type: ignore[arg-type]

        snap = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap is None or snap == INVALID_HANDLE_VALUE:
            return None

        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            if not Process32FirstW(snap, ctypes.byref(entry)):
                return None

            while True:
                if entry.szExeFile.lower() == "explorer.exe":
                    return int(entry.th32ProcessID)
                if not Process32NextW(snap, ctypes.byref(entry)):
                    break
        finally:
            CloseHandle(snap)
    except Exception as exc:
        logger.debug("查找 explorer.exe 进程异常: %s", exc)

    return None


def _try_impersonate_desktop_user_for_smb(unc_root: str) -> bool:
    """尝试借用当前桌面用户的安全令牌来访问 SMB 共享。

    Windows 服务运行在 LocalSystem 下没有桌面用户的网络凭据，
    但可以通过 OpenProcessToken + ImpersonateLoggedOnUser 临时
    借用 explorer.exe 所属的桌面用户令牌来建立 SMB 会话。

    成功时返回 True（路径可达），否则返回 False。
    所有句柄均在 finally 块中正确关闭，ImpersonateLoggedOnUser 与
    RevertToSelf 严格成对调用。
    """
    if not is_windows():
        return False

    explorer_pid = _find_explorer_pid()
    if not explorer_pid:
        logger.debug("未找到 explorer.exe 进程，无法借用桌面用户令牌")
        return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

        PROCESS_QUERY_INFORMATION = 0x0400
        TOKEN_DUPLICATE = 0x0002
        TOKEN_QUERY = 0x0008
        TOKEN_IMPERSONATE = 0x0004
        SecurityImpersonation = 2
        TokenPrimary = 1

        OpenProcess = kernel32.OpenProcess
        OpenProcess.restype = wintypes.HANDLE
        OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]

        OpenProcessToken = advapi32.OpenProcessToken
        OpenProcessToken.restype = wintypes.BOOL
        OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]

        DuplicateTokenEx = advapi32.DuplicateTokenEx
        DuplicateTokenEx.restype = wintypes.BOOL
        DuplicateTokenEx.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.POINTER(wintypes.HANDLE),
        ]

        ImpersonateLoggedOnUser = advapi32.ImpersonateLoggedOnUser
        ImpersonateLoggedOnUser.restype = wintypes.BOOL
        ImpersonateLoggedOnUser.argtypes = [wintypes.HANDLE]

        RevertToSelf = advapi32.RevertToSelf
        RevertToSelf.restype = wintypes.BOOL

        CloseHandle = kernel32.CloseHandle
        CloseHandle.restype = wintypes.BOOL
        CloseHandle.argtypes = [wintypes.HANDLE]

        # 打开 explorer.exe 进程句柄
        proc_handle = OpenProcess(PROCESS_QUERY_INFORMATION, False, explorer_pid)
        if not proc_handle:
            logger.debug(
                "无法打开 explorer.exe 进程 (PID=%d): %s",
                explorer_pid,
                ctypes.FormatError(ctypes.get_last_error()),
            )
            return False

        try:
            # 获取进程令牌
            token = wintypes.HANDLE()
            if not OpenProcessToken(proc_handle, TOKEN_DUPLICATE | TOKEN_QUERY, ctypes.byref(token)):
                logger.debug(
                    "无法获取 explorer.exe 进程令牌: %s",
                    ctypes.FormatError(ctypes.get_last_error()),
                )
                return False

            try:
                # 复制令牌为模拟令牌
                dup_token = wintypes.HANDLE()
                if not DuplicateTokenEx(
                    token,
                    TOKEN_IMPERSONATE | TOKEN_QUERY | TOKEN_DUPLICATE,
                    None,
                    SecurityImpersonation,
                    TokenPrimary,
                    ctypes.byref(dup_token),
                ):
                    logger.debug(
                        "令牌复制失败: %s",
                        ctypes.FormatError(ctypes.get_last_error()),
                    )
                    return False

                try:
                    # 在当前线程模拟桌面用户身份
                    if not ImpersonateLoggedOnUser(dup_token):
                        logger.debug(
                            "用户模拟失败: %s",
                            ctypes.FormatError(ctypes.get_last_error()),
                        )
                        return False

                    try:
                        # 1. 尝试在模拟桌面用户身份的上下文中调用 WNetAddConnection2W 建立连接
                        try:
                            class NETRESOURCEW_LOCAL(ctypes.Structure):
                                _fields_ = [
                                    ("dwScope", wintypes.DWORD),
                                    ("dwType", wintypes.DWORD),
                                    ("dwDisplayType", wintypes.DWORD),
                                    ("dwUsage", wintypes.DWORD),
                                    ("lpLocalName", wintypes.LPCWSTR),
                                    ("lpRemoteName", wintypes.LPCWSTR),
                                    ("lpComment", wintypes.LPCWSTR),
                                    ("lpProvider", wintypes.LPCWSTR),
                                ]

                            mpr = ctypes.WinDLL("mpr.dll", use_last_error=True)
                            net_res = NETRESOURCEW_LOCAL()
                            net_res.dwType = 0x00000001  # RESOURCETYPE_DISK
                            net_res.lpLocalName = None
                            net_res.lpRemoteName = unc_root
                            net_res.lpProvider = None
                            add_conn = mpr.WNetAddConnection2W
                            add_conn.argtypes = [
                                ctypes.POINTER(NETRESOURCEW_LOCAL),
                                wintypes.LPCWSTR,
                                wintypes.LPCWSTR,
                                wintypes.DWORD,
                            ]
                            add_conn.restype = wintypes.DWORD
                            wnet_res = add_conn(ctypes.byref(net_res), None, None, 0x00000004)
                            logger.debug("模拟桌面用户上下文 WNetAddConnection2W 结果: %d", wnet_res)
                        except Exception as wnet_exc:
                            logger.debug("模拟上下文执行 WNetAddConnection2W 异常: %s", wnet_exc)

                        # 2. 以桌面用户安全上下文测试路径可访问性
                        test_path = Path(unc_root)
                        if test_path.exists():
                            logger.info(
                                "通过桌面用户令牌模拟成功访问 SMB 共享: %s",
                                unc_root,
                            )
                            return True
                        else:
                            logger.debug(
                                "桌面用户令牌模拟后路径仍不可访问: %s",
                                unc_root,
                            )
                            return False
                    finally:
                        RevertToSelf()
                finally:
                    CloseHandle(dup_token)
            finally:
                CloseHandle(token)
        finally:
            CloseHandle(proc_handle)
    except Exception as exc:
        logger.debug("桌面用户令牌模拟异常: %s", exc)
        return False


class WindowsSmbError(OSError):
    """Windows SMB / UNC 网络共享访问异常。"""

    def __init__(self, message: str, winerror: int = 0, raw_error: Exception | None = None):
        super().__init__(message)
        self.winerror = winerror
        self.raw_error = raw_error


class UncShare(NamedTuple):
    server: str
    share: str
    normalized_root: str


# Windows SMB 进程级凭据缓存：{server_lower: (username, password)}
_SERVER_CREDENTIALS_CACHE: dict[str, tuple[str, str]] = {}


def _normalize_server(server: str | None) -> str:
    """规范化服务器名称（去除斜杠、端口、空白，统一转小写）。"""
    if not server:
        return ""
    srv = str(server).strip().replace("/", "\\").strip("\\/ ")
    if "\\" in srv:
        srv = srv.split("\\")[0]
    if ":" in srv and not (len(srv) == 2 and srv[1] == ":"):
        srv = srv.split(":")[0]
    return srv.strip().lower()


def _server_matches(srv1: str, srv2: str) -> bool:
    """判断两个服务器标识是否指向同一台主机（支持大小写归一、主机名/FQDN 前缀归一）。"""
    n1 = _normalize_server(srv1)
    n2 = _normalize_server(srv2)
    if not n1 or not n2:
        return False
    if n1 == n2:
        return True
    p1 = n1.split(".")[0]
    p2 = n2.split(".")[0]
    if p1 and p2 and p1 == p2 and not p1.isdigit() and not p2.isdigit():
        return True
    return False


def get_server_credentials(server: str) -> tuple[str, str]:
    """获取指定服务器的主机凭据（进程缓存优先，其次从数据库中跨来源智能匹配同台 NAS 主机已保存凭据）。"""
    srv_key = _normalize_server(server)
    if not srv_key:
        return ("", "")

    # 1. 查进程缓存
    if srv_key in _SERVER_CREDENTIALS_CACHE:
        return _SERVER_CREDENTIALS_CACHE[srv_key]
    for cached_srv, creds in _SERVER_CREDENTIALS_CACHE.items():
        if _server_matches(cached_srv, srv_key):
            return creds

    # 2. 从数据库中跨来源智能匹配
    try:
        from app import database as db
        sources = db.list_local_media_sources()
        for src in sources:
            user = getattr(src, "smb_user", "") or ""
            pwd = getattr(src, "smb_pass", "") or ""
            if not user:
                continue

            unc = parse_unc_share_root(src.local_root)
            if unc and _server_matches(unc.server, srv_key):
                set_server_credentials(srv_key, user, pwd)
                set_server_credentials(unc.server, user, pwd)
                return (user, pwd)

            targets = getattr(src, "targets", []) or []
            for tgt in targets:
                tgt_path = getattr(tgt, "path", "") if not isinstance(tgt, dict) else tgt.get("path", "")
                tgt_unc = parse_unc_share_root(tgt_path)
                if tgt_unc and _server_matches(tgt_unc.server, srv_key):
                    set_server_credentials(srv_key, user, pwd)
                    set_server_credentials(tgt_unc.server, user, pwd)
                    return (user, pwd)
    except Exception as exc:
        logger.debug("从数据库检索服务器凭据异常: %s", exc)

    return ("", "")


def set_server_credentials(server: str, user: str, pwd: str) -> None:
    """更新服务器的凭据缓存。"""
    srv_key = _normalize_server(server)
    if srv_key and user:
        _SERVER_CREDENTIALS_CACHE[srv_key] = (user.strip(), pwd.strip())


def clear_server_credentials_cache() -> None:
    """清空服务器凭据缓存（主要用于测试隔离）。"""
    _SERVER_CREDENTIALS_CACHE.clear()


def is_windows() -> bool:
    """判断当前运行环境是否为 Windows。"""
    return os.name == "nt" or sys.platform == "win32"


def get_windows_mapped_network_drives() -> dict[str, str]:
    """扫描 Windows 系统中已映射的网络驱动器（如 X: -> \\\\Nas\\视频）。

    支持读取当前用户 HKCU 以及系统多用户 HKEY_USERS 下保存的持久化网络映射。
    返回格式：{"X:": r"\\Nas\视频", "W:": r"\\Nas\固态", ...}
    """
    if not is_windows():
        return {}

    import winreg
    drives: dict[str, str] = {}

    # 1. 尝试读取 HKCU\Network
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Network") as net_key:
            num_subkeys, _, _ = winreg.QueryInfoKey(net_key)
            for i in range(num_subkeys):
                drive_letter = winreg.EnumKey(net_key, i)
                try:
                    with winreg.OpenKey(net_key, drive_letter) as drive_key:
                        remote_path, _ = winreg.QueryValueEx(drive_key, "RemotePath")
                        letter = drive_letter.rstrip(":").upper() + ":"
                        drives[letter] = str(remote_path).rstrip("\\")
                except Exception:
                    pass
    except Exception:
        pass

    # 2. 尝试读取 HKEY_USERS\<SID>\Network（适用于 Windows 服务 LocalSystem 跨 Session 读取桌面用户映射）
    try:
        with winreg.OpenKey(winreg.HKEY_USERS, "") as users_key:
            num_users, _, _ = winreg.QueryInfoKey(users_key)
            for u_idx in range(num_users):
                sid = winreg.EnumKey(users_key, u_idx)
                if sid.endswith("_Classes") or sid in (".DEFAULT", "S-1-5-18", "S-1-5-19", "S-1-5-20"):
                    continue
                try:
                    with winreg.OpenKey(winreg.HKEY_USERS, f"{sid}\\Network") as user_net:
                        num_sub, _, _ = winreg.QueryInfoKey(user_net)
                        for d_idx in range(num_sub):
                            drive_letter = winreg.EnumKey(user_net, d_idx)
                            try:
                                with winreg.OpenKey(user_net, drive_letter) as d_key:
                                    remote_path, _ = winreg.QueryValueEx(d_key, "RemotePath")
                                    letter = drive_letter.rstrip(":").upper() + ":"
                                    if letter not in drives:
                                        drives[letter] = str(remote_path).rstrip("\\")
                            except Exception:
                                pass
                except Exception:
                    pass
    except Exception:
        pass

    return drives


def resolve_drive_or_unc_path(path_or_str: str | Path | None) -> str:
    """将包含 Windows 映射网络驱动器盘符的路径（如 X:\\动漫、W:\\）转换为实际的 UNC 路径（如 \\\\Nas\\视频\\动漫、\\\\Nas\\固态）。

    若输入已是 UNC 路径或本地物理路径（如 C:\\...），则原样返回规范化字符串。
    """
    raw = str(path_or_str or "").strip()
    if not raw or not is_windows():
        return raw

    standardized = raw.replace("/", "\\")
    match = re.match(r"^([a-zA-Z]:)(.*)$", standardized)
    if not match:
        return raw

    drive_prefix = match.group(1).upper()
    sub_path = match.group(2).strip("\\/")

    mapped_drives = get_windows_mapped_network_drives()
    if drive_prefix in mapped_drives:
        remote_unc = mapped_drives[drive_prefix].rstrip("\\/")
        if sub_path:
            if remote_unc.startswith("/"):
                return f"{remote_unc}/{sub_path}"
            return f"{remote_unc}\\{sub_path}"
        return remote_unc

    return raw


def parse_unc_share_root(path_or_str: str | Path | None) -> UncShare | None:
    """从给定的路径字符串中解析出 UNC 服务器名与共享名根路径。

    示例：
    - r"\\\\NAS\\视频\\动漫" -> UncShare("NAS", "视频", r"\\\\NAS\\视频")
    - "X:\\动漫" (当 X: 映射到 \\\\NAS\\视频 时) -> UncShare("NAS", "视频", r"\\\\NAS\\视频")
    - "//192.168.88.11/media/movies" -> UncShare("192.168.88.11", "media", r"\\\\192.168.88.11\\media")
    - "D:\\Local\\Folder" -> None
    """
    raw = str(path_or_str or "").strip()
    if not raw:
        return None

    if is_windows():
        raw = resolve_drive_or_unc_path(raw)

    # 统一将斜杠转换为反斜杠进行 UNC 检查
    standardized = raw.replace("/", "\\")
    if not standardized.startswith(r"\\"):
        return None

    parts = [part for part in standardized.strip("\\").split("\\") if part]
    if len(parts) < 2:
        return None

    server = parts[0]
    share = parts[1]
    normalized_root = rf"\\{server}\{share}"
    return UncShare(server=server, share=share, normalized_root=normalized_root)


def explain_windows_network_error(exc: Exception, path: str | None = None) -> str:
    """将 Windows 原生网络/权限异常转译为人性化的中文排查指引。"""
    winerr = getattr(exc, "winerror", None)
    if winerr is None and isinstance(exc, OSError) and exc.errno:
        winerr = getattr(exc, "winerror", exc.errno)

    target_desc = f" [{path}]" if path else ""
    if winerr and winerr in WINERROR_DESCRIPTIONS:
        return f"无法访问网络共享{target_desc}（代码 {winerr}）：{WINERROR_DESCRIPTIONS[winerr]}"

    if isinstance(exc, PermissionError):
        return f"无法访问网络共享{target_desc}：权限被拒绝。若 NAS 需要身份验证，请在来源中配置 NAS 访问账号和密码。"
    if isinstance(exc, FileNotFoundError):
        return f"网络共享路径不存在{target_desc}，请检查 NAS 上的共享文件夹名称是否正确。"

    err_str = str(exc).strip()
    return f"网络共享访问失败{target_desc}：{err_str or type(exc).__name__}"


def ensure_smb_connection(
    unc_path: str | Path,
    username: str | None = None,
    password: str | None = None,
    *,
    force_reconnect: bool = False,
) -> tuple[bool, str]:
    """在 Windows 环境下建立与 UNC 路径的 SMB 会话与网络凭据。

    若非 Windows 系统或路径非 UNC，直接安全返回成功。
    """
    if not is_windows():
        return True, ""

    unc = parse_unc_share_root(unc_path)
    if unc is None:
        return True, ""

    user = str(username or "").strip()
    pwd = str(password or "").strip()

    # 如果未提供账号密码，先尝试从同服务器已知凭据缓存/数据库检索
    if not user:
        cached_user, cached_pwd = get_server_credentials(unc.server)
        if cached_user:
            user, pwd = cached_user, cached_pwd

    # 如果未提供账号密码，且未要求强制重连，则直接测试路径是否已可访问
    if not user and not pwd and not force_reconnect:
        try:
            test_path = Path(unc.normalized_root)
            if test_path.exists():
                return True, ""
        except Exception:
            pass

    import ctypes
    from ctypes import wintypes

    # 定义 Win32 NETRESOURCEW 结构
    class NETRESOURCEW(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD),
            ("dwType", wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD),
            ("lpLocalName", wintypes.LPCWSTR),
            ("lpRemoteName", wintypes.LPCWSTR),
            ("lpComment", wintypes.LPCWSTR),
            ("lpProvider", wintypes.LPCWSTR),
        ]

    RESOURCETYPE_DISK = 0x00000001
    CONNECT_TEMPORARY = 0x00000004

    mpr = ctypes.WinDLL("mpr.dll", use_last_error=True)
    WNetAddConnection2W = mpr.WNetAddConnection2W
    WNetAddConnection2W.argtypes = [
        ctypes.POINTER(NETRESOURCEW),
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    WNetAddConnection2W.restype = wintypes.DWORD

    WNetCancelConnection2W = mpr.WNetCancelConnection2W
    WNetCancelConnection2W.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL]
    WNetCancelConnection2W.restype = wintypes.DWORD

    net_resource = NETRESOURCEW()
    net_resource.dwType = RESOURCETYPE_DISK
    net_resource.lpLocalName = None
    net_resource.lpRemoteName = unc.normalized_root
    net_resource.lpProvider = None

    def _cancel(target: str) -> None:
        try:
            WNetCancelConnection2W(target, 0, True)
        except Exception:
            pass

    def _cleanup_old_sessions() -> None:
        _cancel(unc.normalized_root)
        _cancel(rf"\\{unc.server}\IPC$")

    def _connect(c_user: str | None, c_pwd: str | None) -> int:
        res = WNetAddConnection2W(
            ctypes.byref(net_resource),
            c_pwd,
            c_user,
            CONNECT_TEMPORARY,
        )
        if res == 1219:
            # 凭据冲突 (1219)：强制注销目标共享及 \\server\IPC$ 旧会话，再重新发起连接
            _cleanup_old_sessions()
            res = WNetAddConnection2W(
                ctypes.byref(net_resource),
                c_pwd,
                c_user,
                CONNECT_TEMPORARY,
            )
        return int(res)

    if user:
        c_pwd = pwd if pwd else None
        if force_reconnect:
            _cleanup_old_sessions()

        # Attempt 1: connect with user (e.g. admin)
        res = _connect(user, c_pwd)
        last_res = res
        if last_res in (0, 85, 1202):
            logger.info("Windows SMB 网络共享挂载就绪: root=%s user=%s", unc.normalized_root, user)
            set_server_credentials(unc.server, user, pwd)
            return True, ""

        # If result in (1326, 86) and "\\" not in user: Attempt 2: connect with rf"{unc.server}\{user}"
        if last_res in (1326, 86) and "\\" not in user:
            _cleanup_old_sessions()
            domain_user = rf"{unc.server}\{user}"
            res = _connect(domain_user, c_pwd)
            last_res = res
            if last_res in (0, 85, 1202):
                logger.info("Windows SMB 网络共享挂载就绪: root=%s user=%s", unc.normalized_root, domain_user)
                set_server_credentials(unc.server, user, pwd)
                return True, ""

        # If result in (1326, 86) and "\\" not in user: Attempt 3: connect with rf".\{user}"
        if last_res in (1326, 86) and "\\" not in user:
            _cleanup_old_sessions()
            local_user = rf".\{user}"
            res = _connect(local_user, c_pwd)
            last_res = res
            if last_res in (0, 85, 1202):
                logger.info("Windows SMB 网络共享挂载就绪: root=%s user=%s", unc.normalized_root, local_user)
                set_server_credentials(unc.server, user, pwd)
                return True, ""

        if last_res in (86, 1326):
            err_msg = f"NAS 共享 [{unc.normalized_root}] 认证失败：用户名或密码错误。若账号为 NAS 本地用户，可尝试在账号前添加主机名（如 {unc.server}\\{user}）"
        else:
            desc = WINERROR_DESCRIPTIONS.get(last_res, f"Windows 系统错误码 {last_res}")
            err_msg = f"无法挂载网络共享 [{unc.normalized_root}]（{desc}）"
        logger.warning("Windows SMB 挂载失败: root=%s user=%s code=%d msg=%s", unc.normalized_root, user, last_res, err_msg)
        return False, err_msg

    else:
        # 当未提供 username 与 password：
        try:
            if Path(unc.normalized_root).exists():
                return True, ""
        except Exception:
            pass

        # Try default connection (None, None)
        res = _connect(None, None)
        last_res = res
        if last_res in (0, 85, 1202):
            try:
                if Path(unc.normalized_root).exists():
                    return True, ""
            except Exception:
                pass
            return True, ""

        # Try guest connection (c_user="guest", c_pwd="")
        _cleanup_old_sessions()
        res = _connect("guest", "")
        last_res = res
        if last_res in (0, 85, 1202):
            try:
                if Path(unc.normalized_root).exists():
                    return True, ""
            except Exception:
                pass
            return True, ""

        # 匿名/Guest 均失败后，尝试借用桌面用户会话令牌进行 SMB 认证
        if last_res in AUTH_REQUIRED_WINERRORS:
            if _try_impersonate_desktop_user_for_smb(unc.normalized_root):
                return True, ""

        if last_res in AUTH_REQUIRED_WINERRORS:
            err_msg = f"NAS 共享 [{unc.normalized_root}] 需要身份验证（NAS 未启用 Guest 访客共享或账号已禁用）。请在来源配置中填写「NAS 访问账号」与「NAS 访问密码」。"
        else:
            desc = WINERROR_DESCRIPTIONS.get(last_res, f"Windows 系统错误码 {last_res}")
            err_msg = f"无法挂载网络共享 [{unc.normalized_root}]（{desc}）"
        logger.warning("Windows SMB 挂载失败: root=%s anonymous code=%d msg=%s", unc.normalized_root, last_res, err_msg)
        return False, err_msg
