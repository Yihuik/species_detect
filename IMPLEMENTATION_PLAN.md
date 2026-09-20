# Metadata Agent Implementation Plan

**Goal:** 在 D:/codex/new-agent 独立实现 metadata 工作流前半部分，原项目只读。
**Architecture:** 确定性状态机控制流程与调用预算；视觉模型提供可见性与独立定位；Python 校验、匹配、转换并落盘；受约束规划器只在异常时选择 retry_once 或 needs_review。人工审核是自动流程终止出口。
**Tech Stack:** Python >=3.10, Pydantic 2, SQLite, pytest, Pillow；可选 OpenAI-compatible HTTP transport。
**Spec:** 用户本次请求及目录变更；原项目 D:/codex/qwen-vl。

## Global Constraints
- 不修改、覆盖、删除原项目任何文件，不导入会写入原项目的脚本。
- 新文件和运行产物仅放在 D:/codex/new-agent。
- 物种来自显式信任的 metadata.csv 或物种目录映射；不猜测、不改名。
- 双次独立定位，最多一次第三次调用；规划器不能接受结果、修改名称或调用任意工具。
- 无真实付费请求；以离线测试验收。

## Files and interfaces
- src/agentized_workflow/models.py: immutable TaskSpec, Box, Localization, Decision, persisted TaskState。
- metadata.py: read-only CSV/directory ingestion, exact names, image identity/dimensions。
- tools.py: fixed registry, bbox IoU/perfect matching/pixel conversion, Vision protocol。
- storage.py: SQLite state+audit atomic transactions, review queue, process lock, atomic JSON exports。
- workflow.py: Engine.add/step/run, persisted intents, resume and budget enforcement。
- planner.py: bounded JSON planner with predefined action enum and safe fallback。
- providers.py: stateless HTTP vision/planner adapters, timeout, no transport retries。
- cli.py: offline fixture and optional explicit live provider, queue/audit exports。
- tests/: behavioral tests; examples/: runnable offline fixture; verification/: test evidence and old-file hashes。

## Task 1: contracts, provenance and tools
- [x] Write tests for invalid boxes, coordinate conversion, unordered matching, count mismatch, CSV conflict, unknown tools.
- [x] Run pytest and record missing-feature failure.
- [x] Implement Pydantic contracts, metadata ingestion and fixed registry.
- [x] Run focused tests.

## Task 2: durable deterministic workflow
- [x] Write tests for two passes, single third pass, exhausted failure, empty output, changed provenance, crash recovery, terminal idempotency, audit/review persistence.
- [x] Run failing tests.
- [x] Implement SQLite state/events transaction and runner lock; commit invocation intent before request; recover pending intent to needs_review.
- [x] Implement one-to-one IoU comparison; third pass compared with each earlier valid pass; all outputs carry immutable trusted name.
- [x] Run workflow tests and inspect output files.

## Task 3: constrained planning and runnable adapters
- [x] Write tests for legal retry/review, arbitrary action, injected species/extra fields, planner failure, stateless requests and CLI fixture.
- [x] Run failing tests.
- [x] Implement exception-only planner, no retries in HTTP transport, structured prompts and schema validation.
- [x] Run complete offline suite and CLI twice; confirm no duplicate calls on resume.

## Acceptance
- [x] Tests pass, output schemas validated, queue and audit inspectable.
- [x] Recompute original file hashes and report changed/missing/new files.
- [x] Document commands, lifecycle, matching rule, thresholds and live-validation limitations.

No commits are made to the original project. Execution continues inline as explicitly requested.
