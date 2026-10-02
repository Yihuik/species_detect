# Photo-labeling runbook

## 日常启动与人工复核（2026-09-30 起）

本节是工作人员使用的当前流程。下文以
`D:\codex\new-agent` 为项目目录；所有照片、数据库、标注结果和
`.env` 仅保存在本机，不提交到 Git。请在独立的 Windows PowerShell
窗口运行长任务并保持窗口开启。不要在同一时间启动两个标注进程。

首次使用时，在项目目录执行：

```powershell
Set-Location 'D:\codex\new-agent'
python -m pip install -e .
Test-Path .\.env
```

`.env` 必须提供 `DASHSCOPE_API_KEY` 和 `DASHSCOPE_BASE_URL`。
启动脚本会读取并检查两项，但不会打印凭据，也不会回退到公共地址。
`config/species.txt` 是可信物种清单。工作人员提供的照片放在
`input/inbox/<批次>/<物种名>/`，也可放同结构的 ZIP 包；
目录名必须与可信物种名一致。启动时先同步物种、采集公开照片并导入
用户照片，只有已去重进入 `photos/` 的活动照片会参与打标。

### 首次迁入已有标注

升级前已有运行时，先确认该运行没有工作进程，再**逐个**迁入其结果：

```powershell
Set-Location 'D:\codex\new-agent'
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\import_labels.ps1 `
  -RunDir '.\runs\agent-20260922-142848'
```

该命令只读取原运行的 `state.sqlite3` 和结果 JSON，并用原图、
物种、模型结果和流程参数核对；不会请求模型。导入前会使用 SQLite
在线备份 API 将照片库数据库备份到本地
`runs/backups/photo_library-*.sqlite3`；导入与启动共用独占锁。
命令会把有效的 `done`
登记为“自动完成、未审核”，把 `needs_review` 留在复核队列，并把
未完成任务关联到原运行供续跑。它还补画已有 JSON 对应的
`annotated/` 照片。输出的 `invalid` 必须为 0；若不为 0，
先核查缺失/损坏的原图或结果，不能把这些项当作已完成。
若还有其他 `runs/agent-*/state.sqlite3` 没有迁入，一键启动会明确
报错并列出目录，避免误把历史照片重标。

### 一键启动

```powershell
Set-Location 'D:\codex\new-agent'
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\start_agent.ps1 `
  -Contact '工作人员或项目联系邮箱'
```

脚本优先续跑同一流程中未完成的原运行，然后只为新照片创建运行目录。
启动和手动续跑共用独占锁；出现 `another labeling launcher` 时先检查
已有工作人员的进程，不要删除锁文件来绕过正在运行的任务。
相同照片、物种和流程版本已有有效结果时，直接跳过模型调用；没有
新任务时不会创建时间戳运行目录。人工驳回的结果默认留在复核队列，
不会在每次启动时自动重标。新完成的 JSON 会自动渲染为
`runs/<运行目录>/annotated/<任务ID>.jpg`；被复用照片的原结果仍在
其原运行目录，可通过下方 `list` 查到路径。

如果只是继续一个已知的运行，也可使用：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\resume_agent.ps1 `
  -RunDir '.\runs\agent-20260922-142848'
```

续跑从该运行 `state.sqlite3` 读取冻结的任务信息，不用当前可能已
增加照片的 `workflow_metadata.csv` 改写旧任务。脚本使用项目
`.env` 中的地址，并补画标注图。若同一流程仍有活动进程，先确认其
命令行和启动时间；不要再启动一个进程。

### 查看和处理人工复核

以下命令在项目目录运行；若尚未安装可编辑包，先执行
`$env:PYTHONPATH = (Join-Path (Get-Location) 'src')`。

```powershell
python -m agentized_workflow.cli photos labels list --photos-root .\photos
python -m agentized_workflow.cli photos labels queue --photos-root .\photos
```

`list` 显示每张照片当前标注结果的 `id`、`local_path`、
`result_path`、机器状态和人工状态。机器 `done` 且人工
`unreviewed` 可以抽查，但不重复调用模型。人工检查原图、
画框图和可信物种名称后，使用结果的 `id`（下称 `<编号>`）：

