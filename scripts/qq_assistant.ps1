<#
  以湘 · QQ 助手「一键启动」
  ---------------------------------------------------------------
  平时不用手敲命令，双击项目根目录里的 bat 就行：
    启动以湘.bat  ->  start    启动网关 + 挂上 NapCat（已在运行就复用）
    重启以湘.bat  ->  restart  先停网关再起（改完 .env 用这个）
    停止以湘.bat  ->  stop     只停网关，QQ 不动
    状态以湘.bat  ->  status   只体检，不改动任何东西

  设计要点（都是踩过的坑）：
    * 永远只起一个网关：先看 8766 端口有没有人监听，有就复用（重复启动 = WinError 10048）
    * 顺手清理「抢不到端口的僵尸网关」（它会每分钟往审计日志里刷 listen_failed）
    * 只认项目 .venv 里的解释器（系统 Python 没装依赖，跑一半会崩）
    * 拉起 NapCat 用官方 launcher-win10.bat，会弹一次 UAC
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'restart', 'stop', 'status')]
    [string]$Action = 'start'
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ 常量
$Port        = 8766
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python      = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$NapCatDir   = 'F:\AI工具\NapCat.Shell'
$NapCatBat   = Join-Path $NapCatDir 'launcher-win10.bat'
$AuditLog    = Join-Path $ProjectRoot 'data\logs\qq-audit.jsonl'
$EnvFile     = Join-Path $ProjectRoot '.env'

# ------------------------------------------------------------------ 输出
function Write-Head([string]$Text) { Write-Host ''; Write-Host "-- $Text" -ForegroundColor Cyan }
function Write-Ok([string]$Text)   { Write-Host "   [OK] $Text" -ForegroundColor Green }
function Write-Bad([string]$Text)  { Write-Host "   [!!] $Text" -ForegroundColor Red }
function Write-Note([string]$Text) { Write-Host "   [..] $Text" -ForegroundColor Yellow }
function Write-Tip([string]$Text)  { Write-Host "        $Text" -ForegroundColor DarkGray }

# ------------------------------------------------------------------ 探测
# 返回占用 $Port 的进程号：Listening = 网关本体，Established = 连上来的那一侧（QQ）
function Get-TcpSnapshot([int]$Port) {
    $snapshot = @{ Listening = @(); Established = @() }
    $peers = @()    # 网关这一侧的远端地址（谁连上来了）
    $clients = @()  # 客户端这一侧（本地地址 + 进程号）
    foreach ($line in (& netstat.exe -ano)) {
        $text = "$line".Trim()
        if (-not $text.StartsWith('TCP')) { continue }
        $cells = $text -split '\s+'
        if ($cells.Count -lt 4) { continue }
        $localPort = 0
        [void][int]::TryParse(($cells[1] -replace '^.*:', ''), [ref]$localPort)
        $remotePort = 0
        [void][int]::TryParse(($cells[2] -replace '^.*:', ''), [ref]$remotePort)
        $owner = 0
        if ($cells.Count -ge 5) { [void][int]::TryParse($cells[4], [ref]$owner) }
        if ($cells[3] -eq 'LISTENING' -and $localPort -eq $Port) {
            $snapshot.Listening += $owner
        } elseif ($cells[3] -eq 'ESTABLISHED' -and $localPort -eq $Port) {
            $peers += $cells[2].ToLower()
        } elseif ($cells[3] -eq 'ESTABLISHED' -and $remotePort -eq $Port) {
            $clients += @{ Local = $cells[1].ToLower(); Owner = $owner }
        }
    }
    foreach ($client in $clients) {
        if ($peers -contains $client.Local) { $snapshot.Established += $client.Owner }
    }
    if ($snapshot.Established.Count -eq 0 -and $clients.Count -gt 0) {
        foreach ($client in $clients) { $snapshot.Established += $client.Owner }
    }
    $snapshot.Listening   = @($snapshot.Listening   | Sort-Object -Unique)
    $snapshot.Established = @($snapshot.Established | Sort-Object -Unique)
    return $snapshot
}

function Get-ProcName([int]$Owner) {
    if ($Owner -le 0) { return '' }
    $proc = Get-Process -Id $Owner -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return '' }
    return $proc.ProcessName
}

# 把进程号渲染成「QQ(39468)」这种看得懂的样子
function Format-Owners($Owners) {
    $parts = @()
    foreach ($owner in @($Owners)) {
        $name = Get-ProcName $owner
        if ($name) { $parts += "$name($owner)" } else { $parts += "$owner" }
    }
    return ($parts -join ', ')
}

