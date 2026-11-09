"""普通 Python/Shell 的单次监督器：统一使用系统 Bubblewrap，无 root 或 Docker 调用。

文件、PID、用户及网络由 Bubblewrap 隔离；子进程再加载 seccomp 和资源限制。
取消、超时及父进程退出均结束隔离进程树。正式业务只能通过工作区 SDK 请求。
"""
import argparse
import ctypes
import errno
import json
import math
import os
from pathlib import Path
import resource
import signal
import shutil
import sysconfig
import subprocess
import sys
import time

LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong,
                      ctypes.c_ulong, ctypes.c_ulong]
LIBC.prctl.restype = ctypes.c_int
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/work",
       "TMPDIR": "/tmp", "LANG": "C.UTF-8", "PYTHONPATH": "/opt/mf",
       "PYTHONHOME": "/usr/local", "LD_LIBRARY_PATH": "/usr/local/lib",
       # 避免每个线程的 malloc arena 预留耗尽 512 MiB 虚拟内存额度。
       "MALLOC_ARENA_MAX": "2"}
DENY = ("socket", "connect", "bind", "listen", "accept", "accept4", "sendmsg", "sendmmsg",
        "ptrace", "process_vm_readv", "process_vm_writev", "pidfd_getfd",
        "setsid", "setpgid", "unshare", "setns", "mount", "umount2", "pivot_root",
        "chroot", "open_by_handle_at", "io_uring_setup", "io_uring_enter", "io_uring_register")


def prctl(option, value):
    if LIBC.prctl(option, value, 0, 0, 0):
        raise OSError(ctypes.get_errno(), "prctl")


def seccomp():
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        for name in DENY:
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number == -1:
                raise RuntimeError(f"unsupported syscall: {name}")
            rc = lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM, number, 0)
            if rc:
                raise RuntimeError(f"seccomp rule {name}: {rc}")
        # clone3 的 flags 在指针结构中，seccomp 无法检查；ENOSYS 让 libc 回退 clone。
        rc = lib.seccomp_rule_add(context, 0x00050000 | errno.ENOSYS,
            lib.seccomp_syscall_resolve_name(b"clone3"), 0)
        if rc:
            raise RuntimeError(f"seccomp clone3: {rc}")

        class Compare(ctypes.Structure):
            _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                        ("mask", ctypes.c_uint64), ("value", ctypes.c_uint64)]

        for name in (b"socketpair",):
            rc = lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM,
                lib.seccomp_syscall_resolve_name(name), 1, Compare(0, 1, 1, 0))  # family != AF_UNIX
            if rc:
                raise RuntimeError(f"seccomp address family: {rc}")
        # socketpair 的 send() 使用目的地址为 NULL 的 sendto；禁止另给目标地址。
        rc = lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM,
            lib.seccomp_syscall_resolve_name(b"sendto"), 1, Compare(4, 1, 0, 0))
        if rc:
            raise RuntimeError(f"seccomp sendto destination: {rc}")
        for flag in (0x00020000, 0x02000000, 0x04000000, 0x08000000,
                     0x10000000, 0x20000000, 0x40000000):
            rc = lib.seccomp_rule_add(context, 0x00050000 | errno.EPERM,
                lib.seccomp_syscall_resolve_name(b"clone"), 1, Compare(0, 7, flag, flag))
            if rc:
                raise RuntimeError(f"seccomp clone namespace: {rc}")
        rc = lib.seccomp_load(context)
        if rc:
            raise RuntimeError(f"seccomp_load: {rc}")
    finally:
        lib.seccomp_release(context)



def bubblewrap():
    executable = shutil.which("bwrap")
    if not executable:
        raise RuntimeError("缺少 bubblewrap；请安装系统包 bubblewrap 和 libseccomp2")
    command = [executable, "--unshare-all", "--uid", "1000", "--gid", "1000",
               "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv"]
    # 只挂载系统解释器、标准库、共享库及常用 Shell 工具；不暴露应用、配置或宿主 home。
    for directory in ("/usr/lib", "/usr/lib64", "/lib", "/lib64"):
        if Path(directory).is_dir():
            command += ["--ro-bind", directory, directory]
    libdir = Path(sysconfig.get_config_var("LIBDIR") or "/usr/local/lib")
    for library in sorted(libdir.glob("libpython*.so*")):
        command += ["--ro-bind", str(library.resolve()), str(library)]
    stdlib = Path(sysconfig.get_path("stdlib"))
    target = f"/usr/local/lib/python{sys.version_info.major}.{sys.version_info.minor}"
    command += ["--ro-bind", str(stdlib), target]
    for name in ("site-packages", "dist-packages"):
        if (stdlib / name).is_dir():
            command += ["--tmpfs", target + "/" + name, "--remount-ro", target + "/" + name]
    for name in ("sh", "sleep", "cat", "ls", "mkdir", "cp", "mv", "rm", "env", "true", "id",
                 "find", "grep", "sed", "awk", "head", "tail", "wc", "sort", "uniq", "cut", "tr",
                 "xargs", "tee", "touch", "date", "basename", "dirname", "readlink", "realpath",
                 "stat", "du", "sha256sum", "printf"):
        binary = shutil.which(name)
        if binary is None:
            raise RuntimeError(f"missing workspace command: {name}")
        command += ["--ro-bind", str(Path(binary).resolve()), "/usr/bin/" + name]
    command += ["--symlink", "usr/bin", "/bin",
                "--ro-bind", str(Path(sys.executable).resolve()), "/usr/local/bin/python",
                "--symlink", "python", "/usr/local/bin/python3",
                "--dev", "/dev", "--tmpfs", "/tmp"]
    for key, value in ENV.items():
        command += ["--setenv", key, value]
    return command


