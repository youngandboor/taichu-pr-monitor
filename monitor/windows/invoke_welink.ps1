param(
    [Parameter(Mandatory = $true)][string]$CliPath,
    [Parameter(Mandatory = $true)][string]$Receiver,
    [Parameter(Mandatory = $true)][string]$TextBase64
)

$ErrorActionPreference = "Stop"
$text = [System.Text.Encoding]::UTF8.GetString(
    [System.Convert]::FromBase64String($TextBase64)
)

& $CliPath im send-to-user --receiver $Receiver --text $text
exit $LASTEXITCODE
