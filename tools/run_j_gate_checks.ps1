param(
    [Parameter(Mandatory=$true)][string]$Manifest,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [ValidateSet('ValidateOnly','Targeted','Offline')][string]$Mode = 'ValidateOnly'
)
$ErrorActionPreference = 'Stop'
$runJRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
Set-Location -LiteralPath $runJRoot
if (-not [IO.Path]::IsPathRooted($Manifest) -or -not [IO.Path]::IsPathRooted($OutputDirectory)) {
    throw 'Manifest and OutputDirectory must be absolute paths.'
}
$runJManifestPath = [IO.Path]::GetFullPath($Manifest)
$runJOutput = [IO.Path]::GetFullPath($OutputDirectory)
$runJOutputBoundary = (Join-Path $runJRoot 'runtime\run_j\agent_outputs') + '\'
if (-not $runJOutput.StartsWith($runJOutputBoundary, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Review outputs must be in a dedicated Run J agent_outputs subdirectory.'
}
# PowerShell 5.1 otherwise reads UTF-8 without BOM using the ANSI code page.
$runJFreeze = Get-Content -Raw -Encoding UTF8 -LiteralPath $runJManifestPath | ConvertFrom-Json -ErrorAction Stop
$runJExcludedFiles = @($runJFreeze.regression.excluded_generation_files)
if ($runJExcludedFiles.Count -ne 20 -or @($runJExcludedFiles | Select-Object -Unique).Count -ne 20) {
    throw 'Expected exactly 20 unique declared generation-test exclusions.'
}
foreach ($runJExcluded in $runJExcludedFiles) {
    if ($runJExcluded -notmatch '^tests[/\\]test_[^/\\]+\.py$' -or
        -not (Test-Path -LiteralPath (Join-Path $runJRoot $runJExcluded) -PathType Leaf)) {
        throw "Invalid or missing excluded test: $runJExcluded"
    }
}
$runJHead = git rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $runJHead -ne $runJFreeze.head) { throw 'HEAD differs from the review target.' }
$runJArtifactPython = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe'
$runJProject = Split-Path (Split-Path $runJRoot -Parent) -Parent
$runJTestPython = Join-Path $runJProject 'MMF_Application/runtime/dev_python/Scripts/python.exe'
if (-not $env:RUN_J_NODE) {
    $env:RUN_J_NODE = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'
}
if (-not $env:RUNTIME_NODE_MODULES) {
    $env:RUNTIME_NODE_MODULES = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules'
}
$env:MMF_TEST_PROVIDER_LOCKDOWN = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
if (Test-Path -LiteralPath $runJOutput) { throw 'Use a fresh output directory; historical results must not be overwritten.' }
New-Item -ItemType Directory -Path $runJOutput | Out-Null
$env:TEMP = Join-Path $runJOutput 't'
$env:TMP = $env:TEMP
$env:MMF_SCALE_EVIDENCE_DIR = Join-Path $runJOutput 'unexpected_scale_evidence'
New-Item -ItemType Directory -Path $env:TEMP | Out-Null
$runJTestArgs = @('-m','pytest','tests','-q','-p','no:cacheprovider','--basetemp',(Join-Path $runJOutput 'p'),'--junitxml',(Join-Path $runJOutput 'tests.xml'))
foreach ($runJExcluded in $runJExcludedFiles) { $runJTestArgs += '--ignore=' + (Join-Path $runJRoot $runJExcluded) }
if ($Mode -eq 'Targeted') { $runJTestArgs[2] = Join-Path $runJRoot 'tests/test_run_j_visuals.py' }
$runJContainmentTest = Join-Path $runJRoot 'tests/test_mmf007c_root_redirect_containment.py'
# These four existing tests require TEMP outside package_root. Run them in
# a separate process with a fixture package and sibling TEMP, all inside this
# agent's directory. No assertions are changed and no test is omitted.
if ($Mode -eq 'Offline') { $runJTestArgs += '--ignore=' + $runJContainmentTest }
@{mode=$Mode; head=$runJHead; manifest=$runJManifestPath; excluded=$runJExcludedFiles; arguments=$runJTestArgs; output=$runJOutput; separately_executed_containment_test=$runJContainmentTest} |
    ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $runJOutput 'execution_plan.json') -Encoding UTF8
if ($Mode -eq 'ValidateOnly') { Write-Output 'RUN_J_REVIEW_PLAN_VALID: 20 exclusions; no tests executed.'; exit 0 }
& $runJArtifactPython (Join-Path $runJRoot 'tools/verify_run_j_freeze.py') --manifest $runJManifestPath
if ($LASTEXITCODE -ne 0) { throw 'Frozen target mismatch; tests were not started.' }
$env:MMF_PACKAGE_ROOT = $runJRoot
& $runJTestPython @runJTestArgs *> (Join-Path $runJOutput 'tests.log')
if ($LASTEXITCODE -ne 0) { throw "Regression failed; inspect $runJOutput/tests.log. Do not expand testing." }
if ($Mode -eq 'Offline') {
    $runJPackage = Join-Path $runJOutput 'containment_package'
    New-Item -ItemType Directory -Path $runJPackage | Out-Null
    $runJFixtureFiles = @(git -c core.quotepath=false ls-files -- config app/assets templates)
    if ($LASTEXITCODE -ne 0) { throw 'Could not enumerate tracked fixture resources.' }
    foreach ($runJFixture in $runJFixtureFiles) {
        $runJTarget = Join-Path $runJPackage $runJFixture
        New-Item -ItemType Directory -Force -Path (Split-Path $runJTarget -Parent) | Out-Null
        Copy-Item -LiteralPath (Join-Path $runJRoot $runJFixture) -Destination $runJTarget
    }
    $env:MMF_PACKAGE_ROOT = $runJPackage
    try {
        & $runJTestPython -m pytest $runJContainmentTest -q -p no:cacheprovider --basetemp (Join-Path $runJOutput 'containment_pytest_tmp') --junitxml (Join-Path $runJOutput 'containment.xml') *> (Join-Path $runJOutput 'containment.log')
        if ($LASTEXITCODE -ne 0) { throw 'Containment regression failed; do not omit it or expand testing.' }
    } finally { $env:MMF_PACKAGE_ROOT = $runJRoot }
}
& $runJArtifactPython (Join-Path $runJRoot 'tools/verify_run_j_freeze.py') --manifest $runJManifestPath
if ($LASTEXITCODE -ne 0) { throw 'Freeze changed during review.' }
Write-Output "RUN_J_REVIEW_TESTS_PASS: $runJOutput"
