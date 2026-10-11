; Wireface Desktop's installer (Inno Setup 6). Built by scripts\build-installer.ps1, which stages the files in
; build\app and passes the version: iscc /DAppVersion=0.1.1 installer\wireface.iss
;
; It installs for the current user (no administrator rights) into %LOCALAPPDATA%\Programs\Wireface. The face's
; settings, conversations and logs go in data\ there and are kept when it is uninstalled or upgraded; the speech
; models (deps\models, downloaded on first start) are removed with it.

#ifndef AppVersion
  #error Pass the version: iscc /DAppVersion=x.y.z installer\wireface.iss
#endif
#ifndef Stage
  #define Stage "..\build\app"
#endif

[Setup]
AppId={{6E0C2B8A-5B7E-4E3B-9C1D-7A4F2E9B1C55}
AppName=Wireface
AppVersion={#AppVersion}
AppVerName=Wireface {#AppVersion}
AppPublisher=Compsmart
AppPublisherURL=https://wireface.dev
AppSupportURL=https://wireface.dev/contact/
AppUpdatesURL=https://wireface.dev/start/
AppComments=A face and a voice for the AI agent you already use
AppCopyright=Copyright 2026 Compsmart
LicenseFile=..\LICENSE.md
DefaultDirName={localappdata}\Programs\Wireface
DefaultGroupName=Wireface
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\dist
OutputBaseFilename=WirefaceSetup-{#AppVersion}
SetupIconFile=..\shell\icon.ico
UninstallDisplayIcon={app}\Wireface.exe
UninstallDisplayName=Wireface {#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductName=Wireface Desktop
VersionInfoDescription=Wireface Desktop installer
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#Stage}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\Wireface"; Filename: "{app}\Wireface.exe"; Comment: "A face and a voice for your AI agent"
Name: "{autodesktop}\Wireface"; Filename: "{app}\Wireface.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\Wireface.exe"; Description: "Start Wireface"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; what the app writes next to its own files (byte code, the speech models); data\ is kept
Type: filesandordirs; Name: "{app}\deps\models"
Type: filesandordirs; Name: "{app}\wireface"
Type: filesandordirs; Name: "{app}\python"
