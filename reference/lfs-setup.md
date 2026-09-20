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

**OutGauge off → total, silent failure.** This is the dangerous one:

```
no OutGauge packets
  → VehicleManager._handle_outgauge_data never runs
  → 'own_vehicle_updated' is never emitted
  → AssistanceManager.own_vehicle stays None
  → process_all_systems() returns immediately, every cycle, forever
```

(`own_vehicle` itself survives — it is published from every MCI frame too — but the
OutGauge half of it stands still, and that half includes `viewed_plid`.)

Treat "InSim connected but no assistance" as "check OutGauge first".

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

**Gap:** because the flag is never re-checked, an LFS reinstall or update that resets
`cfg.txt` leaves the app permanently broken with no diagnostic. A startup validation
pass — re-read `cfg.txt`, verify the OutGauge/OutSim keys, and offer to re-run the
wizard — is the obvious hardening step. See `known-issues.md` #24.

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

This removes the entire class of failure in §2 and §3: no `cfg.txt` editing, no
"LFS must be closed", no silent breakage after an LFS reinstall, and no wizard step for
OutGauge at all. It still only streams while the player is in a car
(`conventions.md` §5.3), and it still reports the **viewed** car (`conventions.md` §5.2).

The wizard's `cfg.txt` handling would remain useful only for **OutSim**, which has no
equivalent InSim-side initialiser.
