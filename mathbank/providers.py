from __future__ import annotations

import json
import mimetypes
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageDraw

from .extractor import QUESTION_START, fingerprint, infer_metadata


class ProviderError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class MathpixProvider:
    base_url = "https://api.mathpix.com/v3"

    def __init__(self, app_id: str | None = None, app_key: str | None = None):
        self.app_id = app_id or os.getenv("MATHPIX_APP_ID", "")
        self.app_key = app_key or os.getenv("MATHPIX_APP_KEY", "")

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_key)

    def _headers(self) -> dict[str, str]:
        return {"app_id": self.app_id, "app_key": self.app_key}

    @staticmethod
    def _curl_config_value(value: str) -> str:
        """curl 설정 파일의 따옴표·역슬래시·개행을 안전하게 이스케이프한다."""
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "").replace("\n", "")

    def _request_with_curl(self, request: urllib.request.Request, timeout: int) -> bytes:
        """urllib 소켓이 Windows에서 차단될 때 OS curl 전송으로 재시도한다.

        API 키는 명령줄 인수에 넣지 않고 임시 curl 설정 파일에만 기록하며,
        요청 완료 즉시 설정 파일과 본문을 삭제한다.
        """
        system_curl = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "curl.exe"
        curl = str(system_curl) if system_curl.exists() else shutil.which("curl.exe")
        if not curl:
            raise ProviderError("Mathpix 대체 연결에 필요한 Windows curl.exe를 찾지 못했습니다.")

        with tempfile.TemporaryDirectory(prefix="mathbank-curl-") as directory:
            work = Path(directory)
            response_path = work / "response.bin"
            body_path = work / "request.bin"
            config_path = work / "request.conf"
            data = request.data
            if data is not None:
                body_path.write_bytes(data)

            lines = [
                f'url = "{self._curl_config_value(request.full_url)}"',
                f'request = "{self._curl_config_value(request.get_method())}"',
                f'output = "{response_path.as_posix()}"',
                'write-out = "%{http_code}"',
                "silent",
                "show-error",
                "location",
                "connect-timeout = 20",
                f"max-time = {max(30, int(timeout))}",
            ]
            for name, value in request.header_items():
                header = self._curl_config_value(f"{name}: {value}")
                lines.append(f'header = "{header}"')
            if data is not None:
                lines.append(f'data-binary = "@{body_path.as_posix()}"')
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            completed = subprocess.run(
                [curl, "--config", str(config_path)],
                capture_output=True,
                text=True,
                timeout=max(35, timeout + 5),
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if completed.returncode != 0:
                detail = (completed.stderr or completed.stdout or "알 수 없는 curl 오류").strip()
                raise ProviderError(f"Mathpix 대체 연결 실패: {detail[:500]}")
            status_text = completed.stdout.strip()[-3:]
            status = int(status_text) if status_text.isdigit() else 0
            response = response_path.read_bytes() if response_path.exists() else b""
            if status >= 400:
                detail = response.decode("utf-8", errors="replace")
                raise ProviderError(f"Mathpix 요청 실패({status}): {detail[:300]}", status)
            if status == 0:
                raise ProviderError("Mathpix 대체 연결이 HTTP 상태 코드를 반환하지 않았습니다.")
            return response

    def _request(self, request: urllib.request.Request, timeout: int = 120) -> bytes:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"Mathpix 요청 실패({exc.code}): {detail[:300]}", exc.code) from exc
        except urllib.error.URLError as exc:
            # WinError 10013 등 Python 소켓 접근 거부 시 Windows의 별도
            # Schannel/curl 전송 경로로 같은 HTTPS 요청을 자동 재시도한다.
            try:
                return self._request_with_curl(request, timeout)
            except ProviderError as fallback_error:
                raise ProviderError(
                    f"Mathpix 연결 실패: {exc.reason} / {fallback_error}"
                ) from fallback_error

    def test_connection(self) -> dict[str, Any]:
        if not self.configured:
            raise ProviderError("App ID와 App Key를 모두 입력해 주세요.")
        # 실제 문제 OCR과 동일한 multipart /text 경로로만 연결을 검증한다.
        # 사용량 조회 API는 계정별 권한 차이로 정상 키도 거절할 수 있어 사용하지 않는다.
        with tempfile.TemporaryDirectory(prefix="mathbank-mathpix-") as directory:
            image_path = Path(directory) / "connection-test.png"
            image = Image.new("RGB", (480, 150), "white")
            draw = ImageDraw.Draw(image)
            draw.text((24, 54), "x^2 + 2x + 1 = 0", fill="black")
            image.save(image_path, "PNG")
            result = self.process_image(image_path)
        return {
            "ok": True,
            "method": "image_ocr",
            "recognized": bool(str(result.get("text") or result.get("latex_styled") or "").strip()),
        }

    def process_image(self, image_path: Path) -> dict[str, Any]:
        """문제 하나의 이미지에서 Mathpix Markdown/LaTeX를 직접 얻는다."""
        if not self.configured:
            raise ProviderError("Mathpix API 키가 설정되지 않았습니다.")
        boundary = f"----MathBankImage{uuid.uuid4().hex}"
        file_bytes = image_path.read_bytes()
        mime = mimetypes.guess_type(image_path.name)[0] or "image/png"
        options = json.dumps(
            {
                "formats": ["text", "data"],
                "data_options": {"include_latex": True, "include_asciimath": False},
                "include_line_data": True,
                "math_inline_delimiters": ["\\(", "\\)"],
                "math_display_delimiters": ["\\[", "\\]"],
                "rm_spaces": False,
            }
        )
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"options_json\"\r\n\r\n{options}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{image_path.name}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n"
            ).encode(),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = urllib.request.Request(
            f"{self.base_url}/text",
            data=b"".join(chunks),
            headers={**self._headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        result = json.loads(self._request(request).decode("utf-8"))
        if result.get("error") or result.get("error_info"):
            raise ProviderError(f"Mathpix 이미지 인식 실패: {result.get('error_info') or result.get('error')}")
        return result

    def process_pdf(
        self,
        pdf_path: Path,
        progress: Callable[[int, str], None] | None = None,
        timeout_seconds: int = 600,
    ) -> str:
        if not self.configured:
            raise ProviderError("Mathpix API 키가 설정되지 않았습니다.")
        boundary = f"----MathBank{uuid.uuid4().hex}"
        file_bytes = pdf_path.read_bytes()
        filename = pdf_path.name
        mime = mimetypes.guess_type(filename)[0] or "application/pdf"
        options = json.dumps(
            {
                "conversion_formats": {"mmd": True},
                "math_inline_delimiters": ["\\(", "\\)"],
                "math_display_delimiters": ["\\[", "\\]"],
                "rm_spaces": False,
            }
        )
        chunks = [
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"options_json\"\r\n\r\n{options}\r\n".encode(),
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: {mime}\r\n\r\n"
            ).encode(),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode(),
        ]
        request = urllib.request.Request(
            f"{self.base_url}/pdf",
            data=b"".join(chunks),
            headers={**self._headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        submitted = json.loads(self._request(request).decode("utf-8"))
        pdf_id = submitted.get("pdf_id")
        if not pdf_id:
            raise ProviderError("Mathpix가 작업 ID를 반환하지 않았습니다.")
        deadline = time.monotonic() + timeout_seconds
        try:
            while time.monotonic() < deadline:
                status_request = urllib.request.Request(
                    f"{self.base_url}/pdf/{pdf_id}", headers=self._headers(), method="GET"
                )
                status = json.loads(self._request(status_request).decode("utf-8"))
                state = status.get("status")
                percent = int(float(status.get("percent_done", 0) or 0))
                if progress:
                    progress(min(88, 35 + percent // 2), f"Mathpix 수식 OCR {percent}%")
                if state == "completed":
                    break
                if state == "error":
                    raise ProviderError(f"Mathpix 처리 오류: {status}")
                time.sleep(2)
            else:
                raise ProviderError("Mathpix 처리 시간이 제한을 초과했습니다.")
            result_request = urllib.request.Request(
                f"{self.base_url}/pdf/{pdf_id}.mmd", headers=self._headers(), method="GET"
            )
            return self._request(result_request).decode("utf-8", errors="replace")
        finally:
            try:
                delete_request = urllib.request.Request(
                    f"{self.base_url}/pdf/{pdf_id}", headers=self._headers(), method="DELETE"
                )
                self._request(delete_request, timeout=30)
            except Exception:
                pass


def split_mathpix_markdown(markdown: str) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []
    current: list[str] = []
    number: str | None = None
    for line in markdown.splitlines():
        cleaned = re.sub(r"^[#>*\-\s]+", "", line).strip()
        match = QUESTION_START.match(cleaned)
        if match:
            if current:
                text = "\n".join(current).strip()
                chunks.append({"number": number, "latex": text, "content": text})
            number = match.group("num") or match.group("circled")
            current = [line]
        elif current:
            current.append(line)
    if current:
        text = "\n".join(current).strip()
        chunks.append({"number": number, "latex": text, "content": text})
    return chunks


def merge_mathpix(problems: list[dict[str, Any]], markdown: str) -> tuple[list[dict[str, Any]], str]:
    chunks = split_mathpix_markdown(markdown)
    if not chunks:
        return problems, "Mathpix 결과에서 문제 경계를 찾지 못해 원본 구조만 사용했습니다."
    by_number = {str(item["number"]): item for item in chunks if item.get("number")}
    matched = 0
    for index, problem in enumerate(problems):
        chunk = by_number.get(str(problem.get("number")))
        if chunk is None and len(chunks) == len(problems):
            chunk = chunks[index]
        if chunk:
            problem["latex"] = chunk["latex"]
            problem["content"] = re.sub(r"\\[\(\)\[\]]|\$+", "", chunk["content"])
            problem["fingerprint"] = fingerprint(problem["content"])
            metadata = infer_metadata(problem["content"])
            problem.update(metadata)
            problem["confidence"] = max(problem.get("confidence", 0), 0.88)
            matched += 1
    note = f"Mathpix 수식 결과를 {matched}/{len(problems)}개 문제에 연결했습니다."
    return problems, note


def provider_status(app_id: str = "", app_key: str = "") -> dict[str, Any]:
    mathpix = MathpixProvider(app_id=app_id, app_key=app_key)
    return {
        "mathpix": {
            "configured": mathpix.configured,
            "label": "Mathpix 수학 OCR",
            "purpose": "수식·본문을 LaTeX로 변환",
        },
        "azure": {
            "configured": bool(os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT") and os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")),
            "label": "Azure 문서 구조 분석",
            "purpose": "추후 이중 검증용 보조 분석기",
        },
        "local": {
            "configured": True,
            "label": "로컬 PDF 구조 분석",
            "purpose": "페이지·문제 번호·원본 영역을 로컬에서 추출",
        },
    }
