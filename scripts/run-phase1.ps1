param(
    [Parameter(Mandatory = $true)]
    [string]$Task,
    [switch]$ApproveReadonly
)

$ErrorActionPreference = 'Stop'
$env:PYTHONDONTWRITEBYTECODE = '1'

if ($ApproveReadonly) {
    uv run python -m app.main $Task --approve-readonly
} else {
    uv run python -m app.main $Task
}
