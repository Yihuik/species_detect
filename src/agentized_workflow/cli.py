from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from typing import Sequence
from urllib.parse import urlsplit

from .collection_runner import collect_pending
from .legacy_provenance import migrate_legacy_provenance
from .label_registry import LabelRegistry, profile_for
from .metadata import load_tasks
from .photo_library import IngestReport, PhotoLibrary
from .planner import JsonPlanner
from .providers import ChatPlanner, FixtureVision, HttpChat, HttpVision
from .species_catalog import sync_catalog
from .storage import PROJECT_ROOT, Store
from .workflow import Engine
from .target_workflow import TargetEngine
from .target_providers import TargetFixtureVision, TargetHttpVision


def _project_paths(project_root: Path) -> tuple[Path, Path, Path, Path]:
    root = Path(project_root).resolve()
    return (
        root / "config" / "species.txt",
        root / "config" / "species_taxonomy.json",
        root / "catalog",
        root / "photos",
    )


def _photo_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="同步可信物种目录并维护去重照片库")
    commands = parser.add_subparsers(dest="photo_command", required=True)
    for command in ("sync", "ingest", "collect", "migrate-provenance"):
        child = commands.add_parser(command)
        child.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    commands.choices["ingest"].add_argument(
        "--batch", type=Path, help="导入一个指定批次；默认扫描 input/inbox 下的批次目录"
    )
    commands.choices["collect"].add_argument(
        "--contact", required=True, help="公开联系邮箱或项目 URL，写入采集器 User-Agent"
    )
    commands.choices["collect"].add_argument(
        "--allow-partial", action="store_true",
        help="来源局部失败时仍刷新照片元数据并继续后续工作流；失败物种保持 pending",
    )
    commands.choices["migrate-provenance"].add_argument(
        "--legacy-root", type=Path, required=True,
        help="只读导入其 collector_state.sqlite3 和图片侧车 JSON 的旧采集目录",
    )
    return parser


def _photo_main(argv: Sequence[str]) -> int:
    args = _photo_parser().parse_args(argv)
    species_file, taxonomy_file, catalog_root, photos_root = _project_paths(args.project_root)
    catalog_root = sync_catalog(species_file, taxonomy_file, catalog_root)
    if args.photo_command == "sync":
        print(json.dumps({"catalog": str(catalog_root), "status": "synced"}, ensure_ascii=False))
        return 0
    if args.photo_command == "collect":
        return collect_pending(
            args.project_root, catalog_root, photos_root, args.contact, allow_partial=args.allow_partial
        )
    if args.photo_command == "migrate-provenance":
        report = migrate_legacy_provenance(args.legacy_root, photos_root)
        print(json.dumps(report.__dict__, ensure_ascii=False))
        return 0

    library = PhotoLibrary(catalog_root, photos_root)
    batches = [args.batch.resolve()] if args.batch else _inbox_batches(Path(args.project_root) / "input" / "inbox")
    totals = IngestReport()
    for batch in batches:
        totals = PhotoLibrary._merge_report(totals, library.ingest_batch(batch))
    metadata = library.write_workflow_metadata()
    print(json.dumps({"batches": len(batches), "metadata": str(metadata), **totals.__dict__}, ensure_ascii=False))
    return 0


