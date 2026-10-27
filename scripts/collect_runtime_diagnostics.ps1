#Requires -Version 5.1
# Read-only, offline evidence collection. Does not import or start the application.
[CmdletBinding()]
param(
    [string]$InstallDir = '',
    [string]$OutputDir = [Environment]::GetFolderPath('Desktop'),
    [ValidateRange(1, 30)][int]$Days = 7,
    [switch]$Monitor,
    [ValidateRange(5, 3600)][int]$SampleIntervalSeconds = 15,
    [ValidateRange(0, 43200)][double]$MonitorMinutes = 0,
    [switch]$NonInteractive,
    [switch]$OpenFolder
)

$ErrorActionPreference = 'Stop'
$started = Get-Date
$since = $started.AddDays(-$Days)
$roots = New-Object 'System.Collections.Generic.List[string]'
$issues = New-Object 'System.Collections.Generic.List[string]'
$fileInventory = New-Object 'System.Collections.Generic.List[object]'
$versionCache = @{}
$maxLogBytes = 2MB
$remainingLogBytes = 32MB
if ($Monitor -and $NonInteractive -and $MonitorMinutes -le 0) {
    throw 'Non-interactive monitoring requires a positive -MonitorMinutes duration.'
}

function Protect-Text([string]$Text) {
    # Logs may contain ordinary JSON, escaped JSON, headers or Python reprs.
    $Text = [regex]::Replace($Text, '(?is)-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', '<REDACTED private key>')
    $sensitive = 'authorization|(?:set[-_])?cookie|[\w-]*token|password|passwd|[\w-]*secret|access[_-]?key[\w-]*|api[_-]?key|signature|wxsid|wxuin|密码'
    $Text = [regex]::Replace($Text, '(?im)^.*(?:' + $sensitive + ')[\\"''\s]*[:=][^\r\n]*\r?$', '<REDACTED credential-bearing line>')
    # Also remove a quoted value on the next line after a credential key.
    $Text = [regex]::Replace($Text, '(?m)(<REDACTED credential-bearing line>\r?\n)[ \t]*["''][^\r\n]*', '$1<REDACTED possible continued value>')
    $Text = [regex]::Replace($Text, '(?i)\b(Bearer|Basic)\s+[A-Za-z0-9+/=._-]+', '$1 <REDACTED>')
    $Text = [regex]::Replace($Text, '(?i)([a-z][a-z0-9+.-]*://)[^/\s@]+@', '$1<REDACTED>@')
    $Text = [regex]::Replace($Text, '(?i)(https?://[^\s?"''<>]+)\?[^\s"''<>]*', '$1?<REDACTED>')
    $Text = [regex]::Replace($Text, '(?i)([A-Z]:\\+Users\\+)[^\\/\r\n"<>]+', '$1<USER>')
    $Text = [regex]::Replace($Text, 'S-1-5-21-\d+-\d+-\d+-\d+', '<USER-SID>')
    return $Text
}

function Protect-Value($Value) {
    # Redact values BEFORE serialization so the report stays valid JSON.
    if ($null -eq $Value) { return $null }
    if ($Value -is [string]) { return Protect-Text $Value }
    if ($Value -is [datetime]) { return $Value.ToString('o') }
    # Select-Object calculated numeric properties can carry a PSObject wrapper.
    # Preserve scalars before checking PSCustomObject, otherwise they become {}.
    if ($Value -is [ValueType]) { return $Value }
    if ($Value -is [System.Collections.IDictionary]) {
        $safe = [ordered]@{}
        foreach ($key in $Value.Keys) {
            if ($key -match '(?i)^(authorization|cookie|set-cookie|.*token|password|passwd|.*secret|access[_-]?key.*|api[_-]?key|signature|wxsid|wxuin|密码)$') { $safe[$key] = '<REDACTED>' }
            else { $safe[$key] = Protect-Value $Value[$key] }
        }
        return $safe
    }
    if ($Value -is [System.Collections.IEnumerable]) {
        $safe = @(foreach ($item in $Value) { Protect-Value $item })
        return ,$safe
    }
    if ($Value -is [pscustomobject]) {
        $safe = [ordered]@{}
        foreach ($property in $Value.PSObject.Properties) {
            if ($property.Name -match '(?i)^(authorization|cookie|set-cookie|.*token|password|passwd|.*secret|access[_-]?key.*|api[_-]?key|signature|wxsid|wxuin|密码)$') { $safe[$property.Name] = '<REDACTED>' }
            else { $safe[$property.Name] = Protect-Value $property.Value }
        }
        return $safe
    }
    return $Value
}

