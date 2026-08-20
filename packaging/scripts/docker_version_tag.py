#!/usr/bin/env python3
"""输出 SemVer 对应的 Docker 精确版本标签，并校验字符与长度边界。"""
from __future__ import annotations

import argparse

from generate_build_info import docker_version_tag


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="待映射的 SemVer")
    args = parser.parse_args()
    print(docker_version_tag(args.version))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
