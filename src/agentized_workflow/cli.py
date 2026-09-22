from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .collection_runner import collect_pending
from .legacy_provenance import migrate_legacy_provenance
from .metadata import load_tasks
from .photo_library import IngestReport, PhotoLibrary
from .planner import JsonPlanner
from .providers import ChatPlanner, FixtureVision, HttpChat, HttpVision
from .species_catalog import sync_catalog
from .storage import PROJECT_ROOT, Store
from .workflow import Engine


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


def _legacy_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bounded metadata grounding: isolated outputs, no implicit retries")
    parser.add_argument("--input-dir", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--metadata-csv", type=Path)
    source.add_argument("--directory-map", type=Path, help="JSON object mapping exact relative directories to trusted species names")
    provider = parser.add_mutually_exclusive_group(required=True)
    provider.add_argument("--fixture", type=Path, help="offline response fixture")
    provider.add_argument("--live", action="store_true", help="explicitly enable real model requests")
    parser.add_argument("--run-dir", type=Path, default=PROJECT_ROOT / "runs/default")
    parser.add_argument("--threshold", type=float, default=.7)
    parser.add_argument("--max-targets", type=int, default=10)
    parser.add_argument("--model", default="qwen3-vl-plus")
    parser.add_argument("--base-url", default="https://dashscope.aliyuncs.com/compatible-mode/v1")
    parser.add_argument("--planner-model", help="optional exception-only planning LLM, requires --live")
    parser.add_argument("--api-key-env", default="DASHSCOPE_API_KEY")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--structured", action="store_true", help="request strict JSON schema if supported by provider")
    return parser


def _legacy_main(argv: Sequence[str]) -> int:
    args = _legacy_parser().parse_args(argv)
    if args.planner_model and not args.live:
        raise ValueError("--planner-model requires --live")
    mapping = json.loads(args.directory_map.read_text(encoding="utf-8-sig")) if args.directory_map else None
    specs = load_tasks(args.input_dir, args.metadata_csv, directory_map=mapping)
    planner = None
    if args.fixture:
        vision = FixtureVision(json.loads(args.fixture.read_text(encoding="utf-8-sig")))
    else:
        transport = HttpChat(args.base_url, api_key_env=args.api_key_env, timeout=args.timeout)
        vision = HttpVision(transport, model=args.model, structured=args.structured)
        if args.planner_model:
            planner = JsonPlanner(ChatPlanner(transport, model=args.planner_model, structured=args.structured))
    store = Store(args.run_dir)
    engine = Engine(store, vision, planner, threshold=args.threshold, max_targets=args.max_targets)
    for spec in specs:
        engine.add(spec)
    counts = {"done": 0, "needs_review": 0, "run_dir": str(store.root)}
    for spec in specs:
        counts[engine.run(spec.task_id).phase] += 1
    print(json.dumps(counts, ensure_ascii=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["photos"]:
        return _photo_main(arguments[1:])
    return _legacy_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
