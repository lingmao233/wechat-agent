$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "未找到 .venv\\Scripts\\python.exe，请先创建虚拟环境并安装依赖。"
}

& $python -m wechat_agent desktop --open-browser
