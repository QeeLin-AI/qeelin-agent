# Windows Server 一键部署（PowerShell，管理员运行）
#
#   powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\install-windows.ps1 -Port 8000 -DryRun
#
# 做的事：检查 Python → 建 .venv 装依赖 → 生成访问令牌写进 .env
#         → 注册「开机自启」计划任务 → 打印访问方式
# 不装任何第三方服务（NSSM 之类），只用系统自带 schtasks。
#
# 参数：
#   -Port 8000            监听端口
#   -BindHost 127.0.0.1   监听地址（默认只本机；要公网访问改成 0.0.0.0，见文末提示）
#   -InstallPython        没找到 Python 时用 winget 自动装 3.12（64 位）
#   -DryRun               只打印命令，不真的执行

[CmdletBinding()]
param(
    [int]$Port = 8000,
    [string]$BindHost = "127.0.0.1",
    [switch]$InstallPython,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$TaskName = "crypto-agent"

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Run($exe, $cmdArgs) {
    $line = "$exe $cmdArgs"
    if ($DryRun) { Write-Host "  [dry] $line" -ForegroundColor DarkGray; return }
    Invoke-Expression $line
}

Say "1/6 检查权限"
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    if ($DryRun) { Write-Host "  [dry] 当前不是管理员，正式运行会在这里退出" -ForegroundColor DarkYellow }
    else { throw "请用管理员身份打开 PowerShell 再跑这个脚本（右键 → 以管理员身份运行）" }
} else { Write-Host "  OK（管理员）" }

Say "2/6 找 Python 3.10+"
$py = $null
foreach ($cand in @("python", "py")) {
    try {
        $ver = & $cand -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.10") { $py = $cand; break }
    } catch { }
}
if (-not $py) {
    if ($InstallPython) {
        Say "2b/6 没找到 Python，尝试用 winget 装 3.12（64 位）"
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Run "winget" "install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements"
            if (-not $DryRun) {
                # winget 装完当前会话 PATH 不会刷新，直接把常见安装路径塞进来再找一次
                foreach ($guess in @(
                    "$env:ProgramFiles\Python312\python.exe",
                    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe"
                )) {
                    if (Test-Path $guess) { $py = $guess; break }
                }
                if (-not $py) { $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") }
            }
        } else {
            Write-Host "  这台机器没有 winget，只能手动装。" -ForegroundColor Yellow
        }
    }
}
if (-not $py) {
    foreach ($cand in @("python", "py")) {
        try {
            $ver = & $cand -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($LASTEXITCODE -eq 0 -and [version]$ver -ge [version]"3.10") { $py = $cand; break }
        } catch { }
    }
}
if (-not $py) {
    Write-Host "  没找到 Python 3.10+。推荐装 3.12（64 位）：" -ForegroundColor Yellow
    Write-Host "    a) 一条命令：winget install -e --id Python.Python.3.12"
    Write-Host "    b) 或者重跑本脚本并加 -InstallPython，让它自己装"
    Write-Host "    c) 手动下载 https://www.python.org/downloads/release/python-3129/"
    Write-Host "       安装时勾：Add python.exe to PATH + Install for all users"
    Write-Host "  装完重开 PowerShell 再跑本脚本。"
    exit 1
}
Write-Host "  用 $py"

