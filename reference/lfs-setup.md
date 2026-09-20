# LFS-side setup and prerequisites

The add-on cannot work unless LFS itself is configured to talk to it. Three separate
things must be right: **InSim**, **OutGauge**, and **OutSim**. They are enabled in
different places and fail in different ways.

Start here when the symptom is "nothing happens", "no HUD", "no warnings", or
"it worked yesterday".

---

## 1. What must be configured

| Interface | Enabled by | Required for |
|---|---|---|
| **InSim** (TCP 29999) | `/insim 29999` typed in game, or a line in `autoexec.lfs` | everything — connection, buttons, all car positions, commands |
| **OutGauge** (UDP 30000) | `cfg.txt` in the LFS root folder | **own-car data: speed, rpm, gear, pedals, dashboard lights** |
| **OutSim** (UDP 29998) | `cfg.txt` in the LFS root folder | currently connected but unused (G-forces, per-wheel data) |
| *(TCP 29997)* | nothing — the add-on binds it itself | the single-instance lock, §2.1. LFS is not involved |

### `cfg.txt` (LFS root folder, e.g. `C:\LFS\cfg.txt`)

`core/setup_wizard.py:REQUIRED_CFG_SETTINGS` is the authoritative list:

```
OutSim Mode 2        OutGauge Mode 2
OutSim Delay 1       OutGauge Delay 1
OutSim IP 127.0.0.1  OutGauge IP 127.0.0.1
OutSim Port 29998    OutGauge Port 30000
OutSim ID 0          OutGauge ID 0
OutSim Opts 1ff
```

- **LFS must be closed while `cfg.txt` is edited.** LFS rewrites the file on exit and
  will overwrite any changes made while it was running. The setup wizard enforces this
  by polling `LFS.exe` and refusing to continue until it is gone.
- `OutSim Opts 1ff` selects *all* OutSim blocks, giving a 280-byte packet. Any other
  value changes the packet size, and pyinsim identifies OutGauge/OutSim packets **purely
  by datagram length** (`insim.md` §5) — a mismatched `Opts` means the packets arrive
  and are silently discarded.
- These settings can be reset by an LFS update or a fresh install. See §3.

### `autoexec.lfs` (`<LFS>/data/script/autoexec.lfs`)

LFS runs every line in this script at startup. It must contain:

```
/insim 29999
```

Without it the user has to type `/insim 29999` in the chat manually on every launch,
or the app never connects. `core/setup_wizard.py:add_insim_autoexec()` appends the line
if it is not already present (it does not deduplicate beyond a substring check).

There is a `TODO` in that function about also adding an `/exec` line so LFS launches
the assistant itself.

## 2. Failure modes — know these before debugging

**InSim off → loud failure.** `main.py` runs `LfsConnectionTest` before anything else
and retries with exponential backoff, exiting after ~60 s. The user sees console output.
This path is fine.

**OutGauge off → missing gauge telemetry.** MCI still publishes the car and
warning-only systems can run. The startup validator reports saved configuration
differences; the live HUD warns when expected gauge packets are missing, and
`InputGuard` refuses actuation with `no_outgauge`. A working InSim connection
alone does not prove that OutGauge works.

### 2.1 The third way to lose OutGauge: something else already has port 30000

`cfg.txt` can be perfect and the socket still not open, because **only one process may
bind UDP 30000**. What binds it in practice is another copy of this app that did not
shut down, or a `simulation_tests` relay left over from a run
(`simulation_tests/README.md` §2). LFS is not involved and reports nothing.

Measured on 2026-09-19: a leftover `_temp/fcw35_addon.py` from an earlier scenario had
held the port for over an hour. Every start after it logged `WinError 10048` and ran on
**blind** — see `known-issues.md` #51 for the full chain, which ends in every actuator
being refused while the log says the emergency brake is armed.

What the app does about it now:

