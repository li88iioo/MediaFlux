# 云盘文件调查、批量变更、撤销检查与后台任务核对

用于查找、复制、移动、改名或回收光鸭文件。先定位用户指定范围，复用同一次目录读取，在工作区筛选并组合整批操作。

## 调查与保存

`cloud.list(parent_id)` 返回完整目录及真实 `file_id`、`parent_id`、`name`、`is_dir`。根目录 ID 为 `0`；子目录使用查询得到的 ID。按名称定位时保留父目录，同名文件不能互相替代。递归遍历只进入任务范围内的目录，不为改一个文件遍历全盘。

```python
import json
from mf import cloud

root = cloud.list("0")
with open("cloud-root.json", "w") as stream:
    json.dump(root, stream, ensure_ascii=False)
for item in root["items"]:
    print(item["file_id"], item["name"], item["is_dir"])
```

完整目录、选中对象和计划保存在 `/work`；只打印数量、路径和需要判断的字段。后续需要核实某个对象时用 `cloud.info(file_id)`，不靠旧输出推断当前文件仍在原位。

## 一批操作，一份预览

用 `help(cloud.change)` 查看操作字段。把全部选中对象放进同一个 `operations` 列表；不要按单个对象逐次生成确认卡。典型操作包括：

- `rename`：`file_id`、`new_name`。
- `move` / `copy`：`file_id`、`target_path`。
- `relocate`：`file_id`、`target_path`、`new_name`，一次完成移动与改名。
- `trash`：`file_id`，移入回收站。
- `create_directory`：`parent_path`、`name`。

先保存计划文件，再调用 `cloud.change(operations)`。出现确认卡后，当前脚本已经结束；确认只执行冻结的变更，不会继续脚本中排在调用后面的代码。需要的后续核验应在拿到回执后用新命令完成，避免把已经提交的操作再执行一次。

## 跟踪原任务

提交回执里的 `GY-...` 编号用于 `cloud.operation(operation_ref)`；这是 MediaFlux 持久任务编号。`cloud.task(task_id)` 查询的是云盘原生后台任务，两个编号不可互换。

以同一个操作编号检查整批逐项结果。复制被受理或正在运行时，继续查原任务，不重新提交复制。失败、部分成功或核验未知时，先核对已执行的对象和剩余范围，再决定下一步。文件变更完成、STRM 联动完成和媒体库入库是不同阶段，按回执中实际完成的阶段交付。

改名、移动或复制不等于元数据识别。用户要求整理入库时，还要使用实际可用的整理能力完成对应步骤；不要只改目录名就宣称入库成功。

## 检查最近可撤销的操作

最近一条整理日志不一定可撤销。使用 `activity.organize_logs` 按时间倒序分页，逐条调用 `activity.inspect_undo`；`undo_not_supported` 表示该条没有可逆快照，可以继续下一条，其它错误应保留并查明。下面在一次脚本中找到最近的可撤销记录，只打印检查结果，不执行回退：

```python
import json
import mf

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
with open("undo-inspection.json", "w") as stream:
    json.dump(inspected, stream, ensure_ascii=False)
print(json.dumps(inspected, ensure_ascii=False))
```

查完所有页面仍为 `None` 才表示当前日志没有可撤销项。检查成功后使用结果里的真实 `log_id`、`changes` 和 `affected` 解释回退范围。`receipt_id` 只是一份检查凭据；用户明确要求回退时才调用 `activity.undo(receipt_id)` 生成原有确认计划。下载提交和已经发出的通知不在这个回退范围内。
