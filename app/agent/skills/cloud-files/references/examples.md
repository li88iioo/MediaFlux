# 光鸭 SDK 场景示例

每节独立使用。Python 片段执行时先 `import json, mf`；片段中的 `file_id`、`log_id`、`candidate` 等输入须取自当前任务的真实查询结果，所在节说明来源。名称、路径、季集按用户任务调整。跨命令变量不保留，需先从 `/work` 中保存的 JSON 重新加载；写后的跟踪编号来自实际执行回执，不能从示例猜造。

## 01 连接与容量

“光鸭连得上吗？还剩多少空间？”在同一脚本分别读取：

```python
print(mf.cloud.connection())
print(mf.cloud.account())
```

容量字段单位为 byte；未知值保留未知，不换成 0。连接诊断不需要遍历目录。

## 02 定位目录、找文件、保存事实

“看看根目录下有什么”，读取一次，保留完整结果只展示必要字段：

```python
root = mf.cloud.list("0")
with open("/work/cloud-root.json", "w") as stream:
    json.dump(root, stream, ensure_ascii=False)
print({"total": root["total"], "sample": [
    (item["file_id"], item["name"], item["is_dir"]) for item in root["items"][:20]]})
```

已知用户给出的绝对目录路径时，直接定位并读取；也可传上一步的真实目录 ID：

```python
files = mf.cloud.list("/动漫/某剧", recursive=True, kinds=["video", "subtitle"])
with open("/work/cloud-files.json", "w") as stream:
    json.dump(files, stream, ensure_ascii=False)
print({"total": files["total"], "sample": [row["path"] for row in files["items"][:10]]})
```

`limit=None` 默认返回完整范围；多次筛选复用本地 JSON。`query` 是名称字面包含，不能把简繁、别名未命中当作不存在。重名时保留路径；缩小任务范围优先于反复全盘扫描。要核实选中的 `file_id` 当前属性：

```python
print(mf.cloud.info(file_id))
```

## 03 批量改名、移动、复制、建目录与回收

从第 02 节得到实际对象，将用户要求转换为一份 `operations`。`target_path` 是目标**目录**绝对路径，文件新名另放 `new_name`。缺失的各层目录用 `create_directory` 列入同一批，后续即可移动或复制到它们。以下按任务选用，`file_id` 来自真实清单：

```python
rename = {"op": "rename", "file_id": file_id, "new_name": new_name}
move = {"op": "move", "file_id": file_id, "target_path": target_path}
copy = {"op": "copy", "file_id": file_id, "target_path": target_path, "new_name": new_name}
relocate = {"op": "relocate", "file_id": file_id, "target_path": target_path, "new_name": new_name}
trash = {"op": "trash", "file_id": file_id}
mkdir = {"op": "create_directory", "parent_path": parent_path, "name": directory_name}
```

复制省略 `new_name` 则保留原名；复制改名作用于副本，源文件不变。例如只改名时 `operations = [rename]`；批量时按选中对象逐条构造。先保存完整操作，再作为本脚本最后一个调用提交：

```python
with open("/work/cloud-operations.json", "w") as stream:
    json.dump(operations, stream, ensure_ascii=False)
mf.cloud.change(operations)
```

确认后另开命令，用实际回执中的 `operation_ref` 检查逐项结果，必要时回读文件。复制任务仍在运行就继续跟踪这个编号：

```python
print(mf.cloud.operation(operation_ref))
```

`trash` 移入回收站。若要求连空父目录一起处理，先核实目录全部成员，再用第 05 节的“回收空目录本身”；不能只凭选中的一个视频就删除整个父目录。目录内批处理也可用 `cloud.change` 的 `batch_files`，实际字段和容量通过 `help(mf.cloud.change)` 查询。

## 04 清洗名称、剧集编号与字幕

“去掉这些文件名里的码率”——`selected_file_ids` 来自第 02 节明确选中的真实对象：

```python
preview = mf.cloud.preview_names(selected_file_ids, mode="remove_bitrate")
with open("/work/cloud-name-preview.json", "w") as stream:
    json.dump(preview, stream, ensure_ascii=False)
print({"stats": preview["stats"], "conflicts": preview["conflicts"]})
```