* **A second copy of the add-on does not start at all** (`core/single_instance.py`).
  It takes a listening TCP socket on `127.0.0.1:29997` before anything else — a
  socket rather than a PID file, because the OS releases it however the process
  dies, and the bind is atomic. A second start logs one line naming the cause and
  exits 1. Measured on 2026-09-20, which is what forced this: two copies ran for
  16 minutes, the second blind on `WinError 10048` but still drawing buttons over
  the first one's and arming the same actuators — two `EmergencyBrake` instances
  pressing and releasing the same brake key, where instance 2 releasing takes
  instance 1's brake away mid-intervention. Both also wrote to the same log file,
  which then read as one process alternating between "armed" and "cannot be
  armed". `PACT_ALLOW_MULTIPLE=1` skips the lock for a developer who means it.
* `start_outgauge()` closes the previous socket before binding a new one. Half the
  10048s in that log were self-inflicted, because `StateHandler` re-opens OutGauge on
  track entry and the old socket was still on the port.
* A failed bind logs one explicit line naming the likely cause, publishes
  `outgauge_status`, and the driving menu shows **"OutGauge port 30000 is taken"** in
  red under the emergency-brake entry.
* `InputGuard` refuses every actuation with `no_outgauge` (`ui.md` §1.4).

To find the culprit on Windows:

```powershell
Get-NetUDPEndpoint -LocalPort 30000 | ForEach-Object { Get-Process -Id $_.OwningProcess }
```

`StateHandler.start_game_insim()` re-calls `connector.start_outgauge()` on track entry
if more than 30 s have passed since the menu was opened — a workaround for the OutGauge
socket dying, not a fix for it never being configured.

**OutSim off** — currently harmless; the app does not start its unused OutSim receiver.

## 3. The setup wizard runs once, and only once

`core/setup_wizard.py:run_setup_if_needed()` is called first thing in
`LFSAssistantApp.__init__`. It is skipped entirely if a `.setup_done` file exists next
to the executable / project root.

Wizard steps: wait for LFS to close → locate `cfg.txt` (defaults to `C:\LFS\cfg.txt`,
otherwise a file dialog) → confirm and patch `cfg.txt` → optionally append `/insim 29999`
to `autoexec.lfs` → optionally copy `layouts/*.lyt` into `<LFS>/data/layout/` → write
`.setup_done`.

**Startup validation runs independently of `.setup_done`.**
`core/outgauge_config.py` checks saved OutGauge settings before the main InSim
connection opens. It prefers the running LFS executable's directory over
`lfs_directory`; multiple or inaccessible installations are reported as
inconclusive. Missing/unreadable files, missing/duplicate keys and differences
from the wizard's recommended OutGauge values are logged with the exact path
and expected values. Close LFS before correcting the file, then restart it.

The check is read-only and does not abort startup: disk contents can differ
from the currently running configuration, and a relay can be intentional.
Live socket/telemetry checks remain authoritative for availability. No disk or
process scanning is added to an assistance cycle. OutSim is unused and is not
required by this check.

`.setup_done` and `settings.json` are machine-specific and are git-ignored.

## 4. Reference docs shipped with LFS

`C:\LFS\docs\InSim.txt` documents the `cfg.txt` OutGauge/OutSim keys in its OutGauge
section; `C:\LFS\docs\OutSimPack.txt` documents the `OSO_*` option bits behind
`OutSim Opts`; `C:\LFS\docs\Commands.txt` lists every `/` command, including `/insim`.

## 5. `cfg.txt` is avoidable — `SMALL_SSG`

`InSim.txt` (Dashboard Packets section):

> *"If OutGauge has not been setup in cfg.txt, the SSG packet makes LFS send UDP packets
> if in game, using the OutGauge system […] You do not need to set any OutGauge values in
> LFS cfg.txt — OutGauge is fully initialised by the SSG packet."*

```python
insim.send(pyinsim.ISP_SMALL, SubT=pyinsim.SMALL_SSG, UVal=interval_ms)  # 0 = stop
```

Packets go to the UDP port given as **`UDPPort` in the `IS_ISI` handshake**.
`LFSConnector.connect()` does not currently pass `UDPPort`, so this would need adding
alongside the `SMALL_SSG` request.

This is a potential replacement, not a verified way to override an existing
cfg.txt stream. The live tracer received no independent stream in that setup;
see `testing.md`, "If a driving trace has no OutGauge". Keep the validated cfg.txt
path until the replacement has been exercised against the running game.
OutSim has an InSim-side request too (`SMALL_SSP`); the add-on does not currently
need OutSim.
