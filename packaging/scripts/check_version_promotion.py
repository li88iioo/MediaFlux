#!/usr/bin/env python3
"""判断候选稳定版本是否可以覆盖当前 Docker mutable tags。"""
from __future__ import annotations

import argparse

from generate_build_info import compare_versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", help="候选 SemVer")
    parser.add_argument("current", help="当前 mutable tag 内嵌的 SemVer")
    args = parser.parse_args()
    comparison = compare_versions(args.candidate, args.current)
    if comparison < 0:
        print(f"candidate {args.candidate} is older than current {args.current}")
        return 1
    print(f"candidate {args.candidate} may promote over current {args.current}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
