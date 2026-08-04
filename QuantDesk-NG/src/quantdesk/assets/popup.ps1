param(
    [Parameter(Mandatory=$true)][string]$Title,
    [Parameter(Mandatory=$true)][string]$Body,
    [int]$TimeoutSec = 10
)
$ws = New-Object -ComObject WScript.Shell
# 64=信息图标；超时自动关闭，不阻塞
$ws.Popup($Body, $TimeoutSec, $Title, 64) | Out-Null
Write-Output "POPUP_OK"
