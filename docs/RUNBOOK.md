# Photo-labeling runbook

## 日常启动与人工复核（2026-10-10 更新）

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

### 新流程：mixed 与逐目标局部复核

一键启动现在默认使用 **版本 2、严格 IoU > 0.75**。版本 1 的历史运行
保留原来的两类可见性、默认 IoU ≥ 0.7 和全图第三次定位规则。
版本 2 不使用异常规划大模型，不改写可信物种名称。

可见性分类针对指定物种：所有可确认个体均完整或大部分可见为
`whole_or_mostly_visible`；均局部可见为 `partially_visible`；两类个体
同时存在为 `mixed`。三类定位都要求检查完整及局部个体，不能只框主要
个体。无法确认目标存在时仍可能分到 `partially_visible`，但空框不会
自动通过；当前仍未增加独立的物种正确性核验。

版本 2 先做两次独立的全图定位，再逐目标处理：

- 对应关系明确、无头胸甲形态疑点且 IoU **严格大于**阈值的框立即保留。
- 对应关系明确但未通过的目标，各做至多一次带上下文局部复核。
- 局部范围为前两次框的并集，横纵各增加 25% 上下文并裁到原图范围。
  请求只发送未画框的局部照片及物种，不提供旧框或旧 IoU；返回框映射回
  原图后与该目标前两次结果比较。至少一组 IoU > 阈值且身份唯一才通过。
- 多目标重叠造成对应歧义、找不到对应框、额外不明框、空框、调用失败
  均留待人工复核。对应关系的保守候选阈值 0.25 只用于关联，不能代替
  0.75 的通过阈值。
- 已通过的目标不再重标；若其他目标的局部上下文中出现其头胸甲疑点，则转人工。
  一次中断的局部请求不自动重发；保留无疑点的已通过框。
- 每张照片仍有目标数量预算，默认 10。任一次全图结果达到预算上限时，
  不能证明标注完整，必须复核；这与照片库的照片数量不设上限无关。

例如前两次对应 IoU 为 0.91、0.82、0.74，前两个框保留，第三个目标
单独复核。全部目标解决后才进入 `done`；部分解决记为 `partial_review`。
每个目标的候选框、状态、匹配 IoU、裁图范围和局部调用记录保存在
`state.sqlite3` 的任务状态中。

完整输出仍在 `results/` 与 `annotated/`。部分结果在
`partial_results/` 与 `partial_annotated/`，只供复核，不按完整训练样本
使用；`needs_review.json` 同时包括完全未通过和部分通过的任务。
跨运行数据库将 `partial_review` 纳入机器待复核，不自动复用或自动重试。
画框命令会同时补齐两类图片，并保持目录分开。

升级前先只读预览新流程的任务数（不请求模型、不采集照片）：

```powershell
Set-Location 'D:\codex\new-agent'
python -m agentized_workflow.cli photos labels plan `
  --photos-root .\photos --metadata-csv .\photos\workflow_metadata.csv `
  --env-file .\.env --workflow-version 2 --threshold 0.75
```

已有版本 1 结果不会被当作版本 2 结果复用。默认启动遇到已完成照片的
流程变化会报错；确认愿意重新标注后才显式启动：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\start_agent.ps1 `
  -Contact '工作人员或项目联系邮箱' -WorkflowVersion 2 -AllowNewProfile
```

该命令包含采集和用户照片导入。若要继续旧流程并复用旧结果，使用
`-WorkflowVersion 1`，未指定阈值时采用旧默认 0.7。
`resume_agent.ps1` 默认从 SQLite 读取原版本及阈值，不会把旧运行切到新规则。
底层 Python 标注命令及 `photos labels plan` 为兼容旧调用默认版本 1；
直接运行版本 2 必须传 `--workflow-version 2`。

