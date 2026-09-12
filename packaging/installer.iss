; Inno Setup script for Deeptorrent
; Build after PyInstaller: pyinstaller packaging/app.spec --clean
; Then open this script in Inno Setup and Compile.

#define MyAppName "DeepFlux"
#define MyAppVersion "3.8"
#define MyAppPublisher "DeepFlux"
#define MyAppExeName "DeepFlux.exe"
; PyInstaller onedir output, relative to this script (packaging/..\dist).
#ifndef BuildDir
#define BuildDir "..\dist"
#endif
#ifndef BuildOutputDir
#define BuildOutputDir BuildDir
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
OutputBaseFilename=DeepFlux3.8Setup
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

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{group}\IPTV Player Licenses (LGPL)"; Filename: "notepad.exe"; Parameters: "{app}\_internal\licenses\mpv-ffmpeg-lgpl.txt"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--register-associations"; Description: "Register file associations (.torrent, magnet:, media, html)"; Flags: runhidden
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister-native-host"; RunOnceId: "UnregisterNativeHost"
Filename: "{app}\{#MyAppExeName}"; Parameters: "--unregister-associations"; RunOnceId: "UnregisterAssociations"

[UninstallDelete]
; Delete everything in the install directory (including files not tracked by the installer)
Type: filesandordirs; Name: "{app}\*"
Type: dirifempty; Name: "{app}"

[Code]
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

  // Fresh config on EVERY install: delete any existing file so keys and
  // settings from older builds never linger — no .bak, no leftovers.
  // Users re-enter any custom keys after each update.
  if FileExists(ConfigPath) then
    DeleteFile(ConfigPath);

  // No API keys anywhere: the app ships no built-in/shared keys (config.py
  // defaults are all empty), so the installer only seeds the non-sensitive
  // structural defaults. All api_key fields below stay empty.
  Config := '{' + #13#10 +
    '  "llm": {' + #13#10 +
    '    "provider": "deepseek",' + #13#10 +
    '    "api_key": "",' + #13#10 +
    '    "base_url": "",' + #13#10 +
    '    "model": "deepseek-v4-pro",' + #13#10 +
    '    "fast_model": "deepseek-v4-flash",' + #13#10 +
    '  },' + #13#10 +
    '  "indexer": {' + #13#10 +
    '    "url": "http://localhost:9117",' + #13#10 +
    '    "api_key": "",' + #13#10 +
    '    "torznab_path": "/api/v2.0/indexers/all/results/torznab",' + #13#10 +
    '    "timeout": 30' + #13#10 +
    '  },' + #13#10 +
    '  "web_search": {' + #13#10 +
    '    "provider": "perplexity",' + #13#10 +
    '    "api_key": "",' + #13#10 +
    '    "cx": "",' + #13#10 +
    '    "base_url": ""' + #13#10 +
    '  },' + #13#10 +
    '  "watchdog": {' + #13#10 +
    '    "enabled": false,' + #13#10 +
    '    "stall_threshold_seconds": 300,' + #13#10 +
    '    "auto_heal": false' + #13#10 +
    '  },' + #13#10 +
    '  "rss": {' + #13#10 +
    '    "feeds": [],' + #13#10 +
    '    "check_interval_seconds": 300' + #13#10 +
    '  },' + #13#10 +
    '  "browser": {' + #13#10 +
    '    "homepage": "https://www.youtube.com/",' + #13#10 +
    '    "bookmarks": [],' + #13#10 +
    '    "adblock_enabled": false' + #13#10 +
    '  },' + #13#10 +
    '  "download": {' + #13#10 +
    '    "max_concurrent": 3,' + #13#10 +
    '    "max_connections_per_download": 8,' + #13#10 +
    '    "default_folder": "",' + #13#10 +
    '    "bandwidth_limit_bps": 0,' + #13#10 +
    '    "auto_start": true,' + #13#10 +
    '    "segment_threshold_mb": 1,' + #13#10 +
    '    "control_api_port": 53742,' + #13#10 +
    '    "ffmpeg_path": "",' + #13#10 +
    '    "categories": []' + #13#10 +
    '  },' + #13#10 +
    '  "sources": {' + #13#10 +
    '    "use_jackett": true,' + #13#10 +
    '    "sources": []' + #13#10 +
    '  },' + #13#10 +
    '  "iptv": {' + #13#10 +
    '    "sources": [],' + #13#10 +
    '    "tmdb_api_key": "",' + #13#10 +
    '    "cache_dir": "",' + #13#10 +
    '    "cache_limit_mb": 10240,' + #13#10 +
    '    "cache_seconds": 8,' + #13#10 +
    '    "hwdec": "auto-safe",' + #13#10 +
    '    "preferred_player": "mpv",' + #13#10 +
    '    "enable_epg": true,' + #13#10 +
    '    "auto_try_next_source": false' + #13#10 +
    '  },' + #13#10 +
    '  "default_save_path": "",' + #13#10 +
    '  "categories": ["Movies", "TV", "Software", "Other"],' + #13#10 +
    '  "log_level": "INFO"' + #13#10 +
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
