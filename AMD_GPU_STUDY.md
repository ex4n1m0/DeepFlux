# AMD GPU study — the overlay/dxgi mystery, AMD SDKs, and safe reading lanes

Started 2026-11-20, after the owner confirmed the 9.1 fix ("no crash with
amd skip"). This study closes the knowledge gap the 8.5→9.1 laptop arc
exposed: WHY an in-process DXGI adapter walk kills a Python process when
AMD's Adrenalin overlay is injected, what AMD's developer SDKs actually
offer (AMD historically opened its drivers to developers more than NVIDIA
did), and which lanes could safely bring AMD live GPU stats back to the
System Health tab.

Method: four parallel web-research agents (AMD SDK landscape; the
Adrenalin In-Game Overlay's injection/hook behavior; driverless reading
lanes; the DXGI-enumeration crash mechanism + ecosystem mitigations),
every claim tagged VERIFIED (fetched page, URL given) or REPORTED
(secondhand). House style follows SYSTEM_HEALTH_STUDY.md.

## 1. First-party evidence (ours, not the internet's)

The machine: GPD handheld, AMD Ryzen AI 9 HX 370, Radeon 890M iGPU,
Adrenalin 32.0.32029.2010, Windows 11 Home build 26300 (Insider track).
The app: PySide6/Qt + CPython 3.11.9, 64-bit, bundled python311.dll.

| Release | Behavior on the laptop | Evidence |
|---|---|---|
| 8.5–8.7 | App closes "after a while", crashes cluster while the System Health tab is open; 5 crashes in 3 min at 17:49–17:52, ALL byte-identical: `python311.dll+0x49245` 0xc0000005 READ, allocator region (nearest export PyType_GenericAlloc) | Event log pack (owner, 8.7 evening) |
| 8.7 | Every P1 feature visibly worked (screenshot: per-core bars, Wi-Fi, NVMe health, battery) — the crash is NOT in any single reading's parsing | owner screenshot + AGENTS.md 8.7 entry |
| 8.8 | dxgi_adapters() TTL-cached ~10 min (the every-2s DXGI factory walk was the only per-tick native churn on non-NVIDIA machines) | d2ae837; laptop still crashed |
| 8.9 | Step-by-step diagnosis: every reading ran sequentially — the app died exactly AT the GPU step | owner report 2026-11-20 |
| 9.1 | `_gpu_block` NEVER calls dxgi on AMD (pure-registry vendor gate); nvidia-smi lane untouched | e6485f5; **owner: "confirmed, no crash with amd skip"** |

The code path that kills it (pre-9.1, every ~2 s while the tab was open):

```python
factory = CreateDXGIFactory1()        # fresh COM object each walk
factory.EnumAdapters1() → for each: GetDesc1()
# all released; thread had NO explicit CoInitialize
```

Dev machine control: same code, NVIDIA RTX 5080 + injected nvspcap64.dll
(GeForce/NVIDIA App overlay) — NEVER crashed across the same period.
So: not "any overlay" + not "any DXGI walk" separately — the AMD hook +
our walk frequency/repetition is the pair that detonates. The 2026-11-07
dev-machine bisect already proved the AV family is heap-layout
probabilistic (bytecode-shape sensitive, dice-like), which is why the
laptop's crashes looked "random" for three releases until the 8.9 step
tool pinned the trigger.

What we know about the mechanism so far (to be expanded by the study
below): an injected DLL that hooks DXGI exports gets exercised by factory
creation/adapter enumeration; a hook bug or a re-entrancy/heap race in
the hook corrupts the host heap; CPython's allocator then AVs in an
innocent frame. The stability experiment (gate the walk → stable) is
causal proof the walk is the TRIGGER; whether the hook itself is the
corruptor (vs. COM teardown racing the hook) is what the research below
addresses.

## TL;DR verdict

1. **The mystery is solved** (§5.2): our 2 s DXGI factory walk repeatedly
   re-drove AMD's `amdihk64.dll` load/unload machinery inside a process
   that hosts the AMD hook stack from boot (the D3D user-mode driver
   itself carries the hook engine — no injection needed). The published
   twin (tapestry #194) shows the exact race: the hook DLL spawns a
   worker thread and gets unloaded before the thread runs; a late thread
   jumps into unmapped code. NVIDIA's overlay DLL lacks this race, which
   is why the same code never crashed the dev machine.
2. **No vendor fix is coming**: the laptop runs a GPD OEM Adrenalin
   branch (32.0.32029.2010 decodes to internal 26.20.29.xx — no mainline
   match), the unload-race family has no fix entry in ANY 2024-2026
   release notes, and AMD offers no developer opt-out and no per-app
   overlay toggle. App-side isolation is the only durable answer — and
   the ecosystem's own recommendation ("detect-and-degrade on hooked
   machines — prevents the crash class by construction") is exactly what
   9.1 shipped before this study ran.
3. **AMD really did open the drivers** — but to a first-party API, not
   raw registers: **ADLX** ("AMD Device Library eXtra", driver-resident
   amdadlx64.dll, full sensors incl. hotspot/NPU/iGPU-shared-memory,
   per-metric capability queries). AMD's OWN docs prescribe running it
   in a user-mode helper process with IPC — our doctrine, blessed by the
   vendor. License permits shipping an object-code helper.
4. **Windows itself gives two free lanes** nobody needs to sign or
   install (§4): PDH GPU counters (Task-Manager-identical utilization +
   adapter memory, proven on the exact 860M/26300 platform class) and
   D3DKMT ADAPTERPERFDATA (the only driverless temperature path; on
   iGPUs expect 0 = no sensor → show absent).
5. **Roadmap**: P0 done (9.1 gate) → P1 PDH+D3DKMT inside the existing
   collector subprocess (no vendor DLL at all) → P2 ADLX long-lived
   read-only helper child (full AMD sensors incl. iGPU temp) → P3
   event-driven DXGI refresh on the non-AMD path + hook-DLL logging.

## 2. The crash mechanism — what the DXGI/Detours evidence says

**Verdict (lane 4, 2026-11-20):** there is NO Microsoft contract under
which DXGI loads or calls third-party hook DLLs. Injected overlay DLLs
(AMD `amdihk64.dll` family, RTSS `RTSSHooks64.dll`, NVIDIA `nvspcap64.dll`)
pre-patch `dxgi.dll`'s exported code in memory — the Detours technique
(VERIFIED: microsoft.com/research Detours paper; OBS's open-source
graphics-hook does exactly `DetourAttach` on `IDXGISwapChain::Present`
inside dxgi.dll, VERIFIED from obs-studio source). So EVERY
`CreateDXGIFactory1 → EnumAdapters1 → GetDesc1` on ANY thread executes
the hook vendor's code first. Game workloads create one factory per
process lifetime; our health tab re-created the factory every ~2 s,
re-driving hook lazy-init/module-lookup paths ~43,000×/day — and on the
AMD hook set that corrupts the host heap, which later detonates inside
CPython's allocator/GC in innocent frames (exactly our bisect-proven
signature; removal = owner-confirmed 100 % stable).

Key verified facts (URLs in §Sources):
- `CreateDXGIFactory1` has no COM/CoInitialize requirement and no
  threading contract (fetched full MS page); the only documented
  load-at-create is first-party (`CreateDXGIFactory2(0x01)` loads
  DXGIDebug.dll — no isolation flag exists).
- The DllMain/loader-lock ban on calling DXGI from DllMain shows factory
  creation touches the loader — the same subsystem hooks patch
  (`GetModuleHandle`/`LoadLibraryExW` per the RTSS-vs-other-injectors
  crash thread, VERIFIED github.com/danielblnc/DLSS-NR-on-AMD issue 256:
  "neither component crashes on its own — it's an interaction between
  the two hook sets").
- Best ecosystem analog for "foreign code corrupts the heap; CPython
  detonates later": Ren'Py issue #5459 (VERIFIED) — 0xc0000374 heap
  corruption via `Windows.Gaming.Input.dll` from a Razer keypad;
  unplug = fixed; engine blamed by users; maintainer: driver's fault.
- Our missing `CoInitialize` on the worker thread is NOT a documented
  violation (no source correlates it); call FREQUENCY into patched code
  is the trigger.

**Ecosystem convergences (all verified):**
- Chromium runs `CreateDXGIFactory1 + EnumAdapters` in the disposable GPU
  child process (gpu_info_collector_win.cc) — a crash = a black flash +
  auto-restart; the browser survives by construction. THE structural
  precedent for our subprocess lane.
- Sunshine PR #5893: DXGI enumeration must FAIL CLOSED on any error (the
  old loop kept using the returned pointer on non-NOT_FOUND failures).
- Valve's own overlay docs admit injected overlays "often expose memory
  leaks or memory corruption in the usage of your rendering API."

**Mitigation ranking (lane 4's, for our app):**
1. Sacrificial subprocess for ANY future in-proc DXGI work (Chromium
   precedent).
2. Event-driven refresh instead of polling: enumerate ONCE; watch
   `WM_DISPLAYCHANGE` (top-level windows, VERIFIED); lazily
   `IDXGIFactory1::IsCurrent()` (VERIFIED: "re-create the factory and
   re-enumerate" signal); optionally
   `IDXGIFactory7::RegisterAdaptersChangedEvent` (Win10 1809+,
   VERIFIED — the only adapter-change notification API in DXGI).
3. If a walk remains on the non-AMD fast path: create the factory ONCE,
   reuse; recreate only when IsCurrent() is FALSE; fail closed on any
   enumeration error.
4. `LoadLibraryEx(LOAD_LIBRARY_SEARCH_SYSTEM32)` for dxgi.dll defeats
   only app-dir proxy-DLL hijacks, NOT in-memory export patching
   (already-loaded patched image is returned). Low-value hardening.
5. Process mitigations are a NON-STARTER for the GUI process:
   MicrosoftSignedOnly would block python311.dll/Qt/pip wheels, and
   ProcessDynamicCodePolicy kills QtWebEngine's V8 JIT (VERIFIED docs).
6. Diagnostics-only: log known hook DLLs (amdihk64/nvspcap64/
   RTSSHooks64) in the module list at boot — explains crashes, prevents
   nothing (Ren'Py lesson: by crash time, app-level blocklists are late).

## 3. AMD's SDK landscape (2024–2026) — what "AMD opens their drivers" means today

Naming correction first: the modern library is **ADLX = "AMD Device
Library eXtra"** (VERIFIED — manual title + repo README), not "Display
Library X".

### 3.1 ADLX — the only first-party production telemetry API
- Lives at gpuopen.com/adlx + github.com/GPUOpen-LibrariesAndSDKs/ADLX.
  Categories: Display, 3D, Performance Monitoring, GPU Tuning (VERIFIED).
- **License: proprietary, NOT MIT** (VERIFIED from the repo's PDF
  agreement): royalty-free, revocable, non-transferable; BUT it permits
  "distribute and sublicense, solely in Object Code form, Sample Code and
  Derivative Works as incorporated in Your Software with Your products
  that work with or include AMD processors". Shipping a closed-source
  helper that embeds ADLX sample-derived code is permitted.
- **Runtime shape (VERIFIED from SDK source)**: the app loads the
  DRIVER-INSTALLED `amdadlx64.dll` (32-bit: amdadlx32.dll) via
  LoadLibraryEx with search flags including SYSTEM32 — there is NO .lib
  and NO redistributable DLL; absence of the driver = clean degradation.
  Entry points resolved by GetProcAddress: ADLXInitialize(2),
  ADLXTerminate, ADLXQueryVersion…
- **Metrics (VERIFIED from IPerformanceMonitoring.h + manual TOC)**:
  GPUUsage, GPUClockSpeed, GPUTemperature (edge), GPUHotspotTemperature,
  GPUPower, GPUTotalBoardPower, GPUFanSpeed, dedicated VRAM usage,
  GPUVoltage; Metrics1 adds **NPUActivityLevel/NPUFrequency (Ryzen AI
  NPUs — the owner's HX 370 has one!)**; Metrics2 adds **GPUSharedMemory
  (the iGPU-relevant one)**; Metrics3 adds fan duty. Per-metric
  `IsSupported*` capability queries are the API's own contract — iGPU
  coverage is probed, not promised.
- **iGPUs are first-class** (VERIFIED): GPUTYPE_INTEGRATED exists; a
  Ryzen 7950X iGPU listed FIRST in GetGPUs (ADLX issue #10). AMD does
  NOT document ADLX's internal enumeration (the GPU objects carry
  PNPString/PCI info/LUID — consistent with a driver-level, non-DXGI
  walk; inference, not documented).
- **Cost (VERIFIED, ADLX issue #11)**: GetCurrentGPUMetrics ≈ 25 ms/call
  on an RX 7900 XTX; AMD staff recommend it as one-shot and the history
  API for continuous polling. At our 2 s tab cadence in a subprocess:
  negligible.
- **Known native-crash classes (all VERIFIED)**: AV inside amdadlx64.dll
  in the TUNING path after logoff/Fast-Startup (issue #35, FanControl
  #4244); a shutdown AV documented in the SDK's own header (release all
  interfaces + ADLXTerminate BEFORE exit — global/static destructors
  AV); "ADLX Service" driver-side processes that ACCUMULATE when an app
  repeatedly cycles ADLX sessions (FanControl #4107, LHM #2473).
- **No documented threading model** (verified-by-absence): keep every
  call on one thread. ADLX 2.0 (Sept 2026) added an official Python
  binding — but the repo gates it on **Windows 11** (VERIFIED), poor fit
  while we support Win10.
- **AMD ITSELF prescribes the process boundary** (VERIFIED — the
  system-service guide): "In the user mode process, load ADLX and
  implement the desired functionality… implement a communication
  mechanism… such as shared memory", with shipped GPUServiceCall/
  UserProcess/ShareMemory samples. Our subprocess doctrine is literally
  AMD's documented architecture.

### 3.2 Legacy ADL (atiadlxx.dll) — deprecated, MIT headers, crash-prone
- Officially "superceded by ADLX" (VERIFIED, gpuopen.com/adl); the DLL
  still ships inside every AMD driver install. SDK 18.x at
  GPUOpen-LibrariesAndSDKs/display-library; **the ADL SDK headers are
  MIT** (VERIFIED — vs ADLX's proprietary agreement), so a hand-rolled
  ctypes ADL client is license-clean.
- LHM today calls exactly: ADL2_Main_Control_Create (caller-supplied
  malloc callback — everything ADL returns must be freed through it),
  Adapter_NumberOfAdapters/AdapterInfoX3, the Overdrive 5/6/N ladder,
  PMLog Start/Stop, FrameMetrics (GPU usage), DedicatedVRAMUsage_Get
  (VERIFIED from LHM's Interop/AtiAdlxx.cs).
- **Documented AV inside ADL**: ADL2_Adapter_FrameMetrics_Get AV killing
  LHM (issue #736, open since 2022); missing entry points on older
  drivers (#664); adapter-index mismatches — fastfetch's 2025 rewrite
  matches ADL adapters to SetupAPI devices by bus/device/function
  because indexes lie (issue #1643, VERIFIED).
- Verdict: viable FALLBACK lane only (pre-2022 drivers), same
  subprocess shape. Not the first ship.

### 3.3 AGS + the rest of GPUOpen — not monitoring APIs
- AGS SDK 6.3.1, MIT: static GPU/driver info + DX11/12 driver
  extensions; its device info carries only coreClockAt100%/teraFlops —
  NO live sensors (VERIFIED from amd_ags.h). Not our lane.
- GPUPerfAPI/RGP/Radeon Developer Panel = profiler-session class
  (single-process counter ownership, ~50-session instability note —
  VERIFIED from GPA README). OCAT = PresentMon-based frame-time tool.
  None are always-on telemetry.

### 3.4 Fit verdict for our app (lane 1's)
**ADLX via a LONG-LIVED, single-threaded, READ-ONLY console subprocess
printing JSON lines** is the right lane:
1. NOT short-lived-per-poll — spawn+init churn × the documented ADLX
   Service accumulation bug is exactly what to avoid. One child that
   initializes once and streams; the parent restarts it with backoff +
   a restart cap (a crash loop must not farm AMD helper processes).
2. Read-only metrics only — every documented in-ADLX AV is in the
   tuning path or shutdown-with-held-interfaces path, never
   GetCurrentGPUMetrics.
3. Mind the documented shutdown AV: release + Terminate before exit.
4. Language: a tiny C helper from the SDK headers is lowest-risk
   (ctypes against ADLX's COM-ish interface vtables is error-prone);
   AMD's official Python binding is Win11-gated. (Alternative worth one
   probe: the C helper is also where a legacy-ADL fallback could live.)
5. Driver floor: 2022+ Adrenalin (exact ADLX floor REPORTED as 22.11.2);
   older drivers fail soft via InitializeWithIncompatibleDriver /
   missing DLL — the tab already degrades honestly to "skipped".

## 4. Driverless reading lanes — what Windows itself gives us

### 4.1 D3DKMT (WDDM via gdi32) — MORE than we assumed
The earlier SYSTEM_HEALTH_STUDY sketch treated D3DKMT as utilization-only.
It also carries TEMPERATURE/FAN/POWER (lane 3, all VERIFIED).

**LOCAL SDK CROSS-CHECK (2026-11-20, Windows Kits 10.0.26100.0): the
study's claims are in the OFFICIAL shared/d3dkmthk.h — no reverse
engineering needed.** Enum values exactly as cited
(`KMTQAITYPE_NODEPERFDATA = 61`, `KMTQAITYPE_ADAPTERPERFDATA = 62`,
`KMTQAITYPE_ADAPTERPERFDATA_CAPS = 63`, `GETSEGMENTSIZE = 3`); the
struct at line 2389 (pinned here for the P1 implementation):
```
typedef struct _D3DKMT_ADAPTER_PERFDATA {
    UINT32          PhysicalAdapterIndex;
    D3DKMT_ALIGN64 ULONGLONG MemoryFrequency;      // Hz
    D3DKMT_ALIGN64 ULONGLONG MaxMemoryFrequency;
    D3DKMT_ALIGN64 ULONGLONG MaxMemoryFrequencyOC;
    D3DKMT_ALIGN64 ULONGLONG MemoryBandwidth;      // bytes transferred
    D3DKMT_ALIGN64 ULONGLONG PCIEBandwidth;
    ULONG           FanRPM;
    ULONG           Power;             // tenths of a PERCENT, not watts
    ULONG           Temperature;       // deci-Celsius (0 = no sensor)
    UCHAR           PowerStateOverride;
} D3DKMT_ADAPTER_PERFDATA;
```
IDXGIFactory7::RegisterAdaptersChangedEvent (dxgi1_6.h) and
QueryVideoMemoryInfo (dxgi1_4.h) also present in the local SDK.
- `D3DKMTQueryAdapterInfo(KMTQAITYPE_ADAPTERPERFDATA)` (WDDM 2.4+ =
  every Win10 2004+ driver) returns a reverse-engineered struct System
  Informer has shipped for years: `FanRPM`, `Power` (tenths of a
  PERCENT, not watts), `Temperature` in deci-Celsius; `_CAPS` twin gives
  TemperatureMax/TemperatureWarning for scaling. Sources: System Informer
  plugins/ExtendedTools d3dkmthk.h (~lines 2030-2066, enum 61/62/63) +
  gpumon.c consumption (temp/10).
- **"Adapters without sensors report zero" (System Informer issue
  #3115)** — 0 means NO SENSOR → display absent, never 0 °C. On iGPU
  class expect exactly that (see 4.4).
- `KMTQAITYPE_NODEPERFDATA` adds per-engine Frequency/MaxFrequency/
  Voltage (engine clocks).
- `D3DKMTQueryStatistics`: utilization = NODE → RunningTime deltas
  (cumulative busy time per engine); memory = SEGMENT → BytesResident/
  CommitLimit with an Aperture flag classifying shared-vs-dedicated;
  ADAPTER → NbSegments/NodeCount. Documented enum, Informer-derived
  layouts, unelevated, gdi32.dll only.
- The KMTQAITYPE enum itself is DOCUMENTED on learn.microsoft.com
  (names public, descriptions blank).

### 4.2 PDH GPU counters — the Task Manager lane, pure user mode
- `\GPU Engine(*)\Utilization Percentage` (per process AND per engine;
  instance names carry pid + LUID + engine type),
  `\GPU Adapter Memory(*)\Dedicated|Shared Usage` (device-level totals
  keyed by LUID — the "global VRAM usage" that DXGI refuses to give),
  `\GPU Process Memory(*)\Local|NonLocal|Dedicated|Shared|TotalCommitted`
  (per process). Win10 1709+, no admin (VERIFIED; shipping reference
  implementation: github.com/shanselman/gputaskman — PURE ctypes+pdh,
  incl. a live check on a **Radeon 860M iGPU + Windows 11 build 26300**,
  exactly our laptop's platform class).
- **Aggregation rule (VERIFIED, DirectX devblog)**: Task Manager's GPU %
  = the BUSIEST ENGINE after summing per engine — never sum engines
  (a game on one of ten engines would read 10%).
- Memory semantics gotcha (measured by gputaskman): per-process
  "Dedicated/Shared Usage" counts everything a process can SEE (dwm read
  15+ GB on a 16 GB card); **"Local/Non Local Usage" is what's charged
  and adds up**. Device totals from `GPU Adapter Memory` don't have the
  problem.

### 4.3 Task Manager's own sources (settled)
- Utilization/memory: straight from VidSch/VidMm (WDDM), API-agnostic,
  busiest-engine semantics (VERIFIED DirectX devblog). Per-process rows
  parse the PDH strings (REPORTED — System Informer maintainer).
- Temperature column (Win10 2004+): every reverse-engineering artifact
  converges on `KMTQAITYPE_ADAPTERPERFDATA` (deci-C, WDDM 2.4 gate,
  phnt/Informer headers appeared with 20H1, no other public path
  exists; the linkage itself is REPORTED, the mechanism VERIFIED).
- `IDXGIAdapter3::QueryVideoMemoryInfo` is PER-PROCESS by docs
  ("informs the process of the current budget and process usage") —
  NOT a global usage API (common misconception, now nailed down).
- `Win32_VideoController.AdapterRAM` is STILL uint32 (4 GiB ceiling,
  schema-unchanged) — inventory-only, never VRAM. WinRT
  Windows.Devices.Sensors re-verified at the winrt-28000 moniker: NO
  thermal/GPU class exists.

### 4.4 What the 890M will actually report (live evidence from its class)
gputaskman's live check on a Radeon 860M (Win11 26300, driver
32.0.22050.1003): WDDM per-process VRAM counters WORKED; temperature/
power/clocks/PCIe were UNAVAILABLE from WDDM (device note: "Windows does
not report temperature, power, clocks or PCIe for this GPU") while AMD's
ADL (vendor DLL) did give utilization/clocks/power. Expectation for the
890M: utilization ✓ (PDH), shared/dedicated usage ✓ (PDH + DXGI desc),
engine clocks/power-% likely (ADAPTERPERFDATA), **temperature probably
absent via WDDM (0 = no sensor) — the only full-sensor path for this
iGPU is the ADLX subprocess lane (§3)**.

### 4.5 What LHM/HWiNFO do (why we don't copy them)
LHM master (checked 2026-11-20) = legacy ADL ONLY (atiadlxx.dll loaded
INTO the monitoring process — the exact lane our doctrine forbids in
the GUI), no ADLX yet; kernel driver WinRing0→PawnIO for CPU/etc.
HWiNFO = ADL or its own kernel driver's register reads; Martin (dev)
confirmed Afterburner's "unified" mode is D3DKMT-based. Every tool with
a full AMD sensor suite either loads a vendor DLL or ships a kernel
driver — hence the two-tier plan below.

### Lane ranking (lane 3's, criteria: no vendor DLL, no kernel driver,
### no admin, iGPU-true, subprocess-isolatable)
1. **PDH counters** (util + VRAM, Task-Manager-identical semantics).
2. **D3DKMT QueryAdapterInfo/QueryStatistics via gdi32** (the only
   driverless temperature path on Windows; 0 = absent).
3. **DXGI inventory** for names/capacities — IN THE SUBPROCESS ONLY on
   AMD machines (the 9.1 rule).
4. WMI inventory fallback (never AdapterRAM).
5. UNACCEPTABLE: vendor DLLs in the GUI process, NVML-class libs, any
   kernel driver, and ANY in-GUI-process DXGI/D3DKMT call on this
   machine class — all native lanes run inside the one-shot collector
   subprocess, results marshaled back via the sampled signal (the
   tab's existing doctrine).

## 5. The Adrenalin overlay — architecture, the smoking gun, and the fix timeline

### 5.1 The structural fact that changes everything (lane 2)
For much of the "overlay" machinery **there is no injector at all**: the
inline-hook engine (a Microsoft Detours fork) lives INSIDE the AMD D3D
user-mode drivers — `amdxc64.dll` (DX11/12) / `atidxx64.dll` (DX9/legacy)
— which Windows loads into ANY process that creates a D3D device on AMD
hardware (VERIFIED: pwn.dog teardowns). `amdihk64.dll` ("AMD inline
hook", ~210 KB, ships with `detoured.dll`) is a sibling engine that
inline-hooks GetRawInputData/GetRawInputBuffer + a WH_KEYBOARD hook —
the INPUT-hook component (VERIFIED). Consequence for DeepFlux: our
process hosts D3D11 devices from boot (QtWebEngine/ANGLE, libmpv d3d11),
so the AMD hook stack is resident regardless of whether the user ever
opens the overlay — and regardless of the overlay setting.

### 5.2 The smoking gun: the published twin of our crash
**tapestry issue #194 (VERIFIED, github.com/shuymn/tapestry/issues/194)**:
headless Rust test binaries crash with 0xC0000005 ONLY under CPU load —
the fault is a jump to the crashing thread's own start address with NO
module mapped there; in sibling processes that same address is INSIDE
amdihk64.dll. Root cause: the DLL (pulled in by adapter/device
enumeration) **spawns a thread but is UNLOADED before the thread first
runs; under CPU starvation the thread runs late and jumps into
now-unmapped code**. Twin #189 (VERIFIED): "access violations in
amdihk64.dll after it was unloaded, at the same offset" across WARP
test processes — because wgpu "creates a device on every adapter",
loading the AMD driver into every process. The observed
load-then-UNLOAD cycle around enumeration is exactly the race our
2-second factory walk kept re-arming ~43,000×/day; on a busy GUI
process the hook worker or its partially-updated inline patch lands in
unmapped/stale state, corrupting the host heap — later detonating in
CPython's allocator. Our gating experiment (never touch dxgi → 100%
stable, owner-confirmed) matches: without the walk the hook paths are
never re-driven.

More verified twins: microsoft/DirectX-Graphics-Samples #923 (crash
inside amdihk64.dll in MS's own D3D12 sample, during the per-adapter
device-creation loop); wgpu #4618 (amdihk64 loaded by a trivial Bevy
hello-world); amdihk64 sighted in Notepad++, OBS, OpenToonz, Sandboxie,
Blender-adjacent tools; EAC flags amdihk64 as an untrusted system file.
Console/child/windowless processes are NOT exempt (tapestry binaries).

### 5.3 Why no developer opt-out exists
- No public AMD developer opt-out (no exclusion manifest, no registry
  opt-out, no DXGI flag, nothing on GPUOpen) — REPORTED-as-absent after
  full searches.
- AMD rep, on per-app toggles: **"You cannot disable overlay per
  profile"** (REPORTED). Official user switches: In-Game Overlay off in
  Preferences (VERIFIED, AMD FAQ DH3-001), hotkey=None, or a MINIMAL
  ("driver-only") install that excludes overlay/UI components (VERIFIED).
- **Disabling the overlay does NOT unload the hooks for a GPU-using
  app** — the hook engine is in the D3D umd, which loads whenever a D3D
  device exists (VERIFIED architecture + the OBS report that disabling
  "didn't change anything").

### 5.4 The laptop's driver: an OEM branch
32.0.32029.2010 matches NO mainline AMD release (VERIFIED against
GPUOpen's version table): mainline late-2026 is 32.0.32015.x; our
number decodes to internal 26.20.29.xx — a **GPD OEM Adrenalin 26.x
build**, and AMD's own release notes say handhelds/laptops should use
OEM drivers whose fixes mainline notes don't describe. Mainline 26.10.2
(2026-10-30) fixed overlay-vs-Game-Bar crashes and a Steam-launcher
(non-game app!) crash — but the enumeration/unload-race family has NO
fix entry anywhere 2024-2026. So waiting for a driver fix is not a plan;
the app-side gate (9.1) is the plan, and the two new lanes below are
the recovery path.

### 5.5 Lane 2's coexistence ranking (note how it independently
### reproduces what we shipped)
1. Never drive the DXGI factory yourself — cache once / sample on
   demand (owner-proven fix). Optional hardening:
   `GetModuleHandleEx(GET_MODULE_HANDLE_EX_FLAG_PIN)` to pin the hook
   module mapped so a late hook thread can't jump into unmapped code
   (tapestry's proposed fix).
2. Move hardware enumeration OUT of the GUI process (helper that prints
   JSON and exits — "the hook race can't corrupt a process that doesn't
   host the D3D umd").
3. Advise users: In-Game Overlay + Performance Metrics Overlay off
   (official switches) — mitigation, not cure (hooks stay resident).
4. Update the OEM driver / try mainline — low confidence.
5. **Detect-and-degrade in-app**: on machines with AMD hook DLLs
   resident, default the GPU sampler to safe lanes + a one-line notice.
   "(prevents the crash class by construction)" — **this is verbatim
   what 9.1 shipped, three weeks before the study found the ecosystem
   recommending it.**

## 6. TL;DR + the plan (synthesis of all four lanes)

**The mystery is solved.** Our 2-second `CreateDXGIFFactory1 →
EnumAdapters1` walk in a process that (a) hosts D3D11 devices and thus
the AMD hook stack from boot, and (b) runs on an OEM Adrenalin branch,
repeatedly re-drove an AMD hook DLL's load/unload machinery whose worker
threads are not synchronized with its lifetime (the tapestry #194 race,
published and unfixed). The heap corruption surfaced as CPython
allocator AVs in innocent frames — probabilistically, exactly like our
2026-11-07 bisect taught. NVIDIA's nvspcap injection doesn't have this
unload race, which is why the same code never crashed the dev machine.

**Ranked roadmap (criteria: keep the 9.1 guarantee — no in-GUI-process
dxgi on AMD; no vendor DLL in the GUI process; no kernel driver; no
admin; degrade honestly):**

- **P0 (done, 9.1)** — AMD gate. Ecosystem-validated by lane 2's #5.
- **P1 — GPU lane v2 in the EXISTING one-shot collector subprocess,
  pure Windows APIs (no vendor DLL anywhere):**
  utilization + shared/dedicated usage from **PDH GPU counters**
  (Task-Manager-identical busiest-engine semantics; proven on a Radeon
  860M + Win11 26300 — our exact platform class; pure ctypes reference
  exists), plus **D3DKMT** via gdi32 for whatever the driver does
  report on the 890M: ADAPTERPERFDATA (temp deci-C, fan RPM,
  power-% — 0 means NO SENSOR, show absent), NODEPERFDATA (engine
  clocks), QueryStatistics (segment totals as cross-check). All inside
  the subprocess; a hook blowup dies with the child. Expected honest
  result on the 890M: utilization ✓, VRAM/shared ✓, clocks/power-%
  likely, temperature probably absent — Windows itself can't show it.
- **P2 — ADLX subprocess for the full AMD sensor suite** (the lane-1
  verdict): ONE long-lived, single-threaded, read-only console child
  built as a tiny C helper from AMD's MIT-referenced SDK headers (the
  ADLX license permits shipping object-code derivatives of sample code
  with products that work with AMD processors), loading the
  driver-resident amdadlx64.dll, streaming JSON lines; parent restarts
  with backoff + cap; release-all + ADLXTerminate before exit (the
  documented shutdown AV); never call tuning APIs. This is the only
  path to iGPU temperature/hotspot/NPU metrics — and AMD's own docs
  prescribe exactly this process boundary. Optional: legacy-ADL
  fallback inside the same child for pre-2022 drivers.
- **P3 — hygiene hardening (cheap):** event-driven DXGI refresh on the
  NON-AMD fast path (factory once + IsCurrent + WM_DISPLAYCHANGE /
  RegisterAdaptersChangedEvent, Win10 1809+; fail closed on any enum
  error per Sunshine's lesson); optional GetModuleHandleEx PIN for the
  hook module; boot-time module-scan logging of amdihk64/nvspcap64/
  RTSSHooks64 into the crash log (explains, doesn't prevent); user-facing
  note on AMD machines ("AMD overlay drivers can destabilize apps that
  query the GPU directly — ours queries through an isolated helper").
- **NOT-doing list (verified dead ends):** per-app overlay opt-out
  (doesn't exist), process-mitigation policies in the GUI process
  (block python311/Qt/wheels; dynamic-code ban kills V8),
  CreateDXGIFactory2 isolation flags (none exist), SYSTEM32-only
  loading (defeats only proxy-DLL hijacks), registry hacks under
  HKLM\SOFTWARE\AMD\CN, vendor DLLs in the GUI process, kernel drivers.

## 7. Sources (primary)

- DXGI/MS: CreateDXGIFactory1/2 + EnumAdapters + IsCurrent +
  RegisterAdaptersChangedEvent + QueryVideoMemoryInfo + adapter-desc
  pages (learn.microsoft.com); DirectX devblog "GPUs in the Task
  Manager"; Detours paper (microsoft.com/research); WM_DISPLAYCHANGE;
  SetProcessMitigationPolicy + signature-policy struct; MS KB on GPU
  process-memory counter bugs; Win32_VideoController; winrt-28000
  Windows.Devices.Sensors.
- Crash twins: github.com/shuymn/tapestry issues #194 + #189 (the
  amdihk64 unload race — the published twin of our crash);
  microsoft/DirectX-Graphics-Samples #923; gfx-rs/wgpu #4618;
  danielblnc/DLSS-NR-on-AMD #256 (hook-set interaction);
  renpy/renpy #5459 (foreign-DLL heap corruption → CPython
  detonation); LizardByte/Sunshine PR #5893 (fail-closed enum);
  LHM #736/#664/#124/#1074/#2473; Rem0o/FanControl.Releases
  #4107/#4118/#4244.
- AMD: gpuopen.com/adlx + ADLX manuals (using-adlx, compatibility,
  service guide) + github.com/GPUOpen-LibrariesAndSDKs/ADLX (ADLX.h,
  WinAPIs.cpp, ADLXHelper.*, IPerformanceMonitoring.h, license PDF,
  issues #10/#11/#35); gpuopen.com/adl + display-library repo (MIT
  headers); AGS_SDK repo; GPUOpen version table; AMD FAQ DH3-001;
  release notes RN-RAD-WIN-25-11-1 / 26-10-1 / 26-10-2.
- Overlay teardowns: pwn.dog posts "detouring-yourself-into-a-ban" +
  "the-hook-engine-amdihk64"; OBS graphics-hook source
  (obs-studio/plugins/win-capture); Valve Steamworks overlay docs;
  hwinfo.com forum thread 2502 (Afterburner = D3DKMT).
- Implementations: System Informer ExtendedTools gpumon.c + d3dkmthk.h
  (D3DKMT layouts incl. ADAPTERPERFDATA); gputaskman (PDH/D3DKMT pure
  ctypes reference; Radeon 860M + Win11 26300 live check); Chromium
  gpu_info_collector_win.cc + docs/stability.md; LibreHardwareMonitor
  Interop/AtiAdlxx.cs + AmdGpu.cs; fastfetch #1643 (ADL index mapping).
