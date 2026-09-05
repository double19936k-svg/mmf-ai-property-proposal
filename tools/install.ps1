param(
    [string]$PythonExecutable = 'python',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$OutputEncoding = [System.Text.Encoding]::UTF8
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$packageRoot = Split-Path -Parent $PSScriptRoot
$appDir = Join-Path $packageRoot 'app'
$runtimeDir = Join-Path $packageRoot 'runtime'
$venvDir = Join-Path $runtimeDir 'python'
$logsDir = Join-Path $packageRoot 'logs'
$reqFile = Join-Path $appDir 'requirements.txt'

foreach ($dir in @($runtimeDir, $logsDir, (Join-Path $packageRoot 'runs'), (Join-Path $packageRoot 'output'), (Join-Path $packageRoot 'config'), (Join-Path $packageRoot 'templates'))) {
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
}

function Find-Python {
    param([string]$Preferred)
    $names = @()
    if ($Preferred) { $names += $Preferred }
    $names += @('python', 'python3')
    foreach ($name in $names) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $source = [string]$cmd.Source
        if ($source -match 'WindowsApps') { continue }
        return $source
    }
    $fallbacks = @(
        (Join-Path $env:ProgramFiles 'Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python311\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        'C:\ProgramData\anaconda3\python.exe'
    )
    foreach ($path in $fallbacks) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    throw 'Python 3.10+ was not found. Install Python, then run 首次安装.cmd again.'
}

function Find-Node {
    $cmd = Get-Command node -ErrorAction SilentlyContinue
    if ($cmd -and ([string]$cmd.Source -notmatch 'WindowsApps') -and (Test-Path -LiteralPath $cmd.Source)) {
        return [string]$cmd.Source
    }
    $candidates = @(
        (Join-Path $env:ProgramFiles 'nodejs\node.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\nodejs\node.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\node\node.exe'),
        'C:\nodejs\node.exe'
    )
    if (${env:ProgramFiles(x86)}) {
        $candidates += (Join-Path ${env:ProgramFiles(x86)} 'nodejs\node.exe')
    }
    foreach ($path in $candidates) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return ''
}

function Find-Npm([string]$NodeExe) {
    $cmd = Get-Command npm -ErrorAction SilentlyContinue
    if ($cmd -and ([string]$cmd.Source -notmatch 'WindowsApps')) { return [string]$cmd.Source }
    if ($NodeExe) {
        $sibling = Join-Path (Split-Path -Parent $NodeExe) 'npm.cmd'
        if (Test-Path -LiteralPath $sibling) { return $sibling }
    }
    return ''
}

try {
    Write-Host 'MMF first install started...'
    $python = Find-Python -Preferred $PythonExecutable
    Write-Host ("Using Python: {0}" -f $python)
    if (-not $CheckOnly) {
        if (-not (Test-Path -LiteralPath (Join-Path $venvDir 'Scripts\python.exe'))) {
            Write-Host 'Creating isolated Python environment...'
            & $python -m venv $venvDir
            if ($LASTEXITCODE -ne 0) { throw 'Failed to create isolated Python environment.' }
        }
    }
    $venvPython = Join-Path $venvDir 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPython)) { $venvPython = $python }
    if (-not $CheckOnly) {
        Write-Host 'Installing Python packages into isolated environment...'
        & $venvPython -m pip install --upgrade pip
        if (-not (Test-Path -LiteralPath $reqFile)) { throw 'Missing app\requirements.txt' }
        & $venvPython -m pip install -r $reqFile
        if ($LASTEXITCODE -ne 0) { throw 'Python package install failed.' }
    }

    $node = Find-Node
    $nodeModules = Join-Path $appDir 'node_modules'
    $artifactTool = Join-Path $nodeModules '@oai\artifact-tool\package.json'
    if ($node -and -not $CheckOnly -and -not (Test-Path -LiteralPath $artifactTool)) {
        $npmCmd = Find-Npm $node
        if ($npmCmd) {
            Write-Host ("Installing Node packages with {0}" -f $npmCmd)
            Push-Location $appDir
            try { & $npmCmd install --no-audit --no-fund } finally { Pop-Location }
        }
    }
    if ($node) { Write-Host ("Using Node: {0}" -f $node) }

    $env:MMF_PACKAGE_ROOT = $packageRoot
    $env:MMF_APP_ROOT = $appDir
    $env:PYTHONPATH = $appDir
    Write-Host 'Running environment check...'
    & $venvPython (Join-Path $appDir 'env_check.py')
    $pythonDocx = & $venvPython -c "import docx; print(docx.__version__)" 2>$null
    $status = if ($pythonDocx) { 'PASS' } else { 'FAIL' }
    $config = [ordered]@{
        schema_version = 'runtime-config-v0.3'
        python_executable = $venvPython
        node_executable = $node
        node_modules = $nodeModules
        bin_dir = ''
        isolated_venv = $true
        generated_at = (Get-Date).ToString('o')
    }
    $config | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runtimeDir 'runtime_config.json') -Encoding UTF8
    Write-Host ("Environment check: {0}" -f $status)
    if ($status -ne 'PASS') { throw 'Environment check failed. See runtime\environment_check.json' }
    if (-not $node) { Write-Host 'Node.js not found. Word output is available. PPT needs Node.js.' }
    Write-Host 'Install finished. You can start MMF.'
    exit 0
} catch {
    Write-Host $_.Exception.Message
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue
        [System.Windows.Forms.MessageBox]::Show([string]$_.Exception.Message, 'MMF Desktop Install', 'OK', 'Error') | Out-Null
    } catch {}
    exit 1
}
