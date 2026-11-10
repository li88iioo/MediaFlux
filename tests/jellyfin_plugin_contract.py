"""把 C# HTTP 测试导出的真实插件请求送入真实 MF 校验/云盘规划（仅隔离夹具）。

用法：python -B -m tests.jellyfin_plugin_contract <MEDIAFLUX_DELETE_CONTRACT_DIR>
"""
from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

from app.modules import jellyfin_delete as deletion
from app.modules.strm import STRM_SUBDIR
from app.repositories.strm import upsert_strm_index
from tests.test_jellyfin_delete import fingerprint
from tests.test_jellyfin_scoped_delete import JellyfinScopedDeleteTests
from tests.support import initialized_web_runtime, isolated_test_database


def validate_contract(document: dict) -> dict:
    case = JellyfinScopedDeleteTests(methodName="runTest")
    try:
        case.setUp()
        root = Path(document["root"])
        case.fx.values.update({
            "GY_STRM_SOURCE_DIRS": json.dumps([
                {"id": "source-a", "name": "Source"},
                {"id": "source-b", "name": "Unused source"},
            ]),
            "JELLYFIN_PATH_MAPPINGS": json.dumps([{
                "local": str(case.fx.media_root.parent), "server": str(root),
            }]),
        })
        directories: dict[tuple[str, ...], str] = {(): "source-a"}
        file_ids = set()
        for remote, encoded in document["files"].items():
            relative = Path(remote).relative_to(root)
            if relative.parts[:2] != (STRM_SUBDIR, "Source"):
                raise AssertionError("契约夹具必须位于专用 STRM 来源内")
            components = relative.parts[2:-1]
            for depth in range(1, len(components) + 1):
                key = components[:depth]
                if key not in directories:
                    directory_id = "directory-" + hashlib.sha256("/".join(key).encode()).hexdigest()[:12]
                    case.directory(directory_id, key[-1], directories[key[:-1]])
                    directories[key] = directory_id
            file_id = "video-" + hashlib.sha256(str(relative).encode()).hexdigest()[:12]
            file_ids.add(file_id)
            target = case.video(file_id, relative.stem + ".mkv", directories[components])
            contents = base64.b64decode(encoded, validate=True)
            target.write_bytes(contents)
            node = case.cloud.nodes[file_id]
            upsert_strm_index("guangya:source-a", file_id, node.etag, node.size,
                              node.name, str(target), fingerprint(contents))
        payload = document["payload"]
        # 不重写 scopes、paths、指纹或它们的先后顺序；映射只隔离文件系统根。
        targets = deletion.prepare_delete(payload)
        result = deletion.execute_delete(payload, targets)
        case.assert_success(result, file_ids)
        return {"ok": result["ok"], "media_deleted": result["stats"]["media_deleted"],
                "scopes": [scope["kind"] for scope in payload["scopes"]]}
    finally:
        case.doCleanups()


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    required = {"folder", "movie-parts", "child-first", "folder-first"}
    files = {p.stem: p for p in directory.glob("*.json")}
    if not required <= files.keys():
        raise SystemExit("缺少真实 C# 导出的契约：" + ", ".join(sorted(required - files.keys())))
    with initialized_web_runtime(), isolated_test_database():
        for name in sorted(required):
            print(json.dumps({"contract": name, **validate_contract(json.loads(files[name].read_text()))}, ensure_ascii=False))
