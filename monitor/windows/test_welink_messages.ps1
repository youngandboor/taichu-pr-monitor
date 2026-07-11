param(
    [ValidateSet("single-line", "merge-success", "url-last", "url-followed-by-text", "long-single-line", "multi-line", "all")]
    [string]$TestCase = "url-last",
    [string]$Receiver,
    [switch]$Send
)

$ErrorActionPreference = "Stop"
$python = Get-Command py -ErrorAction Stop
$arguments = @("-3", "-m", "monitor.welink_probe", "--case", $TestCase)

if ($Send) {
    if ([string]::IsNullOrWhiteSpace($Receiver)) {
        throw "-Receiver is required when -Send is used"
    }
    $arguments += @("--send", "--receiver", $Receiver)
}

& $python.Source @arguments
exit $LASTEXITCODE
