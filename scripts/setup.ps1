# Installs everything Wireface needs, inside this folder:
#   .venv\        Python packages (requirements.txt)
#   deps\shell\   Electron + MediaPipe for the desktop shell (shell\package.json)
# Speech and face models are shared by every face: $env:MODELS_DIR, else B:\models if that drive exists,
# else deps\models here. Run again any time: it only fetches what is missing.
#   -WithKokoro   also install the optional Kokoro voice (TTS_ENGINE=kokoro)
param([switch]$WithKokoro)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# --- Python -------------------------------------------------------------------------------------
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host 'Creating .venv'
    $py = (Get-Command py -ErrorAction SilentlyContinue)
    if ($py) { & py -3.11 -m venv (Join-Path $root '.venv') } else { & python -m venv (Join-Path $root '.venv') }
    if (-not (Test-Path $venvPy)) { throw 'could not create .venv (is Python 3.11+ installed?)' }
}
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }

# --- desktop shell ------------------------------------------------------------------------------
$shell = Join-Path $root 'deps\shell'
New-Item -ItemType Directory -Force $shell | Out-Null
Copy-Item (Join-Path $root 'shell\package.json') (Join-Path $shell 'package.json') -Force
Push-Location $shell
try {
    $env:npm_config_cache = Join-Path $root 'deps\npm-cache'
    & npm install --no-audit --no-fund --loglevel=error
    if ($LASTEXITCODE -ne 0) { throw 'npm install failed' }
    if (-not (Test-Path 'node_modules\electron\dist\electron.exe')) {
        $env:electron_config_cache = Join-Path $root 'deps\electron-cache'
        & node node_modules\electron\install.js          # npm may skip the binary download (postinstall)
        if (-not (Test-Path 'node_modules\electron\dist\electron.exe')) { throw 'electron download failed' }
    }
} finally { Pop-Location }

# --- models -------------------------------------------------------------------------------------
# Speech recognition, the voice, the lip-sync lexicon and the face landmarker (about 650 MB the first time).
# wireface\models.py skips what is already there, resumes interrupted downloads and checks the result.
$modelArgs = @('--install')
if ($WithKokoro) { $modelArgs += '--with-kokoro' }
# "python -m wireface..." finds the package through the current folder: always run it from the repo root,
# whichever folder setup.ps1 was started from.
Push-Location $root
try {
    & $venvPy -m wireface.models @modelArgs
    if ($LASTEXITCODE -ne 0) { Write-Warning 'Some models could not be downloaded; run scripts\setup.ps1 again when online.' }
    else {
        Write-Host 'Checking that the speech models load and an audio device is present'
        & $venvPy -m wireface.models --check --load
        if ($LASTEXITCODE -ne 0) { Write-Warning 'Speech check reported problems (see above); the face still runs, captions only.' }
    }
} finally { Pop-Location }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) { Write-Warning 'Claude Code (claude) is not on PATH' }
Write-Host 'Done. Start the face with Wireface.cmd'