def _inbox_batches(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    batches = [path for path in sorted(inbox.iterdir()) if path.is_dir()]
    direct_files = [path for path in inbox.iterdir() if path.is_file()]
    return ([inbox] if direct_files else []) + batches


def _labels_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description="跨运行标注结果与人工复核")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "import-run", "review", "correct", "queue", "list", "render-missing"):
        child = commands.add_parser(name)
        child.add_argument("--photos-root", type=Path, required=True)
    for name in ("plan", "import-run"):
        child = commands.choices[name]
        child.add_argument("--model", default="qwen3-vl-plus")
        child.add_argument("--base-url")
        child.add_argument("--env-file", type=Path, help="read only DASHSCOPE_BASE_URL from project .env")
        child.add_argument("--threshold", type=float)
        child.add_argument("--workflow-version", type=int, choices=(1, 2), default=1)
        child.add_argument("--max-targets", type=int, default=10)
        child.add_argument("--structured", action="store_true")
        child.add_argument("--planner-model")
    commands.choices["plan"].add_argument("--metadata-csv", type=Path)
    commands.choices["plan"].add_argument("--retry-rejected", action="store_true")
    commands.choices["plan"].add_argument("--retry-case-id", type=int)
    commands.choices["import-run"].add_argument("--run-dir", type=Path, required=True)
    for name in ("review", "correct"):
        child = commands.choices[name]
        child.add_argument("--case-id", type=int, required=True)
        child.add_argument("--reviewer", required=True)
        child.add_argument("--reason", required=True)
    commands.choices["review"].add_argument("--decision", choices=("approve", "reject"), required=True)
    commands.choices["correct"].add_argument("--boxes-file", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command in {'plan', 'import-run'} and args.threshold is None:
        args.threshold = .75 if args.workflow_version == 2 else .7
    if args.command in {'plan', 'import-run'} and args.workflow_version == 2 and args.planner_model:
        raise ValueError('version 2 uses bounded target review, not a planner model')
    if args.command == "plan":
        runs_root = args.photos_root.resolve().parent / "runs"
        unmanaged = [str(path.parent) for path in runs_root.glob("agent-*/state.sqlite3")
                     if not (path.parent / "label_profile.json").is_file()]
        if unmanaged:
            raise ValueError("historical runs require explicit photos labels import-run before launch: " +
                             ", ".join(unmanaged))
    registry = LabelRegistry(args.photos_root)
    if args.command in {"plan", "import-run"}:
        if bool(args.base_url) == bool(args.env_file):
            raise ValueError("supply exactly one of --base-url or --env-file")
        base_url = args.base_url
        if args.env_file:
            for line in args.env_file.read_text(encoding="utf-8-sig").splitlines():
                match = re.fullmatch(r"\s*(?:export\s+)?DASHSCOPE_BASE_URL=(.*)", line)
                if match:
                    base_url = match.group(1).strip().strip("\"'")
        parsed = urlsplit(base_url or "")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment):
            raise ValueError("DASHSCOPE_BASE_URL must be an explicit HTTPS endpoint")
        profile = profile_for(args.model, base_url, args.threshold, args.max_targets,
                              args.structured, args.planner_model, version=args.workflow_version)
    if args.command == "import-run":
        report = registry.import_run(args.run_dir, profile, threshold=args.threshold,
                                     max_targets=args.max_targets, model=args.model, version=args.workflow_version)
        print(json.dumps(report, ensure_ascii=False))
        return 1 if report["invalid"] else 0
    if args.command == "plan":
        metadata = args.metadata_csv or args.photos_root / "workflow_metadata.csv"
        specs = load_tasks(args.photos_root, metadata)
        report = registry.plan(specs, profile, retry_rejected=args.retry_rejected,
                               retry_case_id=args.retry_case_id)
        print(json.dumps(report, ensure_ascii=False))
        return 0
    if args.command == "review":
        registry.review(args.case_id, args.decision, args.reviewer, args.reason)
        print(json.dumps({"case_id": args.case_id, "decision": args.decision}))
        return 0
    if args.command == "correct":
        boxes = json.loads(args.boxes_file.read_text(encoding="utf-8-sig"))["boxes"]
        case_id = registry.correct(args.case_id, boxes, args.reviewer, args.reason)
        print(json.dumps({"case_id": case_id, "review_status": "approved"}))
        return 0
    if args.command == "queue":
        print(json.dumps(registry.review_queue(), ensure_ascii=False))
        return 0
    if args.command == "render-missing":
        report = registry.render_missing()
        print(json.dumps(report, ensure_ascii=False))
        return 1 if report["failed"] else 0
    print(json.dumps(registry.list_cases(), ensure_ascii=False))
    return 0


def _legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded metadata grounding: isolated outputs, no implicit retries")
    parser.add_argument("--input-dir", type=Path)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--metadata-csv", type=Path)
    source.add_argument("--directory-map", type=Path, help="JSON object mapping exact relative directories to trusted species names")
    provider = parser.add_mutually_exclusive_group(required=True)
    provider.add_argument("--fixture", type=Path, help="offline response fixture")
    provider.add_argument("--live", action="store_true", help="explicitly enable real model requests")
    parser.add_argument("--run-dir", type=Path, default=PROJECT_ROOT / "runs/default")
    parser.add_argument("--threshold", type=float)
    parser.add_argument("--workflow-version", type=int, choices=(1, 2))
    parser.add_argument("--max-targets", type=int, default=10)
    parser.add_argument("--model", default="qwen3-vl-plus")
    parser.add_argument("--base-url", help="required HTTPS model endpoint for --live")
    parser.add_argument("--planner-model", help="optional exception-only planning LLM, requires --live")
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--structured", action="store_true", help="request strict JSON schema if supported by provider")
    parser.add_argument("--label-registry", type=Path, help="local photos root for workflow-scoped reuse")
    parser.add_argument("--retry-rejected", action="store_true", help="explicitly relabel human-rejected results")
    parser.add_argument("--retry-case-id", type=int, help="relabel only this rejected case")
    parser.add_argument("--allow-new-profile", action="store_true",
                        help="explicitly permit relabeling when model or workflow rules changed")
    parser.add_argument("--resume-existing", action="store_true", help="resume recorded run tasks, ignoring current photo metadata")
    return parser


