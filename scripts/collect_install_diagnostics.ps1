# Requires Windows PowerShell 5.1. Only reads local evidence and writes a report.
[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [string]$OutputDir = [Environment]::GetFolderPath('Desktop'),
    [ValidateRange(1, 90)][int]$Days = 21,
    [switch]$OpenReport
)

$ErrorActionPreference = 'Stop'
$reportName = 'Collector-Diagnostics-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
if (-not $OutputDir) { $OutputDir = $env:TEMP }
$reportDir = Join-Path $OutputDir $reportName
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
$reportFile = Join-Path $reportDir 'report.txt'
$report = New-Object Text.StringBuilder
$since = (Get-Date).AddDays(-$Days)
$appPattern = 'SelfMediaContentCollector|WeChat[ -]MP[ -]Tools|自媒体内容采集工具|E956D31A-06A0-49BB-BA0B-3AE4DF127F6A'
$roots = New-Object 'System.Collections.Generic.List[string]'

function Protect-Text([string]$Text) {
    # Drop whole credential-bearing lines, including JSON and escaped JSON logs.
    $Text = [regex]::Replace($Text, '(?im)^.*(?:authorization|cookie|token|password|passwd|secret|access[_-]?key|api[_-]?key|wxsid|wxuin|密码)[\\"''\s]*[:=].*$', '<REDACTED credential-bearing line>')
    $Text = [regex]::Replace($Text, '(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '<REDACTED private key>')
    $Text = [regex]::Replace($Text, '(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=._-]+', '$1 <REDACTED>')
    $Text = [regex]::Replace($Text, '(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@', '$1<REDACTED>@')
    $Text = [regex]::Replace($Text, '(https?://[^\s?"''<>]+)\?[^\s"''<>]*', '$1?<REDACTED>')
    $Text = [regex]::Replace($Text, '(?i)([A-Z]:\\+Users\\+)[^\\/\r\n"<>]+', '$1<USER>')
    $Text = [regex]::Replace($Text, 'S-1-5-21-\d+-\d+-\d+-\d+', '<USER-SID>')
    return $Text
}

function Add-Section([string]$Title, $Value) {
    [void]$report.AppendLine((Protect-Text "`r`n===== $Title ====="))
    if ($Value -is [string]) { $body = $Value }
    else { $body = ConvertTo-Json -InputObject $Value -Depth 7 }
    [void]$report.AppendLine((Protect-Text $body))
    # Checkpoint after each section, so an interrupted run still leaves evidence.
    $report.ToString() | Set-Content -LiteralPath $reportFile -Encoding UTF8
}

function Add-Root([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    try {
        $full = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path.Trim('"'))).TrimEnd('\')
        if (-not $roots.Contains($full)) { $roots.Add($full) }
    } catch { Add-Section 'Invalid candidate path' $_.Exception.Message }
}

function Read-Excerpt([string]$Path, [bool]$Tail = $true) {
    # Bounded reads even when a log has a huge single line; share with a running app.
    $stream = [IO.File]::Open($Path, 'Open', 'Read', ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        $first = $stream.ReadByte()
        $second = $stream.ReadByte()
        $encoding = [Text.Encoding]::UTF8
        if ($first -eq 255 -and $second -eq 254) { $encoding = [Text.Encoding]::Unicode }
        elseif ($first -eq 254 -and $second -eq 255) { $encoding = [Text.Encoding]::BigEndianUnicode }
        $count = [int][Math]::Min(131072, $stream.Length)
        if ($Tail -and $stream.Length -gt $count) {
            $start = $stream.Length - $count
            if ($encoding -ne [Text.Encoding]::UTF8 -and $start % 2 -ne 0) { $start++; $count-- }
            [void]$stream.Seek($start, 'Begin')
        } else { [void]$stream.Seek(0, 'Begin') }
        $buffer = New-Object byte[] $count
        $offset = 0
        while ($offset -lt $count) {
            $read = $stream.Read($buffer, $offset, $count - $offset)
            if ($read -eq 0) { break }
            $offset += $read
        }
        $text = $encoding.GetString($buffer, 0, $offset)
        if ($stream.Length -gt $count) {
            if ($Tail) { $text = '[last 128 KiB; earlier content omitted]' + "`r`n" + $text }
            else { $text += "`r`n[first 128 KiB; later content omitted]" }
        }
        return $text
    } finally { $stream.Dispose() }
}

Add-Section '说明 / Read me' @"
本工具只读取证，不修改 Defender、注册表、代理或软件安装，不联网、不自动上传。
请发送本目录的 report.txt 或同名 ZIP，并注明最后正常时间、发现消失时间及安装包版本。
报告包含文件状态、安装记录、相关事件及有限日志片段；不读取配置、Cookie、数据库或私钥文件。
已遮盖常见凭据和用户目录名，发送前仍请检查内容。未发现记录不代表未发生删除。
系统事件范围：最近 $Days 天；日志可能已清理、轮转或因权限不足无法读取。
若 Defender 取证提示 Access denied，可用同一 Windows 账户右键 CMD，以管理员身份再运行。
此报告不能单独证明软件安全或告警属于误报。
"@
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
Add-Section 'Environment' ([ordered]@{
    captured_at = (Get-Date).ToString('o'); time_zone = [TimeZoneInfo]::Local.Id
    windows = [Environment]::OSVersion.VersionString; powershell = $PSVersionTable.PSVersion.ToString()
    elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    event_window_start = $since.ToString('o')
})

Write-Host '[1/4] Checking installation records and program files...'
Add-Root $InstallDir
Add-Root (Join-Path $env:LOCALAPPDATA 'Programs\SelfMediaContentCollector')
Add-Root (Join-Path $env:LOCALAPPDATA 'Programs\自媒体内容采集工具')
$registrations = @()
foreach ($key in @(
    'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKCU:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall',
    'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall'
)) {
    try {
        if (-not (Test-Path -LiteralPath $key)) { continue }
        foreach ($item in @(Get-ItemProperty -Path ($key + '\*') -ErrorAction Stop)) {
            if ($item.DisplayName -notmatch $appPattern -and $item.PSChildName -notmatch $appPattern) { continue }
            $registrations += $item | Select-Object DisplayName, DisplayVersion, InstallDate, InstallLocation, DisplayIcon, PSChildName
            Add-Root $item.InstallLocation
            if ($item.DisplayIcon -match '^"?(.+?\.exe)"?(?:,\s*-?\d+)?$') { Add-Root (Split-Path -Parent $matches[1]) }
        }
    } catch { Add-Section "Registry read unavailable: $key" $_.Exception.Message }
}
Add-Section 'Matching installation records (empty does not prove uninstall)' $registrations

$files = @()
foreach ($root in $roots) {
    foreach ($relative in @('', 'WeChat MP Tools.exe', '_internal', 'data', 'unins000.exe', 'unins000.dat')) {
        $path = $root
        if ($relative) { $path = Join-Path $root $relative }
        $entry = [ordered]@{ path = $path; state = 'unknown' }
        try {
            if (Test-Path -LiteralPath $path -ErrorAction Stop) {
                $item = Get-Item -LiteralPath $path -Force
                $entry.state = 'present'; $entry.modified = $item.LastWriteTime.ToString('o')
                if (-not $item.PSIsContainer) { $entry.bytes = $item.Length }
                if ($relative -eq 'WeChat MP Tools.exe') {
                    $entry.file_version = $item.VersionInfo.FileVersion
                    if ($item.Length -le 200MB) { $entry.sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash }
                    else { $entry.hash_status = 'skipped: file larger than 200 MiB' }
                }
            } else { $entry.state = 'missing' }
        } catch { $entry.state = 'read_error'; $entry.error = $_.Exception.Message }
        $files += [pscustomobject]$entry
    }
}
Add-Section 'Installation file inventory' $files
try {
    $processes = @(Get-CimInstance Win32_Process -Filter "Name = 'WeChat MP Tools.exe'" -OperationTimeoutSec 8 | Select-Object Name, ProcessId, ExecutablePath, CreationDate)
    Add-Section 'Running application processes' $processes
} catch { Add-Section 'Process query unavailable' $_.Exception.Message }
try {
    $antivirus = @(Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntivirusProduct -OperationTimeoutSec 8 | Select-Object displayName, productState)
    Add-Section 'Registered antivirus products' $antivirus
} catch { Add-Section 'Antivirus inventory unavailable' $_.Exception.Message }

Write-Host '[2/4] Reading relevant system events (up to 30 seconds)...'
$eventPattern = $appPattern
foreach ($root in $roots) { $eventPattern += '|' + [regex]::Escape($root) }
$jobs = @()
try {
    foreach ($kind in @('Defender', 'Application', 'Restarts')) {
        $jobs += Start-Job -Name $kind -ArgumentList $kind, $since, $eventPattern -ScriptBlock {
            param($Kind, $Since, $Pattern)
            $ErrorActionPreference = 'Stop'
            $limit = 3000
            $filter = @{ StartTime = $Since }
            switch ($Kind) {
                'Defender' { $filter.LogName = 'Microsoft-Windows-Windows Defender/Operational'; $filter.Id = @(1006, 1007, 1008, 1009, 1010, 1011, 1116, 1117, 1118, 1119, 1121) }
                'Application' { $filter.LogName = 'Application'; $filter.ProviderName = @('Application Error', 'Application Hang', 'Windows Error Reporting', 'MsiInstaller') }
                'Restarts' { $filter.LogName = 'System'; $filter.Id = @(41, 6005, 6006, 6008, 1074); $limit = 100 }
            }
            try {
                $events = @(Get-WinEvent -FilterHashtable $filter -MaxEvents $limit)
                $selected = @(foreach ($event in $events) {
                    $message = $event.Message
                    $values = @($event.Properties | ForEach-Object { [string]$_.Value }) -join "`r`n"
                    if ($Kind -ne 'Restarts' -and ($message + "`r`n" + $values) -notmatch $Pattern) { continue }
                    if (-not $message) { $message = $values }
                    if ($message.Length -gt 16000) { $message = $message.Substring(0, 16000) + '[truncated]' }
                    [pscustomobject]@{ time = $event.TimeCreated.ToString('o'); id = $event.Id; provider = $event.ProviderName; record_id = $event.RecordId; message = $message }
                })
                [pscustomobject]@{ status = 'ok'; inspected = $events.Count; relevant = $selected.Count; limit_reached = ($events.Count -eq $limit); events = $selected }
            } catch {
                $status = 'unavailable'
                if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $status = 'no_events_in_window' }
                [pscustomobject]@{ status = $status; error = $_.Exception.Message }
            }
        }
    }
    $jobs | Wait-Job -Timeout 30 | Out-Null
    foreach ($job in $jobs) {
        if ($job.State -eq 'Completed') { Add-Section ("Events: " + $job.Name) (Receive-Job $job) }
        else { Add-Section ("Events: " + $job.Name) ("Incomplete query; state=" + $job.State + '. Timed out or unavailable; this is not evidence of no detections.') }
    }
} catch { Add-Section 'Event collection error' $_.Exception.Message }
finally {
    foreach ($job in $jobs) {
        if ($job.State -eq 'Running') { Stop-Job $job -ErrorAction SilentlyContinue }
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
}

Write-Host '[3/4] Reading selected application and installer log excerpts...'
$logNames = @('wechat_mp_tools.log', 'data\frontend_errors.log')
foreach ($base in @('data\proxy_runtime.log', 'data\logs\channels_environment.jsonl')) {
    $logNames += $base
    foreach ($suffix in 1..3) { $logNames += "$base.$suffix" }
}
foreach ($root in $roots) {
    foreach ($relative in $logNames) {
        $path = Join-Path $root $relative
        try {
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $item = Get-Item -LiteralPath $path
                Add-Section "Log: $path; bytes=$($item.Length); modified=$($item.LastWriteTime.ToString('o'))" (Read-Excerpt $path)
            }
        } catch { Add-Section "Log unavailable: $path" $_.Exception.Message }
    }
}
$setupCount = 0
try {
    $setupLogs = @(Get-ChildItem -LiteralPath $env:TEMP -File | Where-Object { $_.Name -match '^(Setup|Uninstall) Log.*\.txt$' -and $_.LastWriteTime -ge $since } | Sort-Object LastWriteTime -Descending | Select-Object -First 40)
    foreach ($item in $setupLogs) {
        $head = Read-Excerpt $item.FullName $false
        if ($head -notmatch $eventPattern) { continue }
        $setupCount++
        Add-Section "Installer log: $($item.FullName)" $head
        if ($item.Length -gt 131072) { Add-Section "Installer log tail: $($item.FullName)" (Read-Excerpt $item.FullName) }
    }
    Add-Section 'Installer log search limits' "Searched the 40 newest matching Setup/Uninstall log files in the current user's TEMP within $Days days. Related files: $setupCount. Deleted logs and other Windows users are not searched."
} catch { Add-Section 'Installer log search unavailable' $_.Exception.Message }

Add-Section '需要补充 / Please add' "最后正常时间：`r`n发现软件消失时间：`r`n安装包文件名/版本：`r`n是否重装、清理或手动处理过保护历史：`r`n如路径自定义且本报告未找到，请将安装文件夹拖到 CMD 上重新运行。"
Write-Host '[4/4] Saving diagnostic ZIP...'
try {
    $archive = $reportDir + '.zip'
    Compress-Archive -LiteralPath $reportFile -DestinationPath $archive
    Write-Host "Send this ZIP: $archive"
} catch { Write-Warning 'ZIP creation failed; send report.txt instead.' }
Write-Host "Report: $reportFile"
if ($OpenReport) { Start-Process -FilePath notepad.exe -ArgumentList ('"' + $reportFile + '"') }
