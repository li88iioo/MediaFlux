"""为需要被局域网设备访问的 URL 生成保守候选。"""
from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Callable, Iterable

_PRIVATE_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
_ROUTE_PROBES = (
    ("192.0.2.1", 9),
    ("198.51.100.1", 9),
    ("223.5.5.5", 53),
    ("119.29.29.29", 53),
    ("8.8.8.8", 53),
    ("1.1.1.1", 53),
)


def _is_container() -> bool:
    if os.environ.get("MEDIAFLUX_CONTAINER", "").strip().lower() in {"1", "true", "yes"}:
        return True
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods", "podman"))


def _private_ipv4(value: object) -> str | None:
    try:
        address = ipaddress.ip_address(str(value or "").split("%", 1)[0])
    except ValueError:
        return None
    if not isinstance(address, ipaddress.IPv4Address):
        return None
    if any(address in network for network in _PRIVATE_IPV4_NETWORKS):
        return str(address)
    return None


def _hostname_addresses() -> Iterable[str]:
    addresses: list[str] = []
    hostname = socket.gethostname()
    try:
        rows = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
        addresses.extend(str(row[4][0]) for row in rows if row and row[4])
    except OSError:
        pass
    try:
        _, _, ip_list = socket.gethostbyname_ex(hostname)
        addresses.extend(str(ip) for ip in ip_list if ip)
    except OSError:
        pass
    return tuple(addresses)


def _linux_proc_addresses(proc_path: Path | None = None) -> Iterable[str]:
    fib_trie = proc_path or Path("/proc/net/fib_trie")
    if not fib_trie.is_file():
        return ()
    addresses: list[str] = []
    try:
        content = fib_trie.read_text(encoding="utf-8", errors="ignore")
        current_ip = None
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if line.startswith("|-- ") or line.startswith("+-- "):
                parts = line.split()
                if len(parts) >= 2:
                    candidate = parts[1].split("/")[0]
                    if candidate.replace(".", "").isdigit():
                        current_ip = candidate
            elif "/32 host LOCAL" in line or "32 host LOCAL" in line:
                if current_ip:
                    addresses.append(current_ip)
    except OSError:
        pass
    return tuple(addresses)


def _route_addresses() -> Iterable[str]:
    addresses: list[str] = []
    for target in _ROUTE_PROBES:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(target)
            addresses.append(str(sock.getsockname()[0]))
        except OSError:
            continue
        finally:
            sock.close()
    return tuple(addresses)


def discover_lan_ipv4_addresses(
    *,
    hostname_source: Callable[[], Iterable[str]] = _hostname_addresses,
    route_source: Callable[[], Iterable[str]] = _route_addresses,
    proc_source: Callable[[], Iterable[str]] = _linux_proc_addresses,
) -> list[str]:
    """返回去重、稳定排序的 RFC1918 IPv4 候选。"""
    addresses: set[str] = set()
    for raw in (*tuple(hostname_source()), *tuple(route_source()), *tuple(proc_source())):
        normalized = _private_ipv4(raw)
        if normalized:
            addresses.add(normalized)
    return sorted(addresses, key=lambda item: int(ipaddress.ip_address(item)))


def build_lan_url_candidates(
    *,
    bind_host: str,
    port: int,
    container: bool | None = None,
    addresses: Iterable[str] | None = None,
) -> dict[str, object]:
    """根据监听状态构造候选；容器环境不猜测宿主机地址。"""
    normalized_host = str(bind_host or "127.0.0.1").strip()
    normalized_port = max(1, min(int(port), 65535))
    in_container = _is_container() if container is None else bool(container)
    warnings: list[str] = []
    candidates: list[dict[str, str]] = []

    if normalized_host not in {"0.0.0.0", "::"}:
        warnings.append("lan_binding_disabled")
    elif in_container:
        warnings.append("container_address_unreliable")
    else:
        discovered = discover_lan_ipv4_addresses() if addresses is None else [
            value for raw in addresses if (value := _private_ipv4(raw))
        ]
        for address in dict.fromkeys(discovered):
            candidates.append({
                "url": f"http://{address}:{normalized_port}",
                "source": "lan_ipv4",
                "label": "本机局域网",
            })
        if not candidates:
            warnings.append("no_lan_address")
        elif len(candidates) > 1:
            warnings.append("multiple_lan_addresses")

    return {
        "bind": {"host": normalized_host, "port": normalized_port},
        "candidates": candidates,
        "warnings": warnings,
    }
