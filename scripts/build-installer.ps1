# Builds Wireface Desktop's Windows installer: dist\WirefaceSetup-<version>.exe and dist\SHA256SUMS.txt.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-installer.ps1
# The version is the one in wireface\version.py. It needs what scripts\setup.ps1 installs (.venv for pip, deps\shell
# for Electron and MediaPipe), Inno Setup 6 (winget install JRSoftware.InnoSetup) and the C# compiler that comes with
# Windows (.NET Framework 4).
#
# The installer carries everything but the speech models and the agent: its own Python (python.org's embeddable
# build, the same version as .venv) with the app's packages, Electron, MediaPipe, the app, and Wireface.exe
# (installer\launcher.cs), which downloads the speech models on first start, as Wireface.cmd does.
param([string]$Iscc)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$version = ([regex]'VERSION = "([0-9.]+)"').Match((Get-Content (Join-Path $root 'wireface\version.py') -Raw)).Groups[1].Value
if (-not $version) { throw 'no VERSION = "x.y.z" in wireface\version.py' }

$venvPy = Join-Path $root '.venv\Scripts\python.exe'
$electron = Join-Path $root 'deps\shell\node_modules\electron\dist'
$mediapipe = Join-Path $root 'deps\shell\node_modules\@mediapipe\tasks-vision'
foreach ($p in $venvPy, $electron, $mediapipe) { if (-not (Test-Path $p)) { throw "$p is missing: run scripts\setup.ps1 first" } }
if (-not $Iscc) {
    $Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe", "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw 'Inno Setup 6 is not installed: winget install JRSoftware.InnoSetup' }
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path $csc)) { throw "the C# compiler isn't at $csc" }

$build = Join-Path $root 'build'
$stage = Join-Path $build "app-$version"
$cache = Join-Path $build 'cache'
$dist = Join-Path $root 'dist'
if ([IO.Path]::GetFullPath($stage) -ne [IO.Path]::GetFullPath((Join-Path $root "build\app-$version"))) { throw 'Unsafe build stage' }
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force $stage, $cache, $dist | Out-Null

function Copy-Tree($from, $to) {
    robocopy $from $to /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "copying $from failed (robocopy $LASTEXITCODE)" }
    $global:LASTEXITCODE = 0
}

# --- the app ---------------------------------------------------------------------------------------------------------
Write-Host "Wireface $version"
Write-Host '  the app'
foreach ($d in 'wireface', 'web', 'shell', 'catalog') { Copy-Tree (Join-Path $root $d) (Join-Path $stage $d) }   # catalog: the Tools tab's recommendations and Wireface's own skills
Copy-Tree (Join-Path $root 'licenses') (Join-Path $stage 'licenses')
if (Test-Path (Join-Path $stage 'web\face\rig-build.js')) { throw 'Private rig construction code must be removed before distribution' }
foreach ($f in 'README.md', 'LICENSE.md', 'NOTICE', 'profile.example.json') { Copy-Item (Join-Path $root $f) $stage }
# The face goes in minified, as the Wireface SDK ships it: its readable source stays in this repo. esbuild (from npm, at
# the version named here) strips comments, whitespace and local names only: each file keeps its imports, its exports
# and its /*! Required Notice */
Write-Host '  the face, minified'
$face = Join-Path $stage 'web\face'
$js = @(Get-ChildItem $face -Filter *.js | ForEach-Object { $_.FullName })
$noticed = @($js | Where-Object { (Get-Content $_ -TotalCount 1) -match '^/\*! ' })
& npx --yes esbuild@0.28.2 @js --minify --legal-comments=inline --charset=utf8 "--outdir=$face" --allow-overwrite --log-level=warning
if ($LASTEXITCODE -ne 0) { throw 'minifying the face failed' }
foreach ($f in $noticed) {
    if ((Get-Content $f -TotalCount 1) -notmatch '^/\*! ') { throw "$f lost its notice when minified" }
}

# --- Python: the embeddable build, the app's packages, and a path file that finds the app ------------------------------
$pyVersion = (& $venvPy -c "import platform; print(platform.python_version())").Trim()
Write-Host "  Python $pyVersion"
$zip = Join-Path $cache "python-$pyVersion-embed-amd64.zip"
if (-not (Test-Path $zip)) {
    Invoke-WebRequest "https://www.python.org/ftp/python/$pyVersion/python-$pyVersion-embed-amd64.zip" -OutFile $zip -UseBasicParsing
}
$py = Join-Path $stage 'python'
Expand-Archive $zip $py
$pth = Get-ChildItem $py -Filter 'python*._pth' | Select-Object -First 1
$stdlib = (Get-Content $pth.FullName | Where-Object { $_ -match '\.zip$' } | Select-Object -First 1)
# the standard library, the packages, and the app (one folder up); "import site" reads the packages' .pth files
Set-Content $pth.FullName -Encoding ascii -Value @($stdlib, '.', 'Lib\site-packages', '..', 'import site')
$req = Join-Path $build 'requirements-runtime.txt'
Get-Content (Join-Path $root 'requirements.txt') | Where-Object { $_ -notmatch '^\s*pytest' } | Set-Content $req -Encoding ascii
# the exact versions in .venv, the ones the tests ran against
$pins = Join-Path $build 'constraints.txt'
& $venvPy -m pip freeze --disable-pip-version-check | Set-Content $pins -Encoding ascii
Write-Host '  packages'
$sp = Join-Path $py 'Lib\site-packages'
& $venvPy -m pip install --quiet --disable-pip-version-check --no-warn-script-location --no-compile `
    --target $sp -r $req -c $pins
if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
# Validate every recursive cleanup target before touching it.
function Remove-StagedDirectory($target) {
    $resolvedTarget = [IO.Path]::GetFullPath($target)
    $allowedRoot = [IO.Path]::GetFullPath($stage) + [IO.Path]::DirectorySeparatorChar
    if (-not $resolvedTarget.StartsWith($allowedRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "Unsafe cleanup target: $target" }
    if (Test-Path -LiteralPath $resolvedTarget) { Remove-Item -LiteralPath $resolvedTarget -Recurse -Force }
}
# what is never used at run time
$unused = @(
    'claude_agent_sdk\_bundled\claude.exe',    # the SDK's own copy of Claude Code: Wireface runs the one you installed
    'Scripts',                                 # second copies of sherpa-onnx's DLLs (it loads them from sherpa_onnx\lib)
    'pythonwin', 'win32comext', 'PyWin32.chm', # pywin32's IDE, COM extensions and help (mcp uses win32api and win32job)
    'win32\Demos', 'win32\test', 'win32\include', 'win32\libs')
foreach ($u in $unused) { $p = Join-Path $sp $u; Remove-StagedDirectory $p }
Get-ChildItem $sp -Recurse -Directory | Where-Object { $_.Name -in 'tests', 'test' } | ForEach-Object { Remove-StagedDirectory $_.FullName }
Get-ChildItem $sp -Recurse -File -Include *.lib, *.pdb, *.chm | Remove-Item -Force
& (Join-Path $py 'python.exe') -m compileall -q -j 0 $sp (Join-Path $stage 'wireface') | Out-Null
$check = & (Join-Path $py 'python.exe') -c "import wireface.app, wireface.models, sherpa_onnx, sounddevice, numpy, aiohttp, psutil, claude_agent_sdk; from wireface.version import VERSION; print(VERSION)"
if ($LASTEXITCODE -ne 0 -or $check -ne $version) { throw "the bundled Python can't load the app ($check)" }

# --- Electron and MediaPipe (the desktop shell, and finding a face in a photo) ---------------------------------------
Write-Host '  Electron, MediaPipe'
# Electron without its translations of Chromium's own words (the app is in English) and the shader compiler WebGPU
# uses (the face is WebGL; drawn with and without a GPU when this was tested). LICENSES.chromium.html stays (its
# licences require it), and so does resources\default_app.asar: it is what starts the app from the shell folder
$el = Join-Path $stage 'deps\shell\node_modules\electron\dist'
robocopy $electron $el /E /XD locales /XF dxcompiler.dll dxil.dll /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "copying Electron failed (robocopy $LASTEXITCODE)" }
New-Item -ItemType Directory -Force (Join-Path $el 'locales') | Out-Null
Copy-Item (Join-Path $electron 'locales\en-US.pak') (Join-Path $el 'locales')
# MediaPipe: the module the app imports and the SIMD build of its wasm (Electron always has SIMD)
$mp = Join-Path $stage 'deps\shell\node_modules\@mediapipe\tasks-vision'
New-Item -ItemType Directory -Force (Join-Path $mp 'wasm') | Out-Null
foreach ($f in 'vision_bundle.mjs', 'package.json', 'README.md') { Copy-Item (Join-Path $mediapipe $f) $mp }
foreach ($f in 'vision_wasm_internal.js', 'vision_wasm_internal.wasm') { Copy-Item (Join-Path $mediapipe "wasm\$f") (Join-Path $mp 'wasm') }
$global:LASTEXITCODE = 0

# --- THIRD-PARTY.txt: what the installer brings from other projects, and under which licences ------------------------
$elVersion = (Get-Content (Join-Path $electron 'version') -Raw).Trim()
$mpVersion = (Get-Content (Join-Path $mediapipe 'package.json') -Raw | ConvertFrom-Json).version
# (a file, not -c: Windows PowerShell mangles double quotes in a native program's arguments)
$lister = Join-Path $build 'third_party.py'
Set-Content $lister -Encoding ascii -Value @'
from importlib.metadata import distributions

def licence(m):
    if m.get("License-Expression"):
        return m["License-Expression"]
    classifiers = [c.split(" :: ")[-1] for c in m.get_all("Classifier") or [] if c.startswith("License ::")]
    if classifiers:
        return ", ".join(classifiers)
    text = (m.get("License") or "").strip().splitlines()
    return text[0][:60] if text else "see its licence file"

rows = {f"  {d.metadata['Name']} {d.version}: {licence(d.metadata)}" for d in distributions()}
print("\n".join(sorted(rows, key=str.lower)))
'@
$packages = & (Join-Path $py 'python.exe') $lister
if ($LASTEXITCODE -ne 0) { throw 'listing the Python packages failed' }
Set-Content (Join-Path $stage 'THIRD-PARTY.txt') -Encoding utf8 -Value (@(
    "Wireface Desktop $version brings these from other projects. Each keeps its own licence; the licence texts are",
    'in the folders named. Wireface itself: LICENSE.md. Its own third-party parts: NOTICE.',
    '',
    "Python $pyVersion (python\): PSF License Version 2, python\LICENSE.txt",
    "Electron $elVersion and Chromium (deps\shell\node_modules\electron\dist\): MIT (LICENSE) and the licences in",
    '  LICENSES.chromium.html',
    "MediaPipe Tasks Vision $mpVersion (deps\shell\node_modules\@mediapipe\tasks-vision\): Apache License 2.0",
    'Python packages (python\Lib\site-packages\; each licence is in its *.dist-info folder):') + $packages)

# --- Wireface.exe ----------------------------------------------------------------------------------------------------
Write-Host '  Wireface.exe'
$n = (@($version.Split('.')) + @('0', '0', '0'))[0..3] -join '.'
$info = Join-Path $build 'version.cs'
Set-Content $info -Encoding utf8 -Value @(
    'using System.Reflection;',
    '[assembly: AssemblyTitle("Wireface")]',
    '[assembly: AssemblyProduct("Wireface Desktop")]',
    '[assembly: AssemblyCompany("Compsmart")]',
    "[assembly: AssemblyVersion(`"$n`")]",
    "[assembly: AssemblyFileVersion(`"$n`")]",
    "[assembly: AssemblyInformationalVersion(`"$version`")]")
& $csc /nologo /target:winexe /optimize+ "/win32icon:$(Join-Path $root 'shell\icon.ico')" /r:System.Windows.Forms.dll `
    "/out:$(Join-Path $stage 'Wireface.exe')" (Join-Path $root 'installer\launcher.cs') $info
if ($LASTEXITCODE -ne 0) { throw 'compiling Wireface.exe failed' }

# --- the installer and its checksum ----------------------------------------------------------------------------------
$mb = [math]::Round(((Get-ChildItem $stage -Recurse -File | Measure-Object Length -Sum).Sum) / 1MB)
Write-Host "  installer ($mb MB to pack)"
& $Iscc /Q "/DAppVersion=$version" "/DStage=$stage" (Join-Path $root 'installer\wireface.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed' }
$name = "WirefaceSetup-$version.exe"
$exe = Join-Path $dist $name
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
Set-Content (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii -Value "$hash  $name"
Write-Host "Built $exe ($([math]::Round((Get-Item $exe).Length / 1MB)) MB)"
Write-Host "SHA-256 $hash"
