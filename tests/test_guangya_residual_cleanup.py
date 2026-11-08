"""只在内存中模拟云盘，供公共清理与原生确认行为测试使用。"""
from copy import deepcopy
from app.clients.guangya import GuangYaFile
from tests.support import PagedDirectoryTestMixin

class FakeCleanupClient(PagedDirectoryTestMixin):
    def __init__(self):
        self.logged_in = True
        self.credential_generation = 13
        self.closed = False
        self.next_id = 100
        self.directories = {
            "0": [GuangYaFile("source", "整理源", True, parent_id="0", etag="src")],
            "source": [
                GuangYaFile(
                    "empty", "空目录", True, parent_id="source", etag="e1", updated_at=1
                ),
                GuangYaFile(
                    "residual", "a", True, parent_id="source", etag="e2", updated_at=2
                ),
                GuangYaFile(
                    "keep",
                    "保留元数据",
                    True,
                    parent_id="source",
                    etag="e3",
                    updated_at=3,
                ),
                GuangYaFile(
                    "video_dir",
                    "媒体目录",
                    True,
                    parent_id="source",
                    etag="e4",
                    updated_at=4,
                ),
            ],
            "empty": [],
            "residual": [
                GuangYaFile(
                    "junk",
                    "xxx.png",
                    False,
                    parent_id="residual",
                    size=10,
                    etag="j",
                    extension="png",
                )
            ],
            "keep": [
                GuangYaFile(
                    "poster",
                    "poster.jpg",
                    False,
                    parent_id="keep",
                    size=10,
                    etag="p",
                    extension="jpg",
                )
            ],
            "video_dir": [
                GuangYaFile(
                    "video",
                    "ABC-123.mp4",
                    False,
                    parent_id="video_dir",
                    size=100,
                    etag="v",
                    extension="mp4",
                )
            ],
        }

    def list_dir(self, parent_id="0"):
        return [deepcopy(item) for item in self.directories.get(str(parent_id), [])]

    def file_info(self, file_id):
        for items in self.directories.values():
            for item in items:
                if item.file_id == str(file_id):
                    return deepcopy(item)
        return None

    def create_dir(self, name, parent_id="0"):
        self.next_id += 1
        file_id = f"new-{self.next_id}"
        self.directories.setdefault(str(parent_id), []).append(
            GuangYaFile(
                file_id,
                str(name),
                True,
                parent_id=str(parent_id),
                etag=f"etag-{file_id}",
                updated_at=self.next_id,
            )
        )
        self.directories[file_id] = []
        return file_id

    def move(self, file_ids, parent_id):
        for file_id in file_ids:
            found = None
            for items in self.directories.values():
                for index, item in enumerate(items):
                    if item.file_id == str(file_id):
                        found = items.pop(index)
                        break
                if found is not None:
                    break
            if found is None:
                raise RuntimeError("missing")
            found.parent_id = str(parent_id)
            self.directories.setdefault(str(parent_id), []).append(found)
        return True

    def delete_empty_directory(
        self, file_id, *, expected_etag="", expected_updated_at=0
    ):
        if self.directories.get(str(file_id)):
            raise RuntimeError("not empty")
        for items in self.directories.values():
            for index, item in enumerate(items):
                if item.file_id == str(file_id):
                    if expected_etag and item.etag != expected_etag:
                        raise RuntimeError("stale")
                    items.pop(index)
                    self.directories.pop(str(file_id), None)
                    return True
        raise RuntimeError("missing")

    def close(self):
        self.closed = True