function Save-Json([string]$Name, $Value) {
    $json = ConvertTo-Json -InputObject (Protect-Value $Value) -Depth 10
    $json | Set-Content -LiteralPath (Join-Path $reportDir $Name) -Encoding UTF8
}

function Add-Root([string]$Path) {
    if ([string]::IsNullOrWhiteSpace($Path)) { return }
    try {
        $full = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($Path.Trim('"'))).TrimEnd('\')
        if (Test-Path -LiteralPath $full -PathType Leaf) { $full = Split-Path -Parent $full }
        if (-not $roots.Contains($full) -and $roots.Count -lt 12) { $roots.Add($full) }
    } catch { $issues.Add('Invalid installation path: ' + $_.Exception.Message) }
}

function Read-LogTail([string]$Path, [int]$Limit) {
    $stream = [IO.File]::Open($Path, 'Open', 'Read', ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
    try {
        $first = $stream.ReadByte(); $second = $stream.ReadByte()
        $encoding = [Text.Encoding]::UTF8
        if ($first -eq 255 -and $second -eq 254) { $encoding = [Text.Encoding]::Unicode }
        elseif ($first -eq 254 -and $second -eq 255) { $encoding = [Text.Encoding]::BigEndianUnicode }
        $count = [int][Math]::Min($Limit, $stream.Length)
        $start = $stream.Length - $count
        if ($encoding -ne [Text.Encoding]::UTF8 -and $start % 2 -ne 0) { $start++; $count-- }
        [void]$stream.Seek($start, 'Begin')
        $buffer = New-Object byte[] $count
        $offset = 0
        while ($offset -lt $count) {
            $read = $stream.Read($buffer, $offset, $count - $offset)
            if ($read -eq 0) { break }
            $offset += $read
        }
        $value = $encoding.GetString($buffer, 0, $offset)
        if ($start -gt 0) {
            # Discard the incomplete first line; it could be a fragment of a secret.
            $newline = $value.IndexOf("`n")
            if ($newline -lt 0) { return '[Oversized single line omitted; see inventory for file size.]' }
            $value = '[Earlier content and partial first line omitted.]' + "`r`n" + $value.Substring($newline + 1)
        }
        return Protect-Text $value
    } finally { $stream.Dispose() }
}

function Get-ExecutableInfo([string]$Path) {
    if (-not $Path) { return $null }
    if ($versionCache.ContainsKey($Path)) { return $versionCache[$Path] }
    $result = [ordered]@{ path = $Path; status = 'unavailable' }
    try {
        $file = Get-Item -LiteralPath $Path
        $result.status = 'ok'; $result.bytes = $file.Length
        $result.modified = $file.LastWriteTime.ToString('o')
        $result.file_version = $file.VersionInfo.FileVersion
        $result.product_version = $file.VersionInfo.ProductVersion
        # Inspect only the PE header: no executing/loading a third-party binary.
        $stream = [IO.File]::Open($Path, 'Open', 'Read', ([IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete))
        $reader = New-Object IO.BinaryReader($stream)
        try {
            if ($reader.ReadUInt16() -eq 0x5A4D) {
                $stream.Position = 0x3C
                $peOffset = $reader.ReadInt32()
                if ($peOffset -ge 0 -and $peOffset -lt ($stream.Length - 6)) {
                    $stream.Position = $peOffset
                    if ($reader.ReadUInt32() -eq 0x4550) {
                        $machine = $reader.ReadUInt16()
                        $result.architecture = switch ($machine) { 0x014c { 'x86' }; 0x8664 { 'x64' }; 0xAA64 { 'ARM64' }; default { ('0x{0:X4}' -f $machine) } }
                    }
                }
            }
        } finally { $reader.Dispose() }
    } catch { $result.error = $_.Exception.Message }
    $versionCache[$Path] = [pscustomobject]$result
    return $versionCache[$Path]
}

function Get-ProcessEvidence($Processes, $RootPaths) {
    $related = @{}
    foreach ($item in $Processes) {
        if ($item.Name -ieq 'WeChat MP Tools.exe') { $related[[int]$item.ProcessId] = $true; continue }
        if ($item.Name -notmatch '^pythonw?\.exe$') { continue }
        foreach ($root in $RootPaths) {
            # Source-mode recognition only; never serialize CommandLine.
            if ($item.CommandLine -and $item.CommandLine.IndexOf($root + '\', [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
                $item.CommandLine -match '(?:^|[\\\s"''])(?:main|app)\.py(?:[\s"'']|$)') {
                $related[[int]$item.ProcessId] = $true
            }
        }
    }
    do {
        $changed = $false
        foreach ($item in $Processes) {
            if ($related.ContainsKey([int]$item.ParentProcessId) -and -not $related.ContainsKey([int]$item.ProcessId)) {
                $related[[int]$item.ProcessId] = $true; $changed = $true
            }
        }
    } while ($changed)
    $details = @(foreach ($item in $Processes) {
        if (-not $related.ContainsKey([int]$item.ProcessId)) { continue }
        $role = 'application-or-worker'
        if ($item.Name -ieq 'msedgewebview2.exe') { $role = 'webview-browser' }
        if ($item.CommandLine -match '(?:^|\s)--type=([\w-]+)') { $role = $matches[1] }
        [pscustomobject]@{
            name = $item.Name; pid = $item.ProcessId; parent_pid = $item.ParentProcessId; role = $role
            created_at = $item.CreationDate; working_set_bytes = [uint64]$item.WorkingSetSize
            private_bytes = [uint64]$item.PrivatePageCount; virtual_bytes = [uint64]$item.VirtualSize
            pagefile_kib = $item.PageFileUsage; handles = $item.HandleCount; threads = $item.ThreadCount
            executable = Get-ExecutableInfo $item.ExecutablePath
        }
    })
    [pscustomobject]@{
        application_found = (@($Processes | Where-Object { $_.Name -ieq 'WeChat MP Tools.exe' -or $related.ContainsKey([int]$_.ProcessId) }).Count -gt 0)
        related_processes = $details
        # Other apps: memory and names only, to identify system-wide pressure.
        top_memory_processes = @($Processes | Sort-Object { [uint64]$_.PrivatePageCount } -Descending | Select-Object -First 20 Name, ProcessId, @{n='private_bytes';e={[uint64]$_.PrivatePageCount}}, @{n='working_set_bytes';e={[uint64]$_.WorkingSetSize}})
        all_webview_process_count = @($Processes | Where-Object { $_.Name -ieq 'msedgewebview2.exe' }).Count
    }
}

function Add-MemoryRecord([string]$Directory, $Value, [int]$MaxBytes = 8MB) {
    # Only rotate this run's own generated files. Never touch application logs.
    $path = Join-Path $Directory 'memory-history.jsonl'
    $line = (ConvertTo-Json -InputObject (Protect-Value $Value) -Depth 10 -Compress) + "`n"
    $encoding = New-Object Text.UTF8Encoding($false)
    if ((Test-Path -LiteralPath $path) -and ((Get-Item -LiteralPath $path).Length + $encoding.GetByteCount($line)) -gt $MaxBytes) {
        $oldest = $path + '.3'
        if (Test-Path -LiteralPath $oldest) { Remove-Item -LiteralPath $oldest }
        foreach ($slot in 2, 1) {
            $source = $path + '.' + $slot
            if (Test-Path -LiteralPath $source) { Move-Item -LiteralPath $source -Destination ($path + '.' + ($slot + 1)) }
        }
        Move-Item -LiteralPath $path -Destination ($path + '.1')
    }
    # Open/flush/close per record so closing the console still leaves earlier data.
    [IO.File]::AppendAllText($path, $line, $encoding)
}

function Start-MemoryMonitor(
    [string]$Directory, $RootPaths, [int]$IntervalSeconds = 15,
    [double]$DurationMinutes = 0, [switch]$NoConsole, [int]$SampleLimit = 0
) {
    $monitorStarted = Get-Date
    $count = 0
    $previousRenderers = @()
    $stopReason = 'user_enter'
    $consoleAvailable = -not $NoConsole
    if ($consoleAvailable) {
        try { $null = [Console]::KeyAvailable }
        catch {
            if ($DurationMinutes -le 0 -and $SampleLimit -le 0) { throw 'No interactive console. Specify -MonitorMinutes to bound the recording.' }
            $consoleAvailable = $false
        }
    }
    $status = [ordered]@{
        state = 'recording'; started_at = $monitorStarted.ToString('o'); samples = 0
        interval_seconds = $IntervalSeconds; requested_minutes = $DurationMinutes
        rotation = '8 MiB per file, current plus 3 previous files; oldest records roll off'
        note = 'PID/creation-time changes indicate replacement, not proof of a crash. Native memory counters are not JavaScript heap measurements.'
    }
    Save-Json 'monitor-status.json' $status
    Write-Host 'Recording memory history. Keep this window open; it can be minimized.'
    Write-Host 'After the error appears, return here and press ENTER to finish and build the ZIP.'
    Write-Host "Evidence folder: $Directory"
    :recording while ($true) {
        $sample = [ordered]@{ captured_at = (Get-Date).ToString('o'); number = ($count + 1) }
        $queryErrors = @()
        try {
            $sample.system = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -OperationTimeoutSec 8 |
                Select-Object AvailableBytes, CommittedBytes, CommitLimit, PercentCommittedBytesInUse
        } catch { $queryErrors += 'System memory query: ' + $_.Exception.Message }
        try {
            $processes = @(Get-CimInstance Win32_Process -OperationTimeoutSec 8)
            $evidence = Get-ProcessEvidence $processes $RootPaths
            $sample.application_found = $evidence.application_found
            $sample.related_processes = @($evidence.related_processes | Select-Object name, pid, parent_pid, role, created_at, working_set_bytes, private_bytes, handles, threads)
            $renderers = @($sample.related_processes | Where-Object { $_.role -eq 'renderer' } |
                ForEach-Object { ([string]$_.pid) + '@' + ([string]$_.created_at) } | Sort-Object)
            # Do not treat a failed query as the disappearance of all processes.
            $sample.renderer_identity_changed = $count -gt 0 -and (($renderers -join ',') -ne ($previousRenderers -join ','))
            if ($sample.renderer_identity_changed) { $sample.previous_renderer_identities = $previousRenderers }
            $previousRenderers = $renderers
        } catch { $queryErrors += 'Process memory query: ' + $_.Exception.Message }
        $sample.query_errors = $queryErrors
        Add-MemoryRecord $Directory $sample
        if ($count -eq 0) { Save-Json 'monitor-baseline.json' $sample }
        Save-Json 'monitor-last.json' $sample
        $count++
        $status.samples = $count
        $status.last_sample_at = $sample.captured_at
        Save-Json 'monitor-status.json' $status
        if ($count -eq 1 -or $count % 20 -eq 0) { Write-Host ("Memory samples saved: $count; " + $sample.captured_at) }
        if ($SampleLimit -gt 0 -and $count -ge $SampleLimit) { $stopReason = 'sample_limit'; break }
        $nextSampleAt = (Get-Date).AddSeconds($IntervalSeconds)
        do {
            if ($consoleAvailable -and [Console]::KeyAvailable) {
                if ([Console]::ReadKey($true).Key -eq [ConsoleKey]::Enter) { break recording }
            }
            if ($DurationMinutes -gt 0 -and ((Get-Date) - $monitorStarted).TotalMinutes -ge $DurationMinutes) {
                $stopReason = 'duration_elapsed'; break recording
            }
            if ($IntervalSeconds -gt 0) { Start-Sleep -Milliseconds 200 }
        } while ((Get-Date) -lt $nextSampleAt)
    }
    $status.state = 'completed'; $status.stop_reason = $stopReason
    $status.finished_at = (Get-Date).ToString('o')
    Save-Json 'monitor-status.json' $status
}

if (-not $OutputDir) { $OutputDir = $env:TEMP }
$reportName = 'Collector-Runtime-' + $started.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$reportDir = Join-Path $OutputDir $reportName
try { New-Item -ItemType Directory -Path $reportDir -Force | Out-Null }
catch {
    $reportDir = Join-Path $env:TEMP $reportName
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
    $issues.Add('Output directory unavailable; used TEMP: ' + $_.Exception.Message)
}

Write-Host '[1/5] Locating the running application and installation...'
Add-Root $InstallDir
$initialProcesses = @()
try { $initialProcesses = @(Get-CimInstance Win32_Process -OperationTimeoutSec 8) }
catch { $issues.Add('Initial process query: ' + $_.Exception.Message) }
foreach ($item in $initialProcesses) {
    if (-not $InstallDir -and $item.Name -ieq 'WeChat MP Tools.exe' -and $item.ExecutablePath) { Add-Root (Split-Path -Parent $item.ExecutablePath) }
}
$installations = @()
foreach ($key in @('HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall', 'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall')) {
    try {
        if (-not (Test-Path -LiteralPath $key)) { continue }
        foreach ($entry in @(Get-ItemProperty -Path ($key + '\*') -ErrorAction Stop)) {
            if ($entry.DisplayName -notmatch 'SelfMediaContentCollector|WeChat[ -]MP[ -]Tools|自媒体内容采集工具') { continue }
            $installations += $entry | Select-Object DisplayName, DisplayVersion, InstallLocation
            if (-not $InstallDir) {
                Add-Root $entry.InstallLocation
                if ($entry.DisplayIcon -match '^"?(.+?\.exe)"?(?:,\s*-?\d+)?$') { Add-Root (Split-Path -Parent $matches[1]) }
            }
        }
    } catch { $issues.Add('Installation registry query: ' + $_.Exception.Message) }
}
if (-not $InstallDir) {
    Add-Root (Join-Path $env:LOCALAPPDATA 'Programs\SelfMediaContentCollector')
    if ((Test-Path -LiteralPath (Join-Path $PSScriptRoot 'WeChat MP Tools.exe')) -or (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'main.py'))) { Add-Root $PSScriptRoot }
    $sourceRoot = Split-Path -Parent $PSScriptRoot
    if ((Test-Path -LiteralPath (Join-Path $sourceRoot 'main.py')) -and (Test-Path -LiteralPath (Join-Path $sourceRoot 'app.py'))) { Add-Root $sourceRoot }
}
$located = @($roots | Where-Object { (Test-Path -LiteralPath (Join-Path $_ 'WeChat MP Tools.exe')) -or (Test-Path -LiteralPath (Join-Path $_ 'main.py')) })
if ($located.Count -eq 0 -and -not $NonInteractive) {
    try {
        Add-Type -AssemblyName System.Windows.Forms
        $picker = New-Object System.Windows.Forms.FolderBrowserDialog
        try {
            $picker.Description = '请选择包含 WeChat MP Tools.exe 的软件安装文件夹；取消后仍收集系统诊断。'
            $picker.ShowNewFolderButton = $false
            if ($picker.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { Add-Root $picker.SelectedPath }
            else { $issues.Add('Installation folder selection cancelled; application logs may be missing.') }
        } finally { $picker.Dispose() }
    } catch { $issues.Add('Folder selection unavailable: ' + $_.Exception.Message) }
}
Save-Json 'installation.json' ([ordered]@{ captured_at = $started.ToString('o'); roots = @($roots.ToArray()); registrations = $installations; executables = @($roots | ForEach-Object { $path = Join-Path $_ 'WeChat MP Tools.exe'; if (Test-Path -LiteralPath $path -PathType Leaf) { Get-ExecutableInfo $path } }) })

if ($Monitor) {
    try { Start-MemoryMonitor $reportDir $roots.ToArray() -IntervalSeconds $SampleIntervalSeconds -DurationMinutes $MonitorMinutes -NoConsole:$NonInteractive }
    catch { Write-Host "Recording interrupted; keep the evidence folder: $reportDir"; throw }
    # The ordinary report now describes the end of the recording, not its start.
    $since = (Get-Date).AddDays(-$Days)
    try { $initialProcesses = @(Get-CimInstance Win32_Process -OperationTimeoutSec 8) }
    catch { $initialProcesses = @(); $issues.Add('Final process query: ' + $_.Exception.Message) }
}

Write-Host '[2/5] Capturing RAM, commit/pagefile and process memory (3 samples)...'
$samples = @()
foreach ($number in 1..3) {
    $sample = [ordered]@{ number = $number; captured_at = (Get-Date).ToString('o') }
    try {
        $os = Get-CimInstance Win32_OperatingSystem -OperationTimeoutSec 8
        $sample.system = $os | Select-Object Caption, Version, BuildNumber, OSArchitecture, LastBootUpTime, TotalVisibleMemorySize, FreePhysicalMemory, TotalVirtualMemorySize, FreeVirtualMemory, SizeStoredInPagingFiles, FreeSpaceInPagingFiles
    } catch { $issues.Add('OS memory query: ' + $_.Exception.Message) }
    try {
        $sample.commit = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory -OperationTimeoutSec 8 | Select-Object AvailableBytes, CommittedBytes, CommitLimit, PercentCommittedBytesInUse, PoolNonpagedBytes, PoolPagedBytes
    } catch { $issues.Add('Commit counter query: ' + $_.Exception.Message) }
    try {
        if ($number -eq 1) { $processes = $initialProcesses }
        else { $processes = @(Get-CimInstance Win32_Process -OperationTimeoutSec 8) }
        $sample.processes = Get-ProcessEvidence $processes $roots.ToArray()
    } catch { $issues.Add('Process snapshot: ' + $_.Exception.Message) }
    $samples += [pscustomobject]$sample
    Save-Json 'memory-and-processes.json' $samples
    if ($number -lt 3) { Start-Sleep -Seconds 2 }
}
try { Save-Json 'pagefile.json' @(Get-CimInstance Win32_PageFileUsage -OperationTimeoutSec 8 | Select-Object Name, AllocatedBaseSize, CurrentUsage, PeakUsage, TempPageFile) }
catch { $issues.Add('Pagefile query: ' + $_.Exception.Message) }
try { Save-Json 'disks.json' @(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType = 3' -OperationTimeoutSec 8 | Select-Object DeviceID, Size, FreeSpace) }
catch { $issues.Add('Disk space query: ' + $_.Exception.Message) }

Write-Host '[3/5] Reading bounded, redacted application log excerpts...'
$logs = @('wechat_mp_tools.log', 'data\frontend_errors.log', 'data\parse_debug.log', 'data\channels_call_log.jsonl')
foreach ($base in @('data\proxy_runtime.log', 'data\logs\channels_environment.jsonl')) {
    $logs += $base
    foreach ($suffix in 1..3) { $logs += "$base.$suffix" }
}
New-Item -ItemType Directory -Path (Join-Path $reportDir 'logs') | Out-Null
$rootNumber = 0
foreach ($root in $roots) {
    $rootNumber++
    foreach ($relative in $logs) {
        $path = Join-Path $root $relative
        $entry = [ordered]@{ path = $path; status = 'missing' }
        try {
            if (Test-Path -LiteralPath $path -PathType Leaf) {
                $item = Get-Item -LiteralPath $path
                $entry.bytes = $item.Length; $entry.modified = $item.LastWriteTime.ToString('o')
                if ($remainingLogBytes -le 0) { $entry.status = 'skipped_total_32MiB_limit' }
                else {
                    $limit = [int][Math]::Min($maxLogBytes, $remainingLogBytes)
                    $remainingLogBytes -= [int][Math]::Min($limit, $item.Length)
                    $entry.status = 'collected'; $entry.tail_limit_bytes = $limit
                    $entry.truncated = $item.Length -gt $limit
                    $entry.report_file = 'logs\root-' + $rootNumber + '-' + ($relative -replace '[\\/]', '_') + '.txt'
                    Read-LogTail $path $limit | Set-Content -LiteralPath (Join-Path $reportDir $entry.report_file) -Encoding UTF8
                }
            }
        } catch { $entry.status = 'read_error'; $entry.error = $_.Exception.Message }
        $fileInventory.Add([pscustomobject]$entry)
    }
}
Save-Json 'log-inventory.json' @($fileInventory.ToArray())

Write-Host '[4/5] Reading crash and resource-exhaustion events (up to 20 seconds)...'
$jobs = @()
$events = @()
try {
    foreach ($kind in @('Application crashes', 'System resource exhaustion')) {
        $jobs += Start-Job -Name $kind -ArgumentList $kind, $since -ScriptBlock {
            param($Kind, $Since)
            $ErrorActionPreference = 'Stop'
            $filter = @{ StartTime = $Since }
            $limit = 3000
            if ($Kind -eq 'Application crashes') {
                $filter.LogName = 'Application'
                $filter.ProviderName = @('Application Error', 'Application Hang', 'Windows Error Reporting')
            } else {
                $filter.LogName = 'System'; $filter.ProviderName = 'Microsoft-Windows-Resource-Exhaustion-Detector'; $filter.Id = 2004; $limit = 100
            }
            try {
                $found = @(Get-WinEvent -FilterHashtable $filter -MaxEvents $limit)
                $relevant = @(foreach ($event in $found) {
                    $message = $event.Message
                    if (-not $message) { $message = @($event.Properties | ForEach-Object { [string]$_.Value }) -join "`r`n" }
                    if ($Kind -eq 'Application crashes' -and $message -notmatch 'WeChat MP Tools|SelfMediaContentCollector|msedgewebview2|pywebview') { continue }
                    if ($message.Length -gt 16000) { $message = $message.Substring(0, 16000) + '[truncated]' }
                    [pscustomobject]@{ time = $event.TimeCreated.ToString('o'); id = $event.Id; provider = $event.ProviderName; message = $message }
                })
                [pscustomobject]@{ kind = $Kind; status = 'ok'; inspected = $found.Count; query_limit_reached = $found.Count -eq $limit; events = @($relevant | Select-Object -First 100); relevant_count = $relevant.Count }
            } catch {
                $state = 'unavailable'
                if ($_.FullyQualifiedErrorId -like 'NoMatchingEventsFound*') { $state = 'no_events_in_window' }
                [pscustomobject]@{ kind = $Kind; status = $state; error = $_.Exception.Message }
            }
        }
    }
    $jobs | Wait-Job -Timeout 20 | Out-Null
    foreach ($job in $jobs) {
        if ($job.State -eq 'Completed') { $events += Receive-Job $job }
        else { $events += [pscustomobject]@{ kind = $job.Name; status = 'incomplete'; job_state = [string]$job.State } }
    }
} catch { $issues.Add('Event collection: ' + $_.Exception.Message) }
finally {
    foreach ($job in $jobs) {
        if ($job.State -eq 'Running') { Stop-Job $job -ErrorAction SilentlyContinue }
        Remove-Job $job -Force -ErrorAction SilentlyContinue
    }
}
Save-Json 'windows-events.json' $events

$missingApp = @($samples | Where-Object { $_.processes.application_found }).Count -eq 0
if ($missingApp) { $issues.Add('Application process not identified. Crash-time memory cannot be reconstructed; run again while the error window is open.') }
$collected = @($fileInventory | Where-Object { $_.status -eq 'collected' }).Count
if ($collected -eq 0) { $issues.Add('No application logs found. Drag the software installation folder onto the CMD and run again.') }
Save-Json 'collection-status.json' ([ordered]@{
    diagnostic_version = 2; started_at = $started.ToString('o'); finished_at = (Get-Date).ToString('o')
    time_zone = [TimeZoneInfo]::Local.Id; powershell = $PSVersionTable.PSVersion.ToString()
    continuous_memory_recording = [bool]$Monitor
    application_found = -not $missingApp; collected_logs = $collected; issues = @($issues.ToArray())
    note = 'A completed ZIP is evidence collection only, not proof of a root cause. Missing events/logs do not prove no crash occurred.'
})

$summary = @"
自媒体内容采集工具：运行异常 / Out of Memory 诊断包
采集时间：$($started.ToString('o'))；系统时区：$([TimeZoneInfo]::Local.Id)
找到软件进程：$(-not $missingApp)；采集日志数：$collected；问题提示数：$($issues.Count)

请把本目录旁的同名 ZIP 发回，并补充：
1. 出错时间（尽量到分钟）：
2. 当时停留页面 / 正在执行任务：
3. 是连续两周未关闭，还是这两周每天启动使用：
4. 近期是否升级软件、Windows 或 WebView2：
5. 采集时是否仍显示错误；重新打开后能否恢复：

内容：软件/子进程内存（三次采样）、系统物理和虚拟内存、提交量/提交上限、
分页文件、磁盘剩余空间、程序版本和架构、最近 $Days 天的相关崩溃及系统内存耗尽事件、
白名单应用日志尾部（每个最多 2 MiB，总读取预算 32 MiB）。原日志大小见 log-inventory.json。
frontend_errors.log 是微信视频号注入页面的日志，未必记录桌面主页面崩溃。

本工具只读取本机证据并创建诊断文件；不联网、不上传、不连接数据库、不启动/重启软件，
不更改注册表、代理、分页文件或缓存。不复制 Cookie、登录配置、浏览器 Profile、
业务数据/数据库文件、视频、私钥或内存转储；不保存进程完整命令行。
日志会遮盖常见凭据、URL 参数和用户目录名，无法保证识别所有敏感文本，发送前请检查。
top_memory_processes 包含其他内存占用较大进程的名称和用量。
启用持续记录：$([bool]$Monitor)。若启用，memory-history.jsonl（含 .1 至 .3）记录内存走势与
网页渲染进程编号/启动时间变化；monitor-baseline.json 保留初始采样，monitor-last.json 为最后采样。
持续记录每个文件约 8 MiB，保留当前及 3 份旧文件；旧记录会轮转，不保证保留整个运行期。
进程更换可能由刷新、导航或崩溃等引起，单凭进程编号变化不能认定崩溃或内存泄漏。

请先采集再重启；重启后无法还原故障当时的进程内存。此包不能单独证明内存泄漏。
事件查询或性能计数器可能不可用，详见 collection-status.json 和 windows-events.json。
日志可能已经轮转、被清理或没有写入；没有日志不是没有问题。
日志位置不对时，将包含 WeChat MP Tools.exe 的文件夹拖到 CMD 上重新采集。

采集提示：
$($issues -join "`r`n")
"@
Protect-Text $summary | Set-Content -LiteralPath (Join-Path $reportDir '请补充故障情况.txt') -Encoding UTF8

Write-Host '[5/5] Saving the diagnostic ZIP...'
$archive = $reportDir + '.zip'
try {
    # PS 5.1 Compress-Archive mishandles literal directory names containing [].
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Compression.ZipFile]::CreateFromDirectory($reportDir, $archive, [IO.Compression.CompressionLevel]::Optimal, $true)
}
catch {
    Write-Host "ZIP failed. Keep the report folder: $reportDir"
    throw
}
Write-Host "Send this ZIP: $archive"
Write-Host "Collected logs: $collected; application found: $(-not $missingApp); collection notices: $($issues.Count)"
if ($collected -eq 0) { Write-Warning 'No application logs found. Drag the application installation folder onto the CMD to retry.' }
if ($missingApp) { Write-Warning 'Application not found. Keep the error window open when collecting if possible.' }
if ($OpenFolder -and -not $NonInteractive) {
    try { Start-Process -FilePath explorer.exe -ArgumentList ('/select,"' + $archive + '"') }
    catch { Write-Warning 'Explorer could not be opened. Use the ZIP path shown above.' }
}
