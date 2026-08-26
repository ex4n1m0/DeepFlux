# IRC Tab — Research & Implementation Study

Research for adding an **"IRC" tab** to DeepFlux: an embedded IRC client in the
GUI, plus Agent integration so the LLM can monitor open channels and answer
questions about them.

Status: **study only — no code written yet.**

---

## 1. Goal

1. A new main tab `"IRC"` next to Agent / Browse / Download / Play / Command.
2. A *simple but real* IRC client: connect to a network (TLS), join channels,
   chat, see the nick list, handle reconnects.
3. The Agent (agent/) can read what happens in the open channels:
   - On-demand: *"what are they saying about X in #channel?"* → tool call → summary.
   - (Phase 2) Watch mode: keyword triggers that proactively notify, like the
     existing torrent watchdog.

Because DeepFlux is a download manager, the IRC tab also opens the door to
**XDCC** later (file downloads served by IRC bots over DCC) — see §8.

---

## 2. What the codebase already gives us

Verified against the current tree (Aug 2026):

| Piece | Where | Reuse |
|---|---|---|
| Tab strip | `gui/main_window.py` (`QTabWidget`, neon tab bar, `addTab`) | Add `self.main_tabs.addTab(self.irc_tab, "IRC")` next to the others |
| Self-contained tab widgets | `gui/iptv_tab.py` (`IPTVTab`), `gui/commander_tab.py` (`CommanderTab`) | Create `gui/irc_tab.py` (`IRCTab(QWidget)`) — no base class needed |
| Signal bridge for worker→GUI | `_IPTVSignals` in `gui/iptv_tab.py`, `_AgentSignals` in `gui/main_window.py` | `_IRCSignals(QObject)` with `message = Signal(dict)`, `state = Signal(str)`, etc. |
| Background work | `threading.Thread(daemon=True)` everywhere (agent, search, refresh). **No asyncio, no QThread in app code.** | IRC network loop runs in a daemon thread |
| Config sections | `@dataclass` sections in `config.py` merged in `from_file` | Add `IRCConfig` + `irc: IRCConfig` field |
| Agent tools | schema+handler in `agent/tools.py::_build_tools`, classified in `agent/loop.py` (`READ_ONLY_TOOLS` / `DESTRUCTIVE_TOOLS`), GUI labels in `main_window._TOOL_LABELS` | 4–6 new `irc_*` tools |
| Engine injection into tools | `ToolRegistry(engine, config, dl_engine=...)` — GUI injects the real one, CLI lazily creates (AGENTS.md) | Same for `irc_client=` |
| Proactive monitoring pattern | `AgentLoop.start_watchdog` / `_watchdog_loop` / `_escalate` (`agent/loop.py`) | Template for keyword-watch notifications |

Qt binding is **PySide6 ≥ 6.5** (`requirements.txt`). New Python deps must be
pinned to versions ≥ 7 days old (repo policy).

---

## 3. Protocol research — what a minimal client must implement

Primary references (all living docs, better than the raw RFCs):

- **Modern IRC Client Protocol** — https://modern.ircdocs.horse/
  The single best spec: message format, registration, numerics, ISUPPORT,
  best practices. "If a new RFC was released today, this is what it would look like."
- **CTCP** — https://modern.ircdocs.horse/ctcp.html (VERSION/PING/ACTION/DCC framing, `\x01` quoting)
- **DCC** — https://modern.ircdocs.horse/dcc.html (needed later for XDCC)
- Formatting/color codes — https://modern.ircdocs.horse/formatting.html (`\x03` mIRC colors — strip or render)
- Classic RFCs 1459 / 2810–2813 (historical; superseded in practice by the above)
- IRCv3 — https://ircv3.net/irc/ (capability negotiation, SASL)

### 3.1 Wire protocol essentials

IRC is a line-based TCP protocol, lines end `\r\n`, **max 512 bytes/line**
(including CRLF). Message shape:

```
[@tags] [:prefix] COMMAND [param1] [param2] ... [:trailing param with spaces]
:nick!user@host PRIVMSG #chan :hello world
:server 001 deepflux :Welcome to the network
PING :abc123
```

Minimum command set for a usable client:

