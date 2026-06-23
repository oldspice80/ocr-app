from __future__ import annotations

import hashlib
import html
import json
import mimetypes
import os
import re
import shutil
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from PIL import Image

from mathbank.box_blocks import BOX_CONFIDENCE_THRESHOLD, extract_raster_box_blocks
from mathbank.db import Database, now_iso
from mathbank.extractor import (
    clean_figure_image,
    detect_raster_figure_boxes,
    extract_pdf,
    extract_raster_figures_from_source,
    fingerprint,
    infer_metadata,
    latexize,
    save_image_as_pdf,
    similar_score,
    strip_problem_number,
)
from mathbank.manual import detect_initial_regions, extract_manual_regions, normalize_regions, prepare_manual_pdf
from mathbank.latex_text import latexize_plain_numbers
from mathbank.providers import MathpixProvider, ProviderError, merge_mathpix, provider_status


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DATA_ROOT = Path(os.getenv("MATHBANK_DATA_DIR", ROOT / "data")).resolve()
UPLOAD_ROOT = DATA_ROOT / "uploads"
WORK_ROOT = DATA_ROOT / "work"
MEDIA_ROOT = DATA_ROOT / "media"
DB = Database(DATA_ROOT / "mathbank.sqlite3")
EXECUTOR = ThreadPoolExecutor(max_workers=max(1, int(os.getenv("MATHBANK_WORKERS", "2"))))
MAX_UPLOAD_BYTES = int(os.getenv("MATHBANK_MAX_UPLOAD_MB", "250")) * 1024 * 1024

for directory in (UPLOAD_ROOT, WORK_ROOT, MEDIA_ROOT):
    directory.mkdir(parents=True, exist_ok=True)


def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def safe_filename(filename: str) -> str:
    name = Path(filename).name
    name = re.sub(r"[^0-9A-Za-z가-힣._ -]+", "_", name).strip(" .")
    return name or "document.pdf"


def configured_mathpix() -> MathpixProvider:
    return MathpixProvider(
        app_id=DB.get_setting("mathpix_app_id"),
        app_key=DB.get_setting("mathpix_app_key"),
    )


def ocr_config_payload() -> dict:
    app_id = DB.get_setting("mathpix_app_id") or os.getenv("MATHPIX_APP_ID", "")
    app_key = DB.get_setting("mathpix_app_key") or os.getenv("MATHPIX_APP_KEY", "")
    masked_id = ""
    if app_id:
        masked_id = app_id[:4] + ("•" * max(4, len(app_id) - 6)) + app_id[-2:]
    verified = DB.get_setting("mathpix_verified") == "1"
    return {
        "mathpix": {
            "configured": bool(app_id and app_key),
            "verified": verified,
            "app_id_masked": masked_id,
            "source": "앱 설정" if DB.get_setting("mathpix_app_id") else ("환경변수" if app_id else "미설정"),
            "last_error": DB.get_setting("mathpix_last_error"),
            "verified_at": DB.get_setting("mathpix_verified_at"),
        },
        "providers": provider_status(app_id, app_key),
    }


def document_has_exam_items(document_id: int) -> bool:
    row = DB.one(
        """SELECT COUNT(*) AS count FROM exam_items ei
           JOIN problems p ON p.id=ei.problem_id WHERE p.document_id=?""",
        (document_id,),
    )
    return bool(row and row["count"])


def refresh_document_problem_counts(document_id: int) -> None:
    counts = DB.one(
        """SELECT COUNT(*) AS total,
                  SUM(CASE WHEN quality_status='approved' THEN 1 ELSE 0 END) AS approved,
                  SUM(CASE WHEN confidence < 0.6 THEN 1 ELSE 0 END) AS warnings
           FROM problems WHERE document_id=?""",
        (document_id,),
    ) or {"total": 0, "approved": 0, "warnings": 0}
    DB.execute(
        """UPDATE documents SET problem_count=?, reviewed_count=?, warning_count=?, updated_at=?
           WHERE id=?""",
        (
            int(counts.get("total") or 0),
            int(counts.get("approved") or 0),
            int(counts.get("warnings") or 0),
            now_iso(),
            document_id,
        ),
    )


