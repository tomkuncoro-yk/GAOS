; Inno Setup script for Search Engines AI Overviewer.
; Compiled automatically by build.py (via ISCC.exe) -- you shouldn't
; normally need to run this by hand. It packages the PyInstaller
; --onedir build at dist\SearchEnginesAIOverviewer\ into a normal
; click-through installer: dist\SearchEnginesAIOverviewerSetup.exe

#define MyAppName "Search Engines AI Overviewer"
#define MyAppDirName "SearchEnginesAIOverviewer"
#define MyAppExeName "SearchEnginesAIOverviewer.exe"
#define MyAppPublisher "Advancing with Digital Methods"

[Setup]
AppName={#MyAppName}
AppVersion=0.2
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=dist
OutputBaseFilename=SearchEnginesAIOverviewerSetup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Files]
Source: "dist\{#MyAppDirName}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\Uninstall {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent
