"""真实ID业务测试的只读事实和唯一计划执行夹具；不创建观察或会话存储。"""
from app.modules import guangya_workspace as workspace


def snapshot(client, *, path="/source", operation="list", recursive=False, max_items=2000, max_depth=64, **values):
    file = workspace.resolve_workspace_path(client, path) if path != "/" else None
    if operation == "stat":
        item = workspace.file_location(client, file.file_id if file is not None else "0")
        return {"root": item, "items": [item], "total": 1, "scan_complete": True}
    return workspace.list_files(client, file.file_id if file is not None else "0",
        recursive=recursive or operation in {"tree", "search"}, max_items=max_items, max_depth=max_depth,
        query=values.get("query", ""), kinds=values.get("kinds"))

def run_plan(client, plan, *, owner="owner"):
    from uuid import uuid4
    from app.modules import guangya_fs_change as fs

    fs.confirm_fs_change_plan(
        plan["plan_id"], owner=owner, expected_fingerprint=plan["fingerprint"]
    )
    job_id = uuid4().hex
    fs.bind_fs_change_plan_job(
        plan["plan_id"],
        owner_digest=plan["owner_digest"],
        expected_fingerprint=plan["fingerprint"],
        job_id=job_id,
        queue_until_epoch=10**12,
    )
    return fs.execute_fs_change_plan(
        {
            "version": 1,
            "plan_id": plan["plan_id"],
            "plan_fingerprint": plan["fingerprint"],
            "owner_digest": plan["owner_digest"],
            "credential_generation": client.credential_generation,
            "job_id": job_id,
        },
        client_factory=lambda: client,
    )
