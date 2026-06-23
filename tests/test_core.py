from __future__ import annotations

import tempfile
import unittest
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from PIL import Image, ImageDraw

from mathbank.box_blocks import classify_box, detect_raster_box_rects, extract_raster_box_blocks, normalize_box_latex
from mathbank.db import Database
from mathbank.extractor import (
    clean_figure_image,
    detect_raster_figure_boxes,
    extract_pdf,
    extract_figures,
    extract_raster_figures_from_source,
    save_image_as_pdf,
    similar_score,
    strip_problem_number,
)
from mathbank.manual import detect_initial_regions, extract_manual_regions, prepare_manual_pdf
from mathbank.latex_text import latexize_plain_numbers
from mathbank.providers import MathpixProvider


def make_sample_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=(595, 842))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(60, 790, "Sample Mathematics Test")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(60, 745, "1. Solve x + 2 = 5.")
    pdf.drawString(80, 720, "Write the value of x.")
    pdf.drawString(60, 650, "2. Find the area of the rectangle below.")
    pdf.rect(140, 510, 180, 90)
    pdf.drawString(205, 495, "8 cm")
    pdf.drawString(325, 550, "5 cm")
    pdf.showPage()
    pdf.setFont("Helvetica", 11)
    pdf.drawString(60, 750, "3. If f(x) = x^2, find f(4).")
    pdf.drawString(80, 720, "Show your work.")
    pdf.save()


def make_scan_pdf(path: Path, image_path: Path) -> None:
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((120, 180, 780, 820), outline="black", width=5)
    image.save(image_path)
    pdf = canvas.Canvas(str(path), pagesize=(595, 842))
    for _ in range(2):
        pdf.drawImage(ImageReader(str(image_path)), 0, 0, width=595, height=842)
        pdf.showPage()
    pdf.save()


def make_two_column_pdf(path: Path) -> None:
    pdf = canvas.Canvas(str(path), pagesize=(600, 800))
    pdf.setFont("Helvetica", 11)
    pdf.drawString(35, 730, "1. Solve x + 3 = 8.")
    pdf.drawString(35, 705, "Choose the value of x.")
    pdf.drawString(320, 730, "2. Find the area of a square.")
    pdf.drawString(320, 705, "Its side length is 4.")
    pdf.rect(380, 560, 100, 100)
    pdf.save()