# 真正占着 8766 的 python（= 网关本体）
function Get-GatewayOwners {
    $snapshot = Get-TcpSnapshot $Port
    $owners = @()
    foreach ($owner in @($snapshot.Listening + $snapshot.Established)) {
        if ((Get-ProcName $owner) -like 'python*') { $owners += $owner }
    }
    return @($owners | Sort-Object -Unique)
}

# 所有 `-m yixiang serve` 进程（含 venv shim 与它 re-exec 出来的子进程）
function Get-ServeProcesses {
    $found = @()
    try {
        $procs = @(Get-CimInstance -ClassName Win32_Process -Filter "Name = 'python.exe'" -ErrorAction Stop)
    } catch {
        return @()
    }
    foreach ($proc in $procs) {
        if ($proc.CommandLine -and $proc.CommandLine -like '*-m yixiang serve*') {
            $found += [pscustomobject]@{
                Id       = [int]$proc.ProcessId
                ParentId = [int]$proc.ParentProcessId
            }
        }
    }
    return @($found)
}

# 僵尸网关 = 也在跑 serve，却既不是端口持有者、也不是持有者所属的那个 venv shim
# 进程树实测：cmd -> shim(A) -> 真网关(B, 占端口)，A 的 Id 正好是 B 的 ParentId
function Get-ZombieGateways {
    $owners = @(Get-GatewayOwners)
    if ($owners.Count -eq 0) { return @() }
    $keep = @()
    $all = @(Get-ServeProcesses)
    foreach ($owner in $owners) {
        $keep += $owner
        foreach ($proc in $all) {
            if ($proc.Id -eq $owner) { $keep += $proc.ParentId }
        }
    }
    $keep = @($keep | Where-Object { $_ } | Sort-Object -Unique)
    $zombies = @()
    foreach ($proc in $all) {
        if ($keep -contains $proc.Id) { continue }
        if ($owners -contains $proc.Id) { continue }
        $zombies += $proc
    }
    return @($zombies)
}

# 读 .env（同名 key 取最后一条；只用来展示，不参与覆盖环境变量）
function Get-EnvValue([string]$Key) {
    if (-not (Test-Path -LiteralPath $EnvFile)) { return '' }
    $value = ''
    foreach ($line in [System.IO.File]::ReadAllLines($EnvFile)) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*)$')) {
            $raw = $matches[1].Trim()
            if ($raw) { $value = $raw.Trim('"').Trim("'").Trim() }
        }
    }
    return $value
}

