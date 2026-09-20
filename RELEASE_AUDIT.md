# Release readiness — 2026-09-20

**Decision: not yet approved for general release.** A Windows package builds and
passes offline checks; the remaining controller and live acceptance gaps below
prevent a claim that all requested configurations work robustly.

| Area | Result |
|---|---|
| PyInstaller | Python 3.11.3 / PyInstaller 6.11.1 onedir build succeeded. Frozen imports/assets smoke test passed from an unrelated working directory. |
| Frozen watchdog | Separate `--guardian` dispatch tested in the actual EXE with an absent marker; it exits normally without starting the application. Live crash handback is still pending. |
| Package contents | Audio, routes, layouts and release instructions included. No developer settings, setup flag, learned profiles or handover markers included. |
| Pedal logs | Changing sample counts no longer bypass the per-pedal 30-second throttle. The message distinguishes learning from working AEB/full-brake fallback. |
| OutGauge setup | Backup plus atomic patch, duplicate removal, unrelated local-encoding text preserved. LFS-running checks occur at write time. Startup differences show a close-LFS/repair dialog. |
| InSim setup | Active startup commands are normalized to `/insim 29999`; commented or prefix-matching text cannot falsely count as configured. Connection failures already explain the manual command. |
| No vJoy | Warning-only remains usable. Enabling axis braking is refused without ready vJoy and calibration, including when AEB was previously disabled. Offline regression passed. |
| Installation | First-run wizard persists the selected LFS folder, cancellation stops startup, and `--setup` reruns it. Final guidance lists remaining manual steps. Clean-machine UI acceptance is pending. |
| Updates | Frozen state lives under `%LOCALAPPDATA%/PACTDrivingAssistant`, separate from assets. Update-path persistence, failed-save retry and future-version refusal are tested. |
| Keyboard / mouse buttons | Existing key arbitration/guard tests pass. Packaged live acceptance still needed. |
| Wheel / controller / joystick | Existing axis/pedal regressions pass. Device write failure, missing watchdog and failed recovery-marker writes now refuse takeover. Arbitrary-controller commissioning remains incomplete. |
| Native mouse as joystick | Warning-only is the supported fallback. Native mouse-axis restoration/arbitration has not been implemented and verified as a complete intervention path. |

Validation: **1324 passed, 115 skipped**, no failures, using the full offline
suite on Windows. Skipped tests remain unverified on this host; this result is
not a replacement for live LFS runs. No live driving or real controller
configuration was changed during this audit.

The build environment emitted a hook-discovery warning for the separately
installed `pygame_gui` package; PACT does not use it and the packaged runtime
smoke test passed. Release production should use an isolated build environment.

## Remaining release gates

1. Provide and verify safe guided commissioning of driver/virtual axes and
   polarity across different controller enumerations. Never scan with `/axis`:
   it destroys existing assignments. New axis users currently stay warning-only.
2. Implement and measure native mouse-joystick intervention before advertising
   full support for that mode.
3. Exercise the actual package on a fresh Windows user/VM without Python/vJoy:
   setup, cancellation, custom LFS directory, telemetry failure, update retention,
   sound and warnings. Then verify watchdog recovery during live axis takeover.
4. Resolve the synchronous recovery-marker I/O on intervention transitions
   (`reference/known-issues.md` #60) without weakening crash recovery.
5. Revisit the previously recorded digital-AEB impact cases (#57); passing
   packaging tests does not fix that known control limitation.

Installation/build instructions: `RELEASE.md`. Current executable:
`dist/PACTDrivingAssistant/PACTDrivingAssistant.exe`; distribute its entire
directory, including `_internal`.
