param([int]$Port = 8765)

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Project virtual environment was not found. Run uv sync first.' }
$uri = "http://127.0.0.1:$Port"
Start-Process -FilePath $python -ArgumentList @('-m', 'uvicorn', 'app.dashboard:app', '--host', '127.0.0.1', '--port', $Port) -WorkingDirectory $root -WindowStyle Hidden
Start-Sleep -Seconds 1
Start-Process $uri
