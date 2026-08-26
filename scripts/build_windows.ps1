<#
.SYNOPSIS
    Build the VDR Windows executable and its installer.

.DESCRIPTION
    The Windows counterpart to scripts/build_dmg.sh. Three steps:

      1. Fetch ffmpeg/ffprobe and stage them next to the spec, so the frozen
         app can merge separate video+audio streams (most YouTube 1080p and
         above) on a machine with nothing preinstalled.
      2. Freeze with PyInstaller (VDR-windows.spec) into dist\VDR\.
      3. Compile installer.iss with Inno Setup into dist_installer\.

    Run from the repository root:
        powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1

.PARAMETER Version
    Version stamped into the .exe resource and the installer filename.
    Defaults to 0.0.0 for local builds; CI passes the release tag.

.PARAMETER SkipFfmpeg
    Skip the ffmpeg download. The build still succeeds and the app falls back
    to whatever ffmpeg is on PATH at runtime.
#>
[CmdletBinding()]
param(
    [string]$Version = "0.0.0",
    [switch]$SkipFfmpeg
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Always operate from the repo root, whichever directory the script was
# invoked from -- the spec and .iss both use paths relative to it.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot
try {
    Write-Host "==> Building VDR $Version" -ForegroundColor Cyan

    # --- 1. ffmpeg ---------------------------------------------------------
    if (-not $SkipFfmpeg) {
        if (Test-Path "ffmpeg.exe") {
            Write-Host "==> ffmpeg.exe already staged, skipping download"
        } else {
            Write-Host "==> Fetching ffmpeg"
            $zip = Join-Path $env:TEMP "ffmpeg-release-essentials.zip"
            $dir = Join-Path $env:TEMP "ffmpeg-extract"
            # gyan.dev is the build the ffmpeg project itself links from
            # ffmpeg.org/download.html for Windows.
            $url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

            if (Test-Path $dir) { Remove-Item $dir -Recurse -Force }
            Expand-Archive -Path $zip -DestinationPath $dir -Force

            # The archive nests everything under ffmpeg-<version>-essentials_build\bin.
            foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
                $found = Get-ChildItem -Path $dir -Filter $tool -Recurse |
                         Select-Object -First 1
                if (-not $found) { throw "$tool not found in the ffmpeg archive" }
                Copy-Item $found.FullName -Destination (Join-Path $RepoRoot $tool) -Force
                Write-Host "    staged $tool"
            }
            Remove-Item $zip, $dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "==> Skipping ffmpeg (app will use PATH at runtime)" -ForegroundColor Yellow
    }

    # --- 2. version resource ----------------------------------------------
    # Windows VERSIONINFO needs a strict 4-part numeric version; a tag like
    # "v1.2.3" has to be normalised or the resource compiler rejects it.
    $numeric = ($Version -replace '^v', '') -replace '[^0-9.].*$', ''
    $parts = @($numeric -split '\.') | Where-Object { $_ -ne "" }
    while ($parts.Count -lt 4) { $parts += "0" }
    $quad = ($parts[0..3] -join ", ")

    Write-Host "==> Writing version resource ($quad)"
    @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($quad), prodvers=($quad),
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'JayR91'),
      StringStruct('FileDescription', 'VDR - video and file download manager'),
      StringStruct('FileVersion', '$Version'),
      StringStruct('InternalName', 'VDR'),
      StringStruct('LegalCopyright', 'GPL-3.0'),
      StringStruct('OriginalFilename', 'VDR.exe'),
      StringStruct('ProductName', 'VDR'),
      StringStruct('ProductVersion', '$Version')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -Path "version_info.txt" -Encoding UTF8

    # --- 3. freeze ---------------------------------------------------------
    Write-Host "==> Freezing with PyInstaller"
    if (Test-Path "build") { Remove-Item "build" -Recurse -Force }
    if (Test-Path "dist")  { Remove-Item "dist"  -Recurse -Force }
    python -m PyInstaller --noconfirm --clean VDR-windows.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }

    $exe = "dist\VDR\VDR.exe"
    if (-not (Test-Path $exe)) { throw "expected $exe was not produced" }
    Write-Host "    built $exe"

    # --- 4. installer ------------------------------------------------------
    Write-Host "==> Compiling installer"
    $iscc = Get-Command "iscc.exe" -ErrorAction SilentlyContinue
    if (-not $iscc) {
        foreach ($candidate in @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
        )) {
            if (Test-Path $candidate) { $iscc = $candidate; break }
        }
    } else {
        $iscc = $iscc.Source
    }
    if (-not $iscc) {
        throw "Inno Setup (ISCC.exe) not found. Install it with: winget install JRSoftware.InnoSetup"
    }

    & $iscc "/DVDRVersion=$($Version -replace '^v', '')" "installer.iss"
    if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed with exit code $LASTEXITCODE" }

    $setup = Get-ChildItem "dist_installer\*.exe" | Select-Object -First 1
    Write-Host ""
    Write-Host "==> Done: $($setup.FullName)" -ForegroundColor Green
    Write-Host "    $([math]::Round($setup.Length / 1MB, 1)) MB"
}
finally {
    Pop-Location
}