```powershell
python -m agentized_workflow.cli photos labels review `
  --photos-root .\photos --case-id <编号> --decision approve `
  --reviewer '审核人' --reason '位置及物种核对通过'

python -m agentized_workflow.cli photos labels review `
  --photos-root .\photos --case-id <编号> --decision reject `
  --reviewer '审核人' --reason '说明错误位置或其他问题'
```

认可或驳回都追加审核事件，不覆盖机器输出。驳回立即阻止旧结果复用；
`queue` 可看到待处理项。若需要人工修正框，先查看该结果 JSON 中的
`image_width` 和 `image_height`，按**原图像素坐标**建立文件，
每个框为 `[左,上,右,下]`：

```powershell
New-Item -ItemType Directory -Force .\runs\review-input | Out-Null
'{"boxes":[[12,20,130,180],[220,35,315,170]]}' |
  Set-Content -Encoding UTF8 .\runs\review-input\case-<编号>.json
python -m agentized_workflow.cli photos labels correct `
  --photos-root .\photos --case-id <编号> `
  --boxes-file .\runs\review-input\case-<编号>.json `
  --reviewer '审核人' --reason '依据原图修正两个框'
```

修正命令检查坐标边界，保留原机器结果及驳回历史，生成新的本地 JSON
和画框图，并将修正结果记为人工已认可。若希望模型重新标注某个已驳回
结果，必须明确指定该条 `id`：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\start_agent.ps1 `
  -Contact '工作人员或项目联系邮箱' -RetryCaseId <编号>
```

一次性批量重标所有当前已驳回结果需显式使用 `-RetryRejected`。
模型、提示词实现或 IoU 阈值等流程规则改变会形成新流程版本；
默认启动会报出受影响照片数。可先只读查看预计任务数：

```powershell
python -m agentized_workflow.cli photos labels plan `
  --photos-root .\photos `
  --metadata-csv .\photos\workflow_metadata.csv `
  --env-file .\.env
```

核对 `pending`、`reused`、`review`、`profile_changed` 和
`resume_runs` 后，才使用 `-AllowNewProfile`。若计划使用阈值
0.8，规划命令也应加 `--threshold 0.8`。
例如显式启用阈值 0.8：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\start_agent.ps1 `
  -Contact '工作人员或项目联系邮箱' -Threshold 0.8 -AllowNewProfile
```

当前版本对新阈值建立新结果，不会自动从旧定位尝试离线重算 IoU；
这会触发相应模型调用，运行前应先检查 `photos labels plan` 的数量。
日常运行保持默认 0.7，避免意外切换。

### 完成检查

查看启动命令输出的 `new`、`reused`、`review`、
`done` 和 `needs_review`。新运行的 `state.sqlite3` 是执行状态
权威；跨运行结果与审核事件保存在本地
`photos/photo_library.sqlite3`。人工修正的图片位于
`runs/manual-corrections/annotated/`。只有本次实际执行的任务会
写入本次时间戳运行目录；全库有效结果及原路径由
`photos labels list` 汇总。任何 `needs_review`、`rejected` 或
`corrupt_result` 都要保留在复核队列，不按“审核通过”统计。

---

以下内容保留 2026-09-22 历史运行的故障与恢复记录；日常启动以
上面的流程为准。

## Objective and scope

`terminal_scope_id`: `label-agent-20260922-142848`.

Finish automatic visibility, location, comparison, and labeling for the frozen
photo-library inputs represented by `runs/agent-20260922-142848/state.sqlite3`.
The scope is complete only when every task is in `done` or `needs_review`, the
worker has exited successfully, and the published result files agree with the
SQLite state. Collection and new remote downloads are outside this scope.

## Authorities and state

