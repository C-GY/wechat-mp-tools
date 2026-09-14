# Read-only diagnostics for the packaged Windows Channels listener.
# Does not change proxy settings, install certificates, or start collection tasks.
param(
    [string]$InstallDir = '',
    [string]$OutputDir = [Environment]::GetFolderPath('Desktop')
)

$ErrorActionPreference = 'Stop'
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$reportName = 'Channels-Diagnostics-' + $stamp + '-' + [guid]::NewGuid().ToString('N').Substring(0, 6)
$reportDir = Join-Path $OutputDir $reportName
New-Item -ItemType Directory -Path $reportDir -Force | Out-Null

function Protect-LogText([string]$Text) {
    $Text = [regex]::Replace($Text, '(?i)(https?://)[^/\s@]+@', '$1<REDACTED>@')
    $Text = [regex]::Replace($Text, '(https?://[^\s?"''<>]+)\?[^\s"''<>]*', '$1?<REDACTED>')
    $Text = [regex]::Replace($Text, '(?im)((?:authorization|cookie|set-cookie|password|passwd|access_key_secret|accessKeySecret|token|signature|wxsid)["'']?\s*[:=]\s*)[^\r\n]+', '$1<REDACTED>')
    return $Text
}

function Save-Report([string]$Name, $Value) {
    $text = $Value | ConvertTo-Json -Depth 8
    Protect-LogText $text | Set-Content -LiteralPath (Join-Path $reportDir $Name) -Encoding UTF8
}

function Test-ProxyTls([string]$Address, [int]$Port) {
    $result = [ordered]@{ target = 'channels.weixin.qq.com:443'; proxy = "$Address`:$Port"; tcp = $false; tunnel = ''; tls = $false }
    $client = New-Object System.Net.Sockets.TcpClient
    $ssl = $null
    try {
        if (-not $client.ConnectAsync($Address, $Port).Wait(3000)) { throw 'Proxy TCP connection timed out' }
        $result.tcp = $true
        $stream = $client.GetStream()
        $stream.ReadTimeout = 5000
        $stream.WriteTimeout = 5000
        $requestBytes = [Text.Encoding]::ASCII.GetBytes("CONNECT channels.weixin.qq.com:443 HTTP/1.1`r`nHost: channels.weixin.qq.com:443`r`n`r`n")
        $stream.Write($requestBytes, 0, $requestBytes.Length)
        $header = New-Object Text.StringBuilder
        while ($header.Length -lt 8192) {
            $nextByte = $stream.ReadByte()
            if ($nextByte -lt 0) { throw 'Proxy closed the CONNECT response' }
            [void]$header.Append([char]$nextByte)
            if ($header.ToString().EndsWith("`r`n`r`n")) { break }
        }
        $result.tunnel = ($header.ToString() -split "`r`n")[0]
        if ($result.tunnel -notmatch '^HTTP/\d\.\d 200\b') { throw 'Proxy did not establish an HTTPS tunnel' }
        # Use Windows certificate validation; no bypass callback or credentials.
        $ssl = New-Object System.Net.Security.SslStream($stream, $false)
        if (-not $ssl.AuthenticateAsClientAsync('channels.weixin.qq.com').Wait(8000)) { throw 'TLS handshake timed out' }
        $result.tls = $true
        $result.protocol = $ssl.SslProtocol.ToString()
        if ($ssl.RemoteCertificate) {
            $certificate = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($ssl.RemoteCertificate)
            $result.certificate = @{ subject = $certificate.Subject; issuer = $certificate.Issuer; thumbprint = $certificate.Thumbprint; expires = $certificate.NotAfter.ToString('o') }
            $certificate.Dispose()
        }
    } catch {
        $result.error = $_.Exception.GetBaseException().Message
    } finally {
        if ($ssl) { $ssl.Dispose() }
        $client.Dispose()
    }
    return $result
}

$processes = @(Get-CimInstance Win32_Process -Filter "Name = 'WeChat MP Tools.exe' OR Name = 'WeChat.exe' OR Name = 'Weixin.exe' OR Name = 'WeChatAppEx.exe'" -ErrorAction SilentlyContinue)
$processReport = @($processes | ForEach-Object {
    $version = $null
    if ($_.ExecutablePath -and (Test-Path -LiteralPath $_.ExecutablePath)) { $version = (Get-Item -LiteralPath $_.ExecutablePath).VersionInfo.FileVersion }
    [pscustomobject]@{ name = $_.Name; pid = $_.ProcessId; path = $_.ExecutablePath; version = $version }
})
Save-Report 'processes.json' $processReport

