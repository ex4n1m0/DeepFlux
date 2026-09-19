; Inno Setup script for Deeptorrent
; Build after PyInstaller: pyinstaller packaging/app.spec --clean
; Then open this script in Inno Setup and Compile.

#define MyAppName "DeepFlux"
#define MyAppVersion "4.9"
#define MyAppPublisher "DeepFlux"
#define MyAppExeName "DeepFlux.exe"
; PyInstaller onedir output, relative to this script (packaging/..\dist).
#ifndef BuildDir
#define BuildDir "..\dist"
#endif
#ifndef BuildOutputDir
#define BuildOutputDir BuildDir
#endif
; Bundled Jackett installer (fetched+hashed by packaging/fetch_jackett.py;
; the .exe is gitignored — builds without it simply skip the Jackett step).
#ifndef JackettDir
#define JackettDir "jackett"
#endif

[Setup]
AppId={{7E1A3C4D-8B2F-4C5E-9A1D-3F6E2B8C7A0D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir={#BuildOutputDir}
OutputBaseFilename=DeepFlux4.9Setup
SetupIconFile=icon.ico
Compression=lzma
SolidCompression=yes
; Pure black theme — DeepFlux4.png has a black background, so the
; wizard pages match it exactly and the logo blends in seamlessly.
WizardStyle=modern dark polar hidebevels
WizardBackColor=#000000
WizardImageFile=wizard_image.png
WizardSmallImageFile=wizard_small.png
WizardImageAlphaFormat=none
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}
; Silent / minimal install: skip welcome, ready, and program-group pages.
; The user only picks the install directory (and optional desktop icon).
DisableWelcomePage=yes
DisableReadyPage=yes
DisableProgramGroupPage=yes
; /VERYSILENT or /SILENT from the command line skips even the directory page.
PrivilegesRequiredOverridesAllowed=dialog

; --- Code signing / SmartScreen -------------------------------------------
; Downloaded unsigned exes trigger "Windows protected your PC" (SmartScreen)
; and every release restarts from zero reputation. Nothing in this script
; can prevent that — only signing the SETUP EXE with a real certificate can.
; When a certificate exists, sign the inner launcher first:
;   signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
;     dist\DeepFlux\DeepFlux.exe
; then build the installer with a registered sign tool, e.g.:
;   set DF_SIGNTOOL=deepflux
;   ISCC /Sdeepflux="C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe" ^
;     sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $f packaging\installer.iss
; Without DF_SIGNTOOL set, the build stays unsigned exactly as before.
#define DfSignTool GetEnv('DF_SIGNTOOL')
#if DfSignTool != ""
SignTool={#DfSignTool}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Messages]
FinishedHeadingLabel=DeepFlux {#MyAppVersion} installation complete
; Keep this SHORT — the finished-page label has a fixed height (no scroll,
; no auto-grow), and long text gets cropped on machines with larger system
; fonts / text scaling. The full Chrome-extension steps live in the in-app
; Help → User Guide ("Chrome Extension" section).
FinishedLabel=DeepFlux has been installed successfully.%n%nThe Chrome native messaging host was registered automatically. For the Chrome extension steps, open Help → User Guide.%n%nIPTV playback bundles libmpv + FFmpeg (LGPL); license notices are in _internal\licenses\.

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
Source: "{#BuildDir}\DeepFlux\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs overwritereadonly
; Jackett final step payload: the official installer (GPL-2.0, unmodified)
; that --setup-jackett runs elevated, plus its license notice.
Source: "{#JackettDir}\Jackett.Installer.Windows.exe"; DestDir: "{app}\_internal\jackett"; Flags: ignoreversion skipifsourcedoesntexist
Source: "{#JackettDir}\jackett-gpl2.txt"; DestDir: "{app}\_internal\licenses"; Flags: ignoreversion skipifsourcedoesntexist

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{group}\IPTV Player Licenses (LGPL)"; Filename: "notepad.exe"; Parameters: "{app}\_internal\licenses\mpv-ffmpeg-lgpl.txt"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--register-associations"; Description: "Register file associations (.torrent, magnet:, media, html)"; Flags: runhidden
; Jackett final step — MUST stay BEFORE the launch entry so the app only
; starts once the service is linked. Runs the bundled installer elevated
; (one UAC), reads Jackett's API key, adds its public indexers and tests
; the connection. No nowait: the entries are sequential.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--setup-jackett"; Description: "Set up Jackett torrent search now (recommended) — installs the service and public sources"; Flags: postinstall skipifsilent
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister-native-host"; RunOnceId: "UnregisterNativeHost"
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister-associations"; RunOnceId: "UnregisterAssociations"

[UninstallDelete]
; Delete everything in the install directory (including files not tracked by the installer)
Type: filesandordirs; Name: "{app}\*"
Type: dirifempty; Name: "{app}"

[Code]
procedure CurPageChanged(CurPageID: Integer);
var
  I: Integer;
begin
  // Default-CHECK the Jackett final-step checkbox: Inno always creates
  // postinstall checkboxes unchecked, and the whole point of the step is
  // that a default install ships a working torrent search (user decision
  // 2026-09-16). Runs when the finished page is built, after the RunList
  // is populated.
  if CurPageID <> wpFinished then
    Exit;
  for I := 0 to WizardForm.RunList.Items.Count - 1 do
    if Pos('Jackett', WizardForm.RunList.ItemCaption[I]) > 0 then
    begin
      WizardForm.RunList.Checked[I] := True;
      Break;
    end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ConfigPath: String;
  Config: String;
begin
  if CurStep <> ssPostInstall then
    Exit;

  // The app reads its config from ~/.deeptorrent/config.json
  // (DeeptorrentConfig.default_config_path) — NOT AppData\Roaming.
  ConfigPath := GetEnv('USERPROFILE') + '\.deeptorrent\config.json';
  ForceDirectories(ExtractFilePath(ConfigPath));

  // PRESERVE the user's config across installs and updates (owner decision
  // 2026-09-19): the seed below only lands on FRESH installs (no config
  // yet). Deleting an existing config here would silently wipe every
  // source, API key and setting on each upgrade — the in-app auto-updater
  // (infra/updater.py) relies on upgrades keeping the file. Key-rotation
  // safety: config.from_file re-injects the CURRENT shared key on every
  // load (and scrubs any saved copy of it), so a preserved config can never
  // pin a rotated-out key over the new one.
  if FileExists(ConfigPath) then
    Exit;

  // The seed is MINIMAL on purpose: config.py is the single source of
  // truth for every setting (from_file fills in all defaults), and the
  // only shipped value is the sample IPTV playlist below. History
  // (found 2026-09-16): this seed used to duplicate the whole default
  // config WITH trailing commas — invalid JSON, which from_file silently
  // discarded, so fresh installs always ran on config.py defaults anyway
  // while the stale copy fossilized (youtube homepage, adblock off, …).
  // Do NOT paste defaults back in here; ship only real overrides.
  // The sample source: iptv-org Macau country playlist — 7 free-to-air
  // channels, no credentials, verified to serve #EXTM3U — so the Play tab
  // has content on first launch (user decision 2026-09-16). Users add
  // their own in Play → Sources and can remove this one there. A fixed id
  // (not a uuid) keeps re-installs idempotent.
  Config := '{' + #13#10 +
    '  "iptv": {' + #13#10 +
    '    "sources": [' + #13#10 +
    '      {"id": "macau-iptv-org", "name": "Macau", "kind": "m3u_url", ' +
    '"url": "https://iptv-org.github.io/iptv/countries/mo.m3u", "enabled": true}' + #13#10 +
    '    ]' + #13#10 +
    '  }' + #13#10 +
    '}';

  SaveStringToFile(ConfigPath, Config, False);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  AppDataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    // Clean up user AppData: config.json, native messaging host manifest, etc.
    AppDataDir := ExpandConstant('{userappdata}\DeepTorrent');
    if DirExists(AppDataDir) then
      DelTree(AppDataDir, True, True, True);

    // Also clean up the Deeptorrent (lowercase) config directory.
    AppDataDir := ExpandConstant('{userappdata}\Deeptorrent');
    if DirExists(AppDataDir) then
      DelTree(AppDataDir, True, True, True);

    // And the real data dir: ~/.deeptorrent (config, logs, caches, resume data,
    // browser profile).
    AppDataDir := GetEnv('USERPROFILE') + '\.deeptorrent';
    if DirExists(AppDataDir) then
      DelTree(AppDataDir, True, True, True);
  end;
end;
