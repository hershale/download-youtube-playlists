; Inno Setup - מתקין YouTube Playlist Downloader (התקנה פר-משתמש, ללא הרשאות מנהל)
; בנייה:  ISCC.exe /DAppVersion=1.0.0 installer\setup.iss   (אחרי שנוצרה התיקייה dist\YTPlaylistDownloader)

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif
#define AppName "YouTube Playlist Downloader"
#define AppExe "YTPlaylistDownloader.exe"

[Setup]
AppId={{B7C4E5A2-6D1F-4E8B-9A3C-2F5D8E1A7C40}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=hershale
AppPublisherURL=https://github.com/hershale/download-youtube-playlists
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; התקנה פר-משתמש: התיקייה ניתנת לכתיבה, כך ש-yt-dlp יכול להתעדכן בעצמו
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir=..\dist-installer
OutputBaseFilename=Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#AppExe}
CloseApplications=yes

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "..\dist\YTPlaylistDownloader\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExe}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; yt-dlp מתעדכן בעצמו ועלול להשאיר קבצים שהמתקין לא מכיר
Type: filesandordirs; Name: "{app}"
