# Generate a self-contained static Allure report from test results and open it.
#
#   scripts\allure-report.ps1              # generate ./allure-report and open it
#   scripts\allure-report.ps1 -NoOpen      # generate only
#   scripts\allure-report.ps1 -Serve       # skip static build, serve live instead
#
# Requires the Allure CLI on PATH (https://allurereport.org/docs/install/).
[CmdletBinding()]
param(
    [string] $ResultsDir = "allure-results",
    [string] $ReportDir = "allure-report",
    [switch] $NoOpen,
    [switch] $Serve
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    if (-not (Get-Command allure -ErrorAction SilentlyContinue)) {
        throw "The 'allure' CLI is not on PATH. Install it from https://allurereport.org/docs/install/"
    }
    if (-not (Test-Path $ResultsDir)) {
        throw "No results at '$ResultsDir'. Run the suite first: scripts\e2e-test.ps1 (writes --alluredir)."
    }

    if ($Serve) {
        allure serve $ResultsDir
        exit $LASTEXITCODE
    }

    # --clean overwrites any prior report; --single-file yields one portable HTML.
    allure generate $ResultsDir --clean -o $ReportDir
    if ($LASTEXITCODE -ne 0) { throw "allure generate failed" }
    Write-Host "Static report written to $ReportDir\index.html"

    if (-not $NoOpen) { allure open $ReportDir }
}
finally { Pop-Location }
