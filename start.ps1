$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue

if (-not $Python) {
    $RuntimePython = Get-ChildItem "$HOME\.cache\codex-runtimes" -Recurse -Filter python.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -like '*dependencies\python\python.exe' } |
        Select-Object -First 1 -ExpandProperty FullName
    $Python = $RuntimePython
}

if (-not $Python) {
    throw 'Python 실행 환경을 찾지 못했습니다. Python 3.11 이상을 설치해 주세요.'
}

Set-Location $ProjectRoot
Write-Host ''
Write-Host 'MathBank Studio 시작' -ForegroundColor Green
Write-Host '브라우저 주소: http://127.0.0.1:8765' -ForegroundColor Cyan
Write-Host '종료하려면 이 창에서 Ctrl+C를 누르세요.' -ForegroundColor DarkGray
Write-Host ''
& $Python "$ProjectRoot\app.py"

