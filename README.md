# MathBank Studio

PDF 속 수학 문제를 문제별로 분리하고, 원본과 LaTeX 본문을 함께 검수한 뒤 문제은행과 시험지로 사용하는 로컬 웹 앱입니다.

## 현재 구현된 기능

- PDF 업로드와 비동기 페이지 처리
- 문제 번호·문단·페이지 연결을 이용한 문제 단위 분리
- 문제별 원본 이미지와 도형 후보 자동 잘라내기
- 번호 미확정·스캔 PDF·낮은 신뢰도 경고
- 원본과 추출 본문을 나란히 보는 검수 화면
- KaTeX 수식 미리보기와 수정 이력 저장
- 승인된 문제만 노출되는 문제은행
- 단원·개념·문제 구조를 이용한 유사문제 검색
- 시험지 원본 인쇄, 편집본 인쇄, 해설지 출력
- SQLite 로컬 저장
- 앱 화면에서 Mathpix App ID·App Key 등록 및 연결 확인
- 문제 한 건을 Mathpix 이미지 OCR로 다시 인식
- PDF 페이지에서 문제 영역을 직접 드래그하여 분리
- 같은 문제 번호로 여러 페이지 영역을 묶는 이어진 문제 처리

## 실행

PowerShell에서 다음 파일을 실행합니다.

```powershell
.\start.ps1
```

그다음 브라우저에서 `http://127.0.0.1:8765`를 엽니다.

## Mathpix 연결

앱 왼쪽 메뉴의 `OCR 설정`에서 App ID와 App Key를 입력할 수 있습니다. 저장할 때 Mathpix 사용량 API로 연결을 확인하며, 키는 로컬 SQLite 데이터베이스에만 저장되고 화면에 다시 표시되지 않습니다.

환경변수를 사용하는 기존 방식도 계속 지원합니다.

```powershell
$env:MATHPIX_APP_ID='발급받은_APP_ID'
$env:MATHPIX_APP_KEY='발급받은_APP_KEY'
.\start.ps1
```

키가 없을 때도 일반 PDF의 텍스트·문제 번호·원본 영역 추출과 모든 문제은행 기능은 동작합니다. 스캔 PDF는 인식이 된 것처럼 넘기지 않고 검수 경고로 남깁니다.

## 정확도가 중요한 PDF

`PDF 가져오기`에서 **직접 문제 영역 지정**을 선택하는 것을 권장합니다.

1. PDF 페이지가 준비되면 문제 하나의 지문·보기·도형을 사각형으로 감쌉니다.
2. 다음 페이지로 이어지는 영역은 같은 문제 번호를 입력합니다.
3. `이 영역으로 Mathpix OCR 시작`을 누르면 각 영역을 독립된 이미지로 인식합니다.

이 방식은 2단 편집에서 서로 다른 문제가 섞이는 현상을 막고, 문제별 LaTeX 대응을 안정적으로 유지합니다.

## 데이터 위치

업로드 PDF, 문제 이미지와 데이터베이스는 프로젝트의 `data` 폴더에 저장됩니다. 외부 OCR 키를 사용하지 않으면 파일이 외부로 전송되지 않습니다.

## 검사 실행

```powershell
$python = Get-ChildItem "$HOME\.cache\codex-runtimes" -Recurse -Filter python.exe |
  Where-Object { $_.FullName -like '*dependencies\python\python.exe' } |
  Select-Object -First 1 -ExpandProperty FullName
& $python -m unittest discover -s tests -v
```
