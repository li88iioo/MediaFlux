#!/usr/bin/env python3
"""从 tag 生成可复现构建信息与统一产物名称。"""
from __future__ import annotations
import argparse, json, os, re, tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

PRERELEASE_IDENTIFIER = r"(?:0|[1-9]\d*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(
    rf"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    rf"(?:-({PRERELEASE_IDENTIFIER}(?:\.{PRERELEASE_IDENTIFIER})*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
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
    match=SEMVER_RE.fullmatch(value)
    if not match: raise ValueError(f'非法 SemVer tag：{ref}')
    return value.removeprefix('v')

def is_prerelease(version: str) -> bool:
    normalized = normalize_version(version)
    match = SEMVER_RE.fullmatch(normalized)
    assert match is not None
    return match.group(4) is not None


def docker_version_tag(version: str) -> str:
    """将 SemVer 映射为合法且有长度上限的 Docker 精确版本标签。"""
    tag = normalize_version(version).replace('+', '_')
    if not DOCKER_TAG_RE.fullmatch(tag):
        raise ValueError(f'版本无法映射为 Docker tag：{version}')
    return tag


def compare_versions(left: str, right: str) -> int:
    """按 SemVer precedence 比较两个版本，忽略 build metadata。"""
    left_version = normalize_version(left)
    right_version = normalize_version(right)
    left_match = SEMVER_RE.fullmatch(left_version)
    right_match = SEMVER_RE.fullmatch(right_version)
    assert left_match is not None and right_match is not None

    left_core = tuple(int(left_match.group(index)) for index in range(1, 4))
    right_core = tuple(int(right_match.group(index)) for index in range(1, 4))
    if left_core != right_core:
        return 1 if left_core > right_core else -1

    left_pre = left_match.group(4)
    right_pre = right_match.group(4)
    if left_pre is None or right_pre is None:
        if left_pre == right_pre:
            return 0
        return 1 if left_pre is None else -1

    left_parts = left_pre.split('.')
    right_parts = right_pre.split('.')
    for left_part, right_part in zip(left_parts, right_parts):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return 1 if int(left_part) > int(right_part) else -1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return 1 if left_part > right_part else -1
    if len(left_parts) == len(right_parts):
        return 0
    return 1 if len(left_parts) > len(right_parts) else -1

def artifact_name(version: str, platform: str, arch: str, package: str) -> str:
    arch={'amd64':'x86_64','arm64':'aarch64'}.get(arch,arch)
    if package=='docker': return f'MediaFlux-{version}-docker-{arch}'
    if package=='runtime': return f'MediaFlux-runtime-{version}-{platform}-{arch}.tar.gz'
    if package=='source': return f'MediaFlux-{version}-source.tar.gz'
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

def generate_release_manifest(directory: Path, version: str, commit: str, output: Path) -> Path:
    artifacts = []
    for path in sorted((item for item in directory.rglob("*") if item.is_file()), key=lambda item: item.relative_to(directory).as_posix()):
        if path.name in {"BUILD-INFO.json", "SHA256SUMS", "SHA256SUMS.sig", "SBOM.spdx.json"}:
            continue
        import hashlib
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        artifacts.append({"name": path.relative_to(directory).as_posix(), "sha256": hasher.hexdigest(), "size": path.stat().st_size})
    normalized_version = normalize_version(version)
    payload = {
        "name": "MediaFlux",
        "version": normalized_version,
        "commit": commit,
        "prerelease": is_prerelease(normalized_version),
        "artifacts": artifacts,
    }
    output.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return output

def _changelog_section(changelog_text: str, version: str) -> str:
    """抽取 CHANGELOG.md 中指定版本段落（不含版本标题行），找不到返回空串。"""
    pattern = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.MULTILINE | re.DOTALL)
    match = pattern.search(changelog_text)
    return match.group(1).strip() if match else ""


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
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--ref',required=True); p.add_argument('--commit',required=True); p.add_argument('--platform',required=True); p.add_argument('--arch',required=True); p.add_argument('--package',required=True,choices=('docker','runtime','source')); p.add_argument('--output',type=Path,required=True); p.add_argument('--development',action='store_true'); p.add_argument('--build-time')
    a=p.parse_args(argv); info=generate_build_info(a.ref,a.commit,a.platform,a.arch,a.package,build_time=a.build_time,release=not a.development); write_build_info(a.output,info); print(info.artifact_name); return 0
if __name__=='__main__': raise SystemExit(main())
