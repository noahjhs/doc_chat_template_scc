#!/bin/bash
# Installer round trip on the Windows VM (which must be up: tools/winvm.py start, so the e2e
# lock is held): builds the test-environment installer for arm64, then on the guest
# installs it silently, starts Casper on the desktop, installs over the top (the running
# copy must be stopped and replaced), and uninstalls silently, checking the registry,
# Start menu entry, install folder and processes at each step. Needs the *_server.test.txt
# files in the repo root (as tools/test_stack.sh does).
#
#   tools/win_installtest.sh
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
W() { python3 tools/winvm.py "$@"; }
DEPLOY_ENV=test GOARCH=arm64 ./build/build_go_windows.sh | grep -E '^Built'
git checkout -q agent/internal/dialog/assets/ghost.png 2>/dev/null || true
W exec 'New-Item -ItemType Directory -Force C:\casper-test | Out-Null'
tools/win_put.sh dist/Casper-windows-arm64-setup.test.exe C:/casper-test/Casper-Setup.exe
W exec '
function Check($what, $ok) { if ($ok) { "ok    $what" } else { "FAIL  $what"; $script:bad = 1 } }
$d = "$env:LOCALAPPDATA\Programs\Casper Test"
$un = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\Casper-Test"
$lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Casper Test.lnk"
$bad = 0
$p = Start-Process C:\casper-test\Casper-Setup.exe -ArgumentList "/VERYSILENT","/NOLAUNCH" -Wait -PassThru
Check "silent install exits 0" ($p.ExitCode -eq 0)
Check "Casper.exe and syncthing.exe installed" ((Test-Path $d\Casper.exe) -and (Test-Path $d\syncthing.exe))
Check "Apps and features entry" ((Get-ItemProperty $un).InstallLocation -eq $d)
Check "casper-test:// handler" ((Get-ItemProperty HKCU:\Software\Classes\casper-test\shell\open\command)."(default)" -like "*Casper Test\Casper.exe*")
Check "Start menu entry" (Test-Path $lnk)
Check "version info" ((Get-Item $d\Casper.exe).VersionInfo.ProductName -eq "Casper Test")
$pid1 = (Start-Process $d\Casper.exe -PassThru).Id; Start-Sleep 6
Check "Casper runs" ($null -ne (Get-Process -Id $pid1 -EA 0))
$p = Start-Process C:\casper-test\Casper-Setup.exe -ArgumentList "/VERYSILENT","/NOLAUNCH" -Wait -PassThru
Check "install over a running Casper exits 0" ($p.ExitCode -eq 0)
Check "the old Casper was stopped" ($null -eq (Get-Process -Id $pid1 -EA 0))
Check "no syncthing left running from the install" ($null -eq (Get-Process syncthing -EA 0 | ? { $_.Path -like "$d*" }))
$p = Start-Process $d\Casper.exe -ArgumentList "uninstall","--silent" -Wait -PassThru
Start-Sleep 6
Check "uninstall exits 0" ($p.ExitCode -eq 0)
Check "install folder gone" (-not (Test-Path $d))
Check "Apps and features entry gone" (-not (Test-Path $un))
Check "handler gone" (-not (Test-Path HKCU:\Software\Classes\casper-test))
Check "Start menu entry gone" (-not (Test-Path $lnk))
Check "temporary uninstaller cleaned up" ((Get-ChildItem $env:TEMP -Filter casper-uninstall-* | Measure).Count -eq 0)
exit $bad'