原样文本替换用 `mode="replace_text", find=old_text, replace=new_text`；单目录媒体名称清理用 `mode="media_hygiene"`。核对 `entries` 的改名前后路径与 `conflicts` 后，把所选完整 `operations` 交第 03 节；冲突被排除不表示整批全成功。

“把这部剧的绝对集号映射到季集并带上字幕”——先对真实作品目录 `directory_id` 盘点：

```python
inventory = mf.cloud.episodes(directory_id)
with open("/work/cloud-episodes.json", "w") as stream:
    json.dump(inventory, stream, ensure_ascii=False)
print(inventory["groups"])
```

`source_path` 必须来自盘点结果的对应组。以下仅示范**用户明确要求把源 19 映射为 S02E06**；其它季集从用户或核实资料取得，不按文件数量推算 TMDB 季偏移。`title` 使用核实的作品标题：

```python
preview = mf.cloud.preview_episodes(directory_id, title=title, groups=[{
    "source_path": source_path,
    "source_episode_start": 19, "source_episode_end": 19,
    "target_season": 2, "target_episode_start": 6, "expected_count": 1,
}])
with open("/work/cloud-episode-preview.json", "w") as stream:
    json.dump(preview, stream, ensure_ascii=False)
print({"videos": preview["video_count"], "subtitles": preview["subtitle_count"],
       "subtitle_skips": preview["subtitle_skips"]})
```

同目录有多季或多版本时，按盘点事实补 `source_season` 或 `name_contains`；核对后把完整 `preview["operations"]` 交第 03 节。此处完成文件命名；用户要的是**纠正日志并入库**时直接走第 06 节。

## 05 清理残留与空目录

“清理这个目录下面的残留和空目录”：`directory_id` 是已定位的任务目录，先仅预览该范围，来源目录本身保留：

```python
preview = mf.cloud.preview_cleanup(source_ids=[directory_id], include_empty=True)
with open("/work/cloud-cleanup-preview.json", "w") as stream:
    json.dump(preview, stream, ensure_ascii=False)
print({"stats": preview["stats"], "total_candidates": preview["total"],
       "sample": [(row["file_id"], row["root"]["name"], row["file_count"])
                  for row in preview["candidates"][:10]]})
```

核对空目录及残留候选；需要隔离非空残留时，把明确选中的真实 ID 放进 `quarantine_ids` 再预览。`candidates` 为分页展示，`operations` 是完整选中集合，展示页大小不是执行上限。真空目录回收，选中残留移入隔离目录，其余保留；核对完整操作后走第 03 节。省略 `source_ids` 会使用配置来源，不适合只清理一个媒体目录的任务。

“回收这个已空的媒体目录本身”：先 `cloud.list(directory_id)` 核实为空，再精确提交目录自身，执行器仍会检查空目录条件：

```python
mf.cloud.change([{"op": "trash", "file_id": directory_id, "require_empty": True}])
```

如果还有视频、字幕或其他文件，先报告剩余成员；不扩大清理来源来绕过。回收后以原 `operation_ref` 和回收站记录核验，而不是只看目录消失。

## 06 日志纠偏入库与单文件刮削

“把待确认的杀手旅店 19 改为 S02E06 再整理入库”——定位原日志：

```python
page = mf.activity.organize_logs(origin="guangya", status="manual", keyword="杀手旅店")
with open("/work/cloud-organize-logs.json", "w") as stream:
    json.dump(page, stream, ensure_ascii=False)
print(page)
```

按标题、路径选定 `log_id`；查跳过或失败条目时改用对应状态。列表有 `has_more` 时按 `next_offset` 继续；未找到不能以第一页断言不存在。

```python
detail = mf.activity.organize_detail(log_id)
with open("/work/cloud-organize-detail.json", "w") as stream:
    json.dump(detail, stream, ensure_ascii=False)
print(detail)
```

核实原日志的文件、媒体身份和可用操作。`tmdb_id` 来自核实后的日志身份或候选；下面季集来自本例用户明确指令：

```python
mf.activity.reorganize(log_id, tmdb_id=tmdb_id, media_type="tv", season=2, episode=6)
```

一次完整预览覆盖改名、移动、原日志/待确认状态更新及配置的 STRM 联动，无需先改父目录。确认执行后新命令核对原记录：

```python
print(mf.activity.timeline("organize", log_id))
print(mf.activity.organize_detail(log_id))
```

没有对应日志、只有目录或单文件时，选真实 `file_id`；已知目标及搜索词 `query` 后一次查询可同时取得文件事实和候选：

```python
found = mf.cloud.search_media(file_id, query=query)
with open("/work/cloud-media-candidates.json", "w") as stream:
    json.dump(found, stream, ensure_ascii=False)
print(found["candidates"])
```

若只想检查文件而不搜索，用 `cloud.inspect_media(file_id)`。选择匹配身份的 `candidate`，不默认取列表第一项；TMDB 和 MetaTube 都使用候选原有身份：

```python
mf.cloud.scrape(file_id, media_type=candidate["media_type"],
                provider=candidate["provider"], external_id=candidate["external_id"],
                tmdb_id=candidate["tmdb_id"])
```

需要纠正剧集时附加核实的 `season` / `episode`。刮削回执的 `data.operation_ref` 用 `cloud.operation` 跟踪；最终再核验第 11 节的入库阶段。AI 推断不自动保存为人工学习规则。

### 大批待确认组逐项选择

当前批量确认仅支持光鸭整理。已有批次可按 `batch_id` 查询，也可按 `task_id/source/reason/q` 筛选；SDK 的 `page_size` 可取 1–1000：

```python
page = mf.activity.organize_confirmations(batch_id=batch_id)
with open("/work/organize-confirmations.json", "w") as stream:
    json.dump(page, stream, ensure_ascii=False)
print({"total": page["total"], "pages": page["pages"], "counts": page["counts"],
       "selection_truncated": page["selection_truncated"], "items": page["items"]})
```

`items` 只是本页；按 `page=2,3,...` 读取其余页面。`tokens` 包含当前筛选全部匹配项，最多 1000；`selection_truncated=True` 时缩小筛选范围，不能声称已选全部。需要只读模型建议且 `review_enabled=True` 时：

```python
print(mf.activity.review_organize_confirmations(page["tokens"]))
```

这只把复核持久排队，不确认或整理文件。后续用原 `tokens` 读取 `review_status/suggestion`，结合各条文件与候选核对。每条选择来自该条 `candidates[].index`；下面 `token_a/token_b` 和候选序号均来自已核对记录：

```python
entries = [{"token": token_a, "candidate_index": index_a},
           {"token": token_b, "candidate_index": index_b}]
with open("/work/organize-choices.json", "w") as stream:
    json.dump(entries, stream, ensure_ascii=False)
mf.activity.confirm_organize(entries)
```

这一次调用冻结全部选择、文件和来源规则，生成一张确认卡；每组可以选不同候选。确认后逐项进入原队列，`data.accepted/failed/items` 是受理结果。另开命令按原 tokens 查进度；大于一页继续分页：

```python
with open("/work/organize-choices.json") as stream:
    entries = json.load(stream)
progress = mf.activity.organize_confirmations(tokens=[entry["token"] for entry in entries], status="all")
print(progress)
```

`queued/running` 尚未完成，逐条核对终态及 `error`；复核建议、入队数均不证明入库。执行来源保留为 Agent，不自动学习为人工偏好。

## 07 配置来源整理、停止与定时

“看看配置的整理来源和目标”，只读预览：

```python
preview = mf.cloud.preview_organize(max_files=20)
with open("/work/cloud-organize-preview.json", "w") as stream:
    json.dump(preview, stream, ensure_ascii=False)
print({"ok": preview["ok"], "status": preview["status"], "summary": preview["summary"],
       "sources": preview.get("data", {}).get("sources", [])})
```

从 `data.sources[].id` 选用户指定来源得到 `source_ids`；这些是整理来源。`max_files` 仅限制预览，执行会处理所选来源全部内容。只改一个媒体请用第 06 节。要求执行所选来源时：

```python
mf.cloud.organize(source_ids=source_ids)
```

确认执行后，以回执 `data.run_id` 为 `run_id`，另开命令查询：

```python
print(mf.cloud.organize_status(run_id))
```

用户要求停止该次整理才调用以下写操作；已完成的移动保留，`stopping` 后仍须查同一运行直到停止：