Say "3/6 建虚拟环境并装依赖"
if (-not (Test-Path "$Root\.venv")) { Run $py "-m venv `"$Root\.venv`"" }
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
Run $venvPy "-m pip install --upgrade pip --quiet"
Run $venvPy "-m pip install -r `"$Root\requirements.txt`" --quiet"
Write-Host "  依赖装好（numpy / pandas）"

Say "4/6 生成访问令牌"
$envFile = Join-Path $Root ".env"
if (-not (Test-Path $envFile)) {
    $bytes = New-Object byte[] 24
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $token = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
    $lines = @(
        "# crypto-agent 运行环境（本文件不要提交到 git）",
        "CRYPTOAGENT_WEB_TOKEN=$token",
        "# --- 接真模型 ---",
        "# CRYPTOAGENT_LLM_PROVIDER=openai",
        "# CRYPTOAGENT_LLM_BASE_URL=https://api.deepseek.com/v1",
        "# CRYPTOAGENT_LLM_MODEL=deepseek-chat",
        "# OPENAI_API_KEY=sk-xxx",
        "# --- 币安（默认不填 = 本地模拟）---",
        "# BINANCE_API_KEY=",
        "# BINANCE_API_SECRET="
    )
    if (-not $DryRun) { Set-Content -Path $envFile -Value $lines -Encoding utf8 }
    Write-Host "  令牌：$token" -ForegroundColor Green
    if ($DryRun) { Write-Host "  [dry] 未写入 $envFile" -ForegroundColor DarkGray }
    else { Write-Host "  已写入 $envFile（忘了可以打开看）" }
} else {
    $token = (Select-String -Path $envFile -Pattern "^CRYPTOAGENT_WEB_TOKEN=(.+)" -AllMatches).Matches.Groups[1].Value
    if ([string]::IsNullOrWhiteSpace($token)) {
        # 老 .env 可能只有注释行或根本没写令牌：空令牌会让绑定 0.0.0.0 的启动直接失败
        $bytes = New-Object byte[] 24
        [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
        $token = ($bytes | ForEach-Object { $_.ToString("x2") }) -join ""
        Add-Content -Path $envFile -Value "CRYPTOAGENT_WEB_TOKEN=$token" -Encoding utf8
        Write-Host "  .env 缺少令牌，已生成并追加：$token"
    } else {
        Write-Host "  .env 已存在，沿用令牌：$token"
    }
}

Say "5/6 注册开机自启计划任务"
# 生成一个自包含的启动脚本，任务只跑它——绕开命令行的嵌套引号问题
$runner = Join-Path $Root "deploy\service-run.cmd"
$runnerBody = @"
@echo off
chcp 65001 >nul
cd /d "$Root"
set PYTHONPATH=$Root\src
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
"$venvPy" -m cryptoagent web --host $BindHost --port $Port --no-browser
"@
if ($DryRun) {
    Write-Host "  [dry] 会生成 $runner：" -ForegroundColor DarkGray
    ($runnerBody -split "`n") | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
} else {
    Set-Content -Path $runner -Value $runnerBody -Encoding utf8
    Write-Host "  启动脚本：$runner"
}

if (Get-Command Register-ScheduledTask -ErrorAction SilentlyContinue) {
    if (-not $DryRun) {
        $act = New-ScheduledTaskAction -Execute $runner
        $trg = New-ScheduledTaskTrigger -AtStartup
        $set = New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
        $prn = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
        Register-ScheduledTask -TaskName $TaskName -Action $act -Trigger $trg -Settings $set -Principal $prn -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
    } else {
        Write-Host "  [dry] Register-ScheduledTask $TaskName -> $runner (AtStartup, SYSTEM)" -ForegroundColor DarkGray
    }
    Write-Host "  计划任务 [$TaskName] 已注册（开机自启，崩溃重启 3 次）"
} else {
    Write-Host "  没有 Register-ScheduledTask，退回 schtasks" -ForegroundColor Yellow
    Run "schtasks" "/Create /TN $TaskName /TR `"$runner`" /SC ONSTART /RU SYSTEM /RL HIGHEST /F"
    if (-not $DryRun) { schtasks /Run /TN $TaskName | Out-Null }
}

Say "6/6 结果"
Start-Sleep -Seconds 4
try {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/api/status?token=$token" -UseBasicParsing -TimeoutSec 10
    $j = $r.Content | ConvertFrom-Json
    Write-Host "  服务在线：symbol=$($j.data.symbol)  price=$([math]::Round($j.data.price,2))" -ForegroundColor Green
} catch {
    Write-Host "  还没起来，看日志：" -ForegroundColor Yellow
    Write-Host "    schtasks /Query /TN $TaskName /V /FO LIST"
    Write-Host "    或手动跑一次看报错：cd `"$Root`"; .\.venv\Scripts\python.exe -m cryptoagent web --port $Port"
}

Write-Host @"

访问方式（按推荐顺序）：

  1) 远程桌面进来，在服务器上开浏览器：
       http://127.0.0.1:$Port/?token=$token

  2) 只给自己家/公司 IP 开 8000 端口（防火墙 + 令牌）：
       New-NetFirewallRule -DisplayName "crypto-agent" -Direction Inbound -Protocol TCP ``
         -LocalPort $Port -RemoteAddress <你的公网IP> -Action Allow
     然后 PowerShell 里把 -BindHost 改成 0.0.0.0 重跑本脚本（或改计划任务）。

  3) 装 OpenSSH Server 走隧道（最稳）：
       Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
       在你本机：ssh -N -L 8000:127.0.0.1:$Port 用户名@服务器IP

常用命令（PowerShell，管理员）：
  Start-ScheduledTask      -TaskName $TaskName     启动
  Stop-ScheduledTask       -TaskName $TaskName     停止
  Get-ScheduledTask        -TaskName $TaskName     看状态
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false   取消自启

改完 .env（比如填了 LLM Key）后重启：
  Stop-ScheduledTask -TaskName $TaskName; Start-ScheduledTask -TaskName $TaskName

"@ -ForegroundColor Gray
