# System Health study — fail-safe low-level monitoring, from the open-source references

Date: 2026-11-19 · Branch: `svp-out-of-process` (post-8.6) · Status:
**P0 + P1 IMPLEMENTED same day** (see §7.1) — P2 remains study-only.

Owner ask: research internet resources + open-source projects for
low-level hardware system-health solutions we can take inspiration and
code references from, to improve the System Health tab's **compatibility
and stability**, with a fail-safe mindset: *"if a reading might cause a
crash, don't even try to read it."*

Research method: four parallel web-research agents read the actual
source of LibreHardwareMonitor, OpenHardwareMonitor, CrystalDiskInfo,
smartmontools, psutil, glances, htop, btop4win, pynvml, posegraph,
sMkTaskManager + Microsoft Learn docs + vendor docs + issue trackers
(~280 fetches total). Every claim below is marked **VERIFIED** (code or
doc actually fetched) or **REPORTED** (issue thread / forum / blog, URL
given). Local-SDK checks were done on this machine
(`Windows Kits\10\Include\10.0.26100.0`).

---

## TL;DR verdict

**Our architecture is the one the ecosystem converged on.** Every
sampler we shipped (8.5/8.6) sits on the documented / safe side of the
Windows API surface, and each one independently matches a gold-standard
implementation: our `GetSystemTimes` CPU% is exactly psutil's; our
`GlobalMemoryStatusEx` is psutil ≤5.9 and btop4win's; our
`IOCTL_DISK_PERFORMANCE` (0x70020) on a `\\.\C:` access-0 volume handle
is btop4win's *identical* choice (psutil uses `\\.\PhysicalDriveN` with
the same access-0 mask, LibreHardwareMonitor uses the same IOCTL);
spawning `nvidia-smi` instead of loading NVML in-process is *strictly
safer* than what LibreHardwareMonitor does (their in-process NVML has
documented native-AV crash classes); and our "CPU temp = ACPI probe,
None is honest" stance is what psutil settled on after 15+ years of
requests — **no Microsoft-sanctioned driverless CPU-temperature API
exists** (verified against the full `Windows.Devices.Sensors` class
list and every `CallNtPowerInformation` level).

The fail-safe mindset, distilled from eight mature projects (§2):
**never let "can't read" become an exception; degrade to absence, not
to lying zeros; probe-then-gate before touching a source; sanity-clamp
every value; isolate per source in the leaf because a loop-level fence
won't save you; permanently disable a source that hard-failed; never
ship a kernel driver; when data genuinely needs privileged/native
context, consume a trusted publisher's IPC surface instead of loading
code yourself.** Our tab-gated short-lived collector thread + per-source
backoff/cold-marking already implements most of this — LHM's own UI
loops are *weaker* than our shape (their WinForms loop has no try/catch
at all; their new Avalonia loop dies permanently on first error).

What the research says we should actually change:

1. **One real bug-class gap (P0):** we don't clamp *negative* counter
   deltas in `cpu_percent` — Windows counters genuinely go backwards
   (documented by psutil with three linked issues; htop and btop4win
   clamp too). One-line fix + test.
2. **One missing capability (P1):** the tab has **no network
   throughput** at all. Both the documented PDH lane (locale-safe via
   `PdhAddEnglishCounter`) and the simpler `GetIfTable2`/`/proc/net/dev`
   delta lane fit our doctrine perfectly.
3. **Cheap feature wins that fit the doctrine (P1):** per-core CPU% +
   live CPU clock (psutil's exact `NtQuerySystemInformation` /
   `CallNtPowerInformation` discipline — semi-documented but stable
   since NT and used by every monitor), unelevated NVMe disk
   health+temperature (LHM's driverless `IOCTL_STORAGE_QUERY_PROPERTY`
   protocol-query path), unelevated SATA SMART via
   `IOCTL_STORAGE_PREDICT_FAILURE` (smartmontools' documented no-admin
   tier), and laptop battery via `GetSystemPowerStatus`.
4. **The AMD/Intel GPU gap has a zero-new-DLL answer (P2):**
   `D3DKMTQueryStatistics` — the *same data Task Manager shows*, exported
   by `gdi32.dll` (already loaded everywhere), works unelevated for
   every vendor, with an existing pure-ctypes Python reference
   (posegraph) that fails closed. Because its structs are undocumented
   and version-drifted, it must live in a **subprocess**, never the GUI
   process — exactly our flaresolverr/PowerShell pattern.
5. **The only true CPU-core temps without a driver:** consume another
   trusted process's published data — LibreHardwareMonitor's HTTP
   `/data.json`, OHM's `root\OpenHardwareMonitor` WMI namespace, or
   HWiNFO's shared-memory SDK (§5). Optional, opt-in, zero native code.

And the two things the research says to **never** do: ship a kernel
driver (§6.1 — the WinRing0/PawnIO saga is the definitive cautionary
tale), and load vendor DLLs (NVML/NvAPI/ADL/IGCL) into the GUI process
(§6.2 — LHM's own issue tracker documents the AV classes).

---

## 1. What we have today, audited against the research

| Reading | Our implementation | Research verdict |
|---|---|---|
| CPU total % | `GetSystemTimes` deltas (`hardware_monitor._cpu_times_windows`) | ✔ psutil's exact choice; Microsoft's sanctioned aggregate API ("Use GetSystemTimes instead" is literally written on the NtQuerySystemInformation page). ✘ one gap: negative deltas unclamped (P0-1) |
| RAM + commit | `GlobalMemoryStatusEx` with `dwLength` ritual | ✔ universal pattern (psutil ≤5.9, btop4win); current psutil switched to `GetPerformanceInfo` only because it also wants cached/commit split — we already get commit from the same call |
| GPU live (NVIDIA) | `nvidia-smi` subprocess, 60 s failure backoff | ✔ **the safest lane that exists** — see §4.1; in-process NVML/NvAPI would be a regression |
| GPU static (any vendor) | DXGI `EnumAdapters1` COM walk | ✔ documented, correct >4 GB VRAM (SIZE_T); verified GUID/vtable work |
| Disk space | `shutil.disk_usage` per fixed volume | ✔ (wraps `GetDiskFreeSpaceExW`/`statvfs`, documented) |
| Disk speeds | `IOCTL_DISK_PERFORMANCE` (0x70020) on `\\.\C:`, access-0 handle, cold-mark on refusal | ✔ derivation **confirmed against the local SDK header** (winioctl.h:9077); **btop4win's byte-for-byte design choice** ("Based on the method used in psutil", volume handle, access 0, clamped deltas); LHM uses the same IOCTL; psutil uses `\\.\PhysicalDriveN` — both handle forms documented to return the same struct |
| CPU/board temp | `MSAcpi_ThermalZoneTemperature` via one-shot PowerShell, ±(-20..120)°C sanity range, 30 s TTL / 300 s fail backoff | ✔ honest-by-necessity: unelevated queries usually get Access denied, many boards expose nothing, and it's a firmware thermal *zone*, not CPU die temp (§3). Our sanity clamp = LHM's "reject 54000 °C readings" rule |
| Inventory | one PowerShell CIM query, 10 min cache / 60 s fail backoff | ✔ subprocess-isolated, exactly the doctrine; per-section `SilentlyContinue` = glances' fail-soft-per-plugin |
| Sampling engine | tab-gated QTimer → short-lived collector thread, no pile-up, queued signal | ✔ strictly stronger than both LHM UIs (§2 rule 7) and matches glances' "collection must never stack" clamp |

Two structural properties the research especially validates:

- **ctypes-only, no psutil.** psutil itself shipped a use-after-free
  (`_psutil_cpu_stats`, issue #3009, crashed a production Windows
  service with 0xC0000005 across two release lines) — the agent's
  closing line is worth keeping: *"a ctypes implementation that never
  frees buffers and never walks past a returned length is structurally
  immune to that bug class."* Staying ctypes-only also avoids bundling
  another heuristic-flagged native `.pyd` into the frozen build
  (REPORTED: PyInstaller+psutil Defender false positives are a
  recurring Stack Overflow theme).
- **Subprocess for anything that touches a driver-owned world**
  (nvidia-smi, PowerShell/WMI). LHM's documented in-process crash
  history (§4.1, §6.2) is precisely what this isolates.

---

## 2. The fail-safe doctrine, from eight mature projects

Distilled from LibreHardwareMonitor, OpenHardwareMonitor,
CrystalDiskInfo, smartmontools, psutil, glances, htop, btop4win — each
rule with its provenance and how we map to it.

1. **Never let "can't read" become an exception.** Every leaf accessor
   in LHM takes the shape `if (_driver == null) return false;`
   (`Ring0.ReadMsr`, `WindowsSmart.ReadSmartData` returns `bool?` —
   null means "cannot be read", a honest *tri-state*). Our samplers
   already do this — keep it absolute.
2. **Probe before you promise.** LHM's vendor interops set a static
   `IsAvailable` from a LoadLibrary/GetProcAddress existence probe
   (`NvApi.Initialize`, `IntelGcl` static ctor,
   `ADL_Method_Exists`) and never touch an absent DLL again. Our
   nvidia-smi path-resolution + dead-until backoff is the same spirit;
   the PowerShell path resolver (`_powershell_path`) is the same shape.
3. **Degrade to absence, not to lying zeros.** LHM sets
   `sensor.Value = null` (the sensor vanishes from the UI); btop4win
   permanently disables its CPU clock/temp boxes when LHM finds no
   sensors (`has_OHMR = false` for the session). Our None-is-honest
   rendering ("CPU temp —") is the mature projects' rule, and our
   `_disk_dead` cold-mark is btop4win's "feature off for the session"
   pattern.
4. **Sanity-clamp and timestamp-check every hardware value.** LHM's
   AmdGpu rejects impossible readings (some cards "report 54000 °C" →
   ±256 °C range check) and discards PMLog data older than 48 h. Trust
   no reading just because the call succeeded. We do this for
   temperature (-20..120 °C); rule going forward: any NEW reading gets
   a plausibility range before it is displayed or warned on.
5. **Clamp negative deltas.** CPU/disk counters on Windows *do* go
   backwards (psutil `__init__.py::_cpu_times_deltas` does
   `max(0, field_delta)` citing issues #392/#645/#1210 — "top does the
   same"; htop uses `saturatingSub` for every /proc/stat period;
   btop4win `max(0ll, ...)`). We clamp disk deltas but NOT CPU deltas
   → **P0-1 below**.
6. **A capability ladder with a documented no-admin tier beats
   all-or-nothing.** smartmontools' `win_ata_device::ata_pass_through`
   walks an option chain (`smart_ioctl` → scsi miniport → ATA pass
   through → ide) and, when unelevated, restricts to the `'f'` tier —
   `IOCTL_STORAGE_QUERY_PROPERTY` + `IOCTL_STORAGE_PREDICT_FAILURE` +
   WMI — with a one-time "Warning: Limited functionality due to missing
   admin rights". Never hard-fail when a weaker documented lane exists.
7. **Isolate per source in the leaf, because the loop fence won't save
   you.** LHM's WinForms update loop calls `hardware.Update()` with no
   try/catch at all (a throwing hardware silently stops updates via the
   BackgroundWorker's unwired `e.Error`); their Avalonia loop wraps the
   whole 1 Hz pass in one try/catch and **permanently ends polling** on
   any error. Their stability comes entirely from non-throwing leaves.
   Our per-call try/except inside a short-lived collector thread is
   stronger than both — keep it.
8. **Retry only known-transient errors, with a budget; hard-fail means
   off.** psutil's only native-read retry loop is for
   `ERROR_PARTIAL_COPY` (33 tries, exponential backoff, ~1 s total,
   then a typed error) — a race against a process exiting. Glances
   gives each plugin a refresh timer so a slow source samples less
   often instead of stacking. Our ladder (60 s smi backoff / 300 s
   thermal fail backoff / 60 s inventory fail backoff / permanent disk
   cold-mark) is exactly this shape.
9. **No kernel drivers, ever, for a project our size.** See §6.1 —
   this cost LibreHardwareMonitor three years, a custom attestation-
   signed driver rewrite (PawnIO, v0.9.5, 2026-01-07), and years of
   Defender "HackTool:Win32/WinRing0" detections for their users.
   Everything we sample is available user-mode or via rule 10.
10. **When a reading genuinely needs privileged/native context,
    consume a trusted publisher's IPC surface instead of loading code
    yourself.** LHM's HTTP `/data.json`, OHM's `root\OpenHardwareMonitor`
    WMI namespace, HWiNFO's `Global\HWiNFO_SENS_SM2` shared memory —
    CPU/GPU temps with zero new native code in our process (§5).

Bonus corroboration: LHM master's `StorageDevice.cs` reads disk
throughput through the very same `IOCTL_DISK_PERFORMANCE` we do, with a
silent return on failure and a skip-when-asleep guard — independent
validation that the 8.6 fix landed on the industry-standard pattern.

---

## 3. Verified Windows API surface (the compatibility matrix)

Legend: admin column = what a **standard-user** GUI process gets.
Driverless = no kernel driver required.

| Reading | Best driverless API | Admin? | Stability verdict | Source |
|---|---|---|---|---|
| CPU total % | `GetSystemTimes` (kernel32) | No | Documented; **our current choice** | MS Learn (VERIFIED) |
| CPU per-core % | `NtQuerySystemInformation(SystemProcessorPerformanceInformation=8)` | No | Semi-documented ("subject to change", winternl.h partial layout: DpcTime/InterruptTime undocumented via `Reserved1[]` — btop4win reads them anyway); stable in practice since NT; Task Manager's source (REPORTED). psutil's discipline required: size from `GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)`, **recompute count from ReturnLength, never walk past it** | winternl.h + Geoff Chappell (VERIFIED) |
| CPU clock (live MHz) | `CallNtPowerInformation(ProcessorInformation)` | No | Documented level; psutil's `cpu_freq` implementation (VERIFIED) | MS Learn (VERIFIED) |
| RAM / commit | `GlobalMemoryStatusEx`; `GetPerformanceInfo` for cached/commit split | No | Documented; ours is fine | MS Learn (VERIFIED) |
| Disk rates | `IOCTL_DISK_PERFORMANCE` (0x70020) on `\\.\C:` or `\\.\PhysicalDriveN`, access-0 handle — or PDH `\LogicalDisk(X:)\Disk Read Bytes/sec` | No | Both documented; the IOCTL *itself* enables collection (no `diskperf -y` needed — the DDI page says the IOCTL increments the collection reference; `diskperf` only toggles boot-time counter drivers). PDH needs `PdhAddEnglishCounter` for locale safety, 2 samples for rates | MS Learn + local SDK (VERIFIED) |
| Disk identity | `IOCTL_STORAGE_QUERY_PROPERTY` (StorageDeviceProperty) | No | `FILE_ANY_ACCESS` (0x2d1400) — works on access-0 handles unelevated | MS Learn + local SDK (VERIFIED) |
| NVMe health/temp | `IOCTL_STORAGE_QUERY_PROPERTY` with `StorageAdapterProtocolSpecificProperty` + `ProtocolTypeNvme` | No | LHM's entire driverless NVMe path (`NVMeWindows.cs`) — VERIFIED user-mode-only | LHM source (VERIFIED) |
| SATA SMART attributes | `SMART_RCV_DRIVE_DATA` (0x7C088, R\|W access) / `IOCTL_ATA_PASS_THROUGH` (R\|W) | **Yes** | Write-access handle → admin (CreateFile "Physical Disks and Volumes" ACL). Unelevated tier: `IOCTL_STORAGE_PREDICT_FAILURE` returns SMART attribute bytes in VendorSpecific (smartmontools' `'f'` fallback — "works without admin rights", VERIFIED) + `SMART_RCV_DRIVE_DATA_EX` (0x7008C, **FILE_ANY_ACCESS**, Win10+, guarded NTDDI_WIN10_CO — unelevated behavior untested locally: worth an experiment) | MS Learn + local SDK + smartmontools source (VERIFIED) |
| GPU engine util (any vendor) | `D3DKMTQueryStatistics` via **gdi32.dll** (already loaded) — or "GPU Engine" PDH counters | No | Exported user-mode, "Reserved for system use" with **undocumented structs** (version-drifted); the data source Task Manager's GPU tab shows (WDDM 2.0+; Microsoft blog VERIFIED for the tab's provenance, community-VERIFIED for the API identification). Must be subprocess-isolated + fail-closed | MS Learn + LHM/posegraph/sMkTaskManager (VERIFIED code) |
| GPU temp/fan/power (NVIDIA) | nvidia-smi subprocess (ours) or `nvml.dll` (ships in System32 with every DCH driver — machine-verified present next to nvidia-smi.exe) | No | Both fine; per-process GPU metrics NOT available on Windows/WDDM (NVIDIA-documented) | NVIDIA docs (VERIFIED) |
| GPU temp/fan/power (AMD) | `atiadlxx.dll` (driver-shipped, System32) — ADL2 API | No | Works unelevated, but open AV precedent (LHM #736) + GC-pinned allocator-callback footgun → child process only, if ever | LHM source (VERIFIED) |
| GPU temp/fan/power (Intel) | IGCL `ControlLib.dll` (driver-shipped, System32), size-versioned structs | No | What LHM converged on; Level Zero on Windows is partial (no temp/fan/freq) and needs a bundled loader → rejected | LHM + lablup/all-smi (VERIFIED/REPORTED) |
| CPU/board temp | `MSAcpi_ThermalZoneTemperature` (root\wmi) | **Usually yes** | Unelevated = Access denied (community-consistent, REPORTED; our PowerShell runs unelevated → most machines return nothing, which is why None-is-honest). Firmware zone, often absent/stale. **No Microsoft-sanctioned user-mode temperature API exists** — `Windows.Devices.Sensors` has no thermal class (VERIFIED against the full class list), `CallNtPowerInformation` has no temperature level (VERIFIED) | MS Learn (VERIFIED absence) |
| Battery | `GetSystemPowerStatus` | No | Documented, trivial — psutil's `sensors_battery` (VERIFIED) | MS Learn |

Key structural fact behind "None is honest": **even psutil refuses
CPU temperature on Windows** — `sensors_temperatures` doesn't exist
there (Linux/FreeBSD only); maintainer on #1271: *"We can't use
external DLLs"*; the community CoreTemp/OHM patches (#1952) all sit on
kernel drivers and died unmerged over 15+ years. btop4win is the
counter-example that ships the driver route and it gates it exactly
like we would: no sensors found → boxes permanently disabled for the
session.

---

## 4. GPU vendor lanes (detail)

### 4.1 NVIDIA — keep the subprocess. Really.

The in-process alternatives have documented native-AV crash classes in
LibreHardwareMonitor's own tracker:

- **Stale device handle → uncatchable 0xC0000005** (LHM #2523): handle
  cached at discovery; GPU reset/removed → AV inside nvml.dll;
  *"the try/catch … cannot intercept the native fault."* Fixed only by
  re-acquiring the handle before every read (PR #2255).
- **Driver update mid-session** (#2378): same class — the DLL stays
  mapped but the driver state behind it is replaced; in-process calls
  AV until re-init. *Our subprocess never sees this: a driver update
  turns into a non-zero exit + 60 s backoff.*
- **Shutdown races**: NVML query-thread vs `nvmlShutdown()` SIGSEGV
  (REPORTED, GPU failure atlas); `nvmlShutdown` from signal
  handlers/atexit deadlocks (NVIDIA forum). Our child process dying at
  exit is harmless by construction.
- LHM needs **two** NVIDIA DLLs (nvapi.dll + nvml.dll) to match what
  one `nvidia-smi --query-gpu` line gives us.

`nvml.dll` presence is machine-verified (System32, 1.5 MB, same driver
date as nvidia-smi.exe, stock RTX 5080 DCH install), so an in-process
lane *exists* — the research verdict is just that it buys ~80 ms per
call at the cost of a documented AV class. Not worth it. pynvml is
BSD-3 if we ever want the declarations as a reference.

### 4.2 AMD / Intel — the zero-new-DLL answer is D3DKMT

`D3DKMTQueryStatistics` is a **user-mode export of gdi32.dll** (loaded
in every GUI process already). Mechanics, verified across four
codebases (LHM `D3DDisplayDevice.cs`, sMkTaskManager `APIsD3Dkmt.cs`,
Mixaill.HwInfo, and the pure-ctypes Python reference
`extrant/Posegraph backend/src/posegraph/gpu.py`):

- Query types: Adapter=0 / Segment=3 / Node=5 (+NODE2=18…); VRAM =
  per-segment `Limit/Committed/Resident` (skip aperture segments);
  utilization = per-node `RunningTime` deltas ÷ wall-clock — exactly
  Task Manager's per-engine "3D / VideoDecode / Copy" percentages.
- Works unelevated (Task Manager does it from a standard user token).
- The structs are undocumented and version-drifted → copy posegraph's
  **fail-closed** pattern: probe the layout at runtime, cross-validate
  segment totals against DXGI's `DedicatedVideoMemory`, degrade to
  None on any mismatch. Never fixed offsets in the GUI process —
  run it in a **short-lived child** (our PowerShell/nvidia-smi
  pattern), JSON over stdout, per-source backoff on garbage.
- Vendor temps (AMD `atiadlxx.dll`, Intel IGCL `ControlLib.dll`) are
  driver-shipped System32 DLLs and work unelevated, but belong in the
  same child process at most (LHM #736 is an *open*, unfixable-looking
  ADL AV; the ADL2 allocator callback must be GC-pinned for the
  context's lifetime or native code calls a dead thunk).

### 4.3 Recommendation matrix

| Vendor | Safest lane under our doctrine | What it yields |
|---|---|---|
| NVIDIA | **nvidia-smi subprocess (status quo)** | util, VRAM, temp, fan, power, PCIe |
| AMD | D3DKMT subprocess (+ optional ADL2 in the same child) | util + VRAM now; temp/fan/power optional |
| Intel | D3DKMT subprocess (+ optional IGCL in the same child) | util + VRAM (iGPU and Arc); temp/fan/power optional |
| Any | "GPU Engine" PDH counters (documented machinery, community-documented object) | util, no vendor code at all |

---

## 5. Zero-driver channels into other trusted monitors (the only true CPU-core temps)

For users who *want* real CPU core temperatures, the driverless route
is to read a monitor they already run:

- **LibreHardwareMonitor HTTP server** (`HttpServer.cs`, VERIFIED):
  `/data.json` returns the full sensor tree; optional basic auth;
  listener survives errors with a 5 s retry. A plain
  `requests.get("http://127.0.0.1:8085/data.json")` gives us every
  sensor with zero native code.
- **OpenHardwareMonitor WMI** (`WmiProvider.cs`, VERIFIED):
  `root\OpenHardwareMonitor` namespace, `SELECT * FROM Sensor` —
  pollable via our existing PowerShell one-shot lane, no new code
  surface at all.
- **HWiNFO shared memory** (official SDK, commercial): section
  `Global\HWiNFO_SENS_SM2`, mutex-guarded, packed header + 128-byte
  labels + per-reading min/max/avg; clean-room layout documented
  (namazso's gist). Caveat REPORTED: the free tier stops publishing
  after ~12 h.

Design sketch if ever wanted: an *opt-in* setting ("External sensor
bridge: LHM / HWiNFO / OHM, auto-detect at 127.0.0.1"), samples merged
into the tab as a distinct provenance ("via LHM") so users never
confuse it with our native readings; the bridge source hard-disables
for the session on garbage (doctrine rule 3/8). Not part of P0–P1 —
documented here because the research kept surfacing it as *the* answer
to "CPU temps without a driver".

---

## 6. Anti-patterns, with the receipts

### 6.1 Never ship a kernel driver

WinRing0 (the driver every OHM/LHM-class monitor used for CPU temps,
MSRs, fan control) is an abandoned upstream with an unpatchable
LPE (**CVE-2020-14979**, CVSS 7.8 — `IoCreateDevice` without a secure
DACL). Microsoft added it to the Vulnerable Driver Blocklist; Defender
flags it as `HackTool:Win32/WinRing0` / `VulnerableDriver:WinNT/
WinRing0.G`, breaking installs years running (LHM #1660, #1839 —
threads quote the detection names). Escaping it took a community
member writing a new attestation-signed modular driver (PawnIO) and
the project shipping it in v0.9.5 (2026-01-07) — three years after the
fix was proven to "just work", because driver signing needs an EV
certificate + HLK passes a small FOSS project can't afford. Our app
loads **zero** drivers and should stay that way.

### 6.2 Never load vendor DLLs into the GUI process

Same section of LHM's tracker: NVML stale-handle AVs (#2523, #2378),
an *open* AMD ADL AV on RX 6800 (#736 — `ADL2_Adapter_FrameMetrics_Get`,
process-fatal, repeatable), missing-entry-point crashes on old drivers
(#664). LHM's hardening where they succeeded (init-once under lock,
GetProcAddress existence probes, GC-pinned delegates, size-versioned
IGCL structs, fresh handles per poll) is a *mitigation* list, not a
cure — a child process is the cure, and we already have the pattern.

### 6.3 Don't blanket-retry native calls, don't trust layouts

psutil retries exactly one native error class
(`ERROR_PARTIAL_COPY`, 33×/~1 s budget); everything else fails
immediately to a typed error. D3DKMT-style undocumented structs:
posegraph's offset probing + DXGI cross-validation or nothing. And
never walk past a returned length — psutil's per-core CPU code
recomputes the entry count from `ReturnLength` because the kernel may
return fewer entries than requested (>64-CPU processor groups;
btop4win gets this wrong by passing NULL and trusting
`GetSystemInfo`).

### 6.4 Keep MSAcpi expectations honest

Admin-gated in practice, firmware-zone semantics (often board-level,
sometimes absent even elevated), no sanctioned alternative (§3).
Our current behavior — unelevated probe, None honest, warn never on
None — is exactly right. Do not "fix" it into a requirement.

---

## 7.1 Implementation record (same day, 2026-11-19)

Everything in P0 + P1 shipped in one pass, each path SDK-verified and
LIVE-PROBED on the dev machine (9900K / RTX 5080 / 3× NVMe / BE200 +
AC 9260 Wi-Fi) before the production parser was written:

- **P0-1** — `cpu_percent` clamps negative deltas (max(0,·) before the
  ratio); per-core does the same. Regression test pins a backwards step
  reads 0.0.
- **P0-2** — `read_volume_counters` now routes through
  `_query_disk_performance(handle, deviceio)`: ERROR_INSUFFICIENT_BUFFER
  (122)/MORE_DATA (234) doubles the buffer up to 1 MiB (psutil's retry),
  ERROR_INVALID_FUNCTION (1)/NOT_SUPPORTED (50) logs "exotic disk" and
  cold-marks, a short write (returned < sizeof) is distrusted.
- **P0-3** — `_gpu_block` runs every reading through `_clamp(v, lo, hi)`
  (util 0–100, temp 0–120, fan 0–100, power 0–2000, PCIe sane, VRAM
  used clamped to total). Test pins the "54000 °C" class → None.
- **P1-1 network** — `network_snapshot()`: GetIfTable2 on Windows,
  /proc/net/dev on POSIX. The MIB_IF_ROW2 parse is RAW-BYTE at pinned
  offsets (stride 1352 — a ctypes Structure that drops one tail field
  silently shifts every later row; that exact bug was caught live in a
  probe), rows read ONE AT A TIME bounded to the row size, decode the
  full description field before splitting on the NUL char (byte-level
  splitting corrupted a name tail and broke the mirror filter — also
  caught live). Admission: OperStatus up AND (HardwareInterface bit OR
  live traffic), minus name-prefix mirrors of hardware adapters and
  "Miniport" rows — live-verified to yield exactly the two real Wi-Fi
  adapters with live B/s. Counter decrease = replug → that window's
  speeds read None. 60 s failure backoff.
- **P1-2 per-core** — `hardware_monitor.per_core_percent()`: NtQuery-
  SystemInformation(class 8) with EXACT count×stride buffers (an
  oversized non-multiple length FAILS with C0000004 — probed: 816 ok,
  776/1024 fail), stride 48 with a 40-byte fallback, retlen must equal
  the request, hard failure disables per-core for the process.
  Baselines seed through seed_cpu_baseline so the first on-demand
  snapshot is real. Tab renders a painted CoreStrip (≤32 bars) that
  HIDES when the source is absent. The live CLOCK half of P1-2 was
  DROPPED after probing: ACPI CurrentMhz stays pinned at base (3600 on
  the 9900K) even under 4-thread load — displaying it as "live" would
  violate the honesty rule.
- **P1-3 drives** — `drive_health_snapshot()`: NVMe health via
  IOCTL_STORAGE_QUERY_PROPERTY + STORAGE_PROTOCOL_SPECIFIC_DATA
  (SDK-verified field order — note ProtocolDataOffset comes BEFORE
  ProtocolDataLength, unlike the common mis-transcription) parsed from
  the log at correct NVMe-spec offsets (temp@1 u16 Kelvin, spare@3,
  used@5, unsafe shutdowns@144); SATA via IOCTL_STORAGE_PREDICT_FAILURE
  (SMART attr 194/190 raw temps; NVMe/USB answer INVALID_FUNCTION and
  show nothing). Access-0 handles, unelevated — live-verified: three
  NVMe drives report 33–41 °C, spare, % used. TTL cache 30 s / fail
  backoff 300 s serving the last good reading; model names join the
  CACHED inventory's new DeviceId field (`inventory(cached_only=True)`
  NEVER spawns PowerShell). Warnings: `disk_temp` (new user-only
  threshold temp_warn_disk_c=60) and `disk_health` (danger on SMART
  failure prediction / NVMe critical warning, warn on ≥90% used or
  ≤10% spare). NOT validated against a real SATA drive (none on the
  dev machine — NVMe answers INVALID_FUNCTION as expected); the SATA
  parser is fail-closed and unit-tested against a crafted SMART block;
  field reports from SATA machines will confirm.
- **P1-4 battery** — GetSystemPowerStatus / /sys/class/power_supply;
  desktops (BatteryFlag 0x80) read None and the chip hides. Verified
  live (desktop) + parser tests.
- Snapshot now carries cpu.per_core, network, drives, battery; the
  agent tool passes them through automatically; summary_line appends
  battery. All new sources are individually try/excepted in snapshot()
  (failure → empty/None, never raises).

Verification: tests/test_system_health.py 57 tests (30 old + 27 new);
the full nine-chunk release gate GREEN (1,481 tests); offscreen
real-MainWindow smoke OK (16 per-core bars, both Wi-Fi rows with live
throughput, three NVMe drive rows, battery hidden, clean close).

## 7. Ranked recommendations

Effort: S = hours, M = a day or two, L = multi-day. All entries keep
the tab-gated sampling contract (never an always-on thread).

### P0 — hardening of what we have (do regardless)

| # | Change | Why | Effort |
|---|---|---|---|
| P0-1 | Clamp negative deltas in `hardware_monitor.cpu_percent` (`d_idle`/`d_busy` via `max(0, …)` before the ratio; the `total <= 0` guard stays) + regression test | Windows counters go backwards (psutil #392/#645/#1210; htop `saturatingSub`; btop4win `max(0ll,…)`); today a backwards step can render a negative CPU% | S |
| P0-2 | In `read_volume_counters`, distinguish GetLastError after a failed `DeviceIoControl`: `ERROR_INSUFFICIENT_BUFFER` → retry with a doubled buffer up to 1 MB (psutil's exact loop — "drivers may return a bigger struct"); `ERROR_INVALID_FUNCTION`/`ERROR_NOT_SUPPORTED` → cold-mark + debug log ("exotic disk"); anything else → cold-mark | Turns silent cold-marking into a diagnosable ladder; costs nothing when the IOCTL works | S |
| P0-3 | Add plausibility clamps to every *displayed* value we don't already range-check (GPU %, VRAM, power, fan, disk B/s upper bound e.g. 100 GB/s) — LHM's "54000 °C" rule | Doctrine rule 4; also protects `warnings_from` from garbage | S |

### P1 — new capabilities, all documented APIs, GUI-process-safe

| # | Change | Design (references) | Effort |
|---|---|---|---|
| P1-1 | **Network throughput meters** (↓/↑ per interface, next to the disk rows) | Windows: `GetIfTable2`/`GetIfEntry2` (iphlpapi, documented) cumulative-octet deltas — the same two-sample pattern as our disk counters (verify struct locally first); fallback lane: PDH `\Network Interface(*)\Bytes Received/sec` via `PdhAddEnglishCounter` (locale-safe, verified documented). Linux: `/proc/net/dev` deltas (htop's source, trivially safe). Backoff/cold-mark per interface | M |
| P1-2 | **Per-core CPU% + live clock** | `NtQuerySystemInformation(SystemProcessorPerformanceInformation)` with psutil's exact discipline: buffer sized from `GetActiveProcessorCount(ALL_PROCESSOR_GROUPS)`, **count recomputed from ReturnLength**, never walk past it, per-field `max(0, delta)`; clock via `CallNtPowerInformation(ProcessorInformation)` (psutil `cpu_freq`). UI: small per-core bar strip under the CPU meter. Semi-documented API — acceptable *because* the read is a plain ntdll syscall with validated length, no drivers, no DLL loads | M |
| P1-3 | **Disk health + temperature (NVMe first)** | NVMe: `IOCTL_STORAGE_QUERY_PROPERTY(StorageAdapterProtocolSpecificProperty, ProtocolTypeNvme)` health log — LHM `NVMeWindows.cs` is a VERIFIED driverless, unelevated, user-mode-only implementation (MPL-2.0: port the shape, not the file). SATA: `IOCTL_STORAGE_PREDICT_FAILURE` (smartmontools' documented no-admin tier — SMART attribute bytes incl. temperature attributes 190/194 in VendorSpecific); optionally experiment with `SMART_RCV_DRIVE_DATA_EX` (0x7008C, FILE_ANY_ACCESS, Win10+) locally before trusting it. Surfaces as a new "Disk health" column + a `disk_temp` warning kind; parse fails closed to None | M–L |
| P1-4 | **Battery meter** (laptops) | `GetSystemPowerStatus` — documented, trivial, psutil's `sensors_battery` verbatim shape; AC/charging state + %; hide on desktops (BatteryFlag 128 = no battery) | S |

### P2 — optional, subprocess-gated

| # | Change | Design | Effort |
|---|---|---|---|
| P2-1 | **Any-vendor GPU utilization + VRAM** (AMD/Intel gap) | One short-lived `gpu-query` child process (the PowerShell/nvidia-smi pattern) doing D3DKMT via ctypes on gdi32: posegraph's fail-closed layout probing + DXGI cross-validation, JSON to stdout, per-source backoff on garbage; merge with the nvidia-smi lane in `_gpu_block`. GUI process stays clean | M–L |
| P2-2 | Vendor GPU temps (AMD/Intel) | Same child, ADL2 (`atiadlxx.dll`, GC-pinned allocator, `ADL_Method_Exists` probes) / IGCL (`ControlLib.dll`, size-versioned structs) — only after P2-1 proves the child-process lane | L |
| P2-3 | **External sensor bridge** (opt-in) | Detect LHM `/data.json` or OHM WMI or HWiNFO shared memory at 127.0.0.1; merge readings with explicit "via <monitor>" provenance; hard-disable for the session on parse garbage. The only route to true CPU core temps driverless (§5) | M |
| P2-4 | PDH fallback lane for disk rates | Only if field reports show cold volumes on machines where PDH works — same counters, documented route, locale-safe counter add | M |

### Non-goals (explicit)

- No psutil dependency (§1 — structural immunity + frozen-build size/
  Defender heuristics).
- No kernel driver, ever (§6.1). No vendor DLL in the GUI process
  (§6.2). No always-on sampler thread (owner contract, 2026-11-18).
- No making CPU temp a *requirement* — absence stays honest (§6.4).

---

## 8. Licenses — what we may take code references from

| Project | License | Verdict for us |
|---|---|---|
| psutil | BSD-3-Clause | Pattern/constant copying fine; verbatim code needs their header retained |
| btop4win / btop | Apache-2.0 | Pattern copying fine; verbatim code needs NOTICE attribution |
| CrystalDiskInfo | MIT | Copy with attribution — the proof the whole disk-SMART domain is driverless |
| LibreHardwareMonitor | MPL-2.0 | File-level copyleft: study freely, port *shapes* (struct layouts, call sequences), never copy files wholesale |
| openhardwaremonitor master | MPL 2.0 headers (old releases GPLv2 — avoid pre-2015 code) | Same |
| smartmontools | GPL-2.0 | **No code copying** — porting documented Windows IOCTL semantics (kernel API, not expression) is fine |
| glances | LGPL-3.0-only | No code copying; architecture ideas only |
| htop | GPL-2.0 | No code copying; `saturatingSub` idea is generic |
| HWiNFO SDK | Proprietary (registered users) | Read-only consumption of the published shared-memory interface is its intended use; don't copy GPL'd decoders |
| posegraph (D3DKMT ctypes) | (check on adoption) | Reference implementation for P2-1; verify license before importing code |

---

## 9. Sources (primary)

- LibreHardwareMonitor: [repo](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) · Ring0.cs @ v0.9.3 · PawnIo/PawnIo.cs @ master · Interop/{NvApi,NvidiaML,AtiAdlxx,IntelGcl}.cs · Hardware/Gpu/AmdGpu.cs · Hardware/Storage/{WindowsSmart,NVMeWindows,StorageDevice}.cs · UI/UpdateVisitor.cs · HttpServer.cs · issues [#984](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/issues/984), [#1660](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/issues/1660), [#1839](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/issues/1839), [#2523](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/issues/2523), [#2378](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/issues/2378), [#736](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/issues/736), [#664](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/issues/664), [PR #2255](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/pull/2255) · [Defender threat page](https://support.microsoft.com/en-us/windows/security/threat-malware-protection/microsoft-defender-antivirus-alert-vulnerabledriver-winnt-winring0)
- psutil: [arch/windows/cpu.c](https://raw.githubusercontent.com/giampaolo/psutil/master/psutil/arch/windows/cpu.c) · [arch/windows/disk.c](https://raw.githubusercontent.com/giampaolo/psutil/master/psutil/arch/windows/disk.c) · [__init__.py](https://raw.githubusercontent.com/giampaolo/psutil/master/psutil/__init__.py) · issues [#1271](https://github.com/giampaolo/psutil/issues/1271), [#1952](https://github.com/giampaolo/psutil/issues/1952), [#3009](https://github.com/giampaolo/psutil/issues/3009), [#3002](https://github.com/giampaolo/psutil/issues/3002)
- btop4win: [src/btop_collect.cpp](https://raw.githubusercontent.com/aristocratos/btop4win/master/src/btop_collect.cpp) · [src/btop.cpp](https://raw.githubusercontent.com/aristocratos/btop4win/master/src/btop.cpp)
- glances v4.5.9: plugins/plugin/model.py · stats.py · standalone.py · plugins/diskio/__init__.py
- htop: [linux/LinuxMachine.c](https://raw.githubusercontent.com/htop-dev/htop/main/linux/LinuxMachine.c)
- smartmontools: [os_win32.cpp](https://github.com/smartmontools/smartmontools) (capability ladder)
- CrystalDiskInfo: [repo](https://github.com/hiyohiyo/CrystalDiskInfo) (SPTI/driverless SMART)
- posegraph D3DKMT ctypes: [backend/src/posegraph/gpu.py](https://github.com/extrant/Posegraph/blob/main/backend/src/posegraph/gpu.py) · sMkTaskManager [APIsD3Dkmt.cs](https://github.com/sMkTools/sMkTaskManager/blob/master/src/Classes/APIsD3Dkmt.cs)
- Microsoft Learn: [GetSystemTimes](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getsystemtimes) · [NtQuerySystemInformation](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntquerysysteminformation) · [Geoff Chappell: processor performance sysinfo](https://www.geoffchappell.com/studies/windows/km/ntoskrnl/api/ex/sysinfo/processor_performance.htm) · [IOCTL_DISK_PERFORMANCE (user)](https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-ioctl_disk_performance) / [(DDI)](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntdddisk/ni-ntdddisk-ioctl_disk_performance) · [DISK_PERFORMANCE](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntdddisk/ns-ntdddisk-_disk_performance) · [CreateFile (physical disks/dwDesiredAccess=0)](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilea) · [PdhAddEnglishCounter](https://learn.microsoft.com/en-us/windows/win32/api/pdh/nf-pdh-pdhaddenglishcountera) · [IOCTL_STORAGE_QUERY_PROPERTY](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddstor/ni-ntddstor-ioctl_storage_query_property) · [D3DKMTQueryStatistics](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/d3dkmthk/nf-d3dkmthk-d3dkmtquerystatistics) · [Windows.Devices.Sensors](https://learn.microsoft.com/en-us/uwp/api/windows.devices.sensors) · [CallNtPowerInformation](https://learn.microsoft.com/en-us/windows/win32/api/powerbase/nf-powerbase-callntpowerinformation) · [GPUs in the Task Manager (DirectX blog)](https://devblogs.microsoft.com/directx/gpus-in-the-task-manager/)
- NVIDIA: [nvidia-smi docs](https://docs.nvidia.com/deploy/nvidia-smi/index.html) (WDDM per-process caveat) · [NVML R550](https://docs.nvidia.com/deploy/archive/R550/nvml-api/group__nvmlDeviceQueries.html) · [pynvml](https://github.com/gpuopenanalytics/pynvml)
- HWiNFO: [SDK](https://www.hwinfo.com/sdk) · namazso's clean-room [shared-memory layout gist](https://gist.github.com/namazso/0c37be5a53863954c8c8279f66cfb1cc)

Local verifications on this machine (2026-11-19): IOCTL constants and
`SMART_RCV_DRIVE_DATA_EX` read from
`C:\Program Files (x86)\Windows Kits\10\Include\10.0.26100.0\`
(winioctl.h:9077/9099, ntddscsi.h:89-90, ntdddisk.h:86); nvml.dll
present in System32 alongside nvidia-smi.exe (stock RTX 5080 driver).
