#ifndef AppVersion
  #error AppVersion is required. Use scripts/build_windows_installer.py.
#endif

#ifndef VersionInfoVersion
  #error VersionInfoVersion is required. Use scripts/build_windows_installer.py.
#endif

#ifndef SourceDir
  #define SourceDir "..\dist\WeChat MP Tools"
#endif

#ifndef OutputDir
  #define OutputDir "..\dist\installer"
#endif

#ifndef OutputBaseFilename
  #error OutputBaseFilename is required. Use scripts/build_windows_installer.py.
#endif

[Setup]
; Stable installation identity: keep this value across future releases.
AppId={{E956D31A-06A0-49BB-BA0B-3AE4DF127F6A}
AppName=自媒体内容采集工具
AppVersion={#AppVersion}
AppVerName=自媒体内容采集工具 {#AppVersion}
VersionInfoVersion={#VersionInfoVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoProductName=自媒体内容采集工具
VersionInfoDescription=自媒体内容采集工具 Windows x64 Full 安装程序
; Project policy: always offer the English directory, including on upgrades.
DefaultDirName={localappdata}\Programs\SelfMediaContentCollector
UsePreviousAppDir=no
DefaultGroupName=自媒体内容采集工具
UninstallDisplayName=自媒体内容采集工具 {#AppVersion}
UninstallDisplayIcon={app}\WeChat MP Tools.exe
OutputDir={#OutputDir}
OutputBaseFilename={#OutputBaseFilename}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
CloseApplications=yes
RestartApplications=no
SetupLogging=yes
DisableProgramGroupPage=yes

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加快捷方式："; Flags: unchecked

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\自媒体内容采集工具"; Filename: "{app}\WeChat MP Tools.exe"; WorkingDir: "{app}"
Name: "{autodesktop}\自媒体内容采集工具"; Filename: "{app}\WeChat MP Tools.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
Filename: "{app}\WeChat MP Tools.exe"; Description: "启动自媒体内容采集工具"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent
