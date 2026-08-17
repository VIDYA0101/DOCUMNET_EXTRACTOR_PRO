; Document Extractor - Inno Setup installer
;
; Build with:   iscc build\installer.iss
; Requires:     Inno Setup 6  (https://jrsoftware.org/isdl.php)
; Produces:     dist\installer\DocumentExtractor-1.0.0-Setup.exe
;
; Two decisions here are deliberate and should survive future tidying:
;
;   1. The default location is C:\DocumentExtractor, NOT Program Files. The
;      application keeps templates, logs and results in a Data folder beside
;      the executable. Program Files is read-only for standard users, so that
;      layout fails on exactly the machines this is aimed at. The directory
;      page now rejects such a path interactively, not just on the command line.
;
;   2. The Data folder is created explicitly with users-modify permissions.
;      Without that, a standard user cannot save a template, and the failure
;      surfaces as a confusing permission error halfway through their work.

#define AppName        "Document Extractor"
#define AppVersion     "1.0.1"
#define AppPublisher   "Your Company Name"
#define AppExeName     "DocumentExtractor.exe"
#define SourceDir      "..\dist\DocumentExtractor"

; Fail at compile time rather than shipping an empty installer.
#if !FileExists(AddBackslash(SourcePath) + SourceDir + "\" + AppExeName)
  #error Cannot find dist\DocumentExtractor\DocumentExtractor.exe - run build\build.bat first.
#endif

[Setup]
AppId={{8E4C2A91-7B3D-4F5E-9A1C-6D2F8B0E3A47}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName=C:\DocumentExtractor\App
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
DisableDirPage=no
OutputDir=..\dist\installer
OutputBaseFilename=DocumentExtractor-{#AppVersion}-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; Admin is needed to write under C:\ and to set the Data folder ACL.
PrivilegesRequired=admin
UninstallDisplayIcon={app}\{#AppExeName}
LicenseFile=..\LICENSES\LICENSE.txt
MinVersion=10.0
DirExistsWarning=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
Source: "{#SourceDir}\*"; DestDir: "{app}"; \
    Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; THE CRITICAL PART. Every one of these must be writable by a standard user.
; GetDataDir resolves the Data folder beside {app} without relying on literal
; ".." segments, which confuse the uninstaller.
Name: "{code:GetDataDir}";             Permissions: users-modify
Name: "{code:GetDataDir}\Templates";   Permissions: users-modify
Name: "{code:GetDataDir}\Input";       Permissions: users-modify
Name: "{code:GetDataDir}\Output";      Permissions: users-modify
Name: "{code:GetDataDir}\Review";      Permissions: users-modify
Name: "{code:GetDataDir}\Archive";     Permissions: users-modify
Name: "{code:GetDataDir}\Logs";        Permissions: users-modify
Name: "{code:GetDataDir}\Work";        Permissions: users-modify
Name: "{code:GetDataDir}\index";       Permissions: users-modify
Name: "{code:GetDataDir}\cache";       Permissions: users-modify

[Icons]
Name: "{group}\{#AppName}";            Filename: "{app}\{#AppExeName}"
Name: "{group}\Data folder";           Filename: "{code:GetDataDir}"
Name: "{group}\Uninstall {#AppName}";  Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}";      Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Start {#AppName} now"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Removes the application only. Templates, results and logs live in the Data
; folder beside it and are deliberately left behind: they are the user's work,
; and an uninstall during a version upgrade must not destroy them.
Type: filesandordirs; Name: "{app}"

[Code]

function GetDataDir(Param: String): String;
begin
  { C:\DocumentExtractor\App  ->  C:\DocumentExtractor\Data }
  Result := AddBackslash(ExtractFileDir(RemoveBackslash(ExpandConstant('{app}')))) + 'Data';
end;

function IsUnwritableLocation(Path: String): Boolean;
var
  Upper: String;
begin
  Upper := Uppercase(Path);
  Result := (Pos('\PROGRAM FILES', Upper) > 0) or
            (Pos('\WINDOWS\', Upper) > 0) or
            (Pos('\PROGRAMDATA', Upper) > 0);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = wpSelectDir then
  begin
    if IsUnwritableLocation(WizardDirValue) then
    begin
      MsgBox('That location will not work.' + #13#10#13#10 +
             'Document Extractor stores templates, logs and results in a Data ' +
             'folder next to the program. Windows keeps Program Files and the ' +
             'Windows folder read-only for standard users, so saving a ' +
             'template would fail.' + #13#10#13#10 +
             'Please use C:\DocumentExtractor\App, or another folder outside ' +
             'those locations.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  DataDir: String;
begin
  if CurStep = ssPostInstall then
  begin
    DataDir := GetDataDir('');
    if not DirExists(DataDir + '\Templates') then
      MsgBox('The Data folder could not be created at:' + #13#10 +
             DataDir + #13#10#13#10 +
             'Document Extractor stores templates, logs and results there and ' +
             'will not run without it. Please re-run this installer as an ' +
             'administrator.', mbError, MB_OK);
  end;
end;
