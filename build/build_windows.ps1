# Builds a standalone Windows executable of casper_tool.py (Casper), with
# cloudflared bundled inside so users don't need to install it separately.
# Run from the repo root: .\build\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Remove-Item -Recurse -Force build\pyinstaller_work -ErrorAction SilentlyContinue
# Only remove this build's own prior outputs, and only the specific files
# within dist\Casper\ that we're about to regenerate — never the whole
# dist\ tree or that whole subdirectory, either of which can also hold
# runtime files (e.g. command_log.txt) from an already-running copy of the
# server launched from one of these paths.
Remove-Item -Force dist\Casper.exe, dist\Casper-windows.zip -ErrorAction SilentlyContinue
Remove-Item -Force dist\Casper\Casper.exe, dist\Casper\README.md -ErrorAction SilentlyContinue

$stageDir = New-Item -ItemType Directory -Force -Path "$env:TEMP\casper_tool_stage"
Copy-Item "vendor\cloudflared\cloudflared-windows-amd64.exe" "$stageDir\cloudflared.exe" -Force

# Optionally bake in the deployed web app's domain (just the host[:port],
# e.g. my-app.streamlit.app — no scheme, no path; casper_tool.py appends
# https:// and /chat), so the server can open it in a new browser tab on
# launch. Skipped (not fatal) if app_server.txt is
# missing or empty.
$addAppServerArgs = @()
if ((Test-Path app_server.txt) -and ((Get-Item app_server.txt).Length -gt 0)) {
    $appServerContent = (Get-Content app_server.txt -Raw).Trim()
    Set-Content -Path "$($stageDir.FullName)\baked_app_server.txt" -Value $appServerContent -NoNewline -Encoding ascii
    $addAppServerArgs = @("--add-data", "$($stageDir.FullName)\baked_app_server.txt;.")
} else {
    Write-Host "app_server.txt is empty/missing — this build won't auto-open the web app."
}

# Bake in the auth service's domain. Unlike app_server.txt, this one is NOT
# optional -- casper_tool.py can't sign in without it, so catch a missing
# value here at build time instead of at every downloader's first run.
if (-not ((Test-Path auth_server.txt) -and ((Get-Item auth_server.txt).Length -gt 0))) {
    Write-Error "auth_server.txt is missing/empty -- the built binary couldn't sign in. Aborting."
    exit 1
}
$authServerContent = (Get-Content auth_server.txt -Raw).Trim()
Set-Content -Path "$($stageDir.FullName)\baked_auth_server.txt" -Value $authServerContent -NoNewline -Encoding ascii

pyinstaller --onefile --name Casper `
    --add-binary "$($stageDir.FullName)\cloudflared.exe;." `
    --add-data "$($stageDir.FullName)\baked_auth_server.txt;." `
    @addAppServerArgs `
    --workpath build\pyinstaller_work `
    --specpath build `
    casper_tool.py

New-Item -ItemType Directory -Force -Path dist\Casper | Out-Null
Copy-Item dist\Casper.exe dist\Casper\
if (Test-Path README_casper.md) {
    Copy-Item README_casper.md dist\Casper\README.md
}
# Runtime state from a previous local test run under this same directory --
# never part of the build, and must never end up inside the distributable.
Remove-Item -Force dist\Casper\session.json, dist\Casper\command_log.txt -ErrorAction SilentlyContinue
Compress-Archive -Path dist\Casper -DestinationPath dist\Casper-windows.zip -Force

Write-Host "Built: dist\Casper-windows.zip"
