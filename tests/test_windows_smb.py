"""Windows 原生 SMB 挂载、UNC 解析与错误转译契约测试。"""
from __future__ import annotations

import ctypes
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

if not hasattr(ctypes, "WinDLL"):
    ctypes.WinDLL = MagicMock()

from app.modules.windows_smb import (
    UncShare,
    _find_explorer_pid,
    _try_impersonate_desktop_user_for_smb,
    clear_server_credentials_cache,
    ensure_smb_connection,
    explain_windows_network_error,
    get_server_credentials,
    is_windows,
    parse_unc_share_root,
    resolve_drive_or_unc_path,
    set_server_credentials,
)


class WindowsSmbTests(unittest.TestCase):
    def setUp(self):
        clear_server_credentials_cache()

    def tearDown(self):
        clear_server_credentials_cache()

    def test_parse_unc_share_root(self):
        # 标准反斜杠 UNC
        unc1 = parse_unc_share_root(r"\\NAS\视频\动漫\番剧.mkv")
        self.assertEqual(unc1, UncShare(server="NAS", share="视频", normalized_root=r"\\NAS\视频"))

        # 正斜杠 UNC
        unc2 = parse_unc_share_root("//192.168.88.11/media/movies")
        self.assertEqual(unc2, UncShare(server="192.168.88.11", share="media", normalized_root=r"\\192.168.88.11\media"))

        # 仅服务器和共享名
        unc3 = parse_unc_share_root(r"\\MyServer\Share")
        self.assertEqual(unc3, UncShare(server="MyServer", share="Share", normalized_root=r"\\MyServer\Share"))

        # 普通本地盘符和相对路径
        self.assertIsNone(parse_unc_share_root("D:\\Media\\Movies"))
        self.assertIsNone(parse_unc_share_root("/mnt/downloads"))
        self.assertIsNone(parse_unc_share_root(r"\\OnlyServer"))
        self.assertIsNone(parse_unc_share_root(""))
        self.assertIsNone(parse_unc_share_root(None))

    def test_explain_windows_network_error(self):
        # 权限拒绝 (WinError 5)
        err5 = OSError()
        err5.winerror = 5
        msg5 = explain_windows_network_error(err5, r"\\NAS\视频")
        self.assertIn("代码 5", msg5)
        self.assertIn("权限不足", msg5)

        # 凭据错误 / 登录失败 (WinError 1326)
        err1326 = OSError()
        err1326.winerror = 1326
        msg1326 = explain_windows_network_error(err1326)
        self.assertIn("代码 1326", msg1326)
        self.assertIn("用户名或密码错误", msg1326)

        # 找不到网络路径 (WinError 53)
        err53 = OSError()
        err53.winerror = 53
        msg53 = explain_windows_network_error(err53)
        self.assertIn("代码 53", msg53)
        self.assertIn("IP 或主机名", msg53)

        # 普通 PermissionError
        perm_err = PermissionError("Permission denied")
        msg_perm = explain_windows_network_error(perm_err)
        self.assertIn("权限被拒绝", msg_perm)

    def test_ensure_smb_connection_bypasses_non_unc_or_local(self):
        # 普通本地路径安全返回 True
        ok, err = ensure_smb_connection("D:\\Media")
        self.assertTrue(ok)
        self.assertEqual(err, "")

        # 非 Windows 环境模拟
        with patch("app.modules.windows_smb.is_windows", return_value=False):
            ok, err = ensure_smb_connection(r"\\NAS\Share", "user", "pass")
            self.assertTrue(ok)
            self.assertEqual(err, "")

    def test_ensure_smb_connection_fallback_sequence_with_credentials(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            # 模拟第1次 user 登录返回 1326，第2次 server\user 成功返回 0
            attempts = []
            def fake_add_conn(res_ptr, pwd, user, flags):
                attempts.append(user)
                if user == "nasuser":
                    return 1326
                if user == r"NAS\nasuser":
                    return 0
                return 1326

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Video", "nasuser", "secret123")
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertEqual(attempts, ["nasuser", r"NAS\nasuser"])

    def test_ensure_smb_connection_fallback_to_local_user_and_auth_error_message(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            # 所有尝试均返回 1326
            attempts = []
            def fake_add_conn(res_ptr, pwd, user, flags):
                attempts.append(user)
                return 1326

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Video", "nasuser", "wrongpwd")
            self.assertFalse(ok)
            self.assertIn("认证失败：用户名或密码错误", err)
            self.assertIn(r"NAS\nasuser", err)
            self.assertEqual(attempts, ["nasuser", r"NAS\nasuser", r".\nasuser"])

    def test_ensure_smb_connection_1219_credential_conflict_cancels_and_retries(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            canceled = []
            mock_mpr.WNetCancelConnection2W.side_effect = lambda target, flags, force: canceled.append(target) or 0

            call_count = [0]
            def fake_add_conn(res_ptr, pwd, user, flags):
                call_count[0] += 1
                if call_count[0] == 1:
                    return 1219  # 首次报凭据冲突
                return 0  # 断开后重试成功

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Video", "admin", "pwd")
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertIn(r"\\NAS\Video", canceled)
            self.assertIn(r"\\NAS\IPC$", canceled)

    def test_ensure_smb_connection_anonymous_requires_auth_message(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("ctypes.WinDLL") as mock_windll, \
             patch("app.modules.windows_smb._try_impersonate_desktop_user_for_smb", return_value=False):
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            # 默认连接返回 1326，guest 登录返回 5 (Access Denied)
            def fake_add_conn(res_ptr, pwd, user, flags):
                if user is None:
                    return 1326
                if user == "guest":
                    return 5
                return 5

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Video")
            self.assertFalse(ok)
            self.assertIn("需要身份验证", err)
            self.assertIn("「NAS 访问账号」与「NAS 访问密码」", err)

    def test_ensure_smb_connection_anonymous_guest_disabled_1331_triggers_auth_required_message(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("ctypes.WinDLL") as mock_windll, \
             patch("app.modules.windows_smb._try_impersonate_desktop_user_for_smb", return_value=False):
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            # 默认连接返回 1244，guest 登录返回 1331 (ERROR_ACCOUNT_DISABLED)
            def fake_add_conn(res_ptr, pwd, user, flags):
                if user is None:
                    return 1244
                if user == "guest":
                    return 1331
                return 1331

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Download")
            self.assertFalse(ok)
            self.assertIn("需要身份验证", err)
            self.assertIn("Guest", err)
            self.assertIn("「NAS 访问账号」与「NAS 访问密码」", err)

    def test_ensure_smb_connection_explicit_user_account_disabled_1331(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            # 模拟用户指定了账号，但 NAS 该账号已被禁用返回 1331
            def fake_add_conn(res_ptr, pwd, user, flags):
                return 1331

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Download", "disabled_user", "password123")
            self.assertFalse(ok)
            self.assertIn("登录失败：该 NAS 账号已被停用或禁用", err)
            self.assertIn("NAS 用户管理后台", err)

    def test_server_credentials_cache_and_matching(self):
        set_server_credentials("NAS.LAN", "admin", "secret")
        # 直接匹配与大小写不敏感
        self.assertEqual(get_server_credentials("nas.lan"), ("admin", "secret"))
        self.assertEqual(get_server_credentials("NAS.LAN"), ("admin", "secret"))
        # 主机名前缀匹配 (FQDN <-> Short name)
        self.assertEqual(get_server_credentials("NAS"), ("admin", "secret"))
        self.assertEqual(get_server_credentials("nas"), ("admin", "secret"))
        # 未匹配的主机
        self.assertEqual(get_server_credentials("OTHER_NAS"), ("", ""))

    def test_server_credentials_db_fallback(self):
        mock_source = MagicMock()
        mock_source.local_root = r"\\MyNas.local\Media"
        mock_source.smb_user = "nasuser"
        mock_source.smb_pass = "naspass"
        mock_source.targets = []

        with patch("app.database.list_local_media_sources", return_value=[mock_source]):
            creds = get_server_credentials("mynas")
            self.assertEqual(creds, ("nasuser", "naspass"))
            # 验证已被放入进程缓存
            self.assertEqual(get_server_credentials("MyNas.local"), ("nasuser", "naspass"))

    def test_resolve_drive_or_unc_path(self):
        mapped = {"X:": r"\\NAS\Video", "W:": r"\\192.168.1.100\Shared"}
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb.get_windows_mapped_network_drives", return_value=mapped):
            self.assertEqual(resolve_drive_or_unc_path(r"X:\Anime\Season1"), r"\\NAS\Video\Anime\Season1")
            self.assertEqual(resolve_drive_or_unc_path(r"X:"), r"\\NAS\Video")
            self.assertEqual(resolve_drive_or_unc_path(r"W:\Movies"), r"\\192.168.1.100\Shared\Movies")
            self.assertEqual(resolve_drive_or_unc_path(r"C:\LocalFolder"), r"C:\LocalFolder")
            self.assertEqual(resolve_drive_or_unc_path(r"\\Direct\Unc\Path"), r"\\Direct\Unc\Path")

    def test_browse_local_directories_translates_windows_network_oserror(self):
        from app.modules.local_directory_browser import browse_local_directories

        net_err = OSError()
        net_err.winerror = 53

        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb.ensure_smb_connection", return_value=(True, "")), \
             patch("pathlib.Path.is_dir", return_value=True), \
             patch("pathlib.Path.exists", return_value=True), \
             patch("pathlib.Path.iterdir", side_effect=net_err):
            with self.assertRaises(ValueError) as ctx:
                browse_local_directories(r"\\NAS\Video", allowed_root=Path(r"\\NAS\Video"))
            self.assertIn("代码 53", str(ctx.exception))
            self.assertIn("IP 或主机名", str(ctx.exception))


class FindExplorerPidTests(unittest.TestCase):
    """_find_explorer_pid 函数纯逻辑分支覆盖（mock 掉全部 ctypes 调用）。"""

    def test_returns_none_on_non_windows(self):
        with patch("app.modules.windows_smb.is_windows", return_value=False):
            self.assertIsNone(_find_explorer_pid())

    def test_returns_none_when_snapshot_fails(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_kernel32 = MagicMock()
            mock_windll.return_value = mock_kernel32
            # CreateToolhelp32Snapshot 返回无效句柄值
            mock_kernel32.CreateToolhelp32Snapshot.return_value = None
            self.assertIsNone(_find_explorer_pid())

    def test_returns_none_when_no_explorer_in_process_list(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_kernel32 = MagicMock()
            mock_windll.return_value = mock_kernel32
            mock_kernel32.CreateToolhelp32Snapshot.return_value = 12345

            # Process32FirstW 返回 True 但进程名不匹配，Process32NextW 返回 False
            call_count = [0]
            def fake_first(snap, entry_ref):
                entry_ref._obj.szExeFile = "svchost.exe"
                return True

            def fake_next(snap, entry_ref):
                call_count[0] += 1
                if call_count[0] == 1:
                    entry_ref._obj.szExeFile = "notepad.exe"
                    return True
                return False

            mock_kernel32.Process32FirstW.side_effect = fake_first
            mock_kernel32.Process32NextW.side_effect = fake_next

            self.assertIsNone(_find_explorer_pid())
            mock_kernel32.CloseHandle.assert_called()

    def test_returns_none_when_process32first_fails(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_kernel32 = MagicMock()
            mock_windll.return_value = mock_kernel32
            mock_kernel32.CreateToolhelp32Snapshot.return_value = 12345
            mock_kernel32.Process32FirstW.return_value = False

            self.assertIsNone(_find_explorer_pid())
            mock_kernel32.CloseHandle.assert_called()

    def test_returns_none_on_exception(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("ctypes.WinDLL", side_effect=OSError("dll not found")):
            self.assertIsNone(_find_explorer_pid())


class ImpersonateDesktopUserTests(unittest.TestCase):
    """_try_impersonate_desktop_user_for_smb 函数纯逻辑分支覆盖。"""

    def test_returns_false_on_non_windows(self):
        with patch("app.modules.windows_smb.is_windows", return_value=False):
            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))

    def test_returns_false_when_no_explorer_pid(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=None):
            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))

    def test_returns_false_when_open_process_fails(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=1234), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_kernel32 = MagicMock()
            mock_advapi32 = MagicMock()
            mock_windll.side_effect = lambda name, **kw: mock_kernel32 if "kernel32" in name else mock_advapi32
            # OpenProcess 返回 0（失败）
            mock_kernel32.OpenProcess.return_value = 0

            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))

    def test_returns_false_when_open_process_token_fails(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=1234), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_kernel32 = MagicMock()
            mock_advapi32 = MagicMock()
            mock_windll.side_effect = lambda name, **kw: mock_kernel32 if "kernel32" in name else mock_advapi32
            mock_kernel32.OpenProcess.return_value = 42  # 有效句柄
            mock_advapi32.OpenProcessToken.return_value = False

            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))
            # 确保 proc_handle 被关闭
            mock_kernel32.CloseHandle.assert_called_with(42)

    def test_returns_false_when_duplicate_token_fails(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=1234), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_kernel32 = MagicMock()
            mock_advapi32 = MagicMock()
            mock_windll.side_effect = lambda name, **kw: mock_kernel32 if "kernel32" in name else mock_advapi32
            mock_kernel32.OpenProcess.return_value = 42
            mock_advapi32.OpenProcessToken.return_value = True
            mock_advapi32.DuplicateTokenEx.return_value = False

            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))
            # 确保 token 和 proc_handle 都被关闭
            self.assertGreaterEqual(mock_kernel32.CloseHandle.call_count, 2)

    def test_returns_false_when_impersonate_fails(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=1234), \
             patch("ctypes.WinDLL") as mock_windll:
            mock_kernel32 = MagicMock()
            mock_advapi32 = MagicMock()
            mock_windll.side_effect = lambda name, **kw: mock_kernel32 if "kernel32" in name else mock_advapi32
            mock_kernel32.OpenProcess.return_value = 42
            mock_advapi32.OpenProcessToken.return_value = True
            mock_advapi32.DuplicateTokenEx.return_value = True
            mock_advapi32.ImpersonateLoggedOnUser.return_value = False

            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))
            # 确保所有句柄被关闭（dup_token, token, proc_handle）
            self.assertGreaterEqual(mock_kernel32.CloseHandle.call_count, 3)

    def test_returns_true_when_path_exists_after_impersonation(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=1234), \
             patch("ctypes.WinDLL") as mock_windll, \
             patch("pathlib.Path.exists", return_value=True):
            mock_kernel32 = MagicMock()
            mock_advapi32 = MagicMock()
            mock_windll.side_effect = lambda name, **kw: mock_kernel32 if "kernel32" in name else mock_advapi32
            mock_kernel32.OpenProcess.return_value = 42
            mock_advapi32.OpenProcessToken.return_value = True
            mock_advapi32.DuplicateTokenEx.return_value = True
            mock_advapi32.ImpersonateLoggedOnUser.return_value = True

            self.assertTrue(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))
            # RevertToSelf 必须被调用
            mock_advapi32.RevertToSelf.assert_called_once()
            # 所有句柄都应被关闭
            self.assertGreaterEqual(mock_kernel32.CloseHandle.call_count, 3)

    def test_returns_false_when_path_not_exists_after_impersonation(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=1234), \
             patch("ctypes.WinDLL") as mock_windll, \
             patch("pathlib.Path.exists", return_value=False):
            mock_kernel32 = MagicMock()
            mock_advapi32 = MagicMock()
            mock_windll.side_effect = lambda name, **kw: mock_kernel32 if "kernel32" in name else mock_advapi32
            mock_kernel32.OpenProcess.return_value = 42
            mock_advapi32.OpenProcessToken.return_value = True
            mock_advapi32.DuplicateTokenEx.return_value = True
            mock_advapi32.ImpersonateLoggedOnUser.return_value = True

            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))
            mock_advapi32.RevertToSelf.assert_called_once()

    def test_returns_false_on_general_exception(self):
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("app.modules.windows_smb._find_explorer_pid", return_value=1234), \
             patch("ctypes.WinDLL", side_effect=OSError("dll not found")):
            self.assertFalse(_try_impersonate_desktop_user_for_smb(r"\\NAS\Share"))


