from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    sha256 TEXT NOT NULL UNIQUE,
    page_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'queued',
    provider TEXT NOT NULL DEFAULT 'local-layout',
    problem_count INTEGER NOT NULL DEFAULT 0,
    reviewed_count INTEGER NOT NULL DEFAULT 0,
    coverage_percent REAL NOT NULL DEFAULT 0,
    warning_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT '업로드 대기',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS problems (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    number TEXT,
    page_start INTEGER NOT NULL,
    page_end INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    latex TEXT NOT NULL DEFAULT '',
    answer TEXT NOT NULL DEFAULT '',
    solution TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '수학',
    grade TEXT NOT NULL DEFAULT '미분류',
    unit TEXT NOT NULL DEFAULT '미분류',
    concept TEXT NOT NULL DEFAULT '미분류',
    difficulty INTEGER NOT NULL DEFAULT 3,
    problem_type TEXT NOT NULL DEFAULT '주관식',
    quality_status TEXT NOT NULL DEFAULT 'needs_review',
    confidence REAL NOT NULL DEFAULT 0,
    source_image TEXT NOT NULL DEFAULT '',
    figures_json TEXT NOT NULL DEFAULT '[]',
    segments_json TEXT NOT NULL DEFAULT '[]',
    tags_json TEXT NOT NULL DEFAULT '[]',
    fingerprint TEXT NOT NULL DEFAULT '',
    quality_notes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_problems_document ON problems(document_id);
CREATE INDEX IF NOT EXISTS idx_problems_status ON problems(quality_status);
CREATE INDEX IF NOT EXISTS idx_problems_unit ON problems(unit);
CREATE INDEX IF NOT EXISTS idx_problems_fingerprint ON problems(fingerprint);

CREATE TABLE IF NOT EXISTS exams (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    subtitle TEXT NOT NULL DEFAULT '',
    duration INTEGER NOT NULL DEFAULT 50,
    columns_count INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exam_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exam_id INTEGER NOT NULL REFERENCES exams(id) ON DELETE CASCADE,
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE RESTRICT,
    position INTEGER NOT NULL,
    points INTEGER NOT NULL DEFAULT 5,
    UNIQUE(exam_id, problem_id)
);

CREATE TABLE IF NOT EXISTS student_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_name TEXT NOT NULL,
    problem_id INTEGER NOT NULL REFERENCES problems(id) ON DELETE CASCADE,
    selected_answer TEXT NOT NULL DEFAULT '',
    misconception TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_regions (
    document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
    pages_json TEXT NOT NULL DEFAULT '[]',
    regions_json TEXT NOT NULL DEFAULT '[]',
    stage TEXT NOT NULL DEFAULT 'preparing',
    updated_at TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

    def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self.connect() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.lastrowid or 0)

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return str(row["value"]) if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            """INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at""",
            (key, value, now_iso()),
        )

    def get_document_regions(self, document_id: int) -> dict[str, Any] | None:
        row = self.one("SELECT * FROM document_regions WHERE document_id=?", (document_id,))
        if not row:
            return None
        for source, target in (("pages_json", "pages"), ("regions_json", "regions")):
            try:
                row[target] = json.loads(row.get(source) or "[]")
            except json.JSONDecodeError:
                row[target] = []
            row.pop(source, None)
        return row

    def save_document_regions(
        self,
        document_id: int,
        *,
        pages: list[dict[str, Any]] | None = None,
        regions: list[dict[str, Any]] | None = None,
        stage: str | None = None,
    ) -> None:
        existing = self.get_document_regions(document_id) or {"pages": [], "regions": [], "stage": "preparing"}
        page_data = pages if pages is not None else existing["pages"]
        region_data = regions if regions is not None else existing["regions"]
        next_stage = stage or existing["stage"]
        self.execute(
            """INSERT INTO document_regions (document_id, pages_json, regions_json, stage, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(document_id) DO UPDATE SET
                 pages_json=excluded.pages_json,
                 regions_json=excluded.regions_json,
                 stage=excluded.stage,
                 updated_at=excluded.updated_at""",
            (
                document_id,
                json.dumps(page_data, ensure_ascii=False),
                json.dumps(region_data, ensure_ascii=False),
                next_stage,
                now_iso(),
            ),
        )

    def create_document(self, title: str, filename: str, stored_path: str, digest: str) -> int:
        now = now_iso()
        return self.execute(
            """INSERT INTO documents
               (title, filename, stored_path, sha256, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, filename, stored_path, digest, now, now),
        )

    def create_job(self, document_id: int) -> int:
        now = now_iso()
        return self.execute(
            "INSERT INTO jobs (document_id, created_at, updated_at) VALUES (?, ?, ?)",
            (document_id, now, now),
        )

    def update_job(self, job_id: int, status: str, progress: int, message: str) -> None:
        self.execute(
            "UPDATE jobs SET status=?, progress=?, message=?, updated_at=? WHERE id=?",
            (status, max(0, min(100, progress)), message, now_iso(), job_id),
        )

    def insert_problems(self, document_id: int, problems: list[dict[str, Any]]) -> None:
        now = now_iso()
        with self.connect() as conn:
            for problem in problems:
                conn.execute(
                    """INSERT INTO problems (
                        document_id, number, page_start, page_end, content, latex,
                        answer, solution, subject, grade, unit, concept, difficulty,
                        problem_type, quality_status, confidence, source_image,
                        figures_json, segments_json, tags_json, fingerprint,
                        quality_notes, created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        document_id,
                        problem.get("number"),
                        problem["page_start"],
                        problem["page_end"],
                        problem.get("content", ""),
                        problem.get("latex", ""),
                        problem.get("answer", ""),
                        problem.get("solution", ""),
                        problem.get("subject", "수학"),
                        problem.get("grade", "미분류"),
                        problem.get("unit", "미분류"),
                        problem.get("concept", "미분류"),
                        problem.get("difficulty", 3),
                        problem.get("problem_type", "주관식"),
                        problem.get("quality_status", "needs_review"),
                        problem.get("confidence", 0),
                        problem.get("source_image", ""),
                        json.dumps(problem.get("figures", []), ensure_ascii=False),
                        json.dumps(problem.get("segments", []), ensure_ascii=False),
                        json.dumps(problem.get("tags", []), ensure_ascii=False),
                        problem.get("fingerprint", ""),
                        problem.get("quality_notes", ""),
                        now,
                        now,
                    ),
                )

    @staticmethod
    def decode_problem(row: dict[str, Any]) -> dict[str, Any]:
        for source, target in (
            ("figures_json", "figures"),
            ("segments_json", "segments"),
            ("tags_json", "tags"),
        ):
            try:
                row[target] = json.loads(row.get(source) or "[]")
            except json.JSONDecodeError:
                row[target] = []
            row.pop(source, None)
        return row
