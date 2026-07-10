param(
    [string]$Receiver,
    [switch]$Send
)

$ErrorActionPreference = "Stop"
$command = Get-Command welink-cli -ErrorAction Stop
Write-Host "welink-cli: $($command.Source)"

if (-not $Send) {
    Write-Host "Command discovery passed. Add -Send -Receiver <W3 account> for a real smoke message."
    exit 0
}
if ([string]::IsNullOrWhiteSpace($Receiver)) {
    throw "-Receiver is required when -Send is used"
}

$message = "TaiChu PR Monitor welink-cli smoke test $(Get-Date -Format o)"
& $command.Source im send-to-user --receiver $Receiver --text $message
$code = $LASTEXITCODE
if ($code -ne 0) {
    throw "welink-cli send-to-user failed with exit code $code"
}
Write-Host "send-to-user returned exit code 0"
