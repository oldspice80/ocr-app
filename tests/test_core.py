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

from mathbank.db import Database
from mathbank.extractor import extract_pdf, similar_score
from mathbank.manual import detect_initial_regions, extract_manual_regions, prepare_manual_pdf
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
            detected = detect_initial_regions(pdf_path, pages)
            self.assertEqual([item["number"] for item in detected], ["1", "2"])
            self.assertLess(detected[0]["x"], 0.1)
            self.assertGreater(detected[1]["x"], 0.45)
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
