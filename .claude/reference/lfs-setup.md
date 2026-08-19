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

Additionally `own_vehicle.data.player_id` is set **only** from the OutGauge packet, so
without it the app cannot even tell which car on track is the player's. InSim connects
normally, the connection test passes, buttons still draw — and every single assistance
system silently does nothing. Nothing in the code detects or reports this.

Treat "InSim connected but no assistance" as "check OutGauge first".

`StateHandler.start_game_insim()` re-calls `connector.start_outgauge()` on track entry
if more than 30 s have passed since the menu was opened — an existing workaround for
the OutGauge socket dying, not a fix for it never being configured.

**OutSim off** — currently harmless, since nothing subscribes to `outsim_data`.

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
