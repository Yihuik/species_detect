from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from .models import TaskState
from .tools import pixels

PROJECT_ROOT = Path(__file__).resolve().parents[2]

class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        if not self.root.is_relative_to(PROJECT_ROOT):
            raise ValueError('all runtime artifacts must remain under '+str(PROJECT_ROOT))
        self.root.mkdir(parents=True,exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, state TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL, time TEXT NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL);
            """)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.root/'state.sqlite3',timeout=5)
        db.execute('PRAGMA synchronous=FULL')
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def runner_lock(self):
        # OS lock is released on process death; file existence is not a lease.
        with (self.root/'runner.lock').open('a+b') as handle:
            handle.seek(0,2)
            if handle.tell()==0:
                handle.write(b'0'); handle.flush()
            handle.seek(0)
            if os.name=='nt':
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
                except OSError as exc:
                    raise RuntimeError('another runner owns this store') from exc
            else:
                import fcntl
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            try:
                yield
            finally:
                handle.seek(0)
                if os.name=='nt':
                    msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    fcntl.flock(handle,fcntl.LOCK_UN)

    def get(self, task_id: str) -> TaskState:
        with self.connection() as db:
            row = db.execute('SELECT state FROM tasks WHERE id=?',(task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        return TaskState.model_validate_json(row[0])

    def save(self, state: TaskState, event: str, detail: dict | None = None):
        # Validate copies as well as external data before committing.
        state = TaskState.model_validate(state.model_dump())
        payload = {'phase':state.phase, 'attempts':len(state.attempts), **(detail or {})}
        with self.connection() as db:
            db.execute('INSERT INTO tasks VALUES (?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state',
                       (state.spec.task_id,state.model_dump_json()))
            db.execute('INSERT INTO events(task_id,time,event,payload) VALUES (?,?,?,?)',
                       (state.spec.task_id,datetime.now(timezone.utc).isoformat(),event,json.dumps(payload,ensure_ascii=False)))

    def audit(self, task_id: str | None = None) -> list[dict]:
        with self.connection() as db:
            rows = db.execute('SELECT seq,task_id,time,event,payload FROM events'+
                (' WHERE task_id=?' if task_id else '')+' ORDER BY seq', (task_id,) if task_id else ()).fetchall()
        return [dict(seq=r[0],task_id=r[1],time=r[2],event=r[3],payload=json.loads(r[4])) for r in rows]

    def review_queue(self) -> list[dict]:
        with self.connection() as db:
            states = [TaskState.model_validate_json(r[0]) for r in db.execute('SELECT state FROM tasks ORDER BY id')]
        return [s.model_dump(mode='json') for s in states if s.phase=='needs_review']

    def atomic_json(self, relative: str, value):
        path = (self.root/relative).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('output escapes store')
        path.parent.mkdir(parents=True,exist_ok=True)
        fd, name = tempfile.mkstemp(dir=path.parent,suffix='.tmp')
        try:
            with os.fdopen(fd,'w',encoding='utf-8') as handle:
                json.dump(value,handle,ensure_ascii=False,indent=2)
                handle.flush(); os.fsync(handle.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name): os.unlink(name)

    def export(self, state: TaskState):
        if state.phase=='done':
            attempt = state.attempts[state.selected_attempt-1]
            self.atomic_json('results/'+state.spec.task_id+'.json', {
                'workflow':'agentized_metadata_v1', 'source_image':state.spec.source_image,
                'image_width':state.spec.width, 'image_height':state.spec.height,
                'metadata':{'species':state.spec.species,'source':state.spec.species_source,
                            'provenance_sha256':state.spec.provenance_sha256},
                'coordinate_system':'qwen_0_999','pixel_scale_denominator':1000,
                'visibility_route':state.route,'selected_attempt':state.selected_attempt,
                'policy':state.policy.model_dump(),
                'detections':[{'species':state.spec.species,'bbox':list(b.bbox),
                   'bbox_pixel':pixels(b,state.spec.width,state.spec.height)} for b in attempt.result.boxes]})
        self.atomic_json('needs_review.json',self.review_queue())
        self.atomic_json('audit.json',self.audit())