人工处理部分结果时，先查看该 JSON 中的 `targets` 和已通过的
`detections`，核对整张照片。现有 `photos labels correct` 命令接受
**最终完整框列表**：将保留的已通过框及人工修正后的其他框一并填写到
`boxes`，使用原图像素坐标。它会生成新的人工认可结果，旧部分结果和
调用历史保留。此命令不是“只填一个待修正框”的接口，也不是逐目标
再次模型请求；仅未完成照片不能直接执行 `review --decision approve`。
若需要重新启动模型处理，仍须明确创建新流程/新运行，不会因每天启动
自动重复局部请求。

### 按物种和可见性查找画框照片

画框照片按可信物种名称及 `visibility_route` 分类。这里的“完整/局部/
混合”是照片目标的可见性分类，与两次定位或第三次复核的调用次数不同。
各物种固定保留三个分类目录，没有照片的分类为空：

```text
runs/<运行目录>/
├─ results/<任务ID>.json
├─ annotated/
│  ├─ index.csv
│  └─ <物种名称>/
│     ├─ 01_完整或大部分可见/<原照片名>__<任务ID>.jpg
│     ├─ 02_局部可见/<原照片名>__<任务ID>.jpg
│     └─ 03_混合可见/<原照片名>__<任务ID>.jpg
├─ partial_results/<任务ID>.json
└─ partial_annotated/<同样的物种与分类结构>
```

输出名去掉原照片扩展名，加入任务 ID，因此不同目录中同名的原照片也
不会互相覆盖。可信名称中若包含 Windows 禁用字符或名称过长，则目录
名作安全转换并带短哈希；索引保留完整原物种名。缺失或未知的可见性
分类明确计为失败，不猜测类别。

`annotated/index.csv` 可以用 Excel 打开，包含物种、可见性分类、原照片
相对路径、任务 ID、画框照片路径及对应的规范 JSON 路径。索引路径
相对于本次运行目录；部分结果的索引在 `partial_annotated/index.csv`。
这些目录和索引用于查找照片，不能据此认定人工已认可；人工状态仍按
`photos labels list` 和 `queue` 查看。人工修正的图片使用同样的分类结构，
位于 `runs/manual-corrections/annotated/`。

已有平铺画框图也使用原离线命令整理：

```powershell
Set-Location 'D:\codex\new-agent'
$env:PYTHONPATH = (Join-Path (Get-Location) 'src')
python -m agentized_workflow.render_labels `
  --photos-root .\photos --run-dir .\runs\agent-20260922-142848
