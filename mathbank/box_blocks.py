from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps

from .latex_text import latexize_plain_numbers


BOX_CONFIDENCE_THRESHOLD = 0.60


def normalize_box_latex(value: str) -> str:
    """Convert Mathpix table markup into KaTeX-compatible structured math."""
    text = str(value or "").strip()
    if not re.search(r"\\begin\s*\{tabular\}", text):
        return text
    text = re.sub(r"\\begin\s*\{tabular\}\s*", r"\\begin{array}", text, count=1)
    text = re.sub(r"\\end\s*\{tabular\}", r"\\end{array}", text)
    # Inline math delimiters are invalid once the entire table is in display math.
    text = text.replace(r"\(", "").replace(r"\)", "")
    # `\hline\(z\)` would otherwise become the invalid command `\hlinez`.
    text = re.sub(r"\\hline(?!\s)", r"\\hline ", text)
    return "\\[\n" + text + "\n\\]"


def classify_box(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if re.search(r"\\begin\s*\{(?:tabular|array)\}", compact):
        return "table_block"
    if re.search(r"(?:^|[\[【<])\s*(?:보기|예시|예제)", compact):
        return "example_box"
    if re.search(r"(?:^|[\[【<])\s*(?:조건|가정)", compact):
        return "condition_box"
    if len(re.findall(r"[①②③④⑤]|\([1-5]\)", compact)) >= 2:
        return "choice_box"
    return "condition_box"


def _group_positions(values: list[tuple[int, int, int]]) -> list[tuple[int, int, int]]:
    if not values:
        return []
    groups: list[list[tuple[int, int, int]]] = [[values[0]]]
    for item in values[1:]:
        if item[0] <= groups[-1][-1][0] + 2:
            groups[-1].append(item)
        else:
            groups.append([item])
    result = []
    for group in groups:
        position = round(sum(item[0] for item in group) / len(group))
        left = min(item[1] for item in group)
        right = max(item[2] for item in group)
        result.append((position, left, right))
    return result


def detect_raster_box_rects(image: Image.Image) -> list[tuple[int, int, int, int]]:
    """Detect printed rectangular borders without classifying their contents as images."""
    original_width, original_height = image.size
    if original_width < 80 or original_height < 80:
        return []
    scale = min(1.0, 1000 / max(original_width, original_height))
    gray = ImageOps.grayscale(image)
    if scale < 1:
        gray = gray.resize(
            (round(original_width * scale), round(original_height * scale)),
            Image.Resampling.BILINEAR,
        )
    width, height = gray.size
    pixels = gray.load()
    horizontal: list[tuple[int, int, int]] = []
    minimum_run = max(45, round(width * 0.22))
    for y in range(height):
        best_start = best_end = current_start = -1
        for x in range(width):
            if pixels[x, y] < 165:
                if current_start < 0:
                    current_start = x
            elif current_start >= 0:
                if x - current_start > best_end - best_start:
                    best_start, best_end = current_start, x
                current_start = -1
        if current_start >= 0 and width - current_start > best_end - best_start:
            best_start, best_end = current_start, width
        if best_start >= 0 and best_end - best_start >= minimum_run:
            horizontal.append((y, best_start, best_end))

    lines = _group_positions(horizontal)
    candidates: list[tuple[int, int, int, int]] = []
    for top_index, (top, top_left, top_right) in enumerate(lines):
        for bottom, bottom_left, bottom_right in lines[top_index + 1 :]:
            box_height = bottom - top
            left = round((top_left + bottom_left) / 2)
            right = round((top_right + bottom_right) / 2)
            box_width = right - left
            if box_height < height * 0.035 or box_height > height * 0.72:
                continue
            if box_width < width * 0.20 or abs(top_left - bottom_left) > width * 0.035:
                continue
            if abs(top_right - bottom_right) > width * 0.035:
                continue

            def vertical_ratio(x_position: int) -> float:
                hits = 0
                sample_count = max(1, bottom - top + 1)
                for y in range(top, bottom + 1):
                    if any(
                        0 <= x < width and pixels[x, y] < 175
                        for x in range(x_position - 2, x_position + 3)
                    ):
                        hits += 1
                return hits / sample_count

            # Real box sides are nearly continuous. A lower threshold combines
            # unrelated horizontal borders into duplicate, overlapping boxes.
            if vertical_ratio(left) < 0.88 or vertical_ratio(right - 1) < 0.88:
                continue
            candidates.append((left, top, right, bottom + 1))

    # A ruled table creates a candidate from its top edge to every internal
    # horizontal line. Keep only the outer rectangle for one table block.
    outermost: list[tuple[int, int, int, int]] = []
    for box in sorted(candidates, key=lambda item: -((item[2] - item[0]) * (item[3] - item[1]))):
        tolerance = max(5, round((box[2] - box[0]) * 0.04))
        if any(
            old[0] <= box[0] + tolerance
            and old[1] <= box[1] + tolerance
            and old[2] >= box[2] - tolerance
            and old[3] >= box[3] - tolerance
            and abs(old[0] - box[0]) <= tolerance
            and abs(old[2] - box[2]) <= tolerance
            for old in outermost
        ):
            continue
        outermost.append(box)

    deduped: list[tuple[int, int, int, int]] = []
    for box in sorted(outermost, key=lambda item: (item[1], item[0])):
        if any(
            abs(box[0] - old[0]) < 6
            and abs(box[1] - old[1]) < 6
            and abs(box[2] - old[2]) < 6
            and abs(box[3] - old[3]) < 6
            for old in deduped
        ):
            continue
        deduped.append(box)

    inverse = 1 / scale
    return [tuple(round(value * inverse) for value in box) for box in deduped[:10]]


def _save_box_image(
    image: Image.Image,
    box: tuple[int, int, int, int],
    media_root: Path,
    asset_group: str,
    sequence: int,
    box_index: int,
) -> tuple[str, Path]:
    relative = Path("boxes") / asset_group / f"problem-{sequence:04d}-box-{box_index:02d}.webp"
    destination = media_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    image.crop(box).convert("RGB").save(destination, "WEBP", quality=95, method=6)
    return "/media/" + relative.as_posix(), destination


def _block_payload(
    *,
    order: int,
    kind: str,
    content: str,
    latex: str,
    confidence: float,
    source_image: str,
    region: dict[str, float] | None,
    figures: list[str] | None = None,
    elements: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    structured = bool(content.strip() or latex.strip()) and confidence >= BOX_CONFIDENCE_THRESHOLD
    ordered_elements: list[dict[str, Any]] = list(elements or [])
    if not ordered_elements and (content.strip() or latex.strip()):
        ordered_elements.append({"type": "text_math", "content": content, "latex": latex, "order": 1})
    if not elements:
        for index, figure in enumerate(figures or [], len(ordered_elements) + 1):
            ordered_elements.append({"type": "figure", "source": figure, "order": index})
    return {
        "id": f"box-{order}",
        "type": kind,
        "order": order,
        "content": content,
        "latex": latex,
        "confidence": round(max(0.0, min(0.99, confidence)), 2),
        "source_image": source_image,
        "figures": figures or [],
        "elements": ordered_elements,
        "region": region,
        "border": {"style": "solid", "width": 1},
        "display_mode": "structured" if structured else "image_fallback",
    }


def extract_raster_box_blocks(
    source_url: str,
    media_root: Path,
    asset_group: str,
    sequence: int,
    *,
    mathpix: Any | None = None,
    latexizer: Callable[[str], str] | None = None,
    figure_detector: Callable[[Image.Image], list[tuple[int, int, int, int]]] | None = None,
    figure_cleaner: Callable[[Image.Image], Image.Image] | None = None,
) -> list[dict[str, Any]]:
    source_path = media_root / source_url.removeprefix("/media/")
    if not source_url or not source_path.is_file():
        return []
    blocks: list[dict[str, Any]] = []
    with Image.open(source_path) as opened:
        source = opened.convert("RGB")
        for index, box in enumerate(detect_raster_box_rects(source), 1):
            source_image, box_path = _save_box_image(source, box, media_root, asset_group, sequence, index)
            content = latex = ""
            confidence = 0.25
            mathpix_result: dict[str, Any] = {}
            if mathpix is not None and getattr(mathpix, "configured", False):
                mathpix_result = mathpix.process_image(box_path)
                content = str(mathpix_result.get("text") or mathpix_result.get("latex_styled") or "").strip()
                latex = latexize_plain_numbers(normalize_box_latex(content))
                confidence = float(mathpix_result.get("confidence", 0.82) or 0.82)
            elif latexizer:
                latex = latexizer(content)

            figures: list[str] = []
            positioned_elements: list[tuple[float, dict[str, Any]]] = []
            for line_index, line in enumerate(mathpix_result.get("line_data") or []):
                line_text = str(line.get("text") or line.get("latex") or "").strip()
                if not line_text:
                    continue
                contour = line.get("cnt") or []
                if contour and isinstance(contour[0], (list, tuple)):
                    line_top = min(float(point[1]) for point in contour if len(point) >= 2)
                else:
                    line_top = float(line_index)
                positioned_elements.append((line_top, {
                    "type": "text_math", "content": line_text,
                    "latex": latexize_plain_numbers(normalize_box_latex(line_text)),
                }))
            structured_table = bool(re.search(r"\\begin\s*\{(?:tabular|array)\}", latex or content))
            if figure_detector and not structured_table:
                inner_margin = max(3, round(min(box[2] - box[0], box[3] - box[1]) * 0.02))
                crop = source.crop((box[0] + inner_margin, box[1] + inner_margin, box[2] - inner_margin, box[3] - inner_margin))
                for figure_index, figure_box in enumerate(figure_detector(crop), 1):
                    figure = crop.crop(figure_box)
                    if figure_cleaner:
                        figure = figure_cleaner(figure)
                    relative = Path("figures") / asset_group / f"problem-{sequence:04d}-box-{index:02d}-figure-{figure_index:02d}.webp"
                    destination = media_root / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    figure.convert("RGB").save(destination, "WEBP", quality=94, method=6)
                    figures.append("/media/" + relative.as_posix())
                    positioned_elements.append((float(figure_box[1]), {
                        "type": "figure", "source": "/media/" + relative.as_posix(),
                    }))

            if not positioned_elements and (content or latex):
                positioned_elements.append((0.0, {"type": "text_math", "content": content, "latex": latex}))
            ordered_elements = []
            for element_order, (_, element) in enumerate(sorted(positioned_elements, key=lambda item: item[0]), 1):
                ordered_elements.append({**element, "order": element_order})

            region = {
                "x": box[0] / source.width,
                "y": box[1] / source.height,
                "width": (box[2] - box[0]) / source.width,
                "height": (box[3] - box[1]) / source.height,
            }
            blocks.append(
                _block_payload(
                    order=index,
                    kind=classify_box(content),
                    content=content,
                    latex=latex,
                    confidence=confidence,
                    source_image=source_image,
                    region=region,
                    figures=figures,
                    elements=ordered_elements,
                )
            )
    return blocks


def extract_vector_box_blocks(
    problem: dict[str, Any],
    pdf_pages: list[Any],
    page_images: list[Path],
    media_root: Path,
    asset_group: str,
    sequence: int,
    *,
    latexizer: Callable[[str], str],
) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for segment in problem.get("segments", []):
        page_index = int(segment["page"]) - 1
        page = pdf_pages[page_index]
        with Image.open(page_images[page_index]) as opened:
            page_image = opened.convert("RGB")
            scale_x = page_image.width / float(page.width)
            scale_y = page_image.height / float(page.height)
            for rect in page.rects:
                x0 = float(rect.get("x0", 0))
                x1 = float(rect.get("x1", x0))
                top = float(rect.get("top", 0))
                bottom = float(rect.get("bottom", top))
                width, height = x1 - x0, bottom - top
                if width < 28 or height < 18:
                    continue
                if top < segment["top"] or bottom > segment["bottom"]:
                    continue
                if width > (segment["x1"] - segment["x0"]) * 0.96 and height > (segment["bottom"] - segment["top"]) * 0.90:
                    continue
                box = (
                    max(0, round(x0 * scale_x)),
                    max(0, round(top * scale_y)),
                    min(page_image.width, round(x1 * scale_x)),
                    min(page_image.height, round(bottom * scale_y)),
                )
                if box[2] <= box[0] or box[3] <= box[1]:
                    continue
                order = len(blocks) + 1
                source_image, _ = _save_box_image(page_image, box, media_root, asset_group, sequence, order)
                cropped_page = page.crop((x0 + 2, top + 2, x1 - 2, bottom - 2))
                content = (cropped_page.extract_text(x_tolerance=2, y_tolerance=3, layout=True) or "").strip()
                latex = latexizer(content) if content else ""
                confidence = 0.88 if len(re.sub(r"\s+", "", content)) >= 3 else 0.30
                payload = _block_payload(
                        order=order,
                        kind=classify_box(content),
                        content=content,
                        latex=latex,
                        confidence=confidence,
                        source_image=source_image,
                        region=None,
                    )
                payload["page_region"] = {
                    "page": int(segment["page"]), "x0": x0, "top": top, "x1": x1, "bottom": bottom,
                }
                blocks.append(payload)
    return blocks