def enter(args):
    # 已在独立用户/PID/文件/网络命名空间内，且所有 capabilities 已丢弃。
    os.umask(0o077)
    for key, value in ((resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, 64),
                       (resource.RLIMIT_NPROC, 32), (resource.RLIMIT_FSIZE, 64 << 20),
                       (resource.RLIMIT_AS, 512 << 20), (resource.RLIMIT_CPU, 10)):
        resource.setrlimit(key, (value, value))
    prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
    seccomp()
    os.execvpe(args.command[0], args.command, ENV)


def supervise(args):
    parent_pid = int(os.environ.get("MF_WORKSPACE_PARENT_PID", "0"))
    if parent_pid <= 0 or os.getppid() != parent_pid:
        raise RuntimeError("MF_WORKSPACE_PARENT_PID is missing or MF parent exited before startup")
    # PID 1 可以是正常的 MF 父进程；父端提供的 PID 可区分它与启动前已失去父进程的孤儿。
    # 设置前后核对父 PID；准备期间尚无 worker，父进程死亡可直接结束监督器。
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    if os.getppid() != parent_pid:
        raise RuntimeError("MF parent exited during supervisor startup")
    if not args.command:
        raise ValueError("command is required")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    root = Path(args.root).resolve(strict=True)
    prctl(36, 1)  # PR_SET_CHILD_SUBREAPER；只设置独立监督进程。
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, stop)
    if os.getppid() != parent_pid:
        return 143
    command = bubblewrap()
    for source, destination, writable in (
            (root / "work", "/work", True), (root / "output", "/output", False),
            (root / "run", "/run", False), (root / "run/mf-requests", "/run/mf-requests", True),
            (root / "opt/mf", "/opt/mf", False),
            (Path(__file__).parent.parent / "skills", "/skills", False)):
        command += ["--bind" if writable else "--ro-bind", str(source), destination]
    command += ["--ro-bind", __file__, "/launcher.py",
                "--symlink", "/opt/mf/cli", "/usr/local/bin/mf", "--chdir", "/work", "--remount-ro", "/",
                "/usr/local/bin/python", "-I", "-S", "-B", "/launcher.py", "--child", "--"]
    child = subprocess.Popen(command + args.command, close_fds=True, env=ENV, start_new_session=True)
    reason = "exited"
    deadline = time.monotonic() + args.timeout
    try:
        while True:
            # WNOWAIT 保留 leader PID，killpg 前不会重用成别的进程组。
            if os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT):
                break
            if stopped or time.monotonic() >= deadline:
                reason = "cancelled" if stopped else "timeout"
                break
            time.sleep(0.01)
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        rc = child.wait(timeout=2)
        deadline = time.monotonic() + 2
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if not pid:
                if time.monotonic() >= deadline:
                    raise RuntimeError("descendants did not terminate; cleanup incomplete")
                time.sleep(0.01)
    return 143 if reason == "cancelled" else 124 if reason == "timeout" else rc


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root")
    parser.add_argument("--check", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--timeout", type=float, default=5)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ["--"]:
        args.command = args.command[1:]
    try:
        if args.check:
            command = bubblewrap() + ["--ro-bind", __file__, "/probe.py",
                "/usr/local/bin/python", "-I", "-S", "-B", "/probe.py", "--child", "--",
                "/usr/local/bin/python", "-I", "-S", "-c", "import os; assert os.getuid() == 1000"]
            sys.exit(subprocess.run(command, env=ENV, close_fds=True, timeout=8).returncode)
        elif args.child:
            enter(args)
        else:
            rc = supervise(args)
            if rc < 0:
                if -rc != signal.SIGKILL:
                    signal.signal(-rc, signal.SIG_DFL)
                os.kill(os.getpid(), -rc)
            sys.exit(rc)
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        sys.exit(125)
