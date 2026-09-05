param([switch]$NoBrowser, [int]$Port = 0)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$packageRoot = Split-Path -Parent $PSScriptRoot
$appDir = Join-Path $packageRoot 'app'
$runtimeDir = Join-Path $packageRoot 'runtime'
$runtimePath = Join-Path $runtimeDir 'runtime_config.json'
$launcherLog = Join-Path $runtimeDir 'launcher_status.log'
$installScript = Join-Path $PSScriptRoot 'install.ps1'

function Write-LauncherLog([string]$Message) {
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    Add-Content -LiteralPath $launcherLog -Value ("{0} {1}" -f (Get-Date -Format o), $Message) -Encoding UTF8
}

function Show-Error([string]$Message) {
    Write-LauncherLog ("launcher_failed {0}" -f $Message)
    Write-Host $Message
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        [System.Windows.Forms.MessageBox]::Show($Message, 'MMF Desktop', 'OK', 'Error') | Out-Null
    } catch {
        try { (New-Object -ComObject WScript.Shell).Popup($Message, 0, 'MMF Desktop', 16) | Out-Null } catch {}
    }
}

function Get-AppListen([int]$ListenPort = 0) {
    $appFile = Join-Path $packageRoot 'config\app.json'
    if (-not (Test-Path -LiteralPath $appFile)) { throw 'Missing config\app.json' }
    $app = Get-Content -LiteralPath $appFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $hostName = [string]$app.host
    if ($hostName -ne '127.0.0.1' -and $hostName -ne 'localhost') { $hostName = '127.0.0.1' }
    $port = if ($ListenPort -gt 0) { [int]$ListenPort } else { [int]$app.port }
    return @{
        Host = $hostName
        Port = $port
        Url = "http://${hostName}:$port/"
        HealthUrl = "http://${hostName}:$port/api/health"
    }
}

function Ensure-LocalNoProxy {
    $needed = @('localhost', '127.0.0.1', '::1')
    foreach ($key in @('NO_PROXY', 'no_proxy')) {
        $parts = @()
        if ($env:NO_PROXY) { $parts += ($env:NO_PROXY -split ',') }
        if ($env:no_proxy) { $parts += ($env:no_proxy -split ',') }
        $merged = @{}
        foreach ($item in ($parts + $needed)) {
            $value = ([string]$item).Trim()
            if ($value) { $merged[$value] = $true }
        }
        $joined = ($merged.Keys -join ',')
        Set-Item -Path ("Env:{0}" -f $key) -Value $joined
    }
}

function Get-LocalHealth([string]$Url, [int]$TimeoutMs = 2000) {
    try {
        $request = [System.Net.HttpWebRequest]::Create($Url)
        $request.Method = 'GET'
        $request.Timeout = $TimeoutMs
        $request.ReadWriteTimeout = $TimeoutMs
        $request.KeepAlive = $false
        $request.Proxy = [System.Net.GlobalProxySelection]::GetEmptyWebProxy()
        $response = $request.GetResponse()
        try {
            $reader = New-Object System.IO.StreamReader($response.GetResponseStream())
            $text = $reader.ReadToEnd()
            $reader.Close()
            return $text | ConvertFrom-Json
        } finally {
            $response.Close()
        }
    } catch {
        return $null
    }
}