```

它按已有 JSON 移动当前画框图，不调用模型；缺失或过期的图片才在本地
重新绘制。原照片、规范 JSON、SQLite 状态及人工审核记录不迁移。
输出计数为 `rendered`（新绘制）、`moved`（已有图片整理）、`skipped`
（当前位置已是当前图片）、`failed`。重复执行应只出现 `skipped`。
若旧平铺图片与分类目录中已有图片内容不同，会明确失败并保留两者；
不能通过删除 JSON 或改 SQLite 绕过冲突。完全一致的派生副本可合并。

数量检查必须递归进入分类目录：

```powershell
(Get-ChildItem -LiteralPath .\runs\agent-20260922-142848\annotated `
  -Recurse -File -Filter '*.jpg').Count
```

这是展示目录变更，模型请求和结果判定的流程指纹保持不变，不会因此
触发旧照片重标。历史版本 1 没有 `mixed` 分类，其混合目录可为空；
此整理不重新判断旧照片的可见性。

### 蟹类紧框与头胸甲不对称复核

版本 2 的全图及局部定位提示词均先以可见头胸甲作为个体锚点，再纳入
可靠归属于同一个体的可见螯与步足。最终框是这些可见部分的最小外接
矩形，不额外扩边，也不按左右对称补全隐藏身体。蟹类判断只比较
**头胸甲中线至左右外缘**，不能用整个动物框的两侧宽度代替。

每个模型返回框必须包含 `carapace_check`，其 `status` 为：

| 状态 | 含义 | 此项是否触发人工复核 |
| --- | --- | --- |
| `not_applicable` | 非蟹类 | 否 |
| `symmetric` | 头胸甲两侧可比较，未见明显异常 | 否 |
| `unassessable` | 斜视、遮挡、裁切或模糊，无法可靠比较 | 否，仍须通过原有对应关系和 IoU 检查 |
| `suspected_asymmetry` | 可比较的头胸甲一侧明显异常扩展，不能合理用视角解释，或疑似混入邻体/背景 | 是，必须提供非空 `evidence` |

这是模型报告的**定性疑点**，尚未设定“两侧宽度比大于某个数值”的
几何阈值，也没有新增头胸甲分割或中线测量工具。大螯、伸展步足造成的
整体框不对称不能作为本项疑点。模型可能漏报或误报，必须用代表性
真实照片抽查其效果；两次框一致不等于与人工标准一致。

两次全图定位中任一次报告疑点，相关目标即记为 `needs_review`、
原因 `carapace_asymmetry`，即使 IoU=1 也不自动接受，并且不会仅因这项
疑点发起第三次模型请求。若原本因 IoU 未通过而做局部复核，该次结果
发现疑点也转人工；对疑点框有保守重叠关联的邻近目标一并转人工，
避免疑似错误框继续作为已通过输出。其他无疑点的已通过目标保留。

部分通过的照片进入 `partial_review`；没有通过目标的照片进入
`needs_review`。都进入复核队列，跨运行默认不复用、不自动重试。
两次候选框和 `carapace_check.evidence` 保存在 SQLite 及
`partial_results/<任务ID>.json` 的 `targets.first_box/second_box` 中；
第三次局部结果和依据在对应目标的 `review.result.boxes` 中。
局部上下文发现邻近目标疑点时，依据可能位于另一目标的 `review`
记录，应结合整张照片核对。`partial_annotated/` 目前只画已通过框；
待审核候选框应从上述记录查看，不能把该画框图当作完整标注。

通过主原因筛选此类案例（预算上限或其他异常也可能是主原因，仍应查看
队列案例的 `targets`）：

```powershell
Set-Location 'D:\codex\new-agent'
$cases = python -m agentized_workflow.cli photos labels queue --photos-root .\photos | ConvertFrom-Json
$cases | Where-Object { $_.reason -eq 'carapace_asymmetry' } |
  Select-Object id, local_path, result_path, reason
```

人工核对候选框后，用下方 `photos labels correct` 提交**最终完整框列表**。
候选框本来正确时也可以提交相同坐标，并注明人工核对结论；若框错误则
修正后提交。它会生成新的人工认可记录并保留旧模型记录，不应直接
批准不完整的机器结果，也不应手工修改 SQLite 来解除待审核状态。

本次提示词、输出契约和检查规则改变会产生新的版本 2 流程指纹。
已有结果保留，但不会被宣称已通过新增检查；历史照片不会自动重标。
启用新规则前先执行上文的版本 2 `photos labels plan`，确认数量后再
显式使用 `-AllowNewProfile`。旧版本 2 未完成运行不能在新实现下直接
续跑：流程指纹检查会明确报错；应使用原实现续跑，或明确创建新的
运行，不能覆盖原 `label_profile.json` 绕过检查。版本 1 原流程未改动。

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
`runs/<运行目录>/annotated/<物种>/<可见性分类>/<原照片名>__<任务ID>.jpg`；被复用照片的原结果仍在
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
  --env-file .\.env --workflow-version 2
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
版本 2 对每个框按新阈值复核；这会触发相应模型调用，运行前应先检查 `photos labels plan` 的数量。
新流程日常运行保持默认 0.75；旧流程使用 `-WorkflowVersion 1` 和原阈值。

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
last task-state update, and the number of JPEG files recursively under `annotated/` equals the
number of `done` tasks. `needs_review` is a scoped terminal for individual
images and must retain its recorded reason.
