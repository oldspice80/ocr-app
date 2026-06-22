from __future__ import annotations

import hashlib
import io
import math
import os
import re
import shutil
import subprocess
from collections import Counter, deque
from pathlib import Path
from typing import Any, Callable, Iterable

import pdfplumber
from PIL import Image, ImageChops, ImageFilter, ImageOps


QUESTION_START = re.compile(
    r"^\s*(?:(?:문제|문항)\s*)?(?P<num>\d{1,3})\s*[\.\)\]\:：]\s*|^\s*(?P<circled>[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])\s*"
)

CIRCLED_TO_NUMBER = {char: str(index) for index, char in enumerate("①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳", 1)}


def save_image_as_pdf(payload: bytes, destination: Path) -> None:
    """업로드된 JPEG/PNG를 방향·투명도까지 반영한 1페이지 PDF로 저장한다."""
    try:
        with Image.open(io.BytesIO(payload)) as source:
            if source.format not in {"JPEG", "PNG"}:
                raise ValueError("지원하는 이미지 형식이 아닙니다.")
            oriented = ImageOps.exif_transpose(source)
            if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
                rgba = oriented.convert("RGBA")
                background = Image.new("RGBA", rgba.size, "white")
                background.alpha_composite(rgba)
                image = background.convert("RGB")
            else:
                image = oriented.convert("RGB")
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, "PDF", resolution=300.0)
    except Exception as exc:
        raise ValueError("올바른 JPG/JPEG/PNG 이미지가 아닙니다.") from exc

UNIT_RULES = {
    "함수": ("함수", "그래프", "f(", "g("),
    "방정식과 부등식": ("방정식", "부등식", "근", "해를 구"),
    "수열": ("수열", "등차", "등비", "a_n"),
    "미적분": ("미분", "적분", "도함수", "극한", "lim", "∫"),
    "확률과 통계": ("확률", "경우의 수", "평균", "분산", "표준편차"),
    "기하": ("삼각형", "사각형", "원", "직선", "도형", "벡터", "좌표"),
    "집합과 명제": ("집합", "명제", "조건", "부분집합"),
}

MATH_REPLACEMENTS = {
    "≤": r"\le ",
    "≥": r"\ge ",
    "≠": r"\ne ",
    "×": r"\times ",
    "÷": r"\div ",
    "±": r"\pm ",
    "∞": r"\infty ",
    "π": r"\pi ",
    "∑": r"\sum ",
    "∫": r"\int ",
    "∈": r"\in ",
    "∉": r"\notin ",
    "→": r"\to ",
}


def _notify(callback: Callable[[int, str], None] | None, progress: int, message: str) -> None:
    if callback:
        callback(progress, message)


def find_pdftoppm() -> str:
    configured = os.getenv("PDFTOPPM_PATH")
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("pdftoppm")
    if found:
        return found
    candidates = list(
        (Path.home() / ".cache" / "codex-runtimes").glob(
            "**/dependencies/native/poppler/Library/bin/pdftoppm.exe"
        )
    )
    if candidates:
        return str(candidates[0])
    raise RuntimeError("PDF 페이지 렌더러(pdftoppm)를 찾지 못했습니다.")


def render_pages(pdf_path: Path, output_dir: Path, dpi: int = 180) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / "page"
    command = [find_pdftoppm(), "-png", "-r", str(dpi), str(pdf_path), str(prefix)]
    completed = subprocess.run(command, capture_output=True, text=True, timeout=900)
    if completed.returncode != 0:
        raise RuntimeError(f"PDF 페이지 변환 실패: {completed.stderr.strip()}")
    pages = sorted(output_dir.glob("page-*.png"), key=lambda p: int(re.search(r"(\d+)$", p.stem).group(1)))
    if not pages:
        raise RuntimeError("PDF에서 페이지 이미지를 만들지 못했습니다.")
    return pages


