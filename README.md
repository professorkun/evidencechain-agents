# Windows Codex 多 Agent 协同工作台

一个面向 **Windows 本机 Codex** 的多 Agent 协作原型：先让不同职责的 Agent 并行讨论，再由主控汇总；得到明确确认后，才并行执行互不重叠的工作并交给独立角色验证。

本项目的 Agent 只在 Windows 本机 Codex 中运行，不在 NAS、服务器或其他设备部署 Agent。经用户确认后，主控 Codex 可以通过既有 SSH 连接把 NAS 作为远程执行目标；子 Agent 不直接取得 SSH 控制权，也不启动远端常驻服务。

## 第一期开关式原型

第一期已经实现并通过内置测试：

```text
任务
 ├─ 方案 Agent      ┐
 ├─ 反方 Agent      ├─ 并行讨论 → 主控汇总 → 显式确认
 └─ 调研 Agent      ┘                         ↓
                                  两项并行、只读验证
```

- 默认是离线模拟模式：不发送模型请求，也不需要密钥。
- 未传确认开关时，只生成讨论汇总。
- 传入确认开关后，仅对内置样例执行文件清单检查和无缓存测试；第一期没有代码写入能力。
- 每次运行会生成本地报告；报告目录不纳入版本控制。

## 本地运行

```powershell
uv sync
.\scripts\run-phase1.ps1 -Task "检查内置样例并给出风险评审" -ApproveReadonly
```

运行完整测试：

```powershell
uv run python -m pytest
```

## Codex 全局角色模板

`examples/codex-agents/` 提供五个可复用的个人级 Agent 角色模板：方案、反方、调研、执行、验证。它们的分工是：

| 角色 | 职责 | 默认写入权限 |
|---|---|---|
| planner | 拆解任务、依赖与验收标准 | 只读 |
| contrarian | 找风险、遗漏和错误假设 | 只读 |
| researcher | 收集代码、文档和测试证据 | 只读 |
| executor | 处理明确分配且隔离的实现范围 | 继承父任务 |
| verifier | 独立复核范围与验收结果 | 继承父任务 |

建议将 Codex 子 Agent 并发上限设为 2：主控不计入该上限；同一文件、配置、部署或外部操作始终保持单一执行者。

## 路线图

详见 [路线图](docs/roadmap.md)。第二期正在实现隔离 worktree、任务合同、文件范围锁、独立验证和单写者合并门禁；所有合并仍要求用户明确确认。在验证稳定后再考虑真实模型调用。

## Git 分支与回滚

- `main`：已验证的稳定版本，只接受通过验证的合并。
- `codex/phase-1`：第一期开发线；后续每个阶段使用新的 `codex/` 前缀分支。
- 每次任务先在独立分支或 worktree 实施，再由验证角色复跑测试；通过后才合并到 `main`。
- 回滚优先使用 `git revert` 保留可追溯历史，不以覆盖式重置替代正常回滚。

## 第二期安全门禁

`examples/contracts/phase2-sample.json` 展示任务合同的最小格式。先验证合同；只有在调用者传入显式的 `--approve-worktree` 后，第二期工具才允许建立任务分支和隔离 worktree。它不会自动修改 `main` 或自动合并。

测试命令中的 `{python}` 是受控占位符：由主控解析为当前受管理的 Python 解释器，避免 worktree 因未复制虚拟环境而使用错误解释器。

## 安全边界

- 不提交密钥、个人路径、运行报告或缓存。
- 不让 Agent 绕过 Codex 的权限、确认或 Sandbox。
- 并行只用于独立任务；涉及同一写入范围时必须串行。

## 许可

本项目采用 [MIT License](LICENSE)。