function Start-DetachedWatchdog([string]$PythonExe, [string]$WatchdogPath, [string]$WorkingDirectory) {
    if (-not ('MmfNative' -as [type])) {
        Add-Type -TypeDefinition @"
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class MmfNative {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct STARTUPINFO {
    public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
    public int dwX, dwY, dwXSize, dwYSize, dwXCountChars, dwYCountChars, dwFillAttribute, dwFlags;
    public short wShowWindow, cbReserved2; public IntPtr lpReserved2, hStdInput, hStdOutput, hStdError;
  }
  [StructLayout(LayoutKind.Sequential)]
  public struct PROCESS_INFORMATION {
    public IntPtr hProcess; public IntPtr hThread; public uint dwProcessId; public uint dwThreadId;
  }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
  public static extern bool CreateProcess(string app, StringBuilder cmd, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string dir, ref STARTUPINFO si, out PROCESS_INFORMATION pi);
  [DllImport("kernel32.dll", SetLastError=true)] public static extern bool CloseHandle(IntPtr h);
}
"@
    }
    $command = '"{0}" "{1}"' -f $PythonExe, $WatchdogPath
    $flagsList = @([uint32]0x09000208, [uint32]0x08000208, [uint32]0x00000208, [uint32]0x08000200)
    foreach ($flags in $flagsList) {
        $cmd = New-Object System.Text.StringBuilder
        [void]$cmd.Append($command)
        $si = New-Object MmfNative+STARTUPINFO
        $si.cb = [Runtime.InteropServices.Marshal]::SizeOf([type][MmfNative+STARTUPINFO])
        $pi = New-Object MmfNative+PROCESS_INFORMATION
        $ok = [MmfNative]::CreateProcess($PythonExe, $cmd, [IntPtr]::Zero, [IntPtr]::Zero, $false, $flags, [IntPtr]::Zero, $WorkingDirectory, [ref]$si, [ref]$pi)
        if ($ok) {
            if ($pi.hProcess -ne [IntPtr]::Zero) { [void][MmfNative]::CloseHandle($pi.hProcess) }
            if ($pi.hThread -ne [IntPtr]::Zero) { [void][MmfNative]::CloseHandle($pi.hThread) }
            Write-LauncherLog ("watchdog_detached pid={0} flags={1}" -f $pi.dwProcessId, $flags)
            return [int]$pi.dwProcessId
        }
    }
    try {
        $wmi = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{
            CommandLine = $command
            CurrentDirectory = $WorkingDirectory
        }
        if ($wmi.ReturnValue -eq 0 -and $wmi.ProcessId) {
            Write-LauncherLog ("watchdog_wmi pid={0}" -f $wmi.ProcessId)
            return [int]$wmi.ProcessId
        }
    } catch {
        Write-LauncherLog ("watchdog_wmi_failed {0}" -f $_.Exception.Message)
    }
    $fallback = Start-Process -FilePath $PythonExe -ArgumentList @($WatchdogPath) -WorkingDirectory $WorkingDirectory -WindowStyle Hidden -PassThru
    Write-LauncherLog ("watchdog_fallback pid={0}" -f $fallback.Id)
    return [int]$fallback.Id
}