| Phase | Client sends | Server sends / client must handle |
|---|---|---|
| Connect | (optional `CAP LS`), `NICK`, `USER` | `001` (RPL_WELCOME) = ready; `433` = nick in use → retry with fallback nick |
| Keepalive | `PONG x` in reply | `PING :x` — **must answer or get disconnected** |
| Join | `JOIN #chan` | `332` topic, `353`/`366` names list, `JOIN`/`PART`/`QUIT`/`KICK` events |
| Chat | `PRIVMSG #chan :text` | `PRIVMSG` / `NOTICE` from others; CTCP `ACTION` (`/me`) inside PRIVMSG |
| Housekeeping | `PART`, `QUIT`, `NICK newname` | `ERROR`, nick-change events, MOTD `372/375/376` (can be displayed or skipped) |
| Etiquette | auto-reply to CTCP `VERSION`/`PING` | Some networks/k-line bots expect CTCP VERSION replies |

Practical requirements people forget:

- **Partial-line buffering**: TCP delivers arbitrary chunks; accumulate and split on `\r\n`.
- **Encoding**: decode UTF-8 with a latin-1 fallback (old networks).
- **Flood control**: servers kick "Excess Flood". Throttle outgoing PRIVMSGs
  through a token-bucket queue (~1 msg / 2 s is safe on most networks).
- **Nick-in-use (433)**: try fallbacks (`nick_`, `nick1`).
- **Reconnect with backoff** on socket drop.
- **TLS**: port 6697 is the de-facto TLS port (6667 = plain). Use
  `ssl.create_default_context()`.
- **SASL** (IRCv3 `CAP` + `AUTHENTICATE` PLAIN) for registered nicks — optional
  for v1, but the only auth that works on some locked-down networks.
- Outgoing lines must be split to stay < 512 bytes.

A truly minimal client is ~60 lines of socket code (several public examples
exist), but that skips buffering, flood control, reconnects and numerics —
realistic minimal-but-robust core ≈ **300–500 lines**.

---

## 4. Library research — build vs. reuse

| Library | Ver (checked Aug 2026) | Model | License | Fit for DeepFlux |
|---|---|---|---|---|
| **`irc`** (jaraco) | 20.5.0, actively maintained | `select()` reactor — **runs fine in a daemon thread**; also `irc.client_aio` (asyncio) | MIT | **Best fit.** Mature, event handlers (`on_pubmsg`, `on_join`…) map 1:1 onto Qt signals, has SSL/SASL connect params, scheduler, even DCC helpers. `SingleServerIRCBot` shows the pattern. |
| `miniirc` | 1.10.0 | thread-based, "thread-safe-ish" | MIT | Lightest viable option; decorator handlers; nice for v1. Some API churn risk (author announced breaking v2). |
| `pydle` | 1.1.0 (Jul 2025) | **asyncio-only** | BSD-3 | Most standards-complete (IRCv3, SASL, WHOX), but asyncio clashes with the app's thread model — would need a dedicated loop thread or `qasync`. Poor fit *for us*. |
| DIY sockets | — | thread | — | Zero deps, full control, but re-implement §3.1 edge cases. Only sensible for the "absolutely simplest" demo. |
| `irc3` / `bottom` / others | stale | asyncio | — | Not recommended (maintenance). |

**Recommendation: `irc` (jaraco)** — thread-friendly, mature, pure-Python,
Windows-safe, PyInstaller-safe. Run `reactor.process_forever()` inside a
daemon thread; emit Qt signals from handlers.

> ⚠️ **Name collision**: if we depend on PyPI's `irc`, our own package **cannot
> be named `irc/`**. Use `ircmgr/` (mirrors `dlmgr/`) for our client code.

### Open-source Qt clients worth stealing ideas from

- **pyechat** (PyQt5, HexChat-inspired) — clean structure: `irc/irc_client.py`
  protocol core separate from `gui/`; nick coloring, IRC→HTML formatting.
- **qtpyrc** (PySide6 + qasync) — modern reference: network tree, mIRC color
  rendering, searchable output, plugin hooks. Closest to our stack.
- **MERK** (PyQt5 + Twisted) — full mIRC-style MDI client; overkill but good UX reference.
- **TesseractIRC** (PySide6 + `irc.client` + SQLite) — proves the exact combo
  "PySide6 GUI + jaraco/irc core + local persistence".

None are drop-in embeddable; they're references, not dependencies.

---

## 5. Proposed architecture for DeepFlux