$internet = Get-ItemProperty -LiteralPath 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
$samples = @()
foreach ($sampleNumber in 1..3) {
    Write-Output "Checking listener and HTTPS connection ($sampleNumber/3)..."
    $before = @(Get-NetTCPConnection -LocalPort 5202 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort, OwningProcess)
    $started = (Get-Date).ToString('o')
    $probe = Test-ProxyTls '127.0.0.1' 5202
    $after = @(Get-NetTCPConnection -LocalPort 5202 -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress, LocalPort, OwningProcess)
    $samples += [pscustomobject]@{ started_at = $started; finished_at = (Get-Date).ToString('o'); listener_before = $before; probe = $probe; listener_after = $after }
    if ($sampleNumber -lt 3) { Start-Sleep -Seconds 2 }
}
$curlReport = @{ available = $false }
$curlPath = Join-Path $env:SystemRoot 'System32\curl.exe'
if (Test-Path -LiteralPath $curlPath) {
    Write-Output 'Cross-checking with Windows curl...'
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        # Explicit non-matching bypass entry overrides an inherited NO_PROXY=*
        # without relying on empty-argument handling in Windows PowerShell 5.
        $curlText = (& $curlPath --noproxy localhost --proxy http://127.0.0.1:5202 --connect-timeout 3 --max-time 10 --verbose --head https://channels.weixin.qq.com/ 2>&1 | Out-String)
        $curlReport = @{ available = $true; exit_code = $LASTEXITCODE; output = (Protect-LogText $curlText) }
    } finally { $ErrorActionPreference = $savedPreference }
}
Save-Report 'network.json' ([ordered]@{
    diagnostic_version = 2
    captured_at = (Get-Date).ToString('o')
    windows = [Environment]::OSVersion.VersionString
    proxy_enabled = $internet.ProxyEnable
    proxy_server = $internet.ProxyServer
    proxy_override = $internet.ProxyOverride
    auto_config_url = $internet.AutoConfigURL
    samples = $samples
    windows_curl = $curlReport
})

$roots = @()
if ($InstallDir) { $roots += $InstallDir }
$roots += @($processes | Where-Object { $_.Name -eq 'WeChat MP Tools.exe' -and $_.ExecutablePath } | ForEach-Object { Split-Path -Parent $_.ExecutablePath })
$roots += Join-Path $env:LOCALAPPDATA 'Programs\SelfMediaContentCollector'
$roots = @($roots | Select-Object -Unique)
$inventory = @()
$certificates = @()
$executables = @()
$rootIndex = 0
foreach ($root in $roots) {
    $rootIndex++
    $exePath = Join-Path $root 'WeChat MP Tools.exe'
    if (Test-Path -LiteralPath $exePath -PathType Leaf) {
        $file = Get-Item -LiteralPath $exePath
        $executables += [pscustomobject]@{ path = $exePath; bytes = $file.Length; modified = $file.LastWriteTime.ToString('o'); sha256 = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash }
    }
    foreach ($relative in @('wechat_mp_tools.log', 'data\proxy_runtime.log', 'data\proxy_runtime.log.1', 'data\frontend_errors.log', 'data\channels_call_log.jsonl')) {
        $path = Join-Path $root $relative
        $exists = Test-Path -LiteralPath $path -PathType Leaf
        $entry = [ordered]@{ path = $path; exists = $exists }
        if ($exists) {
            $file = Get-Item -LiteralPath $path
            $entry.bytes = $file.Length
            $entry.modified = $file.LastWriteTime.ToString('o')
            $excerpt = (Get-Content -LiteralPath $path -Encoding UTF8 -Tail 1500) -join "`r`n"
            if ($excerpt.Length -gt 2000000) { $excerpt = $excerpt.Substring($excerpt.Length - 2000000) }
            $name = "install-$rootIndex-" + $file.Name
            Protect-LogText $excerpt | Set-Content -LiteralPath (Join-Path $reportDir $name) -Encoding UTF8
        }
        $inventory += [pscustomobject]$entry
    }
    $certPath = Join-Path $root 'data\ca.crt'
    $certInfo = [ordered]@{ path = $certPath; exists = (Test-Path -LiteralPath $certPath -PathType Leaf) }
    if ($certInfo.exists) {
        try {
            $publicCert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($certPath)
            $certInfo.subject = $publicCert.Subject
            $certInfo.thumbprint = $publicCert.Thumbprint
            $certInfo.expires = $publicCert.NotAfter.ToString('o')
            $certInfo.trusted_current_user = Test-Path -LiteralPath ('Cert:\CurrentUser\Root\' + $publicCert.Thumbprint)
            $certInfo.trusted_local_machine = Test-Path -LiteralPath ('Cert:\LocalMachine\Root\' + $publicCert.Thumbprint)
            $publicCert.Dispose()
        } catch { $certInfo.error = $_.Exception.GetBaseException().Message }
    }
    $certificates += [pscustomobject]$certInfo
}
Save-Report 'log-files.json' $inventory
Save-Report 'certificate-status.json' $certificates
Save-Report 'executables.json' $executables
@'
Read-only Channels listener diagnostic report.
Reproduce the blank Channels page while listening is enabled, then run this script.
Only selected log excerpts, proxy/port status, public certificate metadata and
three unauthenticated HTTPS handshake tests and a Windows curl check are collected. No CA private keys,
cookies/configuration files, collection data or database credentials are copied.
Typical tokens and URL query strings are redacted; review before sharing.
Please include the reproduction time and whether stopping listening restores the page.
'@ | Set-Content -LiteralPath (Join-Path $reportDir 'README.txt') -Encoding UTF8
$archive = $reportDir + '.zip'
Compress-Archive -Path (Join-Path $reportDir '*') -DestinationPath $archive
Write-Output "Diagnostic ZIP: $archive"
