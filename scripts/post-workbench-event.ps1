param(
    [Parameter(Mandatory = $true)] [string]$ExternalTaskId,
    [Parameter(Mandatory = $true)] [string]$Title,
    [Parameter(Mandatory = $true)] [string]$EventType,
    [string]$Status,
    [string]$Message = '',
    [string]$Output = '',
    [string]$EventId = ([guid]::NewGuid().ToString()),
    [int]$Port = 8765
)

$ErrorActionPreference = 'Stop'
$payload = @{
    external_task_id = $ExternalTaskId
    title = $Title
    event_id = $EventId
    event_type = $EventType
    status = $Status
    message = $Message
    output = $Output
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/api/events" -ContentType 'application/json; charset=utf-8' -Body $payload | ConvertTo-Json -Depth 8