try {
    Write-Host 'Starting MMF Desktop...'
    Write-LauncherLog 'launcher_started'
    if (-not (Test-Path -LiteralPath $runtimePath)) {
        Write-Host 'First run: installing isolated environment. Please wait...'
        Write-LauncherLog 'auto_install_started'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installScript
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $runtimePath)) {
            throw 'First install failed. Please run 首次安装.cmd and read runtime\environment_check.json'
        }
        Write-LauncherLog 'auto_install_finished'
    }

    $runtime = Get-Content -LiteralPath $runtimePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $runtime.python_executable -or -not (Test-Path -LiteralPath ([string]$runtime.python_executable))) {
        throw 'Python runtime is missing. Please run 首次安装.cmd again.'
    }
    $nodeExe = [string]$runtime.node_executable
    if (-not $nodeExe -or -not (Test-Path -LiteralPath $nodeExe)) {
        foreach ($candidate in @(
            (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
            (Join-Path $env:LOCALAPPDATA 'Programs\nodejs\node.exe')
        )) {
            if ($candidate -and (Test-Path -LiteralPath $candidate)) { $nodeExe = $candidate; break }
        }
    }
    if ($nodeExe -and (Test-Path -LiteralPath $nodeExe)) {
        $env:Path = (Split-Path -Parent $nodeExe) + ';' + $env:Path
        $runtime | Add-Member -NotePropertyName node_executable -NotePropertyValue $nodeExe -Force
    }
    $listen = Get-AppListen -ListenPort $Port
    $env:MMF_PACKAGE_ROOT = $packageRoot
    $env:MMF_APP_ROOT = $appDir
    $env:MMF_RUNTIME_ROOT = $packageRoot
    $env:MMF_HOST = '127.0.0.1'
    $env:MMF_PORT = [string]$listen.Port
    $env:PYTHONPATH = $appDir
    $env:RUNTIME_NODE = [string]$runtime.node_executable
    $env:RUNTIME_NODE_MODULES = [string]$runtime.node_modules
    $env:RUNTIME_BIN_DIR = [string]$runtime.bin_dir
    $grokBin = Join-Path $env:USERPROFILE '.grok\bin'
    if (Test-Path -LiteralPath $grokBin) {
        $env:Path = $grokBin + ';' + $env:Path
    }
    if (-not $env:HTTP_PROXY -and -not $env:HTTPS_PROXY) {
        foreach ($port in @(7897, 7890, 10809, 10808, 6152, 8888)) {
            $listening = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $port -State Listen -ErrorAction SilentlyContinue
            if ($listening) {
                $proxy = "http://127.0.0.1:$port"
                $env:HTTP_PROXY = $proxy
                $env:HTTPS_PROXY = $proxy
                $env:ALL_PROXY = $proxy
                Write-LauncherLog ("proxy_detected {0}" -f $proxy)
                break
            }
        }
    }
    Ensure-LocalNoProxy

    $stderrLog = Join-Path $runtimeDir 'launcher_server_error.log'
    $health = Get-LocalHealth $listen.HealthUrl
    if ($health -and $health.status -eq 'ok' -and $health.runtime_root -and $health.runtime_root -ne $packageRoot) {
        throw 'Port is already used by another program. Close it and try again.'
    }
    $running = [bool]($health -and $health.status -eq 'ok')
    if (-not $running) {
        foreach ($pidFile in @('watchdog.pid', 'server.pid')) {
            $path = Join-Path $runtimeDir $pidFile
            if (Test-Path -LiteralPath $path) {
                $oldId = 0
                [int]::TryParse((Get-Content -LiteralPath $path -Raw), [ref]$oldId) | Out-Null
                if ($oldId -gt 0) { Stop-Process -Id $oldId -Force -ErrorAction SilentlyContinue }
            }
        }
    }

    if (-not $running) {
        $pythonExe = [string]$runtime.python_executable
        $pythonw = Join-Path (Split-Path -Parent $pythonExe) 'pythonw.exe'
        if (Test-Path -LiteralPath $pythonw) { $pythonExe = $pythonw }
        $watchdogPath = Join-Path $appDir 'watchdog.py'
        $watchdogId = Start-DetachedWatchdog $pythonExe $watchdogPath $appDir
        Write-LauncherLog ("watchdog_started pid={0}" -f $watchdogId)
        Set-Content -LiteralPath (Join-Path $runtimeDir 'watchdog.pid') -Value $watchdogId -Encoding UTF8
        $ready = $false
        for ($attempt = 0; $attempt -lt 120; $attempt++) {
            Start-Sleep -Milliseconds 500
            $started = Get-LocalHealth $listen.HealthUrl
            if ($started -and $started.status -eq 'ok') { $ready = $true; break }
        }
        if (-not $ready) {
            $detail = ''
            if (Test-Path -LiteralPath $stderrLog) { $detail = (Get-Content -LiteralPath $stderrLog -Raw -ErrorAction SilentlyContinue) }
            throw ("App failed to start. See runtime\launcher_server_error.log`n{0}" -f $detail)
        }
    }

    if (-not $NoBrowser) {
        Start-Process $listen.Url
        Write-LauncherLog 'browser_open_requested'
    }
    Write-Host ("MMF is running at {0}" -f $listen.Url)
    exit 0
} catch {
    Show-Error ([string]$_.Exception.Message)
    exit 1
}
