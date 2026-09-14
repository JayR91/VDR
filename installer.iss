; Inno Setup script for the VDR Windows installer.
;
; The macOS side ships a DMG, where "install" means dragging one bundle into
; /Applications. Windows has no such convention -- a bare .exe leaves the user
; to decide where it lives, gets no Start menu entry, and cannot be
; uninstalled from Settings. This produces the thing Windows users expect:
; a single setup.exe that installs per-user, registers an uninstaller, and
; puts VDR in the Start menu.
;
; Per-user (not per-machine) is deliberate: it needs no administrator rights,
; which keeps the UAC prompt -- and the SmartScreen friction that comes with
; an unsigned elevated installer -- out of the way. VDR only ever writes to
; the user's own Downloads folder, so there is nothing a machine-wide install
; would buy.
;
; VDRVersion is passed in by scripts/build_windows.ps1 (/DVDRVersion=...).

#ifndef VDRVersion
  #define VDRVersion "0.0.0"
#endif

#define VDRName "VDR"
#define VDRPublisher "JayR91"
#define VDRURL "https://github.com/JayR91/VDR"
#define VDRExe "VDR.exe"

[Setup]
AppId={{9F3C1E62-4A77-4A1B-9E2D-6C51A0D3F8B2}
AppName={#VDRName}
AppVersion={#VDRVersion}
AppVerName={#VDRName} {#VDRVersion}
AppPublisher={#VDRPublisher}
AppPublisherURL={#VDRURL}
AppSupportURL={#VDRURL}/issues
AppUpdatesURL={#VDRURL}/releases
; Always the per-user Programs folder. {autopf} plus an "install for all
; users" override writes to Program Files; an unsigned installer then hits
; UAC or Access Denied and Setup appears to have done nothing.
DefaultDirName={localappdata}\Programs\{#VDRName}
DefaultGroupName={#VDRName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=dist_installer
; Matches the macOS artifact's shape (see scripts/build_dmg.sh) so the two
; sit together on a release page and each names its own platform.
OutputBaseFilename=VDR-{#VDRVersion}-Windows-Setup
SetupIconFile=AppIcon.ico
UninstallDisplayIcon={app}\{#VDRExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install: no elevation, no UAC prompt. Do not offer all-users.
PrivilegesRequired=lowest
UsePreviousPrivileges=no
; The frozen app is 64-bit because the CI runner's Python is; saying so keeps
; it out of the 32-bit Program Files redirect.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"
; Checked by default: the ⬇ VDR latch in Chrome talks to the local server,
; which only runs while VDR is up (window or tray).
Name: "startupicon"; Description: "Start VDR when I sign in (needed for the video button in Chrome)"; GroupDescription: "Startup:"

[Files]
; The whole PyInstaller COLLECT tree -- the exe plus its Python runtime,
; and ffmpeg/ffprobe when the build fetched them. Includes browser_extension\
; next to VDR.exe for --install-browser-extension to copy from.
Source: "dist\VDR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
; Stable path Chrome keeps loaded across app upgrades (same tree as crash.log).
; PyInstaller 6 onedir puts datas under _internal\; older layouts put them
; next to the exe. skipifsourcedoesntexist keeps either freeze working.
Source: "dist\VDR\_internal\browser_extension\*"; DestDir: "{localappdata}\VDR\extension-chrome"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist
Source: "dist\VDR\browser_extension\*"; DestDir: "{localappdata}\VDR\extension-chrome"; Flags: ignoreversion recursesubdirs createallsubdirs skipifsourcedoesntexist

[Icons]
Name: "{group}\{#VDRName}"; Filename: "{app}\{#VDRExe}"
Name: "{group}\{cm:UninstallProgram,{#VDRName}}"; Filename: "{uninstallexe}"
Name: "{userdesktop}\{#VDRName}"; Filename: "{app}\{#VDRExe}"; Tasks: desktopicon
Name: "{userstartup}\{#VDRName}"; Filename: "{app}\{#VDRExe}"; Tasks: startupicon

[Run]
; Register the Chromium latch *before* launching the GUI so the first Chrome
; restart after Setup already sees it. Hidden: this is a file/registry copy,
; not a second window.
Filename: "{app}\{#VDRExe}"; Parameters: "--install-browser-extension"; StatusMsg: "Registering the ⬇ VDR button in Chrome…"; Flags: runhidden waituntilterminated
Filename: "{app}\{#VDRExe}"; Description: "{cm:LaunchProgram,{#StringChange(VDRName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#VDRExe}"; Parameters: "--uninstall-browser-extension"; Flags: runhidden waituntilterminated; RunOnceId: "VDRUnregExt"

[UninstallDelete]
; PyInstaller writes __pycache__ next to the app on first run; without this
; the uninstaller leaves the install directory behind.
Type: filesandordirs; Name: "{app}\__pycache__"
Type: filesandordirs; Name: "{localappdata}\VDR\extension-chrome"
Type: filesandordirs; Name: "{localappdata}\VDR\extension-firefox"
Type: files; Name: "{localappdata}\VDR\extension-firefox.xpi"