```python
mf.cloud.stop_organize(run_id)
```

查看定时策略，再按用户要求仅更新指定字段（例如停用未来定时整理）：

```python
print(mf.cloud.organize_schedule())
```

```python
mf.cloud.set_organize_schedule(enabled=False)
```

保存定时策略不启动整理，也不中断当前任务。其他字段如 `cron`、`notify_enabled` 查实时帮助；按回执核实配置保存与运行时生效状态。

## 08 分享创建、撤销与转存

“把选中的文件分享 7 天”——`selected_file_ids` 来自第 02 节，`share_title` 来自用户要求：

```python
mf.cloud.share(selected_file_ids, title=share_title, expires_days=7)
```

执行回执顶层 `share_url` / `access_code` 只交给当前用户，结合 `status` / `verified` 判断是否核实。结果未知先查已有分享，不再创建一次：

```python
shares = mf.cloud.shares()
with open("/work/cloud-shares.json", "w") as stream:
    json.dump(shares, stream, ensure_ascii=False)
print({"total": len(shares["items"]), "sample": shares["items"][:10]})
```

用户要求撤销时，`selected_share_ids` 取选中记录的 `share_id`，这是管理记录 ID，不能用公开链接中的 ID 替代。创建与撤销各最多 100 项：

```python
mf.cloud.revoke_share(selected_share_ids)
```

确认执行后重新读取 `cloud.shares()` 核对；撤销分享不删除原文件。

“看看这个分享，转存其中指定文件”——`share_url` 是用户提供的光鸭分享：

```python
share = mf.cloud.inspect_share(share_url)
with open("/work/cloud-share-inspection.json", "w") as stream:
    json.dump(share, stream, ensure_ascii=False)
print(share)
```

选择顶层 `files` 中实际的 `id` 形成 `selected_share_file_ids`，并核对当前配置的转存目标；它们不是自己云盘里的 `file_id`。用户要求转存时：

```python
mf.cloud.transfer_share(share_url, selected_share_file_ids)
```

执行回执顶层 `request_id` 用第 10 节时间线跟踪。转存受理、转存完成和入库分开说明；不把分享链接交给普通磁力下载接口。

## 09 回收站恢复与清空

“查查刚回收的文件”，完整读取后按任务筛选，保留真实 `file_id` 与原 `parent_id`：

```python
recycle = mf.cloud.recycle()
with open("/work/cloud-recycle.json", "w") as stream:
    json.dump(recycle, stream, ensure_ascii=False)
print({"total": len(recycle["items"]), "sample": recycle["items"][:10]})
```

要恢复的 `selected_file_ids` 从这份回收站结果选取，最多 200 项：

```python
mf.cloud.restore(selected_file_ids)
```

执行回执 `status/task_id/verified` 判断受理情况；有原生 `task_id` 时，另开命令查该任务，再回读 `cloud.info(file_id)` 和回收站核实恢复结果：

```python
print(mf.cloud.task(task_id))
```

**仅用户明确要求永久清空整个回收站**时用下列操作，由执行器展示高风险确认；不是清空当前筛选结果，执行期间新进入回收站的对象也可能被删除：

```python
mf.cloud.clear_recycle()
```

受理后按原任务核验；永久清空不支持恢复。只想处理少数文件时不能用此方法代替选择性删除。

## 10 光鸭下载与入库跟踪

用户提供可下载的 `url` 并要求下载到光鸭时：

```python
mf.downloads.submit(url, target="guangya")
```

确认执行后，使用回执顶层 `request_id` 跟踪，不通过全盘任务列表猜关联：

```python
progress = mf.activity.timeline("download", request_id)
print(progress)
```

`data.title` 是已补全的名称；`data.stages` 是下载、整理等各阶段，`data.gaps` 是未复核范围。这是持久快照；`task_bound=False` 只表示未记录绑定，不能说“云盘没有任务”。找历史请求用 `downloads.requests` 的真实 `request_number`，有分页继续读 `next_offset`。只有已确认可重试的失败才走 `downloads.retry(request_id, target="guangya")`。

用户要看云盘离线队列时才读完整任务列表：