function Show-AuditTail([int]$Count) {
    if (-not (Test-Path -LiteralPath $AuditLog)) { Write-Tip '审计日志还没生成'; return }
    $lines = @(Get-Content -LiteralPath $AuditLog -Tail $Count -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($lines.Count -eq 0) { Write-Tip '审计日志还是空的'; return }
    foreach ($line in $lines) {
        try {
            $obj = $line | ConvertFrom-Json
            $stamp = ([datetime]$obj.ts).ToString('MM-dd HH:mm:ss')
            $extra = ''
            if ($obj.PSObject.Properties.Name -contains 'user_id' -and $obj.user_id) { $extra = " user=$($obj.user_id)" }
            Write-Host ("        {0}  {1}{2}  {3}" -f $stamp, $obj.event, $extra, $obj.detail) -ForegroundColor DarkGray
        } catch {
            Write-Host "        $line" -ForegroundColor DarkGray
        }
    }
}

function Wait-Listening([int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ((Get-TcpSnapshot $Port).Listening.Count -gt 0) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Wait-NapCat([int]$TimeoutSec) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    $announcedQq = $false
    while ((Get-Date) -lt $deadline) {
        if ((Get-TcpSnapshot $Port).Established.Count -gt 0) { return $true }
        if (-not $announcedQq -and (Get-Process -Name 'QQ' -ErrorAction SilentlyContinue)) {
            $announcedQq = $true
            Write-Note 'QQ 已经起来了，等它登录并让 NapCat 连上网关…'
        }
        Start-Sleep -Seconds 2
    }
    return $false
}

# ------------------------------------------------------------------ 动作
function Stop-Gateway {
    $owners = @(Get-GatewayOwners)
    if ($owners.Count -eq 0) {
        Write-Tip "没有进程在监听 $Port，不用停"
        return $true
    }
    foreach ($owner in $owners) {
        try { Stop-Process -Id $owner -Force -ErrorAction Stop } catch { }
    }
    for ($i = 0; $i -lt 15; $i++) {
        if ((Get-TcpSnapshot $Port).Listening.Count -eq 0) { break }
        Start-Sleep -Seconds 1
    }
    if ((Get-TcpSnapshot $Port).Listening.Count -gt 0) {
        Write-Bad "端口 $Port 还是被占着，请手动处理"
        return $false
    }
    Write-Ok '网关已停'
    return $true
}

function Start-Gateway {
    Start-Process -FilePath $Python `
        -ArgumentList '-m', 'yixiang', 'serve' `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Minimized | Out-Null
}

function Invoke-Stop {
    Write-Head '停止网关'
    [void](Stop-Gateway)
    Write-Tip 'QQ 和 NapCat 不受影响；NapCat 会每 30 秒重试一次连接（正常现象）'
    return 0
}

function Invoke-Status {
    Write-Head '体检'
    $snapshot = Get-TcpSnapshot $Port
    if ($snapshot.Listening.Count -gt 0) {
        Write-Ok "网关在跑（$(Format-Owners $snapshot.Listening)）"
    } else {
        Write-Bad "网关没在跑（$Port 没人监听）"
    }
    if ($snapshot.Established.Count -gt 0) {
        Write-Ok "NapCat 已连上（$(Format-Owners $snapshot.Established)）"
    } else {
        Write-Bad 'NapCat 没连上'
    }
    $zombies = @(Get-ZombieGateways)
    if ($zombies.Count -gt 0) {
        Write-Note "有僵尸网关在抢端口：PID $(($zombies | ForEach-Object { $_.Id }) -join ', ')（双击 重启以湘.bat 会顺手清掉）"
    }
    Write-Head '配置（只读）'
    Write-Tip "可对话的 QQ：$(Get-EnvValue 'YIXIANG_QQ_ALLOWED')"
    Write-Tip "QQ 群消息：$(Get-EnvValue 'YIXIANG_QQ_GROUP_ENABLED')（0 = 忽略群消息）"
    Write-Head '最近 5 条审计'
    Show-AuditTail 5
    return 0
}

function Invoke-Start {
    param([switch]$DoRestart)

    # ---------- 1. 环境 ----------
    Write-Head '第 1 步 / 环境自检'
    if (-not (Test-Path -LiteralPath $Python)) {
        Write-Bad "找不到虚拟环境解释器：$Python"
        Write-Tip '先在项目根目录建好 .venv 并安装依赖（见 README）'
        return 1
    }
    Write-Ok "解释器：$Python"

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $probe = & $Python -c "import apscheduler, sqlite_vec" 2>&1
    $probeCode = $LASTEXITCODE
    $ErrorActionPreference = $previous
    if ($probeCode -ne 0) {
        Write-Note '这个解释器 import apscheduler / sqlite_vec 失败，网关可能起不来：'
        Write-Tip ("$probe" -replace '\s+', ' ')
    } else {
        Write-Ok '依赖自检通过'
    }

    # ---------- 2. 网关 ----------
    Write-Head '第 2 步 / 网关'
    if ($DoRestart) {
        Write-Note '重启模式：先停掉当前网关'
        [void](Stop-Gateway)
    }

    $zombies = @(Get-ZombieGateways)
    if ($zombies.Count -gt 0) {
        $zombieIds = @($zombies | ForEach-Object { $_.Id })
        Write-Note "发现抢不到端口的僵尸网关，先清掉：PID $($zombieIds -join ', ')"
        foreach ($proc in $zombies) {
            try { Stop-Process -Id $proc.Id -Force -ErrorAction Stop } catch { }
        }
    }

    $snapshot = Get-TcpSnapshot $Port
    if ($snapshot.Listening.Count -gt 0) {
        Write-Ok "网关本来就在跑（PID $($snapshot.Listening -join ', ')），直接复用"
    } else {
        Write-Note "启动网关：$Python -m yixiang serve（最小化窗口）"
        Start-Gateway
        if (Wait-Listening 30) {
            Write-Ok "网关已经监听 127.0.0.1:$Port"
        } else {
            Write-Bad "等了 30 秒，网关没起来（$Port 没人监听）"
            Write-Tip '看看这个最小化的窗口是不是一闪就没了；下面两行是日志尾巴：'
            foreach ($name in @('serve.err.log', 'serve.console.log')) {
                $file = Join-Path $ProjectRoot "data\logs\$name"
                if (Test-Path -LiteralPath $file) {
                    Write-Tip "$name："
                    foreach ($line in @(Get-Content -LiteralPath $file -Tail 5 -Encoding UTF8 -ErrorAction SilentlyContinue)) {
                        Write-Tip "  $line"
                    }
                }
            }
            Write-Head '最近 5 条审计'
            Show-AuditTail 5
            return 1
        }
    }

    # ---------- 3. NapCat ----------
    Write-Head '第 3 步 / NapCat（QQ 那一侧）'
    $connected = (Get-TcpSnapshot $Port).Established.Count -gt 0
    if (-not $connected) {
        Write-Note '网关刚起 / 还没连上，等它自动重连（最多 60 秒）…'
        $connected = Wait-NapCat 60
    }

    if (-not $connected) {
        $qqRunning = @(Get-Process -Name 'QQ' -ErrorAction SilentlyContinue).Count -gt 0
        if ($qqRunning) {
            Write-Note 'QQ 在跑，但没有挂上 NapCat：注入必须发生在 QQ 启动时，所以得重启一次 QQ'
            Write-Tip '重启后如果 QQ 要扫码登录，扫一下即可'
            $answer = ''
            try { $answer = Read-Host '        确实要重启 QQ 就输入 y 再回车（其他任何输入都取消）' } catch { $answer = '' }
            if ($answer -notmatch '^\s*(y|yes|是)\s*$') {
                Write-Bad '已取消，没有动你的 QQ。'
                Write-Tip '想让它自动重启 QQ，就重新双击 启动以湘.bat 并在提示时输入 y'
                return 1
            }
            try { Stop-Process -Name 'QQ' -Force -ErrorAction Stop } catch { }
            Start-Sleep -Seconds 4
        }

        if (-not (Test-Path -LiteralPath $NapCatBat)) {
            Write-Bad "找不到 NapCat 启动器：$NapCatBat"
            return 1
        }
        Write-Note '拉起 NapCat（会弹一次 UAC，点“是”）'
        Start-Process -FilePath 'cmd.exe' `
            -ArgumentList '/c', "`"$NapCatBat`"" `
            -WorkingDirectory $NapCatDir | Out-Null
        if (Wait-NapCat 180) {
            $connected = $true
        }
    }

    if (-not $connected) {
        Write-Bad 'NapCat 还是没连上'
        Write-Tip '排查顺序：NapCat 窗口有没有报错 / QQ 有没有登录 / 有没有杀毒软件拦注入'
        Write-Head '最近 5 条审计'
        Show-AuditTail 5
        return 1
    }

    # ---------- 4. 就绪 ----------
    $snapshot = Get-TcpSnapshot $Port
    $allowed  = Get-EnvValue 'YIXIANG_QQ_ALLOWED'
    Write-Head '一切就绪'
    Write-Ok "网关：$(Format-Owners $snapshot.Listening)；NapCat 连接：$(Format-Owners $snapshot.Established)"
    Write-Ok "可以对话的 QQ：$allowed"
    if ((Get-EnvValue 'YIXIANG_QQ_GROUP_ENABLED') -ne '1') {
        Write-Tip '群消息按配置忽略（YIXIANG_QQ_GROUP_ENABLED=0）'
    }
    Write-Host ''
    Write-Host "   现在去 QQ 里给 $allowed 发消息吧（就是之前测通的那个方式）" -ForegroundColor Green
    Write-Host '   这个窗口可以关掉；网关在后台的最小化窗口里跑着' -ForegroundColor DarkGray
    Write-Host '   要停掉就双击 停止以湘.bat；改完 .env 就双击 重启以湘.bat' -ForegroundColor DarkGray
    Write-Head '最近 3 条审计'
    Show-AuditTail 3
    return 0
}

# ------------------------------------------------------------------ 入口
Write-Host ''
Write-Host '  ============================================' -ForegroundColor DarkCyan
Write-Host '   以湘 · QQ 助手一键启动' -ForegroundColor Cyan
Write-Host "   （模式：$Action）" -ForegroundColor DarkCyan
Write-Host '  ============================================' -ForegroundColor DarkCyan

$exitCode = 0
switch ($Action) {
    'stop'    { $exitCode = Invoke-Stop }
    'status'  { $exitCode = Invoke-Status }
    'restart' { $exitCode = Invoke-Start -DoRestart }
    default   { $exitCode = Invoke-Start }
}

Write-Host ''
exit $exitCode
