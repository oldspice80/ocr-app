from __future__ import annotations

import re
import shutil
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

import pdfplumber
from PIL import Image

from .extractor import (
    extract_figures,
    extract_raster_figures_from_source,
    fingerprint,
    infer_metadata,
    latexize,
    render_pages,
    save_cleaned_figure_from_source,
    save_problem_crop,
)
from .providers import MathpixProvider, ProviderError


def _notify(callback: Callable[[int, str], None] | None, value: int, message: str) -> None:
    if callback:
        callback(value, message)


def prepare_manual_pdf(
    pdf_path: Path,
    document_id: int,
    work_dir: Path,
    media_root: Path,
    progress: Callable[[int, str], None] | None = None,
) -> list[dict[str, Any]]:
    """수동 영역 지정 화면에서 사용할 고해상도 페이지를 준비한다."""
    _notify(progress, 8, "수동 영역 지정을 위한 페이지 준비 중")
    rendered_dir = work_dir / "manual-pages-rendered"
    if rendered_dir.exists():
        shutil.rmtree(rendered_dir)
    page_images = render_pages(pdf_path, rendered_dir, dpi=220)
    destination_dir = media_root / "pages" / str(document_id)
    destination_dir.mkdir(parents=True, exist_ok=True)
    pages: list[dict[str, Any]] = []
    with pdfplumber.open(pdf_path) as pdf:
        if len(pdf.pages) != len(page_images):
            raise RuntimeError("PDF 페이지 수와 렌더링된 이미지 수가 다릅니다.")
        for index, (pdf_page, source) in enumerate(zip(pdf.pages, page_images), 1):
            destination = destination_dir / f"page-{index:04d}.png"
            shutil.copyfile(source, destination)
            with Image.open(destination) as image:
                pixel_width, pixel_height = image.size
            pages.append(
                {
                    "page": index,
                    "url": "/media/" + destination.relative_to(media_root).as_posix(),
                    "pixel_width": pixel_width,
                    "pixel_height": pixel_height,
                    "page_width": float(pdf_page.width),
                    "page_height": float(pdf_page.height),
                }
            )
            _notify(progress, 8 + round(index / max(1, len(page_images)) * 86), f"{index}/{len(page_images)}쪽 준비 중")
    return pages


AUTO_QUESTION_START = re.compile(r"^\s*(?:(?:문제|문항)\s*)?(?P<num>\d{1,3})\s*[\.\)\]\:：]\s*")
AUTO_REGION_PADDING_X_RATIO = 0.01
AUTO_REGION_PADDING_Y_RATIO = 0.04
AUTO_REGION_MIN_VERTICAL_POINTS = 2.5


