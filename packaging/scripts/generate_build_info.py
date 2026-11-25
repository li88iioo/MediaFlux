#!/usr/bin/env python3
"""从 tag 生成可复现构建信息与统一产物名称。"""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

# 此脚本也支持从仓库之外直接执行。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.semver import version_order

DOCKER_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")

@dataclass(frozen=True)
class GeneratedBuildInfo:
    name: str; version: str; commit: str; build_time: str; platform: str; arch: str
    package: str; artifact_name: str; prerelease: bool
    def as_dict(self): return asdict(self)

def normalize_version(ref: str, *, release: bool = True) -> str:
    value=(ref or '').strip()
    if not value:
        if release: raise ValueError('正式构建必须提供版本 tag')
        return '0.0.0-dev'
    version_order(value)
    return value.removeprefix('v')

def is_prerelease(version: str) -> bool:
    return not version_order(version).stable


def docker_version_tag(version: str) -> str:
    """将 SemVer 映射为合法且有长度上限的 Docker 精确版本标签。"""
    tag = normalize_version(version).replace('+', '_')
    if not DOCKER_TAG_RE.fullmatch(tag):
        raise ValueError(f'版本无法映射为 Docker tag：{version}')
    return tag


def compare_versions(left: str, right: str) -> int:
    """按 SemVer precedence 比较两个版本，忽略 build metadata。"""
    first, second = version_order(left), version_order(right)
    return (first > second) - (first < second)


def artifact_name(version: str, platform: str, arch: str, package: str) -> str:
    arch={'amd64':'x86_64','arm64':'aarch64'}.get(arch,arch)
    if package=='docker': return f'MediaFlux-{version}-docker-{arch}'
    raise ValueError(f'不支持的产物类型：{package}')

def generate_build_info(ref: str, commit: str, platform: str, arch: str, package: str, *, build_time: str|None=None, release: bool=True) -> GeneratedBuildInfo:
    version=normalize_version(ref,release=release)
    timestamp=build_time or os.getenv('SOURCE_DATE_EPOCH')
    if timestamp and timestamp.isdigit():
        timestamp=datetime.fromtimestamp(int(timestamp),timezone.utc).isoformat().replace('+00:00','Z')
    elif not timestamp:
        timestamp=datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00','Z')
    return GeneratedBuildInfo('MediaFlux',version,commit.strip(),timestamp,platform,arch,package,artifact_name(version,platform,arch,package),is_prerelease(version))

def write_build_info(path: Path, info: GeneratedBuildInfo) -> None:
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(prefix=f'.{path.name}.',suffix='.tmp',dir=path.parent,text=True)
    try:
        with os.fdopen(fd,'w',encoding='utf-8',newline='\n') as f:
            json.dump(info.as_dict(),f,ensure_ascii=False,sort_keys=True,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
        os.replace(tmp,path)
    finally: Path(tmp).unlink(missing_ok=True)

def _changelog_section(changelog_text: str, version: str) -> str:
    """抽取带 ISO 日期的正式版本段落；标题或正文不合规时返回空串。"""
    pattern = re.compile(
        rf"^## \[{re.escape(version)}\] - (\d{{4}}-\d{{2}}-\d{{2}})[ \t]*\n"
        r"(.*?)(?=^## \[|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(changelog_text)
    if match is None:
        return ""
    try:
        date.fromisoformat(match.group(1))
    except ValueError:
        return ""
    return match.group(2).strip()


def generate_release_notes(ref: str, repo: str, changelog_path: Path, output: Path) -> Path:
    """生成 Release 页 notes：标题 + Docker 运行说明 + 从 CHANGELOG.md 抽取的当版变化。"""
    version = normalize_version(ref)
    tag = docker_version_tag(version)
    parts = [
        f"MediaFlux {ref}",
        "",
        "## 容器镜像快速启动",
        "",
        "```bash",
        f"docker pull ghcr.io/{repo.lower()}:{tag}",
        "docker compose up -d",
        "```",
        "",
    ]
    try:
        changelog_section = _changelog_section(changelog_path.read_text(encoding="utf-8"), version)
    except OSError:
        changelog_section = ""
    if changelog_section:
        parts += ["## 本版变化", "", changelog_section, ""]
    parts += [
        "部署与配置说明详见仓库 README.md 与 docs/部署指南.md。",
        "",
    ]
    output.write_text("\n".join(parts), encoding="utf-8")
    return output


def main(argv: Sequence[str]|None=None)->int:
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--ref',required=True); p.add_argument('--commit',required=True); p.add_argument('--platform',required=True); p.add_argument('--arch',required=True); p.add_argument('--package',required=True,choices=('docker',)); p.add_argument('--output',type=Path,required=True); p.add_argument('--development',action='store_true'); p.add_argument('--build-time')
    a=p.parse_args(argv); info=generate_build_info(a.ref,a.commit,a.platform,a.arch,a.package,build_time=a.build_time,release=not a.development); write_build_info(a.output,info); print(info.artifact_name); return 0
if __name__=='__main__': raise SystemExit(main())