- Original task manifest: each task's frozen `TaskSpec` in `state.sqlite3`
- Authoritative task state: `runs/agent-20260922-142848/state.sqlite3`
- Run artifacts and bounded logs: `runs/agent-20260922-142848/`
- Generated bbox JSON: `runs/agent-20260922-142848/results/`
- Generated local photos with boxes and species labels: `runs/agent-20260922-142848/annotated/`
- Launcher configuration: project-root `.env`; credentials are never logged,
  committed, or copied into this document.

## Required invariants

1. Use exactly one labeling worker for this run.
2. `$DASHSCOPE_BASE_URL` is required. Do not fall back to a public endpoint.
3. Start recovery with `python` directly after loading the project `.env` and
   passing its base URL explicitly. Before allowing work to continue, inspect
   the child command line and confirm it contains the configured base URL.
4. Do not use `Start-Process powershell -File tools/resume_agent.ps1` until its
   child-environment propagation has been repaired and independently tested:
   prior invocations silently passed the public default endpoint.
5. If stopping a launcher, reconcile and stop its Python descendants before a
   new worker starts.
6. Preserve `done` tasks and legitimate `needs_review` records. If the known
   runtime failure `visibility_error:HTTPError` appears with no model attempts,
   stop the worker, make a timestamped SQLite backup, and reset only those
   rows to `visibility`.
7. Treat `.env`, photos, databases, run logs, and generated audit files as
   local artifacts. Code and documentation may be pushed to GitHub; photos and
   generated outputs are never pushed there. The user separately authorized
   sending photo bytes to the configured MaaS endpoint for model labeling.

## Established failure signatures

| Signature | Cause and recovery |
| --- | --- |
| No `.env` found | Launchers initially resolved their own directory rather than the project root. Use the project-root configuration path. |
| `visibility_error:HTTPError` on many tasks | A recovery PowerShell process passed a public default URL. Terminate its process tree; restore only rows with this reason and no attempts; relaunch with an explicit configured URL. |
| Parent stopped but Python continued | PowerShell cancellation does not guarantee descendant termination. Inspect the process tree and terminate the owned Python child before recovery. |
| Foreground terminal session disappeared | On 2026-09-24 the retained foreground session and its Python child vanished after about nine hours. No Windows application-crash event was found; the precise trigger is unknown. Back up SQLite, preserve in-flight requests as uncertain, then launch Python directly with `Start-Process -WindowStyle Hidden`, explicit `--base-url`, and redirected stdout/stderr. Verify process identity at launch and a new result after initialization. |
| Long delay before new model calls | Older CLI builds reprocessed terminal tasks and rewrote result/audit output during resume. Commit `cd0b1fb` skips terminal tasks whose outputs exist. |
| No marked-up photo despite completed JSON | Run the local renderer below to backfill `annotated/` from `results/`; it does not request the model or alter task state. |

## Current recovery procedure

1. Inspect the Python process for this run and the task-phase counts.
2. If no valid worker is active, reconcile only the known recoverable HTTP
   failure rows as described above.
3. Load `.env`, verify both the API key and base URL are present without
   printing either, then launch Python directly with the explicit `--base-url`
   value. For a long Windows run, use a hidden background Python process with
   stdout/stderr redirected to local run logs. Do not launch a PowerShell
   wrapper that changes the endpoint.
4. Verify the launched child uses the configured host. Record the PID, process
   creation time, and log paths. Make one delayed check that task counts or
   result files advance; process existence alone does not prove progress.
5. Perform only the user-requested delayed static check. For normal operation,
   do not live-poll the process.
6. After new JSON results appear, backfill marked-up photos with:

   ```powershell
   $env:PYTHONPATH = 'D:\codex\new-agent\src'
   python -m agentized_workflow.render_labels `
     --photos-root 'D:\codex\new-agent\photos' `
     --run-dir 'D:\codex\new-agent\runs\agent-20260922-142848'
   ```

   Rerunning this command skips photos already rendered from current JSON.

## Legal terminal evidence

The run is complete only when phase counts contain no runnable phase, the
worker has ended, the results/audit outputs have timestamps at or after the
last task-state update, and the number of `annotated/*.jpg` files equals the
number of `done` tasks. `needs_review` is a scoped terminal for individual
images and must retain its recorded reason.