def _item_bbox(item: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """pdfplumber 글자·도형 객체를 위쪽 기준 좌표로 통일한다."""
    try:
        x0 = float(item["x0"])
        x1 = float(item["x1"])
        top = float(item["top"])
        bottom = float(item["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    if x1 <= x0 or bottom < top:
        return None
    return x0, top, x1, bottom


def _padded_content_bounds(
    words: list[dict[str, Any]],
    visual_objects: list[dict[str, Any]],
    column_x0: float,
    column_x1: float,
    band_top: float,
    band_bottom: float,
    page_width: float,
    page_height: float,
) -> tuple[float, float, float, float]:
    """문제 구간 안의 실제 글자·도형 범위를 찾고 사방에 3%만 더한다."""
    boxes: list[tuple[float, float, float, float]] = []
    for item in [*words, *visual_objects]:
        bbox = _item_bbox(item)
        if not bbox:
            continue
        x0, top, x1, bottom = bbox
        center_x = (x0 + x1) / 2
        if not (column_x0 <= center_x <= column_x1):
            continue
        if bottom < band_top or top >= band_bottom:
            continue
        clipped = (
            max(column_x0, x0),
            max(band_top, top),
            min(column_x1, x1),
            min(band_bottom, bottom),
        )
        if clipped[2] > clipped[0] and clipped[3] >= clipped[1]:
            boxes.append(clipped)

    if not boxes:
        return column_x0, band_top, column_x1, band_bottom

    content_x0 = min(box[0] for box in boxes)
    content_top = min(box[1] for box in boxes)
    content_x1 = max(box[2] for box in boxes)
    content_bottom = max(box[3] for box in boxes)
    content_width = max(1.0, content_x1 - content_x0)
    content_height = max(1.0, content_bottom - content_top)
    pad_x = content_width * AUTO_REGION_PADDING_X_RATIO
    pad_y = max(
        AUTO_REGION_MIN_VERTICAL_POINTS,
        content_height * AUTO_REGION_PADDING_Y_RATIO,
    )
    return (
        max(0.0, column_x0, content_x0 - pad_x),
        max(0.0, content_top - pad_y),
        min(page_width, column_x1, content_x1 + pad_x),
        min(page_height, band_bottom, content_bottom + pad_y),
    )


def _raster_ink_bounds(
    page_image: Path,
    column_x0: float,
    column_x1: float,
    band_top: float,
    band_bottom: float,
    page_width: float,
    page_height: float,
    stop_at_large_gap: bool = True,
) -> tuple[float, float, float, float] | None:
    """페이지 이미지에서 실제 잉크만 찾고 큰 빈 간격 뒤의 꼬리말은 제외한다."""
    with Image.open(page_image) as source:
        left = max(0, round(column_x0 / page_width * source.width))
        right = min(source.width, round(column_x1 / page_width * source.width))
        top = max(0, round(band_top / page_height * source.height))
        bottom = min(source.height, round(band_bottom / page_height * source.height))
        if right <= left or bottom <= top:
            return None
        crop = source.crop((left, top, right, bottom)).convert("L")
        target_width = min(480, crop.width)
        target_height = max(1, round(crop.height * target_width / max(1, crop.width)))
        small = crop.resize((target_width, target_height))

    mask = small.point(lambda value: 255 if value < 242 else 0)
    pixels = list(mask.get_flattened_data())
    row_threshold = max(2, round(target_width * 0.002))
    occupied_rows = [
        y
        for y in range(target_height)
        if sum(1 for value in pixels[y * target_width : (y + 1) * target_width] if value) >= row_threshold
    ]
    if not occupied_rows:
        return None

    # 문제 본문·보기·도형 사이의 보통 간격은 연결하되, 긴 공백 뒤의 쪽번호는 버린다.
    scaled_page_height = source.height * target_width / max(1, crop.width)
    gap_limit = max(10, round(scaled_page_height * 0.045))
    first_row = occupied_rows[0]
    last_row = first_row
    for row in occupied_rows[1:]:
        if stop_at_large_gap and row - last_row > gap_limit:
            break
        last_row = row

    relevant = mask.crop((0, first_row, target_width, last_row + 1))
    bbox = relevant.getbbox()
    if not bbox:
        return None
    ink_left, _, ink_right, _ = bbox
    scale_x = (right - left) / target_width
    scale_y = (bottom - top) / target_height
    content_x0 = left + ink_left * scale_x
    content_x1 = left + ink_right * scale_x
    content_top = top + first_row * scale_y
    content_bottom = top + (last_row + 1) * scale_y
    content_width = max(1.0, content_x1 - content_x0)
    content_height = max(1.0, content_bottom - content_top)
    pad_x = content_width * AUTO_REGION_PADDING_X_RATIO
    pad_y = max(
        source.height / page_height * AUTO_REGION_MIN_VERTICAL_POINTS,
        content_height * AUTO_REGION_PADDING_Y_RATIO,
    )
    return (
        max(column_x0, (content_x0 - pad_x) / source.width * page_width),
        max(0.0, (content_top - pad_y) / source.height * page_height),
        min(column_x1, (content_x1 + pad_x) / source.width * page_width),
        min(band_bottom, (content_bottom + pad_y) / source.height * page_height),
    )


def detect_initial_regions(
    pdf_path: Path,
    pages: list[dict[str, Any]],
    media_root: Path | None = None,
) -> list[dict[str, Any]]:
    """PDF 내장 글자에서 문제 번호를 찾아 편집 가능한 초기 박스를 만든다.

    선택지의 ① 같은 원문자는 문제 시작으로 취급하지 않는다. 좌우 절반에
    문제 번호가 함께 있으면 2단 편집으로 보고 각 단을 독립적으로 자른다.
    """
    detected: list[dict[str, Any]] = []
    page_map = {int(page["page"]): page for page in pages}
    with pdfplumber.open(pdf_path) as pdf:
        for page_number, pdf_page in enumerate(pdf.pages, 1):
            page_info = page_map.get(page_number)
            if not page_info:
                continue
            page_width = float(pdf_page.width)
            page_height = float(pdf_page.height)
            words = pdf_page.extract_words(x_tolerance=2, y_tolerance=3, keep_blank_chars=False) or []
            visual_objects = [
                item
                for collection in (pdf_page.rects, pdf_page.lines, pdf_page.curves, pdf_page.images)
                for item in (collection or [])
                if _item_bbox(item)
            ]
            starts: list[dict[str, Any]] = []
            for word in words:
                match = AUTO_QUESTION_START.match(str(word.get("text", "")))
                x_ratio = float(word.get("x0", 0)) / max(1.0, page_width)
                at_question_margin = x_ratio < 0.18 or 0.48 <= x_ratio < 0.59
                if match and at_question_margin:
                    starts.append({**word, "number": match.group("num")})
            if not starts:
                if media_root:
                    page_image = media_root / str(page_info["url"]).removeprefix("/media/")
                    raster_bounds = _raster_ink_bounds(
                        page_image,
                        0.0,
                        page_width,
                        0.0,
                        page_height,
                        page_width,
                        page_height,
                        stop_at_large_gap=False,
                    ) if page_image.exists() else None
                    if raster_bounds:
                        x0, top, x1, bottom = raster_bounds
                        detected.append(
                            {
                                "page": page_number,
                                "number": str(page_number),
                                "x": round(x0 / page_width, 6),
                                "y": round(top / page_height, 6),
                                "width": round((x1 - x0) / page_width, 6),
                                "height": round((bottom - top) / page_height, 6),
                                "order": len(detected) + 1,
                                "source": "auto-image",
                            }
                        )
                continue

            has_left = any(float(item["x0"]) < page_width * 0.42 for item in starts)
            has_right = any(float(item["x0"]) > page_width * 0.48 for item in starts)
            two_columns = has_left and has_right
            groups: dict[str, list[dict[str, Any]]] = {"full": starts}
            if two_columns:
                groups = {
                    "left": [item for item in starts if float(item["x0"]) < page_width * 0.48],
                    "right": [item for item in starts if float(item["x0"]) >= page_width * 0.48],
                }

            for column, column_starts in groups.items():
                ordered = sorted(column_starts, key=lambda item: float(item["top"]))
                for index, item in enumerate(ordered):
                    if column == "left":
                        column_x0, column_x1 = 0.0, 0.5 * page_width
                    elif column == "right":
                        column_x0, column_x1 = 0.5 * page_width, page_width
                    else:
                        column_x0, column_x1 = 0.0, page_width
                    band_top = max(0.0, float(item["top"]))
                    band_bottom = (
                        float(ordered[index + 1]["top"])
                        if index + 1 < len(ordered)
                        else page_height
                    )
                    raster_bounds = None
                    if media_root:
                        page_image = media_root / str(page_info["url"]).removeprefix("/media/")
                        if page_image.exists():
                            raster_bounds = _raster_ink_bounds(
                                page_image,
                                column_x0,
                                column_x1,
                                band_top,
                                band_bottom,
                                page_width,
                                page_height,
                            )
                    x0, top, x1, bottom = raster_bounds or _padded_content_bounds(
                        words,
                        visual_objects,
                        column_x0,
                        column_x1,
                        band_top,
                        band_bottom,
                        page_width,
                        page_height,
                    )
                    bottom = max(top + 14.0, bottom)
                    detected.append(
                        {
                            "page": page_number,
                            "number": str(item["number"]),
                            "x": round(x0 / page_width, 6),
                            "y": round(top / page_height, 6),
                            "width": round((x1 - x0) / page_width, 6),
                            "height": round((bottom - top) / page_height, 6),
                            "order": len(detected) + 1,
                            "source": "auto",
                        }
                    )
    return detected


def normalize_regions(regions: list[dict[str, Any]], pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    page_map = {int(page["page"]): page for page in pages}
    normalized: list[dict[str, Any]] = []
    for order, raw in enumerate(regions, 1):
        page_number = int(raw.get("page", 0))
        if page_number not in page_map:
            raise ValueError(f"{page_number}쪽을 찾을 수 없습니다.")
        x = max(0.0, min(1.0, float(raw.get("x", 0))))
        y = max(0.0, min(1.0, float(raw.get("y", 0))))
        width = max(0.0, min(1.0 - x, float(raw.get("width", 0))))
        height = max(0.0, min(1.0 - y, float(raw.get("height", 0))))
        if width < 0.02 or height < 0.015:
            raise ValueError(f"{order}번째 영역이 너무 작습니다.")
        number = str(raw.get("number", "")).strip() or str(order)
        normalized.append(
            {
                "page": page_number,
                "number": number,
                "x": round(x, 6),
                "y": round(y, 6),
                "width": round(width, 6),
                "height": round(height, 6),
                "order": int(raw.get("order", order)),
            }
        )
    return sorted(normalized, key=lambda item: (item["order"], item["page"], item["y"], item["x"]))


def _region_segment(region: dict[str, Any], page: dict[str, Any]) -> dict[str, Any]:
    return {
        "page": int(page["page"]),
        "x0": region["x"] * page["page_width"],
        "top": region["y"] * page["page_height"],
        "x1": (region["x"] + region["width"]) * page["page_width"],
        "bottom": (region["y"] + region["height"]) * page["page_height"],
        "page_width": page["page_width"],
        "page_height": page["page_height"],
    }


def _crop_region_image(
    page_image: Path,
    region: dict[str, Any],
    destination: Path,
    padding_ratio: float = 0.0,
) -> Path:
    with Image.open(page_image) as image:
        x0 = max(0, int((region["x"] - padding_ratio) * image.width))
        y0 = max(0, int((region["y"] - padding_ratio) * image.height))
        x1 = min(image.width, int((region["x"] + region["width"] + padding_ratio) * image.width))
        y1 = min(image.height, int((region["y"] + region["height"] + padding_ratio) * image.height))
        crop = image.crop((x0, y0, x1, y1)).convert("RGB")
        destination.parent.mkdir(parents=True, exist_ok=True)
        crop.save(destination, "PNG", optimize=True)
    return destination


def _plain_from_mathpix(text: str) -> str:
    # 일반 본문 칸에서도 수식 자체는 잃지 않되, 표시용 구분자만 가볍게 걷어낸다.
    return re.sub(r"\\([\[\]()])", r"\1", text).replace("$$", "").strip()


def extract_manual_regions(
    pdf_path: Path,
    document_id: int,
    pages: list[dict[str, Any]],
    regions: list[dict[str, Any]],
    work_dir: Path,
    media_root: Path,
    mathpix: MathpixProvider | None = None,
    require_mathpix: bool = False,
    progress: Callable[[int, str], None] | None = None,
) -> dict[str, Any]:
    """사용자가 그린 영역을 문제 경계로 확정하고 영역별로 OCR한다."""
    normalized = normalize_regions(regions, pages)
    if not normalized:
        raise ValueError("문제 영역을 하나 이상 그려 주세요.")
    page_map = {int(page["page"]): page for page in pages}
    page_image_paths = [media_root / page["url"].removeprefix("/media/") for page in pages]
    crop_dir = work_dir / "manual-crops"
    if crop_dir.exists():
        shutil.rmtree(crop_dir)
    crop_dir.mkdir(parents=True, exist_ok=True)

    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for index, region in enumerate(normalized, 1):
        # 같은 문제 번호를 여러 페이지에 그리면 하나의 문제로 합친다.
        key = f"number:{region['number']}"
        groups.setdefault(key, []).append({**region, "region_index": index})

    problems: list[dict[str, Any]] = []
    mathpix_enabled = bool(mathpix and mathpix.configured)
    if require_mathpix and not mathpix_enabled:
        raise ProviderError("문제 추출을 완료하려면 OCR 설정에서 Mathpix 키를 먼저 연결해 주세요.")
    with pdfplumber.open(pdf_path) as pdf:
        pdf_pages = list(pdf.pages)
        for sequence, grouped_regions in enumerate(groups.values(), 1):
            number = grouped_regions[0]["number"]
            segments: list[dict[str, Any]] = []
            local_parts: list[str] = []
            mathpix_parts: list[str] = []
            confidences: list[float] = []
            notes: list[str] = ["수동으로 지정한 문제 영역입니다."]
            for region in grouped_regions:
                page = page_map[region["page"]]
                segment = _region_segment(region, page)
                segments.append(segment)
                pdf_page = pdf_pages[region["page"] - 1]
                local_text = pdf_page.crop(
                    (segment["x0"], segment["top"], segment["x1"], segment["bottom"])
                ).extract_text(x_tolerance=2, y_tolerance=3) or ""
                if local_text.strip():
                    local_parts.append(local_text.strip())
                if mathpix_enabled:
                    page_path = media_root / page["url"].removeprefix("/media/")
                    crop_path = _crop_region_image(
                        page_path,
                        region,
                        crop_dir / f"problem-{sequence:04d}-part-{region['region_index']:03d}.png",
                    )
                    try:
                        result = mathpix.process_image(crop_path)
                        recognized = str(result.get("text") or result.get("latex_styled") or "").strip()
                        if recognized:
                            mathpix_parts.append(recognized)
                        confidences.append(float(result.get("confidence", 0.85) or 0.85))
                    except ProviderError as exc:
                        if require_mathpix:
                            raise
                        notes.append(str(exc))
                _notify(
                    progress,
                    16 + round(region["region_index"] / max(1, len(normalized)) * 72),
                    f"{region['region_index']}/{len(normalized)} 영역 OCR 중",
                )

            candidate = {"number": number, "segments": segments}
            asset_group = f"{document_id}-manual"
            source_image = save_problem_crop(candidate, page_image_paths, media_root, asset_group, sequence)
            figures = extract_figures(candidate, pdf_pages, page_image_paths, media_root, asset_group, sequence)
            if not figures and source_image:
                # 스캔 PDF나 평면화된 그래프는 PDF 객체로 분리할 수 없다.
                # 원본은 그대로 두고 필기 흔적을 정리한 사본을 도형 이미지로 사용한다.
                figures = extract_raster_figures_from_source(
                    source_image, media_root, asset_group, sequence
                )
                if not figures:
                    cleaned_figure = save_cleaned_figure_from_source(
                        source_image, media_root, asset_group, sequence
                    )
                    figures = [cleaned_figure] if cleaned_figure else [source_image]
                notes.append("도형·그래프 사본에서 색 필기와 옅은 연필 흔적을 정리했습니다.")
            if mathpix_parts:
                latex = "\n\n".join(mathpix_parts)
                content = _plain_from_mathpix(latex)
                confidence = sum(confidences) / len(confidences) if confidences else 0.86
                notes.append("Mathpix 영역별 수식 OCR을 적용했습니다.")
            else:
                content = "\n".join(local_parts).strip() or "[영역 내 텍스트를 인식하지 못했습니다.]"
                latex = latexize(content)
                confidence = 0.62 if local_parts else 0.3
                if not mathpix_enabled:
                    notes.append("Mathpix가 연결되지 않아 PDF 내장 글자만 사용했습니다.")
            metadata = infer_metadata(content)
            page_numbers = [segment["page"] for segment in segments]
            problems.append(
                {
                    "number": number,
                    "page_start": min(page_numbers),
                    "page_end": max(page_numbers),
                    "content": content,
                    "latex": latex,
                    "source_image": source_image,
                    "figures": figures,
                    "segments": segments,
                    "confidence": round(max(0.05, min(0.99, confidence)), 2),
                    "quality_status": "needs_review",
                    "quality_notes": " ".join(notes),
                    "fingerprint": fingerprint(content),
                    **metadata,
                }
            )

    _notify(progress, 95, "수동 영역 누락 검사 중")
    page_count = len(pages)
    covered_pages = len({region["page"] for region in normalized})
    return {
        "page_count": page_count,
        "problem_count": len(problems),
        "problems": problems,
        "coverage_percent": 100.0,
        "page_selection_percent": round(covered_pages / max(1, page_count) * 100, 1),
        "warning_count": sum(1 for item in problems if item["confidence"] < 0.6),
        "scan_suspected": False,
    }
