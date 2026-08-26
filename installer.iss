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
DefaultDirName={autopf}\{#VDRName}
DefaultGroupName={#VDRName}
DisableProgramGroupPage=yes
LicenseFile=LICENSE
OutputDir=dist_installer
OutputBaseFilename=VDR-{#VDRVersion}-Setup
SetupIconFile=AppIcon.ico
UninstallDisplayIcon={app}\{#VDRExe}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; Per-user install: no elevation, no UAC prompt.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
; The frozen app is 64-bit because the CI runner's Python is; saying so keeps
; it out of the 32-bit Program Files redirect.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startupicon"; Description: "Start VDR when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
; The whole PyInstaller COLLECT tree -- the exe plus its Python runtime,
; and ffmpeg/ffprobe when the build fetched them.
Source: "dist\VDR\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#VDRName}"; Filename: "{app}\{#VDRExe}"
Name: "{group}\{cm:UninstallProgram,{#VDRName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#VDRName}"; Filename: "{app}\{#VDRExe}"; Tasks: desktopicon
Name: "{userstartup}\{#VDRName}"; Filename: "{app}\{#VDRExe}"; Tasks: startupicon

[Run]
Filename: "{app}\{#VDRExe}"; Description: "{cm:LaunchProgram,{#StringChange(VDRName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; PyInstaller writes __pycache__ next to the app on first run; without this
; the uninstaller leaves the install directory behind.
Type: filesandordirs; Name: "{app}\__pycache__"
