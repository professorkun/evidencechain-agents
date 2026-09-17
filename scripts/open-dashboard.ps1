param([int]$Port = 8765)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw '未找到项目虚拟环境，请先运行 uv sync。' }
$uri = "http://127.0.0.1:$Port"
Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.dashboard:app', '--host', '127.0.0.1', '--port', $Port) -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 1
Start-Process $uri
