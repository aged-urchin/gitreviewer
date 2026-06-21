<#
.SYNOPSIS
    Submit code to GitReviewer. Claude Code explores the repo as an agent.
.EXAMPLE
    .\send-to-review.ps1 -des "review the last 3 commits"
    .\send-to-review.ps1 -des "check src/auth for permission issues"
    .\send-to-review.ps1 -des "review uncommitted changes" -local
    .\send-to-review.ps1 -des "find design flaws" -NoPoll
#>

param(
    [Alias("Description")]
    [string]$des = "",

    [string]$Server = "http://localhost:8000",

    [switch]$local,

    [int]$PollInterval = 5,

    [switch]$NoPoll,

    [switch]$EndSession
)

$ErrorActionPreference = "Stop"

function Write-Color($text, $color = "White") {
    Write-Host $text -ForegroundColor $color
}

function Invoke-API {
    param($Method, $Path, $Body, $Timeout = 30)
    $headers = @{ "Content-Type" = "application/json" }
    $params = @{
        Uri         = "$Server$Path"
        Method      = $Method
        Headers     = $headers
        TimeoutSec  = $Timeout
    }
    if ($Body) {
        $params["Body"] = ($Body | ConvertTo-Json -Depth 10 -Compress)
    }
    try {
        return Invoke-RestMethod @params
    } catch {
        Write-Color "ERROR: $($_.Exception.Message)" "Red"
        throw
    }
}

# -- 1. Check git --
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Color "ERROR: git not found" "Red"; exit 1
}
$gitRemote = (git remote get-url origin 2>$null)
if (-not $gitRemote) { Write-Color "ERROR: no origin remote" "Red"; exit 1 }
$gitBranch = (git branch --show-current 2>$null)
if (-not $gitBranch) { Write-Color "ERROR: not on a branch" "Red"; exit 1 }

# -- 2. Session --
$sessionFile = Join-Path (Get-Location) ".gitreviewer_session"
if (Test-Path $sessionFile) {
    $sessionId = (Get-Content $sessionFile -Raw).Trim()
    Write-Color "Session: $sessionId" "Gray"
    try {
        $session = Invoke-API -Method Get -Path "/api/v1/sessions/$sessionId" -Timeout 10
        if ($session.status -eq "closed") {
            Write-Color "Session closed, creating new..." "Yellow"
            Remove-Item $sessionFile -Force; $sessionId = $null
        }
    } catch {
        Write-Color "Session gone, creating new..." "Yellow"
        Remove-Item $sessionFile -Force; $sessionId = $null
    }
}
if (-not $sessionId) {
    Write-Color "Creating session for $gitRemote ($gitBranch)..." "Cyan"
    $s = Invoke-API -Method Post -Path "/api/v1/sessions" -Body (@{ git_url = $gitRemote; branch = $gitBranch }) -Timeout 120
    $sessionId = $s.session_id
    $sessionId | Out-File $sessionFile -NoNewline -Encoding utf8
    Write-Color "Session: $sessionId ($($s.status))" "Green"
}

# -- 3. Submit review --
Write-Color "Submitting review..." "Cyan"
$reviewBody = @{ description = $des }
if ($local) {
    $patch = (git diff HEAD 2>$null) -join "`n"
    if (-not $patch.Trim()) { Write-Color "No local changes" "Yellow"; exit 0 }
    $reviewBody["patch"] = $patch
    Write-Color "Including local diff ($($patch.Length) chars)" "Yellow"
}
$review = Invoke-API -Method Post -Path "/api/v1/sessions/$sessionId/reviews" -Body $reviewBody
$reviewId = $review.review_id
Write-Color "Review: $reviewId ($($review.status))" "Green"

# -- 4. Poll --
if ($NoPoll) {
    Write-Color "URL: $Server/api/v1/sessions/$sessionId/reviews/$reviewId" "Cyan"
    exit 0
}
Write-Color "Waiting..." "Cyan"
$waited = 0
do {
    Start-Sleep -Seconds $PollInterval; $waited += $PollInterval
    $result = Invoke-API -Method Get -Path "/api/v1/sessions/$sessionId/reviews/$reviewId" -Timeout 10
    if ($waited % 10 -eq 0) { Write-Host -NoNewline "." }
    if ($result.status -eq "completed" -or $result.status -eq "failed") { break }
    if ($waited -ge 300) {
        Write-Color "Timeout (${waited}s)" "Yellow"
        Write-Color "Check: GET $Server/api/v1/sessions/$sessionId/reviews/$reviewId" "Cyan"
        exit 0
    }
} while ($true)
Write-Host ""

# -- 5. Results --
if ($result.status -eq "failed") {
    Write-Color "FAILED: $($result.error)" "Red"; exit 1
}
Write-Color "========================================" "White"
Write-Color "  Review Complete" "White"
Write-Color "========================================" "White"
Write-Color "Summary: $($result.summary)" "Cyan"
Write-Color "Findings: $($result.findings.Count)" "Yellow"
Write-Color "========================================" "White"

if ($result.findings.Count -gt 0) {
    Write-Host ""
    foreach ($f in $result.findings) {
        $c = switch ($f.severity) { "high" { "Red" } "medium" { "Yellow" } default { "Gray" } }
        Write-Color "[$($f.severity.ToUpper())] $($f.file):$($f.line) - $($f.title)" $c
        Write-Color "  Category: $($f.category)  Problem: $($f.description)  Fix: $($f.suggestion)" "White"
        Write-Host ""
    }
    $hi = ($result.findings | Where-Object { $_.severity -eq "high" }).Count
    $mi = ($result.findings | Where-Object { $_.severity -eq "medium" }).Count
    $lo = ($result.findings | Where-Object { $_.severity -eq "low" }).Count
    Write-Color "Summary: $hi high, $mi medium, $lo low" "White"
}

# -- 6. End session --
if ($EndSession) {
    Write-Host ""
    Write-Color "Ending session..." "Cyan"
    Invoke-API -Method Delete -Path "/api/v1/sessions/$sessionId" | Out-Null
    Remove-Item $sessionFile -Force -ErrorAction SilentlyContinue
    Write-Color "Session ended" "Green"
}
