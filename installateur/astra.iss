; Installateur Windows d'ASTRA Dashboard (compilé automatiquement par GitHub Actions)
#ifndef AppVersion
  #define AppVersion "1.0.0"
#endif

[Setup]
AppId={{094E75E1-6E1C-411E-B720-7AEAC7B8B777}
AppName=ASTRA Dashboard
AppVersion={#AppVersion}
AppVerName=ASTRA Dashboard
AppPublisher=ASTRA CAPITAL
AppPublisherURL=https://astracapital.app
AppSupportURL=https://astracapital.app
DefaultDirName={localappdata}\Programs\ASTRA Dashboard
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
PrivilegesRequired=lowest
OutputDir=..\dist
OutputBaseFilename=ASTRA-Dashboard-Setup
SetupIconFile=..\icone.ico
UninstallDisplayIcon={app}\icone.ico
UninstallDisplayName=ASTRA Dashboard
WizardStyle=modern
Compression=lzma2/max
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes

[Languages]
Name: "fr"; MessagesFile: "compiler:Languages\French.isl"

[Tasks]
Name: "desktopicon"; Description: "Créer un raccourci sur le bureau"; GroupDescription: "Raccourcis :"

[Files]
Source: "..\dist\app\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\ASTRA Dashboard"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icone.ico"
Name: "{autodesktop}\ASTRA Dashboard"; Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; IconFilename: "{app}\icone.ico"; Tasks: desktopicon

[Run]
Filename: "{app}\python\pythonw.exe"; Parameters: """{app}\app.py"""; WorkingDir: "{app}"; Description: "Ouvrir ASTRA Dashboard"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\__pycache__"
Type: files; Name: "{app}\*.maj"
