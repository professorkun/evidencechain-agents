$root = Split-Path -Parent $PSScriptRoot
$launcher = Join-Path $root 'scripts\\open-dashboard.ps1'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutName = 'Agent' + [char]0x5DE5 + [char]0x4F5C + [char]0x53F0 + '.lnk'
$shortcutPath = Join-Path $desktop $shortcutName
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'
$shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`""
$shortcut.WorkingDirectory = $root
$shortcut.Description = 'Open the local Multi-Agent Workbench dashboard.'
$shortcut.IconLocation = "$env:SystemRoot\\System32\\shell32.dll,44"
$shortcut.Save()
Write-Output "Created: $shortcutPath"
