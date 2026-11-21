# Run on a disposable CI runner, after verify_draft_asset.py installed this variant.
param([Parameter(Mandatory)][string]$Archive)
$ErrorActionPreference = 'Stop'
$install = (Resolve-Path 'verification/installed').Path
$app = @(Get-ChildItem $install -Filter '*.exe' |
    Where-Object { $_.Name -notmatch '^(iib_api_server|uninstall)' })
if ($app.Count -ne 1) { throw 'Expected exactly one installed app' }
$marker = Join-Path $install 'iib-update-test.txt'
Set-Content -LiteralPath $marker -Value 'keep'
$extract = Join-Path $env:RUNNER_TEMP 'iib-update-nsis'
Expand-Archive -LiteralPath $Archive -DestinationPath $extract
$setup = @(Get-ChildItem $extract -Filter '*.exe')
if ($setup.Count -ne 1) { throw 'Invalid updater archive' }

# Exercise the exact passive install + relaunch flags used by Tauri. Do not pass
# /D: NSIS must rediscover the existing installation, including a custom path.
$installer = Start-Process -FilePath $setup[0].FullName -ArgumentList '/P','/R' -PassThru
if (!$installer.WaitForExit(120000)) { throw 'Passive update did not finish unattended' }
if ($installer.ExitCode -ne 0) { throw "NSIS returned $($installer.ExitCode)" }
$deadline = (Get-Date).AddSeconds(90)
try {
    do {
        $running = Get-Process -Name $app[0].BaseName -ErrorAction SilentlyContinue |
            Where-Object { $_.Path -eq $app[0].FullName }
        $log = Join-Path $install 'iib_api_server.log'
        if ($running -and (Test-Path $log) -and (Select-String -Quiet -Path $log -Pattern 'name=fe_loaded')) {
            if ((Get-Content -Raw $marker).Trim() -ne 'keep') { throw 'Lost existing user file' }
            Write-Output 'PASS: NSIS updated in place, preserved data and relaunched a working app'
            return
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    throw 'Updated app did not relaunch from its original installation'
} finally {
    if ($running) { $running | Stop-Process -Force }
}
