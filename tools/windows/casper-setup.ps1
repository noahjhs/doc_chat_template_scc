# First-logon setup of the Windows test VM (run by autounattend.xml from the
# small config ISO, which also holds ssh_key.pub and the virtio-win CD is
# attached as another drive). Leaves: virtio drivers (network, display),
# OpenSSH Server on port 22 with the key and password both accepted,
# PowerShell as the SSH shell, no sleep/lock/screen-off, and a marker file
# C:\casper-ready that tools/winvm.py waits for.
$ErrorActionPreference = 'Continue'
Start-Transcript -Path C:\casper-setup.log -Force | Out-Null

$cfg = Get-PSDrive -PSProvider FileSystem | Where-Object { Test-Path (Join-Path $_.Root 'casper-setup.ps1') } | Select-Object -First 1

# Drivers from the virtio-win CD: network first (the rest needs it).
$virtio = Get-PSDrive -PSProvider FileSystem | Where-Object { Test-Path (Join-Path $_.Root 'NetKVM\w11\ARM64') } | Select-Object -First 1
if ($virtio) {
  foreach ($d in 'NetKVM', 'viogpudo', 'vioinput', 'Balloon', 'vioserial') {
    $p = Join-Path $virtio.Root "$d\w11\ARM64"
    if (Test-Path $p) { pnputil /add-driver "$p\*.inf" /install | Out-Null }
  }
}
# (not ping: QEMU's user-mode network doesn't pass ICMP)
for ($i = 0; $i -lt 60; $i++) { if ((Test-NetConnection 1.1.1.1 -Port 443 -WarningAction SilentlyContinue).TcpTestSucceeded) { break }; Start-Sleep 2 }

# Never sleep, lock, or turn the screen off.
powercfg /change standby-timeout-ac 0; powercfg /change monitor-timeout-ac 0; powercfg /change hibernate-timeout-ac 0
powercfg /hibernate off
reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\Personalization' /v NoLockScreen /t REG_DWORD /d 1 /f | Out-Null
reg add 'HKCU\Control Panel\Desktop' /v ScreenSaveActive /t REG_SZ /d 0 /f | Out-Null
reg add 'HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon' /v AutoAdminLogon /t REG_SZ /d 1 /f | Out-Null

# OpenSSH Server.
for ($i = 0; $i -lt 5; $i++) {
  Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
  if (Get-Service sshd -ErrorAction SilentlyContinue) { break }
  Start-Sleep 20
}
Set-Service sshd -StartupType Automatic
Set-Service ssh-agent -StartupType Automatic -ErrorAction SilentlyContinue
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Value 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe' -PropertyType String -Force | Out-Null
$key = Get-Content (Join-Path $cfg.Root 'ssh_key.pub') -Raw
$auth = 'C:\ProgramData\ssh\administrators_authorized_keys'
New-Item -ItemType Directory -Force -Path C:\ProgramData\ssh | Out-Null
Set-Content -Path $auth -Value $key.Trim() -Encoding ascii
icacls $auth /inheritance:r /grant 'Administrators:F' /grant 'SYSTEM:F' | Out-Null
Start-Service sshd
# The VM's network starts out "Public", and the rule the capability adds covers only private/domain networks.
Get-NetConnectionProfile | Set-NetConnectionProfile -NetworkCategory Private
if (Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue) {
  Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Profile Any -Enabled True
} else {
  New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -DisplayName 'OpenSSH Server' -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22 -Profile Any | Out-Null
}

# No "Edge first run", no OneDrive nag, no Windows Update restarts during a test.
reg add 'HKLM\SOFTWARE\Policies\Microsoft\Edge' /v HideFirstRunExperience /t REG_DWORD /d 1 /f | Out-Null
reg add 'HKLM\SOFTWARE\Policies\Microsoft\Windows\WindowsUpdate\AU' /v NoAutoRebootWithLoggedOnUsers /t REG_DWORD /d 1 /f | Out-Null

Set-Content C:\casper-ready (Get-Date -Format o)
Stop-Transcript | Out-Null
