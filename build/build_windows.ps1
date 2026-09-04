# Builds a standalone Windows executable of control_api.py, with cloudflared
# bundled inside so users don't need to install it separately.
# Run from the repo root: .\build\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

Remove-Item -Recurse -Force build\pyinstaller_work -ErrorAction SilentlyContinue
# Only remove this build's own prior outputs, and only the specific files
# within dist\DocChatControlAPI\ that we're about to regenerate — never the
# whole dist\ tree or that whole subdirectory, either of which can also hold
# runtime files (e.g. command_log.txt) from an already-running copy of the
# server launched from one of these paths.
Remove-Item -Force dist\control_api.exe, dist\DocChatControlAPI-windows.zip -ErrorAction SilentlyContinue
Remove-Item -Force dist\DocChatControlAPI\control_api.exe, dist\DocChatControlAPI\README.md -ErrorAction SilentlyContinue

$stageDir = New-Item -ItemType Directory -Force -Path "$env:TEMP\control_api_stage"
Copy-Item "vendor\cloudflared\cloudflared-windows-amd64.exe" "$stageDir\cloudflared.exe" -Force

# Bake in the same LOCAL_AGENT_API_KEY the web app reads from its own
# secrets.toml, so a freshly downloaded build already matches it.
python -c @"
import tomllib
with open('.streamlit/secrets.toml', 'rb') as f:
    key = tomllib.load(f)['LOCAL_AGENT_API_KEY']
print(key, end='')
"@ | Out-File -FilePath "$($stageDir.FullName)\baked_api_key.txt" -Encoding ascii -NoNewline

# Optionally bake in the deployed web app's URL, so the server can open it
# in a new browser tab on launch. Skipped (not fatal) if app_url.txt is
# missing or empty.
$addAppUrlArgs = @()
if ((Test-Path app_url.txt) -and ((Get-Item app_url.txt).Length -gt 0)) {
    $appUrlContent = (Get-Content app_url.txt -Raw).Trim()
    Set-Content -Path "$($stageDir.FullName)\baked_app_url.txt" -Value $appUrlContent -NoNewline -Encoding ascii
    $addAppUrlArgs = @("--add-data", "$($stageDir.FullName)\baked_app_url.txt;.")
} else {
    Write-Host "app_url.txt is empty/missing — this build won't auto-open the web app."
}

pyinstaller --onefile --name control_api `
    --add-binary "$($stageDir.FullName)\cloudflared.exe;." `
    --add-data "$($stageDir.FullName)\baked_api_key.txt;." `
    @addAppUrlArgs `
    --workpath build\pyinstaller_work `
    --specpath build `
    control_api.py

New-Item -ItemType Directory -Force -Path dist\DocChatControlAPI | Out-Null
Copy-Item dist\control_api.exe dist\DocChatControlAPI\
if (Test-Path README_control_api.md) {
    Copy-Item README_control_api.md dist\DocChatControlAPI\README.md
}
Compress-Archive -Path dist\DocChatControlAPI -DestinationPath dist\DocChatControlAPI-windows.zip -Force

Write-Host "Built: dist\DocChatControlAPI-windows.zip"
