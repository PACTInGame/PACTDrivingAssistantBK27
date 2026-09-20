"""Frozen entry point; guardian dispatch must precede all application startup."""
import sys


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ['--guardian']:
        import guardian
        return guardian.main(argv[1:])
    if argv == ['--smoke-test']:
        from core.release_check import smoke_test
        return smoke_test()
    if argv == ['--setup']:
        from core.single_instance import SingleInstance
        from core.settings_manager import SettingsManager
        from core.setup_wizard import SetupWizard
        lock = SingleInstance()
        if not lock.acquire():
            raise SystemExit('Close the running PACT assistant before setup.')
        try:
            wizard = SetupWizard(SettingsManager())
            wizard.run()
            return 0 if wizard.completed else 1
        finally:
            lock.release()
    if argv:
        print('Usage: PACTDrivingAssistant.exe [--setup | --smoke-test]')
        return 2
    from misc.logging_setup import setup_logging
    from main import LFSAssistantApp
    setup_logging()
    app = LFSAssistantApp()
    app.start()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