def group_words_into_lines(words: list[dict[str, Any]], tolerance: float = 3.5) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for word in sorted(words, key=lambda item: (round(float(item["top"]), 1), float(item["x0"]))):
        top = float(word["top"])
        target = None
        for line in reversed(lines[-5:]):
            if abs(line["top_avg"] - top) <= tolerance:
                target = line
                break
        if target is None:
            target = {"words": [], "top_avg": top}
            lines.append(target)
        target["words"].append(word)
        count = len(target["words"])
        target["top_avg"] = ((target["top_avg"] * (count - 1)) + top) / count

    result: list[dict[str, Any]] = []
    for line in lines:
        ordered = sorted(line["words"], key=lambda item: float(item["x0"]))
        result.append(
            {
                "text": " ".join(str(item["text"]) for item in ordered).strip(),
                "x0": min(float(item["x0"]) for item in ordered),
                "x1": max(float(item["x1"]) for item in ordered),
                "top": min(float(item["top"]) for item in ordered),
                "bottom": max(float(item["bottom"]) for item in ordered),
            }
        )
    return sorted(result, key=lambda item: (item["top"], item["x0"]))


def _text_for_lines(lines: Iterable[dict[str, Any]]) -> str:
    return "\n".join(line["text"] for line in lines if line["text"].strip()).strip()


def _number_from_match(match: re.Match[str]) -> str:
    return match.group("num") or CIRCLED_TO_NUMBER.get(match.group("circled") or "", "")


def _has_meaningful_text(text: str) -> bool:
    normalized = re.sub(r"\s+", "", text)
    return len(normalized) >= 12