class EnsureSmbImpersonationFallbackTests(unittest.TestCase):
    """测试 ensure_smb_connection 中匿名/Guest 失败后的桌面用户令牌模拟兜底路径。"""

    def setUp(self):
        clear_server_credentials_cache()

    def tearDown(self):
        clear_server_credentials_cache()

    def test_anonymous_auth_failure_falls_back_to_impersonation_success(self):
        """匿名和 Guest 均返回 AUTH_REQUIRED 后，令牌模拟成功则整体返回 True。"""
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("ctypes.WinDLL") as mock_windll, \
             patch("app.modules.windows_smb._try_impersonate_desktop_user_for_smb", return_value=True) as mock_imp:
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            def fake_add_conn(res_ptr, pwd, user, flags):
                if user is None:
                    return 1331
                if user == "guest":
                    return 1331
                return 1331

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Download")
            self.assertTrue(ok)
            self.assertEqual(err, "")
            mock_imp.assert_called_once_with(r"\\NAS\Download")

    def test_anonymous_auth_failure_impersonation_also_fails(self):
        """匿名和 Guest 均返回 AUTH_REQUIRED 且令牌模拟也失败时，返回认证错误消息。"""
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("ctypes.WinDLL") as mock_windll, \
             patch("app.modules.windows_smb._try_impersonate_desktop_user_for_smb", return_value=False) as mock_imp:
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            def fake_add_conn(res_ptr, pwd, user, flags):
                if user is None:
                    return 1326
                if user == "guest":
                    return 1331
                return 1331

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Download")
            self.assertFalse(ok)
            self.assertIn("需要身份验证", err)
            mock_imp.assert_called_once_with(r"\\NAS\Download")

    def test_non_auth_error_does_not_attempt_impersonation(self):
        """当匿名失败错误码不在 AUTH_REQUIRED_WINERRORS 中时，不尝试令牌模拟。"""
        with patch("app.modules.windows_smb.is_windows", return_value=True), \
             patch("pathlib.Path.exists", return_value=False), \
             patch("ctypes.WinDLL") as mock_windll, \
             patch("app.modules.windows_smb._try_impersonate_desktop_user_for_smb") as mock_imp:
            mock_mpr = MagicMock()
            mock_windll.return_value = mock_mpr

            # 返回错误码 53 (找不到网络路径)，不在 AUTH_REQUIRED_WINERRORS 中
            def fake_add_conn(res_ptr, pwd, user, flags):
                return 53

            mock_mpr.WNetAddConnection2W.side_effect = fake_add_conn

            ok, err = ensure_smb_connection(r"\\NAS\Download")
            self.assertFalse(ok)
            self.assertIn("IP 或主机名", err)
            mock_imp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
