# MediaFlux 项目级 Agent 规则

本文件补充全局 `AGENTS.md`，作用范围为本仓库及其所有子目录。系统约束、用户当前明确指令和全局安全规则仍具有更高优先级。

## Agent / Superpowers 内部文件

- 所有 Agent 内部设计、实施计划、任务状态、执行报告、审查包和恢复数据，统一写入 `.superpowers/`。
- 设计文档写入 `.superpowers/specs/`。
- 实施计划写入 `.superpowers/plans/`。
- SDD 状态、brief、report 和 review diff 写入 `.superpowers/sdd/`。
- 视觉 brainstorming 的运行状态写入 `.superpowers/brainstorm/`。
- 禁止新建或使用 `docs/superpowers/`。
- 禁止使用 `git add -f`、`git add --force` 或其他方式强制提交被忽略的 Agent 内部文件。
- `.superpowers/` 内文件只用于本地协作与上下文恢复，不得提交到 Git。

## `docs/` 目录边界

- `docs/` 仅保存面向用户或项目维护者的正式文档，例如安装、部署、配置、API、使用指南和用户明确要求提交的架构说明。
- Agent 临时分析、内部设计、实施计划、执行日志、测试过程报告和上下文恢复信息不得写入 `docs/`。
- 若无法判断文档是否属于正式项目文档，默认写入 `.superpowers/` 并保持未跟踪。

## Git 纪律

- 遇到 `.gitignore` 或 `.git/info/exclude` 拒绝路径时，必须尊重忽略规则；不得为了满足技能默认路径而强制加入。
- 提交前检查 `git status --short`，确保没有误加入 `.superpowers/`、`docs/superpowers/` 或其他 Agent 临时文件。