def _legacy_main(argv: Sequence[str]) -> int:
    args = _legacy_parser().parse_args(argv)
    if not args.resume_existing and (args.input_dir is None or
            (args.metadata_csv is None) == (args.directory_map is None)):
        raise ValueError("supply --input-dir and exactly one trusted metadata source")
    if args.resume_existing and not (args.run_dir / "state.sqlite3").is_file():
        raise ValueError("--resume-existing requires an existing run state.sqlite3")
    if args.planner_model and not args.live:
        raise ValueError("--planner-model requires --live")
    if args.live and not args.base_url:
        raise ValueError("--base-url is required for --live")
    if args.resume_existing:
        store = Store(args.run_dir)
        states = store.all_states()
        specs = [state.spec for state in states]
        if not specs:
            raise ValueError("existing run has no tasks")
        if any(state.policy != states[0].policy for state in states):
            raise ValueError('run contains mixed policies')
        original = states[0].policy
        if args.workflow_version is not None and args.workflow_version != original.version:
            raise ValueError('resume must use original workflow version')
        args.workflow_version = original.version
        if args.threshold is None:
            args.threshold = original.threshold
        if args.max_targets != original.max_targets:
            raise ValueError('resume must use original max-targets')
    else:
        mapping = json.loads(args.directory_map.read_text(encoding="utf-8-sig")) if args.directory_map else None
        specs = load_tasks(args.input_dir, args.metadata_csv, directory_map=mapping)
    args.workflow_version = args.workflow_version or 1
    if args.threshold is None:
        args.threshold = .75 if args.workflow_version == 2 else .7
    if args.workflow_version == 2 and args.planner_model:
        raise ValueError('version 2 uses bounded target review, not a planner model')
    registry = LabelRegistry(args.label_registry) if args.label_registry else None
    profile = profile_for(args.model, args.base_url if args.live else None,
                          args.threshold, args.max_targets, args.structured, args.planner_model, version=args.workflow_version)
    reused = review = 0
    if registry and not args.resume_existing:
        preflight = registry.plan(specs, profile, retry_rejected=args.retry_rejected,
                                  retry_case_id=args.retry_case_id)
        if preflight["profile_changed"] and not args.allow_new_profile:
            raise ValueError("labeling profile changed for existing photos; pass --allow-new-profile explicitly")
        pending = []
        for spec in specs:
            match = registry.lookup(spec, profile, retry_rejected=args.retry_rejected,
                                    retry_case_id=args.retry_case_id)
            if match.action == "reuse":
                reused += 1
            elif match.action == "review":
                review += 1
            elif match.action == "resume":
                raise ValueError("unfinished task belongs to " + str(match.run_dir) +
                                 "; resume that run before creating a new one")
            else:
                pending.append(spec)
        specs = pending
        if not specs:
            print(json.dumps({"new": 0, "reused": reused, "review": review,
                              "run_dir": None}, ensure_ascii=True))
            return 0
    planner = None
    if args.fixture:
        vision_class = TargetFixtureVision if args.workflow_version == 2 else FixtureVision
        vision = vision_class(json.loads(args.fixture.read_text(encoding="utf-8-sig")))
    else:
        transport = HttpChat(args.base_url, api_key_env=args.api_key_env, timeout=args.timeout)
        vision_class = TargetHttpVision if args.workflow_version == 2 else HttpVision
        vision = vision_class(transport, model=args.model, structured=args.structured)
        if args.planner_model:
            planner = JsonPlanner(ChatPlanner(transport, model=args.planner_model, structured=args.structured))
    store = Store(args.run_dir)
    if registry:
        profile_path = store.root / "label_profile.json"
        if profile_path.is_file():
            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            if saved.get("profile") != profile:
                raise ValueError("run labeling profile changed; use original model and policy")
        elif args.resume_existing:
            raise ValueError("run has no labeling profile; import it explicitly before registry resume")
        else:
            store.atomic_json("label_profile.json", {"profile": profile, "model": args.model,
                             "threshold": args.threshold, "max_targets": args.max_targets,
                             "workflow_version": args.workflow_version})
    engine = (TargetEngine(store, vision, threshold=args.threshold, max_targets=args.max_targets)
              if args.workflow_version == 2 else
              Engine(store, vision, planner, threshold=args.threshold, max_targets=args.max_targets))
    if not args.resume_existing:
        for spec in specs:
            engine.add(spec)
            if registry:
                registry.record(store.get(spec.task_id), store.root, profile)
    counts = {"done": 0, "needs_review": 0, "partial_review": 0, "run_dir": str(store.root),
              "new": len(specs), "reused": reused, "review": review}
    for spec in specs:
        state = store.get(spec.task_id)
        terminal_outputs_exist = (
            (state.phase != "done" or (store.root / "results" / f"{spec.task_id}.json").is_file())
            and (state.policy.version != 2 or not state.targets or state.phase == 'done' or
                 (store.root / 'partial_results' / f'{spec.task_id}.json').is_file())
            and (store.root / "needs_review.json").is_file()
            and (store.root / "audit.json").is_file()
            and (registry is None or registry.run_result_valid(state, store.root))
        )
        if state.phase in {"done", "needs_review", "partial_review"} and terminal_outputs_exist:
            if registry:
                registry.record(state, store.root, profile)
            counts[state.phase] += 1
            continue
        state = engine.run(spec.task_id)
        if registry:
            registry.record(state, store.root, profile)
        counts[state.phase] += 1
    print(json.dumps(counts, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:2] == ["photos", "labels"]:
        return _labels_main(arguments[2:])
    if arguments[:1] == ["photos"]:
        return _photo_main(arguments[1:])
    return _legacy_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