```python
tasks = mf.cloud.offline_tasks()
with open("/work/cloud-offline-tasks.json", "w") as stream:
    json.dump(tasks, stream, ensure_ascii=False)
print({"total": len(tasks["items"]), "sample": tasks["items"][:10]})
```

原生任务、下载请求、整理运行是不同编号。离线下载完成尚未证明入库；详情继续用原下载请求时间线，缺哪一阶段再处理哪一阶段。

## 11 STRM 与媒体库验收

先查看原整理/下载时间线，已有 STRM 联动则跟踪原任务。需诊断同步配置与本地索引时 `strm.diagnose()` 不触发全盘扫描；需补同步时从状态中取得真实来源：

```python
status = mf.strm.status()
with open("/work/cloud-strm-status.json", "w") as stream:
    json.dump(status, stream, ensure_ascii=False)
print(status)
```

`strm_source_ids` 从 `data.sources[].id` 选择，与整理来源 ID 区分。要求补同步时：

```python
mf.strm.run(source_ids=strm_source_ids)
```

确认执行后，实际回执的 `data.run_id` 为 `strm_run_id`，另开命令核验：

```python
print(mf.strm.status(strm_run_id))
```

`data.task` 是指定运行，不能用最近一次成功替代。部分失败时只读明细：

```python
failures = mf.strm.failures(status="open", source_id=strm_source_id)
with open("/work/cloud-strm-failures.json", "w") as stream:
    json.dump(failures, stream, ensure_ascii=False)
print(failures)
```

`strm_source_id` 同样来自已选来源；按 `data.next_offset` 继续读完所需范围。用户要求重试时，将选定 open 记录的整数 `id` 组成 `failure_ids`：

```python
mf.strm.retry_failures(failure_ids)
```

核对回执实际重试数量并重读记录，不以排队数声称修复成功。需要更早运行时读 `strm.history`；定时策略用 `strm.schedule/set_schedule`，元数据队列用 `strm.metadata_status`。关队列和取消积压的差别见实时帮助，不从文件同步状态推断元数据已完成。

STRM 完成后，媒体库扫描受理仍不等于已入库。用 `library.libraries(server="jellyfin")` 确定目标库，必要时 `library.refresh("jellyfin", library_id)` 提交扫描。以第 06 节 S02E06 为例，`title/tmdb_id/library_name` 是该任务已核实身份与库名：

```python
audit = mf.library.audit(title, tmdb_id=tmdb_id, season=2, target_episode=6,
                         library_name=library_name, refresh=True)
with open("/work/cloud-library-audit.json", "w") as stream:
    json.dump(audit, stream, ensure_ascii=False)
print(audit)
```

结合 `data.local_inventory_complete`、`data.sources` 和目标集的库存证据交付；仅为一集入库，不要求整季齐全才算成功。电影可用 `library.presence` 核对身份存在性；`possible/indeterminate` 和来源失败保留未知，存在性检查不能替代指定库归属、版本或实际播放验收。

## 12 检查并撤销有效历史操作

“最近有没有能撤销的整理？”只读检查，跳过没有回退快照的条目，其它异常保留：

```python
offset, inspected = 0, None
while inspected is None:
    page = mf.activity.organize_logs(origin="guangya", limit=50, offset=offset)
    for item in page["items"]:
        try:
            inspected = mf.activity.inspect_undo(item["id"])
            break
        except mf.Error as error:
            if error.code != "undo_not_supported":
                raise
    if inspected is not None or not page["has_more"]:
        break
    offset = page["next_offset"]
with open("/work/undo-inspection.json", "w") as stream:
    json.dump(inspected, stream, ensure_ascii=False)
print(inspected)
```

读完全部页面仍为 `None` 才能报告没有可撤销记录。有结果时 `data.affected` 是总量，`data.changes` 只展示最多 20 项，方向为当前 `from` → 撤回后 `to`。用户明确要求撤销后另开命令加载凭据，最后一步提交：

```python
with open("/work/undo-inspection.json") as stream:
    inspected = json.load(stream)
mf.activity.undo(inspected["data"]["receipt_id"])
```

凭据仍需执行器复验时效、当前版本和归属。受理后沿原 `log_id` 查时间线，不重复消费凭据；不能借此撤回下载、通知或任意无快照的历史操作。
