# 光鸭云盘：查询、文件变更、整理入库、分享与回收

处理光鸭文件、分享转存、下载跟踪、整理纠偏、STRM 和可回退的历史操作。先定位用户指定范围，复用一次读取，在工作区筛选并组合整批操作。

## 按任务读取示例

详细示例在 [references/examples.md](references/examples.md)，沙箱路径 `/skills/cloud-files/references/examples.md`。只读当前任务对应的节；参数以 `help(mf.cloud.change)` 或 `mf cloud.change --help` 等实时帮助为准。

Python 片段执行前 `import json, mf`。未定义的输入变量取自所在节说明的真实查询结果；跨命令从 `/work` 重新加载，完整结果留文件、终端只打印所需摘要。

| 用户要做的事 | 示例节 |
| --- | --- |
| 连接、容量、找文件、检查属性 | 01、02 |
| 批量改名、移动、复制、建目录、回收文件 | 03 |
| 清洗名称、剧集编号与字幕、清理残留和空目录 | 04、05 |
| 日志中纠正季集并入库、单文件或目录刮削 | 06 |
| 已配置来源整理、停止、定时策略 | 07 |
| 创建/撤销分享、检查/转存分享 | 08 |
| 回收站恢复或清空 | 09 |
| 光鸭离线下载与入库进度 | 10；搜资源先读 `/skills/media-research/SKILL.md` |
| STRM 同步、失败重试、入库验收 | 11 |
| 检查或执行有凭据的撤销 | 12 |

例如读取日志纠偏与单文件刮削示例：

```sh
sed -n '/^## 06 /,/^## 07 /p' /skills/cloud-files/references/examples.md
```

## 调查、执行与交付

1. 根目录 ID 是 `0`。文件和目录使用读取结果里的真实 `file_id`，同名时结合路径选择。只遍历任务范围；完整结果保存到 `/work`，对模型打印数量、必要路径和异常。SDK 报扫描不完整时保留未知。
2. 用户只要调查就停在只读结果；用户要求变更时，把选中对象组合成完整计划。文件操作用 `cloud.change`；已有整理日志的纠偏入库用 `activity.reorganize`，一次覆盖后续整理，避免先改名再追问是否入库。
3. 写调用放在脚本最后。确认卡出现后脚本已结束，确认只执行冻结计划；后续核验另开命令，从 `/work`、`/output` 或执行回执读取事实。授权由统一执行器判断。
4. 受理、运行中和结果未知均沿原编号追踪，不重复提交。部分完成时核对逐项结果，明确剩余范围。仅发现文件消失不能证明删除、移动或入库成功。
5. 文件变更、整理、STRM、媒体库可见是独立阶段。先查原链路已经联动的阶段，按需补后续动作；交付只说明实际核实的结果。

## 用回执里的对应编号

| 回执或读取来源 | 后续查询 |
| --- | --- |
| `cloud.change` / `cloud.scrape` 的 `operation_ref`（`GY-...`） | `cloud.operation(operation_ref)` |
| 回收站等操作的云盘原生 `task_id` | `cloud.task(task_id)` |
| `cloud.organize` 的 `data.run_id` | `cloud.organize_status(run_id)` |
| `activity.organize_logs` 的条目 `id` | `activity.organize_detail(log_id)` / `activity.timeline("organize", log_id)` |
| 下载/转存回执的 `request_id` | `activity.timeline("download", request_id)` |
| `strm.run` 的 `data.run_id` | `strm.status(run_id)` 的 `data.task` |

## 当前能力边界

SDK 尚未开放本地文件上传、播放直链/画质地址导出、光鸭离线任务暂停/恢复/删除。`downloads.pause/resume/remove` 控制的是 qBittorrent。历史撤销仅限 `activity.inspect_undo` 检查通过的有效凭据；回收站恢复则走 `cloud.restore`。