def segment_pages(page_models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def new_problem(number: str | None, needs_number_review: bool = False) -> dict[str, Any]:
        return {
            "number": number,
            "texts": [],
            "segments": [],
            "needs_number_review": needs_number_review,
        }

    def append_region(problem: dict[str, Any], page: dict[str, Any], region_lines: list[dict[str, Any]], top: float, bottom: float) -> None:
        if bottom <= top:
            return
        text = _text_for_lines(region_lines)
        if text:
            problem["texts"].append(text)
        problem["segments"].append(
            {
                "page": page["page_number"],
                "x0": 0,
                "top": max(0, round(top, 2)),
                "x1": round(page["width"], 2),
                "bottom": min(round(page["height"], 2), round(bottom, 2)),
                "page_width": round(page["width"], 2),
                "page_height": round(page["height"], 2),
            }
        )

    def flush() -> None:
        nonlocal current
        if current and (current["texts"] or current["segments"]):
            problems.append(current)
        current = None

    for page in page_models:
        lines = page["lines"]
        starts: list[tuple[int, str]] = []
        for index, line in enumerate(lines):
            match = QUESTION_START.match(line["text"])
            if match:
                starts.append((index, _number_from_match(match)))

        if not starts:
            page_text = _text_for_lines(lines)
            if current is None:
                current = new_problem(None, needs_number_review=True)
            append_region(current, page, lines, 0, page["height"])
            if not _has_meaningful_text(page_text):
                current["needs_number_review"] = True
            continue

        first_index = starts[0][0]
        first_top = lines[first_index]["top"]
        pre_lines = lines[:first_index]
        pre_text = _text_for_lines(pre_lines)
        if current is not None:
            # 다음 페이지의 첫 문제 위에 실제 본문이 있을 때만 이전 문제의
            # 연속 영역으로 붙인다. 빈 상단 여백까지 붙이면 page_end가 늘어난다.
            if _has_meaningful_text(pre_text):
                append_region(current, page, pre_lines, 0, first_top)
            flush()
        # 첫 문제 위의 짧은 상단 문구는 시험지 제목/머리말인 경우가 대부분이다.
        # 이전 페이지에서 이어진 문제가 있을 때만 상단 영역을 본문으로 보존한다.
        header_like = bool(pre_lines) and len(pre_lines) <= 3 and max(line["bottom"] for line in pre_lines) < page["height"] * 0.16
        if current is None and _has_meaningful_text(pre_text) and not header_like:
            current = new_problem(None, needs_number_review=True)
            append_region(current, page, pre_lines, 0, first_top)
            flush()

        for position, (line_index, number) in enumerate(starts):
            if current is not None:
                flush()
            next_line_index = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
            region_lines = lines[line_index:next_line_index]
            top = lines[line_index]["top"]
            bottom = lines[next_line_index]["top"] if next_line_index < len(lines) else page["height"]
            current = new_problem(number)
            append_region(current, page, region_lines, top, bottom)
            if position + 1 < len(starts):
                flush()

    flush()
    return problems


def crop_segment(image: Image.Image, segment: dict[str, Any], padding_points: float = 7) -> Image.Image:
    scale_x = image.width / segment["page_width"]
    scale_y = image.height / segment["page_height"]
    left = max(0, int((segment["x0"] - padding_points) * scale_x))
    top = max(0, int((segment["top"] - padding_points) * scale_y))
    right = min(image.width, int((segment["x1"] + padding_points) * scale_x))
    # 아래 경계는 다음 문제의 시작점일 수 있으므로 여백을 더하지 않는다.
    bottom = min(image.height, int(segment["bottom"] * scale_y))
    return image.crop((left, top, right, bottom)).convert("RGB")


def trim_white(image: Image.Image, border: int = 12) -> Image.Image:
    grayscale = image.convert("L")
    background = Image.new("L", grayscale.size, 255)
    difference = ImageChops.difference(grayscale, background)
    difference = difference.point(lambda value: 0 if value < 12 else value)
    bbox = difference.getbbox()
    if not bbox:
        return image
    left, top, right, bottom = bbox
    left = max(0, left - border)
    top = max(0, top - border)
    right = min(image.width, right + border)
    bottom = min(image.height, bottom + border)
    return image.crop((left, top, right, bottom))


def trim_vertical_white(image: Image.Image, border: int = 12) -> Image.Image:
    """문제 원본의 페이지 폭은 유지하고 빈 위아래 여백만 제거한다."""
    grayscale = image.convert("L")
    background = Image.new("L", grayscale.size, 255)
    difference = ImageChops.difference(grayscale, background)
    difference = difference.point(lambda value: 0 if value < 12 else value)
    bbox = difference.getbbox()
    if not bbox:
        return image
    top = max(0, bbox[1] - border)
    bottom = min(image.height, bbox[3] + border)
    return image.crop((0, top, image.width, bottom))


def clean_figure_image(image: Image.Image) -> Image.Image:
    """검은 인쇄선은 남기고 색 필기·옅은 연필·배경 얼룩을 제거한다."""
    rgb = image.convert("RGB")
    red, green, blue = rgb.split()
    maximum = ImageChops.lighter(ImageChops.lighter(red, green), blue)
    minimum = ImageChops.darker(ImageChops.darker(red, green), blue)
    saturation = ImageChops.subtract(maximum, minimum)
    colored_marks = saturation.point(lambda value: 255 if value >= 28 else 0)

    grayscale = ImageOps.grayscale(rgb)
    # 인쇄된 검정 선·문자는 유지하고 옅은 연필과 회색 배경은 흰색으로 정리한다.
    printed_ink = grayscale.point(lambda value: 0 if value < 178 else 255)
    cleaned = Image.composite(Image.new("L", rgb.size, 255), printed_ink, colored_marks)
    return cleaned.convert("RGB")


def save_cleaned_figure_from_source(
    source_url: str,
    media_root: Path,
    asset_group: str,
    sequence: int,
) -> str:
    """원본 문제는 보존하고 필기만 정리한 도형용 사본을 만든다."""
    source_path = media_root / source_url.removeprefix("/media/")
    if not source_url or not source_path.is_file():
        return ""
    with Image.open(source_path) as source:
        cleaned = trim_white(clean_figure_image(source), border=10)
    relative = Path("figures") / asset_group / f"problem-{sequence:04d}-clean.webp"
    destination = media_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    cleaned.save(destination, "WEBP", quality=94, method=6)
    return "/media/" + relative.as_posix()


def detect_raster_figure_boxes(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Find large connected diagrams in a scanned problem while ignoring text rows."""
    original_width, original_height = image.size
    if original_width < 40 or original_height < 40:
        return []
    scale = min(1.0, 900 / max(original_width, original_height))
    working = image.convert("L")
    if scale < 1:
        working = working.resize(
            (max(1, round(original_width * scale)), max(1, round(original_height * scale))),
            Image.Resampling.BILINEAR,
        )
    ink = working.point(lambda value: 255 if value < 185 else 0).filter(ImageFilter.MaxFilter(5))
    width, height = ink.size
    pixels = ink.load()
    visited = bytearray(width * height)
    candidates: list[tuple[int, int, int, int]] = []
    for y in range(height):
        for x in range(width):
            offset = y * width + x
            if visited[offset] or not pixels[x, y]:
                continue
            visited[offset] = 1
            queue = deque([(x, y)])
            left = right = x
            top = bottom = y
            count = 0
            while queue:
                current_x, current_y = queue.popleft()
                count += 1
                left, right = min(left, current_x), max(right, current_x)
                top, bottom = min(top, current_y), max(bottom, current_y)
                for next_x, next_y in (
                    (current_x - 1, current_y), (current_x + 1, current_y),
                    (current_x, current_y - 1), (current_x, current_y + 1),
                ):
                    if next_x < 0 or next_y < 0 or next_x >= width or next_y >= height:
                        continue
                    next_offset = next_y * width + next_x
                    if visited[next_offset] or not pixels[next_x, next_y]:
                        continue
                    visited[next_offset] = 1
                    queue.append((next_x, next_y))
            box_width, box_height = right - left + 1, bottom - top + 1
            if (
                box_width >= width * 0.16
                and box_height >= height * 0.13
                and count >= max(120, int(width * height * 0.0015))
            ):
                candidates.append((left, top, right + 1, bottom + 1))

    inverse_scale = 1 / scale
    margin_x = max(12, round(original_width * 0.035))
    margin_y = max(12, round(original_height * 0.035))
    boxes = []
    for left, top, right, bottom in cluster_boxes(candidates):
        boxes.append((
            max(0, round(left * inverse_scale) - margin_x),
            max(0, round(top * inverse_scale) - margin_y),
            min(original_width, round(right * inverse_scale) + margin_x),
            min(original_height, round(bottom * inverse_scale) + margin_y),
        ))
    return boxes[:4]


def extract_raster_figures_from_source(
    source_url: str,
    media_root: Path,
    asset_group: str,
    sequence: int,
) -> list[str]:
    """Crop diagrams from a JPG/PNG/scanned problem source and save clean copies."""
    source_path = media_root / source_url.removeprefix("/media/")
    if not source_url or not source_path.is_file():
        return []
    saved: list[str] = []
    with Image.open(source_path) as source:
        source_rgb = source.convert("RGB")
        for index, box in enumerate(detect_raster_figure_boxes(source_rgb), 1):
            crop = trim_white(clean_figure_image(source_rgb.crop(box)), border=8)
            relative = Path("figures") / asset_group / f"problem-{sequence:04d}-raster-{index:02d}.webp"
            destination = media_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            crop.save(destination, "WEBP", quality=94, method=6)
            saved.append("/media/" + relative.as_posix())
    return saved


def save_problem_crop(
    problem: dict[str, Any],
    page_images: list[Path],
    media_root: Path,
    document_id: int,
    sequence: int,
) -> str:
    crops: list[Image.Image] = []
    for segment in problem["segments"]:
        image = Image.open(page_images[segment["page"] - 1])
        crops.append(trim_vertical_white(crop_segment(image, segment)))
    if not crops:
        return ""
    max_width = max(crop.width for crop in crops)
    normalized: list[Image.Image] = []
    for crop in crops:
        if crop.width < max_width:
            canvas = Image.new("RGB", (max_width, crop.height), "white")
            canvas.paste(crop, (0, 0))
            normalized.append(canvas)
        else:
            normalized.append(crop)
    gap = 10
    stitched = Image.new("RGB", (max_width, sum(item.height for item in normalized) + gap * (len(normalized) - 1)), "white")
    cursor = 0
    for item in normalized:
        stitched.paste(item, (0, cursor))
        cursor += item.height + gap
    relative = Path("problems") / str(document_id) / f"problem-{sequence:04d}.webp"
    destination = media_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    stitched.save(destination, "WEBP", quality=92, method=6)
    return "/media/" + relative.as_posix()


def _object_bbox(item: dict[str, Any], page_height: float) -> tuple[float, float, float, float] | None:
    try:
        x0 = float(item.get("x0", 0))
        x1 = float(item.get("x1", x0))
        if "top" in item and "bottom" in item:
            top = float(item["top"])
            bottom = float(item["bottom"])
        else:
            y0 = float(item.get("y0", 0))
            y1 = float(item.get("y1", y0))
            top = page_height - max(y0, y1)
            bottom = page_height - min(y0, y1)
        if x1 < x0:
            x0, x1 = x1, x0
        if bottom < top:
            top, bottom = bottom, top
        return x0, top, x1, bottom
    except (TypeError, ValueError):
        return None


def _boxes_touch(a: tuple[float, float, float, float], b: tuple[float, float, float, float], margin: float = 14) -> bool:
    return not (a[2] + margin < b[0] or b[2] + margin < a[0] or a[3] + margin < b[1] or b[3] + margin < a[1])


def cluster_boxes(boxes: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    clusters: list[tuple[float, float, float, float]] = []
    for box in boxes:
        merged = box
        changed = True
        while changed:
            changed = False
            remaining = []
            for cluster in clusters:
                if _boxes_touch(merged, cluster):
                    merged = (
                        min(merged[0], cluster[0]), min(merged[1], cluster[1]),
                        max(merged[2], cluster[2]), max(merged[3], cluster[3]),
                    )
                    changed = True
                else:
                    remaining.append(cluster)
            clusters = remaining
        clusters.append(merged)
    return clusters


def extract_figures(
    problem: dict[str, Any],
    pdf_pages: list[Any],
    page_images: list[Path],
    media_root: Path,
    document_id: int,
    sequence: int,
) -> list[str]:
    figures: list[str] = []
    figure_index = 0
    for segment in problem["segments"]:
        page_index = segment["page"] - 1
        page = pdf_pages[page_index]
        candidates: list[tuple[float, float, float, float]] = []
        for item in list(page.images) + list(page.curves) + list(page.rects) + list(page.lines):
            bbox = _object_bbox(item, float(page.height))
            if not bbox:
                continue
            x0, top, x1, bottom = bbox
            if bottom < segment["top"] or top > segment["bottom"]:
                continue
            width, height = x1 - x0, bottom - top
            if width < 2 and height < 2:
                continue
            candidates.append((x0, max(top, segment["top"]), x1, min(bottom, segment["bottom"])))
        for bbox in cluster_boxes(candidates):
            width, height = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if width < 24 or height < 18 or width * height < 700:
                continue
            figure_index += 1
            image = Image.open(page_images[page_index])
            figure_segment = {
                **segment,
                "x0": max(0, bbox[0] - 12),
                "top": max(segment["top"], bbox[1] - 18),
                "x1": min(segment["page_width"], bbox[2] + 12),
                "bottom": min(segment["bottom"], bbox[3] + 18),
            }
            crop = trim_white(clean_figure_image(crop_segment(image, figure_segment, padding_points=3)), border=8)
            relative = Path("figures") / str(document_id) / f"problem-{sequence:04d}-{figure_index:02d}.webp"
            destination = media_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            crop.save(destination, "WEBP", quality=94, method=6)
            figures.append("/media/" + relative.as_posix())
    return figures[:6]


def infer_metadata(text: str) -> dict[str, Any]:
    lowered = text.lower()
    unit = "미분류"
    tags: list[str] = []
    for candidate, keywords in UNIT_RULES.items():
        if any(keyword.lower() in lowered for keyword in keywords):
            tags.append(candidate)
            if unit == "미분류":
                unit = candidate
    choices = len(re.findall(r"[①②③④⑤]|\([1-5]\)", text))
    problem_type = "객관식" if choices >= 3 else "주관식"
    complexity = len(text) / 160 + len(re.findall(r"[=<>∫∑√^]", text)) * 0.35
    difficulty = max(1, min(5, 1 + round(complexity)))
    concept = tags[0] if tags else "미분류"
    return {
        "unit": unit,
        "concept": concept,
        "tags": tags,
        "problem_type": problem_type,
        "difficulty": difficulty,
    }


def latexize(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        math_signal = any(symbol in line for symbol in MATH_REPLACEMENTS) or bool(re.search(r"[a-zA-Z]\s*[=<>]\s*[-+\d]", line))
        korean_count = len(re.findall(r"[가-힣]", line))
        transformed = line
        for source, target in MATH_REPLACEMENTS.items():
            transformed = transformed.replace(source, target)
        transformed = re.sub(r"([A-Za-z0-9\)])²", r"\1^{2}", transformed)
        transformed = re.sub(r"([A-Za-z0-9\)])³", r"\1^{3}", transformed)
        if math_signal and korean_count == 0 and not ("$" in transformed or "\\(" in transformed):
            transformed = f"\\({transformed}\\)"
        lines.append(transformed)
    return "\n".join(lines)


def fingerprint(text: str) -> str:
    normalized = re.sub(r"\d+", "#", text.lower())
    normalized = re.sub(r"\s+", "", normalized)
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()


def extract_pdf(
    pdf_path: Path,
    document_id: int,
    work_dir: Path,
    media_root: Path,
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    _notify(progress, 8, "PDF 구조 확인 중")
    page_image_dir = work_dir / "pages"
    page_images = render_pages(pdf_path, page_image_dir)
    _notify(progress, 28, "페이지 이미지 생성 완료")

    with pdfplumber.open(pdf_path) as pdf:
        page_models: list[dict[str, Any]] = []
        total_chars = 0
        for index, page in enumerate(pdf.pages, 1):
            words = page.extract_words(
                x_tolerance=2,
                y_tolerance=3,
                keep_blank_chars=False,
                use_text_flow=False,
            )
            lines = group_words_into_lines(words)
            total_chars += sum(len(line["text"]) for line in lines)
            page_models.append(
                {
                    "page_number": index,
                    "width": float(page.width),
                    "height": float(page.height),
                    "lines": lines,
                }
            )
        _notify(progress, 48, "문제 번호와 문단 분석 중")
        scan_suspected = total_chars < max(8, len(page_models) * 8)
        if scan_suspected:
            # OCR 키가 없는 이미지 PDF도 어떤 페이지도 사라지지 않게
            # 페이지마다 별도의 검수 후보를 만든다.
            candidates = [
                {
                    "number": None,
                    "texts": [f"[스캔 페이지 {index + 1} · 수학 OCR 재처리 필요]"],
                    "segments": [
                        {
                            "page": index + 1,
                            "x0": 0,
                            "top": 0,
                            "x1": page_model["width"],
                            "bottom": page_model["height"],
                            "page_width": page_model["width"],
                            "page_height": page_model["height"],
                        }
                    ],
                    "needs_number_review": True,
                }
                for index, page_model in enumerate(page_models)
            ]
        else:
            candidates = segment_pages(page_models)
        if not candidates:
            candidates = [
                {
                    "number": None,
                    "texts": ["이 페이지에서는 텍스트를 인식하지 못했습니다."],
                    "segments": [
                        {
                            "page": index + 1,
                            "x0": 0,
                            "top": 0,
                            "x1": page_models[index]["width"],
                            "bottom": page_models[index]["height"],
                            "page_width": page_models[index]["width"],
                            "page_height": page_models[index]["height"],
                        }
                    ],
                    "needs_number_review": True,
                }
                for index in range(len(page_models))
            ]

        results: list[dict[str, Any]] = []
        page_objects = list(pdf.pages)
        for sequence, candidate in enumerate(candidates, 1):
            text = "\n".join(candidate["texts"]).strip()
            metadata = infer_metadata(text)
            source_image = save_problem_crop(candidate, page_images, media_root, document_id, sequence)
            figures = extract_figures(candidate, page_objects, page_images, media_root, document_id, sequence)
            if not figures and source_image:
                figures = extract_raster_figures_from_source(
                    source_image, media_root, str(document_id), sequence
                )
                if not figures:
                    cleaned_figure = save_cleaned_figure_from_source(
                        source_image, media_root, str(document_id), sequence
                    )
                    figures = [cleaned_figure] if cleaned_figure else []
            notes: list[str] = []
            confidence = 0.78
            if candidate.get("needs_number_review") or not candidate.get("number"):
                confidence -= 0.22
                notes.append("문제 번호를 확정하지 못했습니다.")
            if len(re.sub(r"\s+", "", text)) < 20:
                confidence -= 0.22
                notes.append("인식된 본문이 짧아 원본 확인이 필요합니다.")
            if scan_suspected:
                confidence = min(confidence, 0.38)
                notes.append("스캔 PDF로 보입니다. 수학 OCR 연결 후 재처리해야 합니다.")
            page_numbers = [segment["page"] for segment in candidate["segments"]]
            results.append(
                {
                    "number": candidate.get("number"),
                    "page_start": min(page_numbers),
                    "page_end": max(page_numbers),
                    "content": text,
                    "latex": latexize(text),
                    "source_image": source_image,
                    "figures": figures,
                    "segments": candidate["segments"],
                    "confidence": round(max(0.05, min(0.98, confidence)), 2),
                    "quality_status": "needs_review",
                    "quality_notes": " ".join(notes),
                    "fingerprint": fingerprint(text),
                    **metadata,
                }
            )
            _notify(progress, 48 + round(sequence / max(1, len(candidates)) * 42), f"문제 {sequence}개 정리 중")

    numbered = [int(item["number"]) for item in results if item.get("number") and str(item["number"]).isdigit()]
    number_gaps: list[int] = []
    if numbered:
        expected = set(range(min(numbered), max(numbered) + 1))
        number_gaps = sorted(expected - set(numbered))
    duplicate_numbers = sorted(number for number, count in Counter(numbered).items() if count > 1)
    warnings = len(number_gaps) + len(duplicate_numbers) + sum(1 for item in results if item["confidence"] < 0.6)
    coverage = 100.0 if len(page_images) == len(page_models) else round(len(page_images) / max(1, len(page_models)) * 100, 1)
    _notify(progress, 96, "누락·중복 검사 중")
    return {
        "page_count": len(page_models),
        "problem_count": len(results),
        "problems": results,
        "coverage_percent": coverage,
        "warning_count": warnings,
        "number_gaps": number_gaps,
        "duplicate_numbers": duplicate_numbers,
        "scan_suspected": scan_suspected,
    }


def similarity_tokens(text: str) -> Counter[str]:
    normalized = re.sub(r"\d+", "#", text.lower())
    words = re.findall(r"[가-힣]{2,}|[a-z_\\]+|[#=+\-*/<>]+", normalized)
    compact = re.sub(r"\s+", "", normalized)
    bigrams = [compact[index:index + 2] for index in range(max(0, len(compact) - 1))]
    return Counter(words + bigrams)


def cosine_similarity(left: Counter[str], right: Counter[str]) -> float:
    if not left or not right:
        return 0.0
    dot = sum(value * right.get(key, 0) for key, value in left.items())
    left_norm = math.sqrt(sum(value * value for value in left.values()))
    right_norm = math.sqrt(sum(value * value for value in right.values()))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


def similar_score(source: dict[str, Any], candidate: dict[str, Any]) -> float:
    text_score = cosine_similarity(
        similarity_tokens((source.get("content") or "") + " " + (source.get("latex") or "")),
        similarity_tokens((candidate.get("content") or "") + " " + (candidate.get("latex") or "")),
    )
    metadata = 0.0
    if source.get("unit") == candidate.get("unit") and source.get("unit") != "미분류":
        metadata += 0.14
    if source.get("concept") == candidate.get("concept") and source.get("concept") != "미분류":
        metadata += 0.10
    if source.get("problem_type") == candidate.get("problem_type"):
        metadata += 0.05
    difficulty_distance = abs(int(source.get("difficulty", 3)) - int(candidate.get("difficulty", 3)))
    metadata += max(0, 0.06 - difficulty_distance * 0.02)
    return round(min(1.0, text_score * 0.65 + metadata), 4)
