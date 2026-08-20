#!/usr/bin/env python3
"""为冻结构建生成不可被运行时环境变量覆盖的包类型模块。"""
from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from typing import Sequence

PACKAGE_TYPES = ("source", "docker")


def render(package_type: str) -> str:
    if package_type not in PACKAGE_TYPES:
        raise ValueError(f"unsupported package type: {package_type}")
    return f'''"""构建阶段生成的不可变软件包元数据。

请仅在仓库外的 staging 副本中使用 ``packaging/scripts/generate_package_type.py``
生成；运行时环境变量不能改变该值。规范源码检出恒为 ``source``。
"""
from __future__ import annotations

from typing import Final, Literal

PackageType = Literal["source", "docker"]
PACKAGE_TYPE: Final[PackageType] = "{package_type}"
'''


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _require_staging_output(output: Path) -> Path:
    resolved = output.resolve(strict=False)
    project_root = _project_root()
    try:
        resolved.relative_to(project_root)
    except ValueError:
        return resolved
    raise ValueError(
        "构建元数据只能写入仓库外的 staging 副本；禁止覆盖规范源码树。"
    )


def write_module(output: Path, package_type: str) -> None:
    payload = render(package_type)
    output = _require_staging_output(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.",
        suffix=".tmp",
        dir=output.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            temporary.chmod(0o644)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-type", required=True, choices=PACKAGE_TYPES)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    write_module(args.output, args.package_type)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
