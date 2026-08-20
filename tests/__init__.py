"""MediaFlux automated test package.

测试默认使用一次性运行目录与 SQLite 数据库，绝不读取或修改开发机的
``db/user.env``、``db/mediaflux.db``、缓存和日志。需要验证真实运行路径或
首次安装状态的测试，必须在用例内显式配置独立的 ``RuntimePaths``。

可选外部 LLM 路由同样默认关闭，避免真实网络请求或让确定性路由断言随
本机配置漂移。日志隔离和运行路径变量必须在应用模块导入前设置；LLM 覆盖
仍在配置模块加载后设置，避免被误判为部署环境托管项。
"""
from __future__ import annotations

import atexit
import os
import tempfile
from pathlib import Path

# 整个 unittest 进程共享一个隔离根目录；各测试仍可通过 configure_database()
# 或 configure_runtime_paths() 使用更细粒度的临时目录。
_TEST_RUNTIME = tempfile.TemporaryDirectory(
    prefix="mediaflux-test-runtime-", ignore_cleanup_errors=True
)
_TEST_ROOT = Path(_TEST_RUNTIME.name)
_TEST_DATA = _TEST_ROOT / "data"
_TEST_CONFIG = _TEST_ROOT / "config"
_TEST_CACHE = _TEST_ROOT / "cache"
_TEST_LOGS = _TEST_ROOT / "logs"
_TEST_STRM = _TEST_ROOT / "strm-data"
for _directory in (_TEST_DATA, _TEST_CONFIG, _TEST_CACHE, _TEST_LOGS, _TEST_STRM):
    _directory.mkdir(parents=True, exist_ok=True)

# 用文件而不是进程凭据覆盖提供已初始化测试状态，避免配置保存测试把这些
# 字段误判为不可修改的部署环境变量。
(_TEST_CONFIG / "user.env").write_text(
    "ENV_WEB_PASSPORT='admin' # mediaflux-literal\n"
    "ENV_WEB_PASSWORD='123456' # mediaflux-literal\n"
    "WEB_SECRET_KEY='mediaflux-unit-test-secret' # mediaflux-literal\n"
    "AGENT_ENABLED='1' # mediaflux-literal\n",
    encoding="utf-8",
)

# 测试包必须覆盖调用者遗留的部署变量；setdefault 会让 CI、IDE 或开发机
# 预置的真实运行目录绕过隔离，并在导入 app.config 时固化为生产路径。
os.environ["MEDIAFLUX_TESTING"] = "1"
os.environ["MEDIAFLUX_TEST_MODE"] = "1"
os.environ["MEDIAFLUX_TEST_DB_PATH"] = str(_TEST_DATA / "mediaflux-test.db")
os.environ["MEDIAFLUX_DATA_DIR"] = str(_TEST_DATA)
os.environ["MEDIAFLUX_CONFIG_DIR"] = str(_TEST_CONFIG)
os.environ["MEDIAFLUX_CACHE_DIR"] = str(_TEST_CACHE)
os.environ["MEDIAFLUX_LOG_DIR"] = str(_TEST_LOGS)
os.environ["MEDIAFLUX_STRM_DIR"] = str(_TEST_STRM)
os.environ["MEDIAFLUX_DISABLE_FILE_LOGGING"] = "1"

from app import config as _config  # noqa: F401  # capture startup overrides first

os.environ["AGENT_LLM_ENABLED"] = "0"


def _cleanup_test_runtime() -> None:
    _TEST_RUNTIME.cleanup()


atexit.register(_cleanup_test_runtime)
