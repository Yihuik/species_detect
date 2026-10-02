"""Local, workflow-scoped labels and append-only human review decisions."""

from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sqlite3
from uuid import uuid4

from .metadata import digest
from .models import TaskSpec, TaskState
from .tools import pixels


WORKFLOW_ID = "agentized_metadata_v1"
PROFILE_SCHEMA = 1


def profile_for(model: str, base_url: str | None, threshold: float, max_targets: int,
                structured: bool, planner_model: str | None) -> str:
    """Fingerprint model requests and decisions without recording a credential."""
    root = Path(__file__).resolve().parent
    payload = {
        "schema": PROFILE_SCHEMA, "workflow": WORKFLOW_ID, "model": model,
        "endpoint": (base_url or "fixture"), "threshold": threshold,
        "max_targets": max_targets, "structured": structured,
        "planner_model": planner_model,
        "implementation": {name: digest(root / name) for name in
                           ("providers.py", "workflow.py", "tools.py")},
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Lookup:
    action: str
    review_status: str = "unreviewed"
    case_id: int | None = None
    run_dir: str | None = None


class LabelRegistry:
    def __init__(self, photos_root: Path):
        self.photos_root = Path(photos_root).resolve(strict=True)
        self.path = self.photos_root / "photo_library.sqlite3"
        if not self.path.is_file():
            raise FileNotFoundError(f"photo library database is missing: {self.path}")
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS label_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
                    image_sha256 TEXT NOT NULL,
                    species TEXT NOT NULL,
                    workflow_profile TEXT NOT NULL,
                    run_dir TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    machine_status TEXT NOT NULL CHECK(machine_status IN ('done','needs_review','in_progress','not_applicable')),
                    result_kind TEXT NOT NULL DEFAULT 'model' CHECK(result_kind IN ('model','human_correction')),
                    result_path TEXT,
                    result_sha256 TEXT,
                    reason TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_dir, task_id, workflow_profile)
                );
                CREATE INDEX IF NOT EXISTS label_lookup ON label_results(asset_id,workflow_profile,id);
                CREATE TABLE IF NOT EXISTS review_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    label_result_id INTEGER NOT NULL REFERENCES label_results(id),
                    decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
                    reviewer TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def _asset(self, spec: TaskSpec):
        with self._connect() as db:
            row = db.execute(
                "SELECT asset_id,sha256,species,local_path FROM assets "
                "WHERE sha256=? AND species=? AND status='active'",
                (spec.image_sha256, spec.species),
            ).fetchone()
        if row is None:
            return None
        expected = (self.photos_root / row["local_path"]).resolve()
        if expected != Path(spec.image_path).resolve():
            return None
        return row

    @staticmethod
    def _result_valid(path: Path, spec: TaskSpec, expected_sha: str | None = None) -> bool:
        try:
            if expected_sha and digest(path) != expected_sha:
                return False
            data = json.loads(path.read_text(encoding="utf-8"))
            return (data.get("workflow") == WORKFLOW_ID
                    and data.get("source_image") == spec.source_image
                    and data.get("metadata", {}).get("species") == spec.species
                    and data.get("image_width") == spec.width
                    and data.get("image_height") == spec.height
                    and isinstance(data.get("detections"), list))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    @classmethod
    def _result_matches_state(cls, path: Path, state: TaskState) -> bool:
        if state.phase != "done" or state.selected_attempt is None or not cls._result_valid(path, state.spec):
            return False
        try:
            selected = state.attempts[state.selected_attempt - 1]
            data = json.loads(path.read_text(encoding="utf-8"))
            expected = [
                {"species": state.spec.species, "bbox": list(box.bbox),
                 "bbox_pixel": pixels(box, state.spec.width, state.spec.height)}
                for box in selected.result.boxes
            ]
            return (data.get("detections") == expected
                    and data.get("selected_attempt") == state.selected_attempt
                    and data.get("visibility_route") == state.route
                    and data.get("policy") == state.policy.model_dump())
        except (IndexError, AttributeError, OSError, ValueError, TypeError):
            return False

    def record(self, state: TaskState, run_dir: Path, profile: str) -> int:
        spec = state.spec
        asset = self._asset(spec)
        if asset is None or digest(Path(spec.image_path)) != spec.image_sha256:
            raise ValueError("run task does not match an active photo-library asset")
        root = Path(run_dir).resolve(strict=True)
        result = root / "results" / f"{spec.task_id}.json"
        result_path = None
        result_sha = None
        if state.phase == "done":
            if not self._result_matches_state(result, state):
                raise ValueError(f"completed task has no valid result: {spec.task_id}")
            result_path = str(result)
            result_sha = digest(result)
        machine_status = state.phase if state.phase in {"done", "needs_review"} else "in_progress"
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                "INSERT INTO label_results(asset_id,image_sha256,species,workflow_profile,run_dir,task_id,"
                "machine_status,result_path,result_sha256,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_dir,task_id,workflow_profile) DO UPDATE SET "
                "machine_status=excluded.machine_status,result_path=excluded.result_path,"
                "result_sha256=excluded.result_sha256,reason=excluded.reason",
                (asset["asset_id"], spec.image_sha256, spec.species, profile, str(root), spec.task_id,
                 machine_status, result_path, result_sha, state.reason, now),
            )
            row = db.execute("SELECT id FROM label_results WHERE run_dir=? AND task_id=? AND workflow_profile=?",
                             (str(root), spec.task_id, profile)).fetchone()
        return int(row["id"])

    def lookup(self, spec: TaskSpec, profile: str, *, retry_rejected: bool = False,
               retry_case_id: int | None = None) -> Lookup:
        asset = self._asset(spec)
        if asset is None:
            return Lookup("pending")
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM label_results WHERE asset_id=? AND image_sha256=? AND species=? "
                "AND workflow_profile=? ORDER BY id DESC LIMIT 1",
                (asset["asset_id"], spec.image_sha256, spec.species, profile),
            ).fetchone()
            if row is None:
                return Lookup("pending")
            review = db.execute("SELECT decision FROM review_events WHERE label_result_id=? "
                                "ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
        decision = review["decision"] if review else "unreviewed"
        if decision == "reject":
            retry = retry_rejected or retry_case_id == int(row["id"])
            return Lookup("pending" if retry else "review", "rejected", int(row["id"]))
        if row["machine_status"] == "needs_review":
            return Lookup("review", decision, int(row["id"]))
        if row["machine_status"] == "in_progress":
            return Lookup("resume", decision, int(row["id"]), row["run_dir"])
        result = Path(row["result_path"]) if row["result_path"] else None
        if result is None or not self._result_valid(result, spec, row["result_sha256"]):
            if row["result_kind"] == "human_correction":
                return Lookup("review", "corrupt_result", int(row["id"]))
            return Lookup("resume", decision, int(row["id"]), row["run_dir"])
        return Lookup("reuse", "approved" if decision == "approve" else "unreviewed", int(row["id"]))

    def run_result_valid(self, state: TaskState, run_dir: Path) -> bool:
        if state.phase != "done":
            return True
        return self._result_matches_state(Path(run_dir) / "results" /
                                          f"{state.spec.task_id}.json", state)

    def review(self, case_id: int, decision: str, reviewer: str, reason: str) -> None:
        if decision not in {"approve", "reject"} or not reviewer.strip() or not reason.strip():
            raise ValueError("review needs approve/reject, reviewer, and reason")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT machine_status,result_kind,asset_id,workflow_profile FROM label_results WHERE id=?",
                             (case_id,)).fetchone()
            if row is None:
                raise KeyError(case_id)
            latest = db.execute("SELECT MAX(id) FROM label_results WHERE asset_id=? AND workflow_profile=?",
                                (row["asset_id"], row["workflow_profile"])).fetchone()[0]
            if latest != case_id:
                raise ValueError("result was superseded by a newer label")
            if row["machine_status"] != "done" and row["result_kind"] != "human_correction":
                raise ValueError("only completed labels can be approved or rejected")
            db.execute("INSERT INTO review_events(label_result_id,decision,reviewer,reason,created_at) "
                       "VALUES (?,?,?,?,?)", (case_id, decision, reviewer.strip(), reason.strip(),
                                             datetime.now(timezone.utc).isoformat()))

    def review_history(self, case_id: int) -> list[dict]:
        with self._connect() as db:
            rows = db.execute("SELECT decision,reviewer,reason,created_at FROM review_events "
                              "WHERE label_result_id=? ORDER BY id", (case_id,)).fetchall()
        return [dict(row) for row in rows]

    def import_run(self, run_dir: Path, profile: str, *, threshold: float,
                   max_targets: int, model: str) -> dict[str, int]:
        """Adopt a historical run only after an operator declares its model profile."""
        root = Path(run_dir).resolve(strict=True)
        if not (root / "state.sqlite3").is_file():
            raise ValueError("run has no state.sqlite3")
        from .storage import Store

        store = Store(root)
        profile_path = root / "label_profile.json"
        if profile_path.is_file():
            saved = json.loads(profile_path.read_text(encoding="utf-8"))
            if saved.get("profile") != profile:
                raise ValueError("run already belongs to a different labeling profile")
        states = store.all_states()
        if not states:
            raise ValueError("run has no tasks")
        if any(state.policy.threshold != threshold or state.policy.max_targets != max_targets
               for state in states):
            raise ValueError("historical run policy does not match declared threshold/max-targets")
        counts = {"done": 0, "needs_review": 0, "in_progress": 0, "invalid": 0}
        for state in states:
            try:
                self.record(state, root, profile)
            except (OSError, ValueError, KeyError):
                counts["invalid"] += 1
            else:
                status = state.phase if state.phase in {"done", "needs_review"} else "in_progress"
                counts[status] += 1
        if not profile_path.is_file() and counts["invalid"] == 0:
            store.atomic_json("label_profile.json", {
                "profile": profile, "model": model, "threshold": threshold,
                "max_targets": max_targets, "historical_import": True,
            })
        return counts

    def plan(self, specs: list[TaskSpec], profile: str, *,
             retry_rejected: bool = False, retry_case_id: int | None = None) -> dict:
        if retry_rejected and retry_case_id is not None:
            raise ValueError("choose either retry_rejected or retry_case_id")
        if retry_case_id is not None:
            current = next((case for case in self.list_cases()
                            if case["id"] == retry_case_id and case["workflow_profile"] == profile), None)
            if (current is None or current["review_status"] != "rejected" or
                    not any(spec.image_sha256 == current["image_sha256"] and
                            spec.species == current["species"] for spec in specs)):
                raise ValueError("retry_case_id must name a current rejected photo in this workflow")
        counts = {"pending": 0, "reused": 0, "review": 0, "profile_changed": 0}
        resume_runs = set()
        for spec in specs:
            match = self.lookup(spec, profile, retry_rejected=retry_rejected,
                                retry_case_id=retry_case_id)
            if match.action == "pending":
                counts["pending"] += 1
                asset = self._asset(spec)
                if asset is not None:
                    with self._connect() as db:
                        previous = db.execute(
                            "SELECT 1 FROM label_results WHERE asset_id=? AND image_sha256=? "
                            "AND species=? AND workflow_profile<>? "
                            "AND (machine_status='done' OR result_kind='human_correction') "
                            "LIMIT 1",
                            (asset["asset_id"], spec.image_sha256, spec.species, profile),
                        ).fetchone()
                    if previous is not None:
                        counts["profile_changed"] += 1
            elif match.action == "reuse":
                counts["reused"] += 1
            elif match.action == "review":
                counts["review"] += 1
            elif match.action == "resume":
                resume_runs.add(match.run_dir)
        return {**counts, "resume_runs": sorted(resume_runs)}

    def case(self, case_id: int) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT * FROM label_results WHERE id=?", (case_id,)).fetchone()
        if row is None:
            raise KeyError(case_id)
        return dict(row)

    def review_queue(self) -> list[dict]:
        return [case for case in self.list_cases()
                if case["machine_status"] == "needs_review" or
                case["review_status"] in {"rejected", "corrupt_result"}]

    def list_cases(self) -> list[dict]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT r.*, a.local_path, a.status AS asset_status FROM label_results r JOIN assets a ON a.asset_id=r.asset_id "
                "WHERE r.id IN (SELECT MAX(id) FROM label_results GROUP BY asset_id,workflow_profile) "
                "ORDER BY r.id"
            ).fetchall()
        cases = []
        for row in rows:
            status = self.review_history(int(row["id"]))
            value = status[-1]["decision"] if status else "unreviewed"
            if row["result_kind"] == "human_correction":
                result = Path(row["result_path"]) if row["result_path"] else None
                try:
                    intact = result is not None and digest(result) == row["result_sha256"]
                except OSError:
                    intact = False
                if not intact:
                    value = "corrupt_result"
            cases.append({**dict(row), "review_status": "approved" if value == "approve" else
                          "rejected" if value == "reject" else value})
        return cases

    def render_missing(self) -> dict[str, int]:
        """Regenerate current, accepted visual artifacts without a model call."""
        from .render_labels import _render_one

        counts = {"rendered": 0, "skipped": 0, "failed": 0}
        for case in self.list_cases():
            if case["asset_status"] != "active":
                continue
            if ((case["machine_status"] != "done" and case["result_kind"] != "human_correction") or
                    case["review_status"] in {"rejected", "corrupt_result"}):
                continue
            result = Path(case["result_path"]) if case["result_path"] else None
            source = self.photos_root / case["local_path"]
            try:
                if (result is None or digest(result) != case["result_sha256"] or
                        digest(source) != case["image_sha256"]):
                    raise ValueError("result or source image changed")
                target = Path(case["run_dir"]) / "annotated" / f"{result.stem}.jpg"
                if target.is_file() and target.stat().st_mtime_ns >= result.stat().st_mtime_ns:
                    counts["skipped"] += 1
                    continue
                _render_one(self.photos_root, result, target)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                counts["failed"] += 1
            else:
                counts["rendered"] += 1
        return counts

    def correct(self, case_id: int, boxes: list[list[float]], reviewer: str, reason: str) -> int:
        if not reviewer.strip() or not reason.strip():
            raise ValueError("reviewer and reason are required")
        source = self.case(case_id)
        with self._connect() as db:
            latest = db.execute("SELECT MAX(id) FROM label_results WHERE asset_id=? AND workflow_profile=?",
                                (source["asset_id"], source["workflow_profile"])).fetchone()[0]
        if latest != case_id:
            raise ValueError("result was superseded by a newer label")
        if source["machine_status"] != "done" and source["result_kind"] != "human_correction":
            raise ValueError("only completed results can be corrected")
        from .storage import Store
        from .render_labels import _render_one

        ancestor = source
        for _ in range(100):
            if ancestor["result_kind"] == "model" and ancestor["machine_status"] == "done":
                break
            parent_path = Path(ancestor["result_path"])
            if not parent_path.is_file() or digest(parent_path) != ancestor["result_sha256"]:
                raise ValueError("source correction is missing or changed")
            parent_data = json.loads(parent_path.read_text(encoding="utf-8"))
            ancestor = self.case(int(parent_data["human_correction"]["source_case_id"]))
        else:
            raise ValueError("correction history exceeds limit")
        state = Store(Path(ancestor["run_dir"])).get(ancestor["task_id"])
        if state.phase != "done":
            raise ValueError("original run is no longer done")
        spec = state.spec
        result_path = Path(source["result_path"])
        if not self._result_valid(result_path, spec, source["result_sha256"]):
            raise ValueError("source result is missing or changed")
        data = json.loads(result_path.read_text(encoding="utf-8"))
        if not isinstance(boxes, list):
            raise ValueError("boxes must be a list of pixel-coordinate rectangles")
        detections = []
        for box in boxes:
            if (not isinstance(box, list) or len(box) != 4 or
                    any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                        not math.isfinite(value) for value in box)):
                raise ValueError("each box needs four finite pixel coordinates")
            left, top, right, bottom = box
            if not (0 <= left < right <= spec.width and 0 <= top < bottom <= spec.height):
                raise ValueError("corrected box is outside the image or has no area")
            normalized = [min(999, round(value * 999 / dimension, 3))
                          for value, dimension in zip(box, (spec.width, spec.height, spec.width, spec.height))]
            detections.append({"species": spec.species, "bbox": normalized, "bbox_pixel": box})
        data["detections"] = detections
        data["human_correction"] = {"source_case_id": case_id, "reviewer": reviewer.strip(),
                                    "reason": reason.strip()}
        manual_root = self.photos_root.parent / "runs" / "manual-corrections"
        manual_store = Store(manual_root)
        manual_id = uuid4().hex
        manual_store.atomic_json(f"results/{manual_id}.json", data)
        corrected_path = manual_root / "results" / f"{manual_id}.json"
        rendered_path = manual_root / "annotated" / f"{manual_id}.jpg"
        _render_one(self.photos_root, corrected_path, rendered_path)
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = db.execute("SELECT MAX(id) FROM label_results WHERE asset_id=? AND workflow_profile=?",
                                (source["asset_id"], source["workflow_profile"])).fetchone()[0]
            if latest != case_id:
                raise ValueError("result was superseded by a newer label")
            previous = db.execute("SELECT decision FROM review_events WHERE label_result_id=? "
                                  "ORDER BY id DESC LIMIT 1", (case_id,)).fetchone()
            if previous is None or previous["decision"] != "reject":
                db.execute("INSERT INTO review_events(label_result_id,decision,reviewer,reason,created_at) "
                           "VALUES (?,?,?,?,?)", (case_id, "reject", reviewer.strip(), reason.strip(), now))
            cursor = db.execute(
                "INSERT INTO label_results(asset_id,image_sha256,species,workflow_profile,run_dir,task_id,"
                "machine_status,result_kind,result_path,result_sha256,reason,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (source["asset_id"], source["image_sha256"], source["species"], source["workflow_profile"],
                 str(manual_root), manual_id, "not_applicable", "human_correction",
                 str(corrected_path), digest(corrected_path),
                 reason.strip(), now),
            )
            new_id = int(cursor.lastrowid)
            db.execute("INSERT INTO review_events(label_result_id,decision,reviewer,reason,created_at) "
                       "VALUES (?,?,?,?,?)", (new_id, "approve", reviewer.strip(), reason.strip(), now))
        return new_id