class ExtractorTests(unittest.TestCase):
    def test_leading_problem_number_is_removed_from_ocr_text(self):
        self.assertEqual(strip_problem_number("26. 그림과 같이 함수가 있다."), "그림과 같이 함수가 있다.")
        self.assertEqual(strip_problem_number("문항 7) 값을 구하여라."), "값을 구하여라.")
        self.assertEqual(strip_problem_number("① 다음 중 옳은 것은?"), "다음 중 옳은 것은?")
        self.assertEqual(strip_problem_number("2024년 시행 문제"), "2024년 시행 문제")

    def test_plain_numbers_become_latex_without_double_wrapping_math(self):
        value = latexize_plain_numbers(r"6 이하이고 \(X=4800\)일 때 0.5와 3번")
        self.assertEqual(value, r"\(6\) 이하이고 \(X=4800\)일 때 \(0.5\)와 \(3\)번")
        self.assertEqual(latexize_plain_numbers(value), value)

    def test_condition_box_is_detected_but_open_graph_axes_are_not(self):
        boxed = Image.new("RGB", (600, 500), "white")
        boxed_draw = ImageDraw.Draw(boxed)
        boxed_draw.rectangle((80, 120, 520, 330), outline="black", width=4)
        boxed_draw.text((110, 165), "Condition: x + y = 3", fill="black")
        boxes = detect_raster_box_rects(boxed)
        self.assertEqual(len(boxes), 1)
        self.assertLess(abs(boxes[0][0] - 80), 8)

        graph = Image.new("RGB", (600, 500), "white")
        graph_draw = ImageDraw.Draw(graph)
        graph_draw.line((100, 400, 520, 400), fill="black", width=4)
        graph_draw.line((100, 400, 100, 80), fill="black", width=4)
        graph_draw.line((100, 350, 450, 120), fill="black", width=4)
        self.assertEqual(detect_raster_box_rects(graph), [])

    def test_separate_boxes_do_not_create_cross_spanning_duplicates(self):
        image = Image.new("RGB", (600, 900), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((60, 80, 540, 260), outline="black", width=4)
        draw.rectangle((60, 390, 540, 780), outline="black", width=4)
        boxes = detect_raster_box_rects(image)
        self.assertEqual(len(boxes), 2)
        self.assertLess(boxes[0][3], boxes[1][1])

    def test_ruled_normal_distribution_table_is_one_outer_box(self):
        image = Image.new("RGB", (420, 420), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((250, 120, 390, 360), outline="black", width=2)
        draw.line((300, 120, 300, 360), fill="black", width=2)
        for y in range(150, 361, 30):
            draw.line((250, y, 390, y), fill="black", width=2)
        boxes = detect_raster_box_rects(image)
        self.assertEqual(len(boxes), 1)
        left, top, right, bottom = boxes[0]
        self.assertLessEqual(left, 252)
        self.assertLessEqual(top, 122)
        self.assertGreaterEqual(right, 389)
        self.assertGreaterEqual(bottom, 359)

    def test_math_table_is_latex_not_an_internal_figure(self):
        class FakeTableMathpix:
            configured = True

            def process_image(self, _path):
                table = r"\begin{tabular}{|c|c|}\hline \(z\)&\(P(0\le Z\le z)\)\\\hline 0.5&0.191\\\hline\end{tabular}"
                return {"text": table, "confidence": 0.97, "line_data": [{"text": table}]}

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            source = media / "problems" / "1" / "problem-0001.webp"
            source.parent.mkdir(parents=True)
            image = Image.new("RGB", (600, 500), "white")
            ImageDraw.Draw(image).rectangle((180, 100, 520, 390), outline="black", width=4)
            image.save(source, "WEBP")
            blocks = extract_raster_box_blocks(
                "/media/problems/1/problem-0001.webp", media, "1", 1,
                mathpix=FakeTableMathpix(), figure_detector=lambda _image: [(0, 0, 300, 250)],
            )
            self.assertEqual(blocks[0]["figures"], [])
            self.assertFalse(any(item["type"] == "figure" for item in blocks[0]["elements"]))
            self.assertIn(r"\begin{array}", blocks[0]["latex"])
            self.assertNotIn(r"\begin{tabular}", blocks[0]["latex"])
            self.assertNotIn(r"\hlinez", blocks[0]["latex"])
            self.assertIn(r"\hline z", blocks[0]["latex"])
            self.assertEqual(classify_box(blocks[0]["content"]), "table_block")
            self.assertEqual(normalize_box_latex("ordinary text"), "ordinary text")

    def test_condition_box_region_is_excluded_from_figure_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            source = media / "problems" / "1" / "problem-0001.webp"
            source.parent.mkdir(parents=True)
            image = Image.new("RGB", (600, 500), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((60, 80, 540, 400), outline="black", width=5)
            draw.line((100, 170, 480, 170), fill="black", width=4)
            image.save(source, "WEBP")
            figures = extract_raster_figures_from_source(
                "/media/problems/1/problem-0001.webp",
                media,
                "1",
                1,
                exclude_regions=[{"x":0.1,"y":0.16,"width":0.8,"height":0.64}],
            )
            self.assertEqual(figures, [])

    def test_low_confidence_box_keeps_original_as_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            source = media / "problems" / "1" / "problem-0001.webp"
            source.parent.mkdir(parents=True)
            image = Image.new("RGB", (600, 500), "white")
            ImageDraw.Draw(image).rectangle((80, 120, 520, 330), outline="black", width=4)
            image.save(source, "WEBP")
            blocks = extract_raster_box_blocks(
                "/media/problems/1/problem-0001.webp", media, "1", 1
            )
            self.assertEqual(len(blocks), 1)
            self.assertEqual(blocks[0]["display_mode"], "image_fallback")
            self.assertTrue((media / blocks[0]["source_image"].removeprefix("/media/")).exists())

    def test_box_mathpix_line_order_is_preserved(self):
        class FakeBoxMathpix:
            configured = True

            def process_image(self, _path):
                return {
                    "text": "조건 x=1\ny=2",
                    "confidence": 0.95,
                    "line_data": [
                        {"text": "조건 x=1", "cnt": [[0, 12], [100, 12], [100, 28], [0, 28]]},
                        {"text": "y=2", "cnt": [[0, 42], [80, 42], [80, 58], [0, 58]]},
                    ],
                }

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "media"
            source = media / "problems" / "1" / "problem-0001.webp"
            source.parent.mkdir(parents=True)
            image = Image.new("RGB", (600, 500), "white")
            ImageDraw.Draw(image).rectangle((80, 120, 520, 330), outline="black", width=4)
            image.save(source, "WEBP")
            blocks = extract_raster_box_blocks(
                "/media/problems/1/problem-0001.webp", media, "1", 1, mathpix=FakeBoxMathpix()
            )
            self.assertEqual(blocks[0]["display_mode"], "structured")
            self.assertEqual([item["content"] for item in blocks[0]["elements"]], ["조건 x=1", "y=2"])

    def test_raster_diagram_is_separated_from_text_rows(self):
        image = Image.new("RGB", (600, 800), "white")
        draw = ImageDraw.Draw(image)
        for y in (40, 75, 110, 145):
            draw.text((30, y), "A short mathematics problem line", fill="black")
        draw.ellipse((180, 260, 480, 610), outline="black", width=5)
        draw.line((210, 560, 445, 315), fill="black", width=5)
        boxes = detect_raster_figure_boxes(image)
        self.assertTrue(boxes)
        left, top, right, bottom = boxes[0]
        self.assertGreater(top, 180)
        self.assertGreater(right - left, 250)
        self.assertGreater(bottom - top, 300)

    def test_full_problem_scan_is_not_saved_as_a_figure(self):
        class ScannedPage:
            height = 800
            images = [{"x0": 0, "top": 0, "x1": 600, "bottom": 800}]
            curves: list[dict] = []
            rects: list[dict] = []
            lines: list[dict] = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page_image = root / "page.png"
            Image.new("RGB", (600, 800), "white").save(page_image)
            problem = {"segments": [{
                "page": 1, "page_width": 600, "page_height": 800,
                "x0": 0, "top": 0, "x1": 600, "bottom": 800,
            }]}
            figures = extract_figures(problem, [ScannedPage()], [page_image], root / "media", 1, 1)
            self.assertEqual(figures, [])

    def test_figure_cleanup_removes_colored_and_faint_marks(self):
        image = Image.new("RGB", (120, 80), "white")
        draw = ImageDraw.Draw(image)
        draw.line((10, 20, 110, 20), fill="black", width=3)
        draw.line((10, 40, 110, 40), fill=(255, 0, 0), width=3)
        draw.line((10, 60, 110, 60), fill=(205, 205, 205), width=3)
        cleaned = clean_figure_image(image)
        self.assertLess(cleaned.getpixel((60, 20))[0], 20)
        self.assertGreater(cleaned.getpixel((60, 40))[0], 240)
        self.assertGreater(cleaned.getpixel((60, 60))[0], 240)

    def test_jpeg_upload_is_converted_to_one_page_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "problem.jpg"
            pdf_path = root / "problem.pdf"
            Image.new("RGB", (640, 480), "white").save(image_path, "JPEG")
            save_image_as_pdf(image_path.read_bytes(), pdf_path)
            result = extract_pdf(pdf_path, 7, root / "work", root / "media")
            self.assertEqual(result["page_count"], 1)

    def test_transparent_png_upload_is_converted_to_one_page_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image_path = root / "capture.png"
            pdf_path = root / "capture.pdf"
            image = Image.new("RGBA", (640, 480), (255, 255, 255, 0))
            ImageDraw.Draw(image).text((40, 40), "1. x + 1 = 2", fill=(0, 0, 0, 255))
            image.save(image_path, "PNG")
            save_image_as_pdf(image_path.read_bytes(), pdf_path)
            result = extract_pdf(pdf_path, 8, root / "work", root / "media")
            self.assertEqual(result["page_count"], 1)

    def test_extracts_numbered_problems_and_images(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "sample.pdf"
            make_sample_pdf(pdf_path)
            result = extract_pdf(pdf_path, 1, root / "work", root / "media")
            self.assertEqual(result["page_count"], 2)
            self.assertEqual([item["number"] for item in result["problems"]], ["1", "2", "3"])
            self.assertEqual(result["coverage_percent"], 100.0)
            self.assertEqual(result["problems"][1]["page_end"], 1)
            for problem in result["problems"]:
                media_path = root / "media" / problem["source_image"].removeprefix("/media/")
                self.assertTrue(media_path.exists())
            self.assertTrue(result["problems"][1]["figures"])

    def test_similarity_prefers_same_concept(self):
        source = {"content": "함수 f(x)=x+1의 값을 구하여라", "latex": "", "unit": "함수", "concept": "함수", "problem_type": "주관식", "difficulty": 2}
        close = {"content": "함수 g(x)=x+3의 값을 구하여라", "latex": "", "unit": "함수", "concept": "함수", "problem_type": "주관식", "difficulty": 2}
        far = {"content": "삼각형의 넓이를 구하여라", "latex": "", "unit": "기하", "concept": "기하", "problem_type": "주관식", "difficulty": 4}
        self.assertGreater(similar_score(source, close), similar_score(source, far))

    def test_scan_keeps_every_page_as_review_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "scan.pdf"
            make_scan_pdf(pdf_path, root / "scan.png")
            result = extract_pdf(pdf_path, 2, root / "work", root / "media")
            self.assertTrue(result["scan_suspected"])
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(result["problem_count"], 2)
            self.assertTrue(all(item["confidence"] <= 0.38 for item in result["problems"]))

    def test_manual_regions_keep_two_columns_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "columns.pdf"
            make_two_column_pdf(pdf_path)
            pages = prepare_manual_pdf(pdf_path, 3, root / "work", root / "media")
            detected = detect_initial_regions(pdf_path, pages, root / "media")
            self.assertEqual([item["number"] for item in detected], ["1", "2"])
            self.assertLess(detected[0]["x"], 0.1)
            self.assertGreater(detected[1]["x"], 0.45)
            self.assertLess(detected[0]["width"], 0.4)
            self.assertLess(detected[1]["width"], 0.4)
            self.assertLess(detected[0]["height"], 0.2)
            self.assertLess(detected[1]["height"], 0.35)
            regions = [
                {"page": 1, "number": "1", "x": 0.02, "y": 0.04, "width": 0.46, "height": 0.32, "order": 1},
                {"page": 1, "number": "2", "x": 0.50, "y": 0.04, "width": 0.48, "height": 0.36, "order": 2},
            ]
            result = extract_manual_regions(
                pdf_path, 3, pages, regions, root / "work", root / "media"
            )
            self.assertEqual(result["problem_count"], 2)
            self.assertIn("Solve", result["problems"][0]["content"])
            self.assertNotIn("square", result["problems"][0]["content"])
            self.assertIn("square", result["problems"][1]["content"])

    def test_manual_region_extraction_completes_with_mathpix_latex(self):
        class FakeMathpix:
            configured = True

            def process_image(self, _image_path):
                return {"text": r"\(x+3=8\)", "confidence": 0.97}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pdf_path = root / "columns.pdf"
            make_two_column_pdf(pdf_path)
            pages = prepare_manual_pdf(pdf_path, 4, root / "work", root / "media")
            regions = detect_initial_regions(pdf_path, pages, root / "media")
            result = extract_manual_regions(
                pdf_path,
                4,
                pages,
                [regions[0]],
                root / "work",
                root / "media",
                mathpix=FakeMathpix(),
                require_mathpix=True,
            )
            self.assertEqual(result["problems"][0]["latex"], r"\(x+3=8\)")
            self.assertGreater(result["problems"][0]["confidence"], 0.9)


class DatabaseTests(unittest.TestCase):
    def test_database_schema_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            self.assertEqual(db.one("SELECT COUNT(*) AS n FROM documents")["n"], 0)

    def test_settings_and_manual_region_state_are_persisted(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "test.sqlite3")
            db.set_setting("mathpix_app_id", "demo-id")
            self.assertEqual(db.get_setting("mathpix_app_id"), "demo-id")
            document_id = db.create_document("demo", "demo.pdf", "demo.pdf", "abc123")
            db.save_document_regions(
                document_id,
                pages=[{"page": 1, "url": "/media/page.png"}],
                regions=[{"page": 1, "number": "1", "x": 0.1, "y": 0.1, "width": 0.8, "height": 0.3}],
                stage="ready",
            )
            state = db.get_document_regions(document_id)
            self.assertEqual(state["stage"], "ready")
            self.assertEqual(state["regions"][0]["number"], "1")


class ProviderTests(unittest.TestCase):
    def test_mathpix_request_retries_with_curl_after_socket_denied(self):
        provider = MathpixProvider(app_id="demo-id", app_key="demo-key")
        request = urllib.request.Request("https://api.mathpix.com/v3/text", method="POST", data=b"test")
        socket_denied = OSError(10013, "socket access denied")
        captured = {}

        def fake_curl(request_arg, timeout_arg):
            captured["url"] = request_arg.full_url
            captured["timeout"] = timeout_arg
            return b'{"text":"ok"}'

        provider._request_with_curl = fake_curl
        with patch("mathbank.providers.urllib.request.urlopen", side_effect=urllib.error.URLError(socket_denied)):
            result = provider._request(request, timeout=12)

        self.assertEqual(result, b'{"text":"ok"}')
        self.assertEqual(captured["url"], "https://api.mathpix.com/v3/text")
        self.assertEqual(captured["timeout"], 12)

    def test_mathpix_connection_uses_production_image_ocr_path(self):
        provider = MathpixProvider(app_id="demo-id", app_key="demo-key")
        captured = {}

        def fake_process_image(image_path):
            captured["exists"] = image_path.exists()
            with Image.open(image_path) as image:
                captured["size"] = image.size
            return {"text": "\\(x^2+2x+1=0\\)", "confidence": 0.99}

        provider.process_image = fake_process_image
        result = provider.test_connection()
        self.assertEqual(result["method"], "image_ocr")
        self.assertTrue(result["recognized"])
        self.assertTrue(captured["exists"])
        self.assertEqual(captured["size"], (480, 150))

    def test_mathpix_image_request_asks_for_latex_data(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "problem.png"
            Image.new("RGB", (120, 80), "white").save(image_path)
            provider = MathpixProvider(app_id="demo-id", app_key="demo-key")
            captured = {}

            def fake_request(request, timeout=120):
                captured["url"] = request.full_url
                captured["headers"] = dict(request.headers)
                captured["body"] = request.data
                return json.dumps({"text": "\\(x^2\\)", "confidence": 0.98}).encode()

            provider._request = fake_request
            result = provider.process_image(image_path)
            self.assertEqual(result["text"], "\\(x^2\\)")
            self.assertTrue(captured["url"].endswith("/v3/text"))
            self.assertIn(b'"include_latex": true', captured["body"])
            self.assertIn(b'"include_line_data": true', captured["body"])
            self.assertNotIn(b'"include_word_data"', captured["body"])


if __name__ == "__main__":
    unittest.main()
