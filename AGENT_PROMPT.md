# Agent prompt (copy & paste)

Replace `<WP-NUMBER>` and `<WP-TITLE>` with the work package you want implemented,
everything else stays as it is.

---

You are implementing **exactly one** work package of the refactoring plan for the PACT
Driving Assistant, an add-on for the racing simulator Live for Speed.

**Your work package: WP<WP-NUMBER> — <WP-TITLE>**

## Before you write any code

1. Read `.claude/CLAUDE.md`. Its three rules (soft-real-time embedded software / the
   physics must be right / robust and idiot-proof) and its working agreements override
   your defaults.
2. Read `REFACTORING_PLAN.md` completely — the dependency graph, the file-ownership table
   and the cross-cutting rules at the end — then re-read the section for **your** WP. Its
   "Scope" list is your task, its "Acceptance" list is your definition of done.
3. Read only the `.claude/reference/*.md` files your WP actually needs. The routing table
   in `CLAUDE.md` §4 tells you which. `reference/conventions.md` is mandatory before you
   touch any coordinate, angle, speed or unit maths.
4. Verify each defect in the code before fixing it. The plan cites file and line, but the
   line may have moved and the plan can be wrong. If a claimed bug is not real, say so in
   your final report instead of "fixing" it.

## Rules while you work

- **Stay inside your work package.** Only touch the files your WP owns in the ownership
  table. If you find a defect that belongs to another WP, write it into your final report
  and leave it alone — another agent owns it. Do not widen the scope.
- **No new features and no feature removal.** After your change the add-on does what it
  does today, minus the bug. The one exception is code the plan explicitly tells you to
  delete.
- **Never re-enable automatic braking intervention.** `ControllerEmulator` stays commented
  out, `collision_warning.py` keeps its TODO. Read `reference/control-intervention.md`
  before going anywhere near it.
- **Respect the real-time budget.** All assistance systems share one `process()` pass
  every 100 ms with up to ~40 vehicles on track, on a mid-range laptop. No per-cycle
  allocation storms, no `print()` in `process()`, no blocking I/O, no O(n·m) scans in the
  hot path. If you add work to a `process()` method, state its per-cycle cost in the
  commit message.
- **The EventBus is the only interface between components.** No component holds a
  reference to another. If you change an event's name or payload, grep every emitter and
  subscriber first and update `reference/events.md` in the same commit.
- **Assume LFS state is hostile.** No unguarded dict or index access on packet data, no
  assumption that a packet field exists, no silent thread death.
- **Keep each file's comment language.** German files stay German, English files stay
  English. Match the surrounding style.

## Environment

You are on Linux in a cloud container. **LFS is not available, Windows is not available,
and you cannot run the app.** `python -m pytest` is your only feedback loop. Install
`requirements-dev.txt` (or `pytest psutil shapely numpy`) with pip. Windows-only imports
(`winsound`, `pyautogui`, `vjoy`, `tkinter`) must stay behind the platform shim from WP1 —
never import them at module level in code you touch. Target runtime is Python 3.11
(`pyinsim` uses `asyncore`, which is gone in 3.12) — do not "modernise" the transport.

## Definition of done

1. Every point of your WP's Scope list is implemented, or explicitly reported as not done
   with the reason.
2. Every point of your WP's Acceptance list is covered by a test in `tests/`, and
   `python -m pytest` is green.
3. The reference docs your change touched are updated (`CLAUDE.md` §6 lists what goes
   where), and entries you fixed are removed from `reference/known-issues.md`. No
   changelog, no step-by-step log of what you did.
4. Committed on the branch you were given, with a message that says what changed and why.

## Final report

End with a short report containing:
- what you changed, file by file,
- which acceptance criteria are covered by which test,
- claimed defects that turned out not to be real,
- defects you found that belong to a different work package (with file and line),
- anything you could not verify without LFS, and what a human should check in the game.
