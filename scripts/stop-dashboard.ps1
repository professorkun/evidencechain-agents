param([int]$Port = 8765)

$connection = Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($connection) { Stop-Process -Id $connection.OwningProcess -Confirm:$false }
$bridges = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -like '*multi-agent-workbench*app.codex_bridge*' }
foreach ($bridge in $bridges) { Stop-Process -Id $bridge.ProcessId -Confirm:$false }