def process_document(document_id: int, job_id: int, replace_existing: bool = False) -> None:
    document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
    if not document:
        return

    def progress(value: int, message: str) -> None:
        DB.update_job(job_id, "processing", value, message)
        DB.execute(
            "UPDATE documents SET status='processing', updated_at=? WHERE id=?",
            (now_iso(), document_id),
        )

    try:
        work_dir = WORK_ROOT / str(document_id)
        if work_dir.exists():
            shutil.rmtree(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        result = extract_pdf(
            Path(document["stored_path"]), document_id, work_dir, MEDIA_ROOT, progress
        )
        provider = "local-layout"
        notes: list[str] = []
        mathpix = configured_mathpix()
        if mathpix.configured:
            progress(36, "Mathpix 수식 OCR 연결 중")
            try:
                markdown = mathpix.process_pdf(Path(document["stored_path"]), progress)
                result["problems"], note = merge_mathpix(result["problems"], markdown)
                notes.append(note)
                provider = "mathpix+local-layout"
            except ProviderError as exc:
                result["warning_count"] += 1
                notes.append(str(exc))
        elif result.get("scan_suspected"):
            result["warning_count"] += 1
            notes.append("스캔 PDF입니다. Mathpix 키를 설정한 뒤 재처리하는 것을 권장합니다.")

        if replace_existing:
            DB.execute("DELETE FROM problems WHERE document_id=?", (document_id,))
        DB.insert_problems(document_id, result["problems"])
        DB.execute(
            """UPDATE documents SET page_count=?, status='review', provider=?, problem_count=?,
               coverage_percent=?, warning_count=?, error=?, updated_at=? WHERE id=?""",
            (
                result["page_count"],
                provider,
                result["problem_count"],
                result["coverage_percent"],
                result["warning_count"],
                " ".join(notes) or None,
                now_iso(),
                document_id,
            ),
        )
        DB.update_job(job_id, "completed", 100, f"{result['problem_count']}개 문제 추출 완료 · 검수 필요")
    except Exception as exc:
        traceback.print_exc()
        DB.execute(
            "UPDATE documents SET status='failed', error=?, updated_at=? WHERE id=?",
            (str(exc)[:1000], now_iso(), document_id),
        )
        DB.update_job(job_id, "failed", 100, f"처리 실패: {str(exc)[:200]}")


def prepare_document_regions(document_id: int, job_id: int) -> None:
    document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
    if not document:
        return

    def progress(value: int, message: str) -> None:
        DB.update_job(job_id, "processing", value, message)
        DB.execute(
            "UPDATE documents SET status='preparing_pages', updated_at=? WHERE id=?",
            (now_iso(), document_id),
        )

    try:
        work_dir = WORK_ROOT / str(document_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        DB.save_document_regions(document_id, stage="preparing")
        pages = prepare_manual_pdf(
            Path(document["stored_path"]), document_id, work_dir, MEDIA_ROOT, progress
        )
        regions = detect_initial_regions(Path(document["stored_path"]), pages, MEDIA_ROOT)
        DB.save_document_regions(document_id, pages=pages, regions=regions, stage="ready")
        DB.execute(
            """UPDATE documents SET page_count=?, status='region_setup', provider='manual-regions',
               coverage_percent=100, error=NULL, updated_at=? WHERE id=?""",
            (len(pages), now_iso(), document_id),
        )
        DB.update_job(job_id, "completed", 100, f"페이지 준비 완료 · 자동 감지 영역 {len(regions)}개를 확인해 주세요")
    except Exception as exc:
        traceback.print_exc()
        DB.save_document_regions(document_id, stage="failed")
        DB.execute(
            "UPDATE documents SET status='failed', error=?, updated_at=? WHERE id=?",
            (str(exc)[:1000], now_iso(), document_id),
        )
        DB.update_job(job_id, "failed", 100, f"페이지 준비 실패: {str(exc)[:200]}")


def process_manual_document(document_id: int, job_id: int) -> None:
    document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
    region_data = DB.get_document_regions(document_id)
    if not document or not region_data:
        return

    def progress(value: int, message: str) -> None:
        DB.update_job(job_id, "processing", value, message)
        DB.execute(
            "UPDATE documents SET status='processing', updated_at=? WHERE id=?",
            (now_iso(), document_id),
        )

    try:
        work_dir = WORK_ROOT / str(document_id)
        work_dir.mkdir(parents=True, exist_ok=True)
        mathpix = configured_mathpix()
        if not mathpix.configured:
            raise ProviderError("문제 추출을 완료하려면 OCR 설정에서 Mathpix 키를 먼저 연결해 주세요.")
        result = extract_manual_regions(
            Path(document["stored_path"]),
            document_id,
            region_data["pages"],
            region_data["regions"],
            work_dir,
            MEDIA_ROOT,
            mathpix=mathpix,
            require_mathpix=True,
            progress=progress,
        )
        # OCR가 끝까지 성공한 뒤에만 기존 추출 문제를 교체한다.
        DB.execute("DELETE FROM problems WHERE document_id=?", (document_id,))
        DB.insert_problems(document_id, result["problems"])
        DB.save_document_regions(document_id, stage="extracted")
        provider = "manual+mathpix-image"
        DB.execute(
            """UPDATE documents SET page_count=?, status='review', provider=?, problem_count=?,
               reviewed_count=0, coverage_percent=?, warning_count=?, error=NULL, updated_at=? WHERE id=?""",
            (
                result["page_count"],
                provider,
                result["problem_count"],
                result["coverage_percent"],
                result["warning_count"],
                now_iso(),
                document_id,
            ),
        )
        DB.update_job(job_id, "completed", 100, f"수동 영역 {result['problem_count']}개 추출 완료 · 검수 필요")
    except Exception as exc:
        traceback.print_exc()
        DB.save_document_regions(document_id, stage="ready")
        DB.execute(
            "UPDATE documents SET status='region_setup', error=?, updated_at=? WHERE id=?",
            (str(exc)[:1000], now_iso(), document_id),
        )
        DB.update_job(job_id, "failed", 100, f"영역 OCR 실패: {str(exc)[:200]}")


def dashboard_payload() -> dict:
    totals = DB.one(
        """SELECT
            (SELECT COUNT(*) FROM documents) AS documents,
            (SELECT COUNT(*) FROM problems) AS problems,
            (SELECT COUNT(*) FROM problems WHERE quality_status='approved') AS approved,
            (SELECT COUNT(*) FROM problems WHERE quality_status='needs_review') AS needs_review,
            (SELECT COUNT(*) FROM exams) AS exams"""
    ) or {}
    documents = DB.all(
        """SELECT d.*, j.progress, j.message AS job_message, r.stage AS region_stage
           FROM documents d
           LEFT JOIN jobs j ON j.id=(SELECT MAX(id) FROM jobs WHERE document_id=d.id)
           LEFT JOIN document_regions r ON r.document_id=d.id
           ORDER BY d.id DESC LIMIT 8"""
    )
    units = DB.all(
        """SELECT unit, COUNT(*) AS count FROM problems
           GROUP BY unit ORDER BY count DESC, unit LIMIT 8"""
    )
    return {"totals": totals, "documents": documents, "units": units}


def sync_export_payload() -> dict:
    documents = DB.all("SELECT * FROM documents ORDER BY id")
    problems = [DB.decode_problem(row) for row in DB.all(
        """SELECT p.*, d.title AS document_title
           FROM problems p JOIN documents d ON d.id=p.document_id
           ORDER BY p.id"""
    )]
    exams = DB.all(
        """SELECT e.*, COUNT(ei.id) AS item_count, COALESCE(SUM(ei.points),0) AS total_points
           FROM exams e LEFT JOIN exam_items ei ON ei.exam_id=e.id
           GROUP BY e.id ORDER BY e.id"""
    )
    for exam in exams:
        items = DB.all(
            "SELECT problem_id, position, points FROM exam_items WHERE exam_id=? ORDER BY position",
            (exam["id"],),
        )
        exam["items"] = items
    return {
        "exported_at": now_iso(),
        "documents": documents,
        "problems": problems,
        "exams": exams,
    }


def list_problems(params: dict[str, list[str]]) -> list[dict]:
    clauses = ["1=1"]
    values: list[object] = []
    q = (params.get("q") or [""])[0].strip()
    status = (params.get("status") or [""])[0].strip()
    document_id = (params.get("document_id") or [""])[0].strip()
    unit = (params.get("unit") or [""])[0].strip()
    if q:
        clauses.append("(p.content LIKE ? OR p.latex LIKE ? OR p.concept LIKE ?)")
        values.extend([f"%{q}%"] * 3)
    if status:
        clauses.append("p.quality_status=?")
        values.append(status)
    if document_id:
        clauses.append("p.document_id=?")
        values.append(int(document_id))
    if unit:
        clauses.append("p.unit=?")
        values.append(unit)
    rows = DB.all(
        f"""SELECT p.*, d.title AS document_title
            FROM problems p JOIN documents d ON d.id=p.document_id
            WHERE {' AND '.join(clauses)} ORDER BY p.id DESC LIMIT 500""",
        values,
    )
    return [DB.decode_problem(row) for row in rows]


def exam_payload(exam_id: int) -> dict | None:
    exam = DB.one("SELECT * FROM exams WHERE id=?", (exam_id,))
    if not exam:
        return None
    rows = DB.all(
        """SELECT p.*, ei.position, ei.points, d.title AS document_title
           FROM exam_items ei
           JOIN problems p ON p.id=ei.problem_id
           JOIN documents d ON d.id=p.document_id
           WHERE ei.exam_id=? ORDER BY ei.position""",
        (exam_id,),
    )
    exam["items"] = [DB.decode_problem(row) for row in rows]
    return exam


def structured_problem_html(item: dict) -> str | None:
    text = str(item.get("latex") or item.get("content") or "")
    blocks = sorted(item.get("content_blocks", []), key=lambda value: value.get("order", 0))
    if not blocks:
        return f'<div class="math-content">{html.escape(text)}</div>'

    def normalized_map(value: str) -> tuple[str, list[int]]:
        compact, positions = [], []
        for index, character in enumerate(value):
            if character.isspace() or character in "\\{}()[]":
                continue
            compact.append(character)
            positions.append(index)
        return "".join(compact), positions

    source_compact, source_positions = normalized_map(text)
    placements: list[tuple[int, int, bool, dict]] = []
    for block_index, block in enumerate(blocks):
        needle = str(block.get("content") or block.get("latex") or "")
        start = text.find(needle) if block.get("display_mode") == "structured" and needle.strip() else -1
        replaces_text = start >= 0
        if replaces_text:
            end = start + len(needle)
        elif block.get("display_mode") == "structured" and needle.strip():
            needle_compact, _ = normalized_map(needle)
            compact_index = source_compact.find(needle_compact)
            if compact_index >= 0 and needle_compact:
                start = source_positions[compact_index]
                end = source_positions[compact_index + len(needle_compact) - 1] + 1
                replaces_text = True
            elif len(needle_compact) >= 24:
                # The isolated box OCR can contain a couple of character errors
                # compared with the full-problem OCR. Match stable anchors at
                # both ends so print output does not duplicate the paragraph.
                anchor_length = min(20, max(12, int(len(needle_compact) * 0.16)))
                prefix = needle_compact[:anchor_length]
                suffix = needle_compact[-anchor_length:]
                prefix_index = source_compact.find(prefix)
                suffix_start = max(
                    prefix_index + anchor_length,
                    prefix_index + len(needle_compact) - anchor_length * 3,
                )
                suffix_index = source_compact.find(suffix, suffix_start) if prefix_index >= 0 else -1
                if (
                    prefix_index >= 0
                    and suffix_index >= 0
                    and suffix_index - prefix_index <= len(needle_compact) + anchor_length * 4
                ):
                    start = source_positions[prefix_index]
                    end = source_positions[suffix_index + len(suffix) - 1] + 1
                    replaces_text = True
        if not replaces_text:
            region = block.get("region") or {}
            relative = float(region.get("y", (block_index + 1) / (len(blocks) + 1))) + float(region.get("height", 0)) / 2
            start = max(0, min(len(text), round(len(text) * max(0.0, min(1.0, relative)))))
            search_start = max(0, start - max(20, round(len(text) * 0.16)))
            search_end = min(len(text), start + max(20, round(len(text) * 0.16)))
            boundaries = [
                index + 1 for index in range(search_start, search_end)
                if text[index] == "\n" or text[index] in ".?!。"
            ]
            if boundaries:
                start = min(boundaries, key=lambda value: abs(value - start))
            end = start
        placements.append((start, end, replaces_text, block))
    placements.sort(key=lambda value: value[0])

    parts: list[str] = []
    cursor = 0
    for start, end, replaces_text, block in placements:
        if start < cursor:
            start = end = cursor
            replaces_text = False
        parts.append(f'<div class="math-content">{html.escape(text[cursor:start])}</div>')
        if block.get("display_mode") == "structured" and (block.get("latex") or block.get("content")):
            block_body = f'<div class="math-content">{html.escape(block.get("latex") or block.get("content") or "")}</div>'
            block_body += "".join(
                f'<img class="figure" src="{html.escape(path)}" alt="박스 내부 도형">'
                for path in block.get("figures", [])
            )
        else:
            block_body = f'<img class="box-source" src="{html.escape(block.get("source_image", ""))}" alt="OCR 확인이 필요한 박스 원본">'
        parts.append(f'<div class="content-box {html.escape(block.get("type", "condition_box"))}">{block_body}</div>')
        cursor = end if replaces_text else start
    parts.append(f'<div class="math-content">{html.escape(text[cursor:])}</div>')
    return "".join(parts)


def print_exam_html(exam: dict, mode: str = "original", answers: bool = False) -> str:
    items_html: list[str] = []
    for index, item in enumerate(exam["items"], 1):
        points = int(item.get("points", 5))
        if mode == "edited":
            body = structured_problem_html(item)
            if body is None:
                body = f'<img class="source" src="{html.escape(item.get("source_image", ""))}" alt="{index}번 문제">'
            figures = "".join(
                f'<img class="figure" src="{html.escape(path)}" alt="문제 도형">'
                for path in item.get("figures", [])
            )
            if not figures and item.get("source_image"):
                figures = f'<details><summary>원본 확인</summary><img class="source-mini" src="{html.escape(item["source_image"])}" alt="원본 문제"></details>'
            if structured_problem_html(item) is not None:
                body += figures
        else:
            body = f'<img class="source" src="{html.escape(item.get("source_image", ""))}" alt="{index}번 문제">'
        answer = ""
        if answers:
            answer = f'<div class="answer"><b>정답</b> {html.escape(item.get("answer") or "미입력")}<br>{html.escape(item.get("solution") or "")}</div>'
        items_html.append(
            f'<section class="problem"><div class="problem-number">{index}. <span>{points}점</span></div>{body}{answer}</section>'
        )
    columns = 2 if int(exam.get("columns_count", 1)) == 2 else 1
    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{html.escape(exam['title'])}</title>
<link rel="stylesheet" href="/vendor/katex/katex.min.css">
<style>
@page {{ size:A4; margin:16mm 15mm; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; color:#151515; font-family:'Malgun Gothic','Apple SD Gothic Neo',sans-serif; font-size:11pt; }}
.paper-head {{ border-top:3px solid #111; border-bottom:1px solid #777; padding:10px 0 9px; margin-bottom:18px; text-align:center; }}
h1 {{ font-size:20pt; margin:0 0 5px; letter-spacing:-.04em; }}
.meta {{ display:flex; justify-content:center; gap:28px; color:#555; font-size:9.5pt; }}
.student {{ display:flex; justify-content:flex-end; gap:30px; margin:12px 0 16px; }}
.student span {{ min-width:110px; border-bottom:1px solid #777; padding-bottom:3px; }}
.sheet {{ column-count:{columns}; column-gap:16mm; }}
.problem {{ break-inside:avoid; margin:0 0 18px; padding:0 1px 12px; }}
.problem-number {{ font-weight:800; font-size:12pt; margin-bottom:7px; }}
.problem-number span {{ float:right; font-weight:400; color:#777; font-size:9pt; }}
.source {{ display:block; width:100%; max-height:690px; object-fit:contain; object-position:left top; }}
.source-mini {{ width:100%; margin-top:6px; }}
.figure {{ display:block; max-width:84%; max-height:260px; margin:10px auto; }}
.content-box {{ margin:10px 0; padding:10px 12px; border:1px solid #222; break-inside:avoid; }}
.content-box.table_block {{ padding:0; border:0; background:transparent; }}
.box-source {{ display:block; width:100%; max-height:260px; object-fit:contain; }}
.math-content {{ white-space:pre-wrap; line-height:1.75; }}
.answer {{ margin-top:12px; border-top:1px dashed #aaa; padding-top:8px; line-height:1.6; color:#333; }}
details {{ color:#777; font-size:8pt; margin-top:10px; }}
.print-actions {{ position:fixed; right:18px; top:18px; display:flex; gap:8px; }}
.print-actions button {{ border:0; border-radius:8px; padding:9px 13px; background:#17261f; color:#fff; cursor:pointer; }}
@media print {{ .print-actions {{ display:none; }} }}
</style></head>
<body><div class="print-actions"><button onclick="window.print()">인쇄 / PDF 저장</button></div>
<header class="paper-head"><h1>{html.escape(exam['title'])}</h1><div class="meta"><span>{html.escape(exam.get('subtitle',''))}</span><span>시험 시간 {int(exam.get('duration',50))}분</span></div></header>
<div class="student"><span>학년·반</span><span>이름</span></div><main class="sheet">{''.join(items_html)}</main>
<script src="/vendor/katex/katex.min.js"></script><script src="/vendor/katex/contrib/auto-render.min.js"></script>
<script>document.addEventListener('DOMContentLoaded',()=>{{document.querySelectorAll('.math-content').forEach(el=>{{el.textContent=el.textContent.replace(/\\\\sum(?!\\s*\\\\limits)/g,'\\\\sum\\\\limits');renderMathInElement(el,{{delimiters:[{{left:'\\\\(',right:'\\\\)',display:false}},{{left:'\\\\[',right:'\\\\]',display:true}},{{left:'$',right:'$',display:false}}],throwOnError:false}});}});}});</script>
</body></html>"""


class MathBankHandler(BaseHTTPRequestHandler):
    server_version = "MathBankStudio/0.1"

    def log_message(self, format: str, *args) -> None:
        sys.stdout.write(f"[웹] {self.address_string()} - {format % args}\n")

    def send_bytes(self, payload: bytes, content_type: str, status: int = 200, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def send_json(self, data: object, status: int = 200) -> None:
        self.send_bytes(json_text(data).encode("utf-8"), "application/json; charset=utf-8", status)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 2 * 1024 * 1024:
            raise ValueError("요청 데이터가 너무 큽니다.")
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)
        try:
            if path == "/api/health":
                return self.send_json({"ok": True, "service": "MathBank Studio"})
            if path == "/api/config":
                config = ocr_config_payload()
                return self.send_json({**config, "max_upload_mb": MAX_UPLOAD_BYTES // 1024 // 1024})
            if path == "/api/settings/ocr":
                return self.send_json(ocr_config_payload())
            if path == "/api/sync/export":
                return self.send_json(sync_export_payload())
            if path == "/api/dashboard":
                return self.send_json(dashboard_payload())
            if path == "/api/documents":
                return self.send_json(dashboard_payload()["documents"])
            match = re.fullmatch(r"/api/documents/(\d+)", path)
            if match:
                document_id = int(match.group(1))
                document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
                if not document:
                    return self.send_error_json("문서를 찾지 못했습니다.", 404)
                document["job"] = DB.one("SELECT * FROM jobs WHERE document_id=? ORDER BY id DESC LIMIT 1", (document_id,))
                return self.send_json(document)
            match = re.fullmatch(r"/api/documents/(\d+)/regions", path)
            if match:
                document_id = int(match.group(1))
                document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
                region_data = DB.get_document_regions(document_id)
                if not document or not region_data:
                    return self.send_error_json("수동 영역 지정용 페이지가 아직 준비되지 않았습니다.", 404)
                return self.send_json({"document": document, **region_data})
            if path == "/api/problems":
                return self.send_json(list_problems(params))
            match = re.fullmatch(r"/api/problems/(\d+)/similar", path)
            if match:
                problem_id = int(match.group(1))
                source_raw = DB.one("SELECT * FROM problems WHERE id=?", (problem_id,))
                if not source_raw:
                    return self.send_error_json("문제를 찾지 못했습니다.", 404)
                source = DB.decode_problem(source_raw)
                candidates = [DB.decode_problem(row) for row in DB.all(
                    "SELECT * FROM problems WHERE id<>? AND quality_status='approved' LIMIT 1000", (problem_id,)
                )]
                for candidate in candidates:
                    candidate["similarity"] = similar_score(source, candidate)
                candidates.sort(key=lambda item: item["similarity"], reverse=True)
                return self.send_json(candidates[:12])
            match = re.fullmatch(r"/api/problems/(\d+)", path)
            if match:
                row = DB.one("SELECT * FROM problems WHERE id=?", (int(match.group(1)),))
                return self.send_json(DB.decode_problem(row), 200) if row else self.send_error_json("문제를 찾지 못했습니다.", 404)
            if path == "/api/exams":
                return self.send_json(DB.all(
                    """SELECT e.*, COUNT(ei.id) AS item_count, COALESCE(SUM(ei.points),0) AS total_points
                       FROM exams e LEFT JOIN exam_items ei ON ei.exam_id=e.id
                       GROUP BY e.id ORDER BY e.id DESC"""
                ))
            match = re.fullmatch(r"/api/exams/(\d+)", path)
            if match:
                exam = exam_payload(int(match.group(1)))
                return self.send_json(exam) if exam else self.send_error_json("시험지를 찾지 못했습니다.", 404)
            match = re.fullmatch(r"/print/exams/(\d+)", path)
            if match:
                exam = exam_payload(int(match.group(1)))
                if not exam:
                    return self.send_error_json("시험지를 찾지 못했습니다.", 404)
                mode = (params.get("mode") or ["original"])[0]
                answers = (params.get("answers") or ["0"])[0] == "1"
                return self.send_bytes(print_exam_html(exam, mode, answers).encode("utf-8"), "text/html; charset=utf-8")
            if path.startswith("/media/"):
                return self.serve_file(MEDIA_ROOT, path.removeprefix("/media/"), cache="public, max-age=86400")
            if path.startswith("/vendor/"):
                return self.serve_file(WEB_ROOT / "vendor", path.removeprefix("/vendor/"), cache="public, max-age=31536000")
            if path == "/" or path == "/index.html":
                return self.serve_file(WEB_ROOT, "index.html")
            if path.startswith("/assets/"):
                return self.serve_file(WEB_ROOT, path.removeprefix("/"), cache="no-cache")
            return self.serve_file(WEB_ROOT, path.lstrip("/"))
        except Exception as exc:
            traceback.print_exc()
            self.send_error_json(str(exc), 500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        params = parse_qs(parsed.query)
        try:
            if path == "/api/documents":
                return self.upload_document(params)
            if path == "/api/settings/mathpix":
                body = self.read_json()
                app_id = str(body.get("app_id", "")).strip()
                app_key = str(body.get("app_key", "")).strip()
                if not app_id or not app_key:
                    return self.send_error_json("Mathpix App ID와 App Key를 모두 입력해 주세요.")
                # 키 등록과 연결 확인을 분리한다. 검증 실패가 등록 실패로 이어지지 않는다.
                DB.set_setting("mathpix_app_id", app_id)
                DB.set_setting("mathpix_app_key", app_key)
                DB.set_setting("mathpix_verified", "0")
                DB.set_setting("mathpix_last_error", "")
                provider = MathpixProvider(app_id=app_id, app_key=app_key)
                try:
                    test_result = provider.test_connection()
                    DB.set_setting("mathpix_verified", "1")
                    DB.set_setting("mathpix_verified_at", now_iso())
                    DB.set_setting("mathpix_last_error", "")
                    return self.send_json({"saved": True, "verified": True, "test": test_result, **ocr_config_payload()})
                except ProviderError as exc:
                    DB.set_setting("mathpix_last_error", str(exc)[:1000])
                    return self.send_json({"saved": True, "verified": False, "warning": str(exc), **ocr_config_payload()})
            if path == "/api/settings/mathpix/test":
                provider = configured_mathpix()
                if not provider.configured:
                    return self.send_error_json("먼저 Mathpix 키를 저장해 주세요.", 409)
                try:
                    test_result = provider.test_connection()
                    DB.set_setting("mathpix_verified", "1")
                    DB.set_setting("mathpix_verified_at", now_iso())
                    DB.set_setting("mathpix_last_error", "")
                    return self.send_json({"verified": True, "test": test_result, **ocr_config_payload()})
                except ProviderError as exc:
                    DB.set_setting("mathpix_verified", "0")
                    DB.set_setting("mathpix_last_error", str(exc)[:1000])
                    return self.send_json({"verified": False, "warning": str(exc), **ocr_config_payload()})
            match = re.fullmatch(r"/api/documents/(\d+)/prepare-regions", path)
            if match:
                document_id = int(match.group(1))
                document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
                if not document:
                    return self.send_error_json("문서를 찾지 못했습니다.", 404)
                job_id = DB.create_job(document_id)
                DB.execute(
                    "UPDATE documents SET status='preparing_pages', error=NULL, updated_at=? WHERE id=?",
                    (now_iso(), document_id),
                )
                EXECUTOR.submit(prepare_document_regions, document_id, job_id)
                return self.send_json({"document_id": document_id, "job_id": job_id, "status": "preparing_pages"}, 202)
            match = re.fullmatch(r"/api/documents/(\d+)/regions", path)
            if match:
                document_id = int(match.group(1))
                body = self.read_json()
                if not configured_mathpix().configured:
                    return self.send_error_json("먼저 OCR 설정에서 Mathpix 키를 연결해 주세요.", 409)
                region_data = DB.get_document_regions(document_id)
                if not region_data:
                    return self.send_error_json("페이지 준비가 끝난 뒤 영역을 저장해 주세요.", 409)
                if document_has_exam_items(document_id):
                    return self.send_error_json("이 문서의 문제가 시험지에 사용 중이라 교체할 수 없습니다. 시험지에서 먼저 제거해 주세요.", 409)
                regions = normalize_regions(list(body.get("regions") or []), region_data["pages"])
                DB.save_document_regions(document_id, regions=regions, stage="processing")
                job_id = DB.create_job(document_id)
                DB.execute(
                    "UPDATE documents SET status='processing', error=NULL, updated_at=? WHERE id=?",
                    (now_iso(), document_id),
                )
                EXECUTOR.submit(process_manual_document, document_id, job_id)
                return self.send_json({"document_id": document_id, "job_id": job_id, "region_count": len(regions)}, 202)
            match = re.fullmatch(r"/api/documents/(\d+)/reprocess", path)
            if match:
                document_id = int(match.group(1))
                document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
                if not document:
                    return self.send_error_json("문서를 찾지 못했습니다.", 404)
                if not configured_mathpix().configured:
                    return self.send_error_json("먼저 OCR 설정에서 Mathpix 키를 연결해 주세요.", 409)
                if document_has_exam_items(document_id):
                    return self.send_error_json("이 문서의 문제가 시험지에 사용 중이라 재처리할 수 없습니다.", 409)
                job_id = DB.create_job(document_id)
                DB.execute(
                    "UPDATE documents SET status='processing', error=NULL, updated_at=? WHERE id=?",
                    (now_iso(), document_id),
                )
                EXECUTOR.submit(process_document, document_id, job_id, True)
                return self.send_json({"document_id": document_id, "job_id": job_id}, 202)
            match = re.fullmatch(r"/api/problems/(\d+)/ocr", path)
            if match:
                problem_id = int(match.group(1))
                row = DB.one("SELECT * FROM problems WHERE id=?", (problem_id,))
                if not row:
                    return self.send_error_json("문제를 찾지 못했습니다.", 404)
                provider = configured_mathpix()
                if not provider.configured:
                    return self.send_error_json("먼저 OCR 설정에서 Mathpix 키를 연결해 주세요.", 409)
                source_url = str(row.get("source_image") or "")
                source_path = (MEDIA_ROOT / source_url.removeprefix("/media/")).resolve()
                try:
                    source_path.relative_to(MEDIA_ROOT.resolve())
                except ValueError:
                    return self.send_error_json("문제 원본 경로가 올바르지 않습니다.", 400)
                if not source_path.is_file():
                    return self.send_error_json("문제 원본 이미지를 찾지 못했습니다.", 404)
                result = provider.process_image(source_path)
                recognized = latexize_plain_numbers(strip_problem_number(str(result.get("text") or result.get("latex_styled") or "").strip()))
                if not recognized:
                    return self.send_error_json("Mathpix가 인식 결과를 반환하지 않았습니다.", 422)
                metadata = infer_metadata(recognized)
                confidence = float(result.get("confidence", 0.86) or 0.86)
                asset_group = source_path.parent.name or f"problem-{problem_id}"
                sequence_match = re.search(r"(\d+)$", source_path.stem)
                sequence = int(sequence_match.group(1)) if sequence_match else problem_id
                content_blocks = extract_raster_box_blocks(
                    source_url,
                    MEDIA_ROOT,
                    asset_group,
                    sequence,
                    mathpix=provider,
                    latexizer=latexize,
                    figure_detector=detect_raster_figure_boxes,
                    figure_cleaner=clean_figure_image,
                )
                figures = extract_raster_figures_from_source(
                    source_url,
                    MEDIA_ROOT,
                    asset_group,
                    sequence,
                    exclude_regions=[block["region"] for block in content_blocks if block.get("region")],
                )
                DB.execute(
                    """UPDATE problems SET content=?, latex=?, confidence=?, unit=?, concept=?, difficulty=?,
                       problem_type=?, tags_json=?, fingerprint=?, figures_json=?, content_blocks_json=?, quality_status='needs_review',
                       quality_notes=?, updated_at=? WHERE id=?""",
                    (
                        recognized,
                        recognized,
                        confidence,
                        metadata["unit"],
                        metadata["concept"],
                        metadata["difficulty"],
                        metadata["problem_type"],
                        json.dumps(metadata["tags"], ensure_ascii=False),
                        fingerprint(recognized),
                        json.dumps(figures, ensure_ascii=False),
                        json.dumps(content_blocks, ensure_ascii=False),
                        "Mathpix 이미지 OCR로 다시 인식했습니다. 원본과 수식을 확인해 주세요.",
                        now_iso(),
                        problem_id,
                    ),
                )
                updated = DB.one("SELECT * FROM problems WHERE id=?", (problem_id,))
                return self.send_json(DB.decode_problem(updated))
            match = re.fullmatch(r"/api/problems/(\d+)/blocks/(\d+)/(ocr|region)", path)
            if match:
                problem_id = int(match.group(1))
                block_index = int(match.group(2))
                action = match.group(3)
                row = DB.one("SELECT * FROM problems WHERE id=?", (problem_id,))
                if not row:
                    return self.send_error_json("문제를 찾지 못했습니다.", 404)
                problem = DB.decode_problem(row)
                blocks = list(problem.get("content_blocks") or [])
                if block_index < 0 or block_index >= len(blocks):
                    return self.send_error_json("박스 콘텐츠를 찾지 못했습니다.", 404)
                block = dict(blocks[block_index])
                provider = configured_mathpix()
                if not provider.configured:
                    return self.send_error_json("박스 OCR을 다시 실행하려면 Mathpix 키를 연결해 주세요.", 409)

                if action == "region":
                    body = self.read_json()
                    region = body.get("region") or {}
                    x = max(0.0, min(1.0, float(region.get("x", 0))))
                    y = max(0.0, min(1.0, float(region.get("y", 0))))
                    width = max(0.02, min(1.0 - x, float(region.get("width", 1 - x))))
                    height = max(0.02, min(1.0 - y, float(region.get("height", 1 - y))))
                    source_url = str(problem.get("source_image") or "")
                    source_path = (MEDIA_ROOT / source_url.removeprefix("/media/")).resolve()
                    try:
                        source_path.relative_to(MEDIA_ROOT.resolve())
                    except ValueError:
                        return self.send_error_json("문제 원본 경로가 올바르지 않습니다.", 400)
                    if not source_path.is_file():
                        return self.send_error_json("문제 원본 이미지를 찾지 못했습니다.", 404)
                    relative = Path("boxes") / "adjusted" / f"problem-{problem_id}-box-{block_index + 1}.webp"
                    destination = MEDIA_ROOT / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with Image.open(source_path) as source:
                        crop_box = (
                            round(x * source.width), round(y * source.height),
                            round((x + width) * source.width), round((y + height) * source.height),
                        )
                        source.crop(crop_box).convert("RGB").save(destination, "WEBP", quality=95, method=6)
                    block["source_image"] = "/media/" + relative.as_posix()
                    block["region"] = {"x": x, "y": y, "width": width, "height": height}

                block_source = (MEDIA_ROOT / str(block.get("source_image") or "").removeprefix("/media/")).resolve()
                try:
                    block_source.relative_to(MEDIA_ROOT.resolve())
                except ValueError:
                    return self.send_error_json("박스 원본 경로가 올바르지 않습니다.", 400)
                if not block_source.is_file():
                    return self.send_error_json("박스 원본 이미지를 찾지 못했습니다.", 404)
                result = provider.process_image(block_source)
                recognized = str(result.get("text") or result.get("latex_styled") or "").strip()
                confidence = float(result.get("confidence", 0.82) or 0.82)
                block["content"] = recognized
                block["latex"] = recognized
                block["confidence"] = round(confidence, 2)
                block["display_mode"] = (
                    "structured" if recognized and confidence >= BOX_CONFIDENCE_THRESHOLD else "image_fallback"
                )
                block["elements"] = ([{
                    "type": "text_math", "content": recognized, "latex": recognized, "order": 1,
                }] if recognized else []) + [
                    {"type": "figure", "source": figure, "order": index + 2}
                    for index, figure in enumerate(block.get("figures") or [])
                ]
                blocks[block_index] = block
                DB.execute(
                    "UPDATE problems SET content_blocks_json=?, quality_status='needs_review', updated_at=? WHERE id=?",
                    (json.dumps(blocks, ensure_ascii=False), now_iso(), problem_id),
                )
                return self.send_json({"block": block, "content_blocks": blocks})
            if path == "/api/exams":
                body = self.read_json()
                title = str(body.get("title", "")).strip() or "새 시험지"
                problem_ids = [int(item) for item in body.get("problem_ids", [])]
                if not problem_ids:
                    return self.send_error_json("시험지에 넣을 문제를 선택해 주세요.")
                placeholders = ",".join("?" for _ in problem_ids)
                approved = DB.all(
                    f"SELECT id FROM problems WHERE id IN ({placeholders}) AND quality_status='approved'", problem_ids
                )
                if len(approved) != len(set(problem_ids)):
                    return self.send_error_json("검수 승인된 문제만 시험지에 넣을 수 있습니다.")
                now = now_iso()
                exam_id = DB.execute(
                    "INSERT INTO exams (title, subtitle, duration, columns_count, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                    (title, str(body.get("subtitle", "")), int(body.get("duration", 50)), int(body.get("columns_count", 1)), now, now),
                )
                with DB.connect() as conn:
                    for position, problem_id in enumerate(problem_ids, 1):
                        conn.execute(
                            "INSERT INTO exam_items (exam_id, problem_id, position, points) VALUES (?,?,?,?)",
                            (exam_id, problem_id, position, int(body.get("points", 5))),
                        )
                return self.send_json(exam_payload(exam_id), 201)
            if path == "/api/student-errors":
                body = self.read_json()
                error_id = DB.execute(
                    "INSERT INTO student_errors (student_name, problem_id, selected_answer, misconception, created_at) VALUES (?,?,?,?,?)",
                    (str(body.get("student_name", "학생")), int(body["problem_id"]), str(body.get("selected_answer", "")), str(body.get("misconception", "")), now_iso()),
                )
                return self.send_json({"id": error_id}, 201)
            self.send_error_json("지원하지 않는 요청입니다.", 404)
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            self.send_error_json(f"입력값을 확인해 주세요: {exc}")
        except Exception as exc:
            traceback.print_exc()
            self.send_error_json(str(exc), 500)

    def do_PATCH(self) -> None:
        path = unquote(urlparse(self.path).path)
        match = re.fullmatch(r"/api/problems/(\d+)", path)
        if not match:
            return self.send_error_json("지원하지 않는 요청입니다.", 404)
        try:
            problem_id = int(match.group(1))
            body = self.read_json()
            allowed = {
                "number", "content", "latex", "answer", "solution", "subject", "grade",
                "unit", "concept", "difficulty", "problem_type", "quality_status", "quality_notes",
            }
            updates = {key: value for key, value in body.items() if key in allowed}
            if "content_blocks" in body:
                updates["content_blocks_json"] = json.dumps(body["content_blocks"], ensure_ascii=False)
            if not updates:
                return self.send_error_json("수정할 내용이 없습니다.")
            if "quality_status" in updates and updates["quality_status"] not in {"needs_review", "approved", "rejected"}:
                return self.send_error_json("올바르지 않은 검수 상태입니다.")
            assignments = ", ".join(f"{key}=?" for key in updates)
            DB.execute(
                f"UPDATE problems SET {assignments}, updated_at=? WHERE id=?",
                [*updates.values(), now_iso(), problem_id],
            )
            row = DB.one("SELECT * FROM problems WHERE id=?", (problem_id,))
            if not row:
                return self.send_error_json("문제를 찾지 못했습니다.", 404)
            document_id = row["document_id"]
            reviewed = DB.one(
                "SELECT COUNT(*) AS count FROM problems WHERE document_id=? AND quality_status='approved'", (document_id,)
            )["count"]
            total = DB.one("SELECT COUNT(*) AS count FROM problems WHERE document_id=?", (document_id,))["count"]
            status = "approved" if total and reviewed == total else "review"
            DB.execute(
                "UPDATE documents SET reviewed_count=?, status=?, updated_at=? WHERE id=?",
                (reviewed, status, now_iso(), document_id),
            )
            return self.send_json(DB.decode_problem(row))
        except Exception as exc:
            traceback.print_exc()
            self.send_error_json(str(exc), 500)

    def do_DELETE(self) -> None:
        path = unquote(urlparse(self.path).path)
        if path == "/api/settings/mathpix":
            DB.set_setting("mathpix_app_id", "")
            DB.set_setting("mathpix_app_key", "")
            DB.set_setting("mathpix_verified", "0")
            DB.set_setting("mathpix_verified_at", "")
            DB.set_setting("mathpix_last_error", "")
            return self.send_json({"ok": True, **ocr_config_payload()})
        if path == "/api/problems":
            body = self.read_json()
            raw_ids = list(body.get("problem_ids") or [])
            try:
                problem_ids = list(dict.fromkeys(int(value) for value in raw_ids if int(value) > 0))
            except (TypeError, ValueError):
                return self.send_error_json("삭제할 문제 번호가 올바르지 않습니다.")
            if not problem_ids:
                return self.send_error_json("삭제할 문제를 하나 이상 선택해 주세요.")
            if len(problem_ids) > 500:
                return self.send_error_json("한 번에 최대 500문제까지 삭제할 수 있습니다.")
            placeholders = ",".join("?" for _ in problem_ids)
            rows = DB.all(
                f"SELECT id, document_id FROM problems WHERE id IN ({placeholders})",
                problem_ids,
            )
            existing_ids = [int(row["id"]) for row in rows]
            if not existing_ids:
                return self.send_error_json("선택한 문제를 찾지 못했습니다.", 404)
            existing_placeholders = ",".join("?" for _ in existing_ids)
            used = DB.all(
                f"""SELECT DISTINCT p.id, p.number FROM exam_items ei
                    JOIN problems p ON p.id=ei.problem_id
                    WHERE p.id IN ({existing_placeholders})""",
                existing_ids,
            )
            if used:
                labels = ", ".join(str(item.get("number") or item["id"]) for item in used[:8])
                return self.send_error_json(
                    f"시험지에 사용 중인 문제({labels})가 포함되어 있습니다. 시험지에서 먼저 제거해 주세요.",
                    409,
                )
            document_ids = sorted({int(row["document_id"]) for row in rows})
            with DB.connect() as conn:
                conn.execute(
                    f"DELETE FROM problems WHERE id IN ({existing_placeholders})",
                    tuple(existing_ids),
                )
            for document_id in document_ids:
                refresh_document_problem_counts(document_id)
            return self.send_json({"ok": True, "deleted_ids": existing_ids, "deleted_count": len(existing_ids)})
        match = re.fullmatch(r"/api/problems/(\d+)", path)
        if match:
            problem_id = int(match.group(1))
            problem = DB.one("SELECT id, document_id, number FROM problems WHERE id=?", (problem_id,))
            if not problem:
                return self.send_error_json("문제를 찾지 못했습니다.", 404)
            used = DB.one("SELECT COUNT(*) AS count FROM exam_items WHERE problem_id=?", (problem_id,))
            if used and used["count"]:
                return self.send_error_json("시험지에 사용 중인 문제입니다. 시험지에서 먼저 제거해 주세요.", 409)
            document_id = int(problem["document_id"])
            DB.execute("DELETE FROM problems WHERE id=?", (problem_id,))
            refresh_document_problem_counts(document_id)
            return self.send_json({"ok": True, "deleted_id": problem_id, "document_id": document_id})
        match = re.fullmatch(r"/api/exams/(\d+)", path)
        if match:
            DB.execute("DELETE FROM exams WHERE id=?", (int(match.group(1)),))
            return self.send_json({"ok": True})
        self.send_error_json("지원하지 않는 요청입니다.", 404)

    def upload_document(self, params: dict[str, list[str]]) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return self.send_error_json("업로드한 파일이 비어 있습니다.")
        if length > MAX_UPLOAD_BYTES:
            return self.send_error_json(f"파일은 {MAX_UPLOAD_BYTES // 1024 // 1024}MB까지 올릴 수 있습니다.", 413)
        filename = safe_filename((params.get("filename") or ["document.pdf"])[0])
        suffix = Path(filename).suffix.lower()
        if suffix not in {".pdf", ".jpg", ".jpeg", ".png"}:
            return self.send_error_json("PDF 또는 JPG/JPEG/PNG 파일만 올릴 수 있습니다.")
        payload = self.rfile.read(length)
        if suffix == ".pdf" and not payload.startswith(b"%PDF"):
            return self.send_error_json("올바른 PDF 파일이 아닙니다.")
        digest = hashlib.sha256(payload).hexdigest()
        mode = (params.get("mode") or ["auto"])[0]
        if mode not in {"auto", "manual"}:
            return self.send_error_json("올바르지 않은 추출 방식입니다.")
        existing = DB.one("SELECT * FROM documents WHERE sha256=?", (digest,))
        if existing:
            if mode == "manual":
                job_id = DB.create_job(existing["id"])
                DB.execute(
                    "UPDATE documents SET status='preparing_pages', error=NULL, updated_at=? WHERE id=?",
                    (now_iso(), existing["id"]),
                )
                EXECUTOR.submit(prepare_document_regions, existing["id"], job_id)
                return self.send_json({"document": existing, "job_id": job_id, "duplicate": True, "manual_preparing": True}, 202)
            return self.send_json({"document": existing, "duplicate": True}, 200)
        if suffix == ".pdf":
            stored_path = UPLOAD_ROOT / f"{digest[:16]}-{filename}"
            stored_path.write_bytes(payload)
        else:
            stored_path = UPLOAD_ROOT / f"{digest[:16]}-{Path(filename).stem}.pdf"
            try:
                save_image_as_pdf(payload, stored_path)
            except ValueError as exc:
                return self.send_error_json(str(exc))
        title = Path(filename).stem
        document_id = DB.create_document(title, filename, str(stored_path), digest)
        job_id = DB.create_job(document_id)
        if mode == "manual":
            DB.execute(
                "UPDATE documents SET status='preparing_pages', provider='manual-regions', updated_at=? WHERE id=?",
                (now_iso(), document_id),
            )
            EXECUTOR.submit(prepare_document_regions, document_id, job_id)
        else:
            EXECUTOR.submit(process_document, document_id, job_id)
        document = DB.one("SELECT * FROM documents WHERE id=?", (document_id,))
        self.send_json({"document": document, "job_id": job_id, "duplicate": False, "mode": mode}, 202)

    def serve_file(self, root: Path, relative: str, cache: str = "no-store") -> None:
        root = root.resolve()
        target = (root / relative).resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return self.send_error_json("허용되지 않은 경로입니다.", 403)
        if not target.is_file():
            return self.send_error_json("파일을 찾지 못했습니다.", 404)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or target.suffix in {".js", ".css", ".json"}:
            content_type += "; charset=utf-8"
        self.send_bytes(target.read_bytes(), content_type, cache=cache)


def main() -> None:
    host = os.getenv("MATHBANK_HOST", "127.0.0.1")
    port = int(os.getenv("MATHBANK_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), MathBankHandler)
    print(f"\nMathBank Studio가 실행되었습니다: http://{host}:{port}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료합니다.")
    finally:
        server.server_close()
        EXECUTOR.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