```
┌─ gui/irc_tab.py ─ IRCTab(QWidget) ────────────────────────────┐
│  QTreeWidget networks/channels | chat view | nick list | input │
└──────────────▲ Qt signals (queued) ───────────┐ command calls │
                │                                ▼               │
┌─ ircmgr/ (new package) ───────────────────────────────────────┴─┐
│  client.py   IRCClient — jaraco/irc reactor in daemon thread,  │
│              send queue + flood throttle, reconnect/backoff    │
│  state.py    NetworkState/ChannelState: topic, nicks,          │
│              ring buffer deque(maxlen=config.irc.buffer_lines) │
│  watch.py    (phase 2) keyword watch registry                  │
└───────────▲──────────────────────────────▲─────────────────────┘
            │ ToolRegistry(irc_client=…)    │ events
┌─ agent/tools.py ──────────────────────────┴─────────────────────┐
│  irc_status · irc_list_messages · irc_search_messages           │
│  irc_send_message · irc_join / irc_part                         │
└─────────────────────────────────────────────────────────────────┘
```

Key design point: **the IRC client core lives outside the GUI** (in `ircmgr/`)
so the Agent tools and the IRC tab share the same connection and buffers —
the tab renders state, tools read it. Mirrors how `dlmgr.DownloadEngine` is
shared (`ToolRegistry(dl_engine=…)`; GUI injects the real one, CLI lazily
creates its own — same pattern for `irc_client=`).

### 5.1 `gui/irc_tab.py` (new)

HexChat-lite layout:

- Left: `QTreeWidget` — networks → channels (click to switch buffer).
- Center: `QTextBrowser` chat view, HTML-formatted, per-nick colors
  (pyechat's `colors.py` approach), mIRC color codes stripped or rendered.
- Right: `QListWidget` nicks (from `353` NAMES + join/part tracking).
- Bottom: `QLineEdit` — plain text or `/join #x`, `/nick`, `/me`, `/msg`,
  `/raw …`; passthrough of unknown `/cmd` as raw IRC.
- Toolbar: Connect/disconnect, current nick, connection state dot.
- `_IRCSignals(QObject)` bridge — handlers in the IRC thread **must not**
  touch Qt widgets directly; emit signals (exactly like `_IPTVSignals`).

### 5.2 `ircmgr/client.py` (new)

- `IRCClient` wrapping `irc.client.Reactor` + `ServerConnection`.
- `reactor.process_forever()` in a daemon `threading.Thread`.
- Outgoing: `queue.Queue` + token-bucket flush (flood protection).
- Handlers → normalized dict events → callbacks (GUI signals + ring buffer):
  `on_welcome, on_pubmsg, on_privmsg, on_action, on_join, on_part, on_quit,
  on_nick, on_topic, on_namreply, on_disconnect, on_ctcp_version (auto-reply)`.
- Reconnect with exponential backoff; fallback nicks on `433`.

### 5.3 `config.py`

```python
@dataclass
class IRCNetworkConfig:
    id: str = ""                     # e.g. "libera"
    host: str = "irc.libera.chat"
    port: int = 6697
    tls: bool = True
    nick: str = "DeepFluxUser"
    username: str = ""
    realname: str = "DeepFlux"
    password: str = ""               # server password (rarely needed)
    sasl_account: str = ""           # optional NickServ-style auth
    sasl_password: str = ""
    channels: List[str] = field(default_factory=list)
    auto_connect: bool = False

@dataclass
class IRCConfig:
    networks: List[IRCNetworkConfig] = field(default_factory=list)
    buffer_lines: int = 500          # per-channel ring buffer (agent reads this)
    flood_delay: float = 2.0
    reconnect_max_seconds: int = 300
```

Add `irc: IRCConfig = field(default_factory=IRCConfig)` to `DeeptorrentConfig`.
`from_file` merging already handles new sections (like `DEFAULT_SOURCES`).

### 5.4 Agent tools (`agent/tools.py` + `loop.py` + `_TOOL_LABELS`)

| Tool | Class | What it does |
|---|---|---|
| `irc_status` | READ_ONLY | Connected networks, nick, joined channels, per-channel user counts |
| `irc_list_messages` | READ_ONLY | Last N msgs of a channel (optionally since timestamp) → LLM summarizes |
| `irc_search_messages` | READ_ONLY | Substring/regex over the ring buffers ("did anyone mention X?") |
| `irc_send_message` | **REQUIRES_CONFIRMATION** | Post to a channel/PM — real-world side effect, needs user confirm like `add_magnet` |
| `irc_join` / `irc_part` | REQUIRES_CONFIRMATION | Side-effecting; safer behind confirm |

Wiring per AGENTS.md: schema+handler in `tools.py::_build_tools`, classify in
`loop.py` (`READ_ONLY_TOOLS` runs concurrently — buffer reads are thread-safe
behind a lock), labels in `main_window._TOOL_LABELS`, e.g.
`"irc_list_messages": ("💬", "Reading IRC"),`.

This satisfies *"Agent monitors open channels and answers when asked"* with
**zero extra infrastructure** — the IRC thread is itself the always-on monitor;
the agent just queries the buffer.

### 5.5 Phase 2 — proactive watch (optional)

Model on the torrent watchdog (`agent/loop.py::start_watchdog/_escalate`):
user says *"ping me when someone posts a magnet / mentions release Y"* →
agent saves a watch (keyword list persisted in config or memory); a match in
`ircmgr` emits an event → GUI toast + Agent-tab note. Keep for after v1.

---

## 6. Testing

- Unit: feed raw IRC lines (`PING`, `001`, `353`, `PRIVMSG`…) through the
  parser/buffer — no network needed. The `irc` lib also lets you drive events
  programmatically.
- Integration: local throwaway server — `pip install irc` test doubles, or a
  tiny socket server speaking the minimal handshake; or `ergo`/`inspircd` in a
  container for manual testing.
- Quick check per AGENTS.md: `python -m pytest tests/ -v`.

---

## 7. Effort estimate (phased)

| Phase | Scope | Size |
|---|---|---|
| 1 | `ircmgr` core + `IRCConfig` + tab with connect/join/chat/nicks | ~600–800 LOC + config/UI |
| 2 | Agent tools (status/list/search/send) + tool labels | ~200 LOC |
| 3 | Polish: SASL, nick colors, topic bar, `/whois`, logging to disk | incremental |
| 4 | Watch triggers (agent notifications) | ~150 LOC |
| 5 | (Optional, separate project) XDCC receive → DownloadEngine | see §8 |

Dependencies to add: `irc>=20.4,<21` (pin per 7-day rule).

---

## 8. XDCC outlook (why IRC matters for a download manager)

XDCC = file sharing over IRC: bots in channels serve numbered "packs";
`/msg bot xdcc send #N` makes the bot open a **DCC SEND** connection directly
to you. Facts from research (Wikipedia "XDCC", modern.ircdocs.horse/dcc.html,
XDCC bot guide on Wikibooks):

- Handshake: CTCP `DCC SEND filename ip port filesize`; receiver connects to
  that ip:port; `DCC RESUME` extension exists; **no encryption, exposes your IP**
  (modern spec: user MUST get an accept/ignore prompt).
- A DeepFlux integration would parse incoming `DCC SEND` offers and hand the
  ip:port stream to `dlmgr.DownloadEngine` as a direct-connection download —
  architecturally similar to the existing `add_download` (direct URL) tool.
- Note: XDCC is widely associated with copyright-infringing distribution;
  treat it as a neutral protocol feature, no curated channel/bot lists.

---

## 9. Risks & gotchas

1. **Package name collision** — our code can't live in `irc/` if we use PyPI `irc`. Use `ircmgr/`.
2. **Threading discipline** — IRC callbacks fire on the network thread; GUI updates only via Qt signals; agent tool reads guarded by a lock.
3. **Flood limits** — always throttle outgoing messages; agents must not be able to spam (confirmation + throttle).
4. **Windows subprocess rule (AGENTS.md)** — IRC uses pure sockets, no subprocesses; nothing to do, just don't introduce any.
5. **PyInstaller** — `irc` is pure Python, bundles cleanly; re-check the spec after adding.
6. **Graceful shutdown** — hook `QUIT` + reactor stop into the existing X/File→Exit flow (AGENTS.md: force-kill skips saving).
7. **Privacy/security** — store SASL passwords in config like existing API keys; never log them; CTCP/DCC offers need explicit user consent.

---

## 10. Decision requested

Proceed with **Phase 1 + 2** (library: `irc` 20.x, package `ircmgr/`, tab
`gui/irc_tab.py`, 6 agent tools)? Alternatives: miniirc (lighter, more API
risk) or DIY sockets (no dep, more code to maintain).
