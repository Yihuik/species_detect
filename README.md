# 独立 Metadata Agent 工作流

所有新增代码、测试、示例和产物位于 `D:/codex/new-agent`。原项目 `D:/codex/qwen-vl` 仅用于审查，未导入或调用其写出流程。该实现覆盖前半部分定位流程；标注图绘制、知识库检索和后续业务处理不在本阶段范围。

## 快速运行（离线，无模型费用）

PowerShell，从新项目运行：

```powershell
Set-Location D:/codex/new-agent
$env:PYTHONPATH = 'D:/codex/new-agent/src'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m pytest tests -q --basetemp=D:/codex/new-agent/.test_tmp
python -m agentized_workflow.cli --input-dir examples/images --metadata-csv examples/metadata.csv --fixture examples/responses.json --run-dir D:/codex/new-agent/runs/demo
```

再次执行相同命令即断点续跑，已结束任务不再次请求模型。示例有 3 张纯色占位图和预设响应，仅验证控制逻辑：2 个 done、1 个 needs_review。它们不证明视觉定位准确率。

依赖为 Python >=3.10、Pydantic 2 和 Pillow；测试还需 pytest，声明见 pyproject.toml。开发和运行使用源码目录，不需修改原项目的虚拟环境或配置。

## 流程与硬约束

```text
可信 CSV/目录映射 -> visibility -> locate(1) -> locate(2) -> compare
  一致 -> done
  不一致 -> plan -> needs_review
                -> retry_once -> locate(3) -> compare -> done / needs_review
```

- 普通流程完全由 Python 状态机控制，规划器只处理两次定位未达成一致的分支。
- 默认规划器为确定性策略：允许时第三次定位一次。可选 LLM 也只能返回 `retry_once` 或 `needs_review`，不能接受结果、改名或调用工具；不合法 JSON、额外字段和异常均转人工审核。
- 每张图片正常有 1 次可见性请求、2 次独立定位。最多 3 次定位和 1 次规划请求。HTTP 层没有重试；设置有限超时。计数包含失败和结果不明的调用。
- 两次独立定位使用独立请求与全新消息，不传递前次框、回答或历史。它表示上下文隔离，不保证同一个模型在统计上独立。
- IoU 门槛默认 0.7，框数量必须相同且非零；使用二分图增广路径寻找覆盖所有框的一对一匹配。空结果、不等数量、无完整匹配均不通过。
- 第三次结果分别与前两次有效结果比较，任一完整匹配通过则选用第三次框。不会平均框或自行改框。双次通过时使用第二次框。
- Qwen 坐标限定 0–999，拒绝负数、越界、逆序、零面积、NaN 和多余字段。像素转换遵循原项目 `/1000` 约定，坐标对应 EXIF 旋转后的图像尺寸。
- 物种名称从显式提供的 `metadata.csv`（列 `source_image,species`）或可信目录映射逐字取得。CSV 冲突、路径逃逸和目录映射歧义会拒绝。没有模糊匹配、文件名猜测、名称标准化或模型改名。
- 目录映射示例：`{"1.大鳍弹涂鱼":"大鳍弹涂鱼"}`，用 `--directory-map trusted_directories.json` 代替 CSV。只有用户明确指定的来源才视为可信；文件哈希用于追溯而非认证。

## 持久化、审核与恢复

`runs/<run>/state.sqlite3` 是唯一权威状态源，任务更新与审计事件在同一 SQLite 事务提交。每个外部调用前先写意图；进程崩溃后遇到未完成意图，直接转 `needs_review/interrupted_call`，不会重发可能已计费的请求。已完成第一轮定位可在重启后继续第二轮。

跨进程操作系统锁保护整个单步执行，进程死亡会自动释放，避免两个 runner 重复提交请求。每个 run 串行运行，不支持分布式调度。变更 IoU 门槛、目标数、图片内容或可信 metadata 后，应另开 run 目录；已有任务不被覆盖改名。

导出文件可从数据库重复生成：

- `results/<task_id>.json`：可信名称、选定框、像素坐标、来源哈希、策略和被选轮次。
- `needs_review.json`：完整待人工审核任务（原因、来源、历次框或错误类型）。人工审核为自动流程终止出口，没有自动再次入队、无限重试或自动确认功能。
- `audit.json`：有序审计事件，包括调用意图、结果、决策、终止状态。数据库 events 表保留权威日志；JSON 是可重建快照。

终止状态提交后若导出失败，再次运行仅重建文件、不请求模型。输入读写失败不会假装成功；异常审计不保存 API key、HTTP 请求头或潜在含凭证的异常正文。规范化模型结果写入审计，不保存原始响应正文。

运行目录必须位于新项目内，原图只读。`needs_review` 是成功完成自动流程的一种状态，所以 CLI 退出码为 0，同时输出非零审核数；配置或数据库错误为非零退出码。请以队列数量判断是否尚需人工处理。

## 可选真实模型（本次未运行）

显式 `--live` 才会联网；不会读取原项目 `.env`。在环境中提供 `DASHSCOPE_API_KEY` 后运行：

```powershell
python -m agentized_workflow.cli --input-dir D:/path/to/images --metadata-csv D:/path/to/metadata.csv --live --model qwen3-vl-plus --run-dir D:/codex/new-agent/runs/live01
```

增加 `--planner-model <已确认可用的规划模型名>` 接入规划 LLM。视觉与规划默认共享指定 endpoint 和 key，可在 Python 中注入不同 transport。endpoint 用 `--base-url` 指定；`--timeout` 默认 90 秒且上限 600 秒。

默认使用与原客户端相同的 JSON-object 响应方式并在本地强校验。仅在服务支持该 schema 时使用 `--structured` 请求服务端严格 JSON Schema。具体模型、参数与服务端兼容性尚未真实验证；不支持时会进入审核，而不会放宽约束或偷偷重试。实际成本、视觉质量和 IoU 阈值需另做小样本标注评测。

## 模块

`models.py`：不可变 Pydantic 契约；`metadata.py`：可信来源与输入身份；`tools.py`：固定白名单几何工具和视觉协议；`storage.py`：事务、锁、审核队列、原子导出；`workflow.py`：有界状态机；`planner.py`：有限动作规划接口；`providers.py`：独立视觉/规划 HTTP 适配；`cli.py`：离线和显式联网入口。

此处的白名单是 Python 调用边界，规划 LLM 没有通用工具调用能力。数据库写入只由状态机控制；人工修改数据库、替换代码或自定义不守约的 provider 不属于这一边界可防御的行为。
