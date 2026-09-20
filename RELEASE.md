# PACT Driving Assistant: installation and updates

Windows, Live for Speed with InSim TCP 29999 and OutGauge UDP 30000.
The packaged application needs neither Python nor a vJoy driver for warnings.

## Install

Extract the **entire** PACTDrivingAssistant folder, including `_internal`, then
run `PACTDrivingAssistant.exe`. Close LFS when the setup wizard asks. Select
the correct LFS `cfg.txt`; setup preserves a `.pact-backup` before changing it.
Enable InSim autostart when asked: `data/script/autoexec.lfs` needs an active
`/insim 29999` line. Otherwise type that command in LFS each time you start it.
Install the supplied layouts if you want AI traffic. Start LFS after setup.
You can rerun setup with `PACTDrivingAssistant.exe --setup`.

Setup writes OutGauge Mode 2, Delay 1, IP 127.0.0.1, Port 30000, ID 0.
Never edit cfg.txt while LFS is running: LFS overwrites it on exit. A configured
file alone does not prove live telemetry: check the HUD after joining the track.
Only one PACT instance can run; another telemetry tool must not occupy UDP 30000.

## Controls to check manually

* Mouse steering plus buttons and keyboard-only driving use key intervention.
  In PACT's Keys menu match brake, throttle, handbrake, clutch and shift keys to
  your existing LFS assignments. PACT pushes the configured brake/throttle keys
  when armed; these must be your actual driving keys, including mouse buttons.
* Wheel, controller and joystick users can use warnings without vJoy.
  Enabling braking requires vJoy plus a verified driver brake axis, a separate
  unused virtual brake axis, and measured virtual-axis polarity. Defaults are
  **not** controller calibration. Never copy another driver's axis numbers.
* LFS's mouse-as-joystick mode follows the mode reported by LFS. If LFS reports
  wheel/joystick, it needs the axis path too. Native mouse-axis restoration and
  arbitration are not yet release-verified; keep this setup in warning-only mode.
* vJoy installation can change LFS controller enumeration. Back up cfg.txt and
  `data/misc` first. There is no verified automatic controller-restore installer.
* Use partial, smooth pedal travel for physical-pedal learning. Working AEB does
  not prove that pedal learning has completed: an unknown physical brake uses
  full braking rather than reducing the driver's stronger braking request.
* Automatic gearbox calibration is per car. Turn off LFS automatic shifting
  before enabling PACT's gearbox; verify clutch/shift bindings.

## Updates and saved data

Exit PACT cleanly before replacing the complete application folder. Packaged
versions store settings, setup state, learned calibrations, profiles and logs in
`%LOCALAPPDATA%\PACTDrivingAssistant`. Updating or moving the application folder
does not reset them. Do not delete that user-data folder during updates.
Source runs keep their development settings in the repository. Old releases are
not migrated. The release archive must never contain developer settings,
`.setup_done`, learned profiles or handover markers.

## Build and verification

Use 64-bit Python 3.11 on Windows, install `requirements.txt` and
`requirements-dev.txt`, plus `pyinstaller==6.11.1`. Then:

```
python -m pytest
python -m PyInstaller --noconfirm PACTDrivingAssistant.spec
dist\PACTDrivingAssistant\PACTDrivingAssistant.exe --smoke-test
```

The specification explicitly includes dynamic input/UI imports and read-only
assets. The offline smoke test does not open LFS connections, install input
hooks or acquire controllers. Distribute the whole output folder as a ZIP.

Release acceptance still requires a clean Windows user/VM without Python and
without vJoy: first-run wizard, cancellation, a non-default LFS path, connection
failure instructions, missing OutGauge, warning-only operation, then an update
that retains custom settings. Test each supported controller mode in LFS and
test watchdog handback after terminating PACT during an axis intervention.
Offline tests and a successful build do not establish these live outcomes.
