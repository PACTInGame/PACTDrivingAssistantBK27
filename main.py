import logging
import signal
import sys
import time

import pyinsim
from assistance.manager import AssistanceManager
from core.connection_test import LfsConnectionTest
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from core.setup_wizard import run_setup_if_needed
from core.thread_manager import ThreadManager, ScheduledTask
from lfs.connector import LFSConnector
from lfs.message_sender import MessageSender
from misc import helpers
from misc.audio_player import AudioPlayer
from misc.logging_setup import setup_logging
from ui.menu_system import MenuSystem
from ui.ui_manager import UIManager
from vehicles.vehicle_manager import VehicleManager

logger = logging.getLogger(__name__)

# Warten auf LFS: exponentiell, aber mit einer harten Obergrenze pro Wartezeit,
# damit ein Nutzer nicht 60 s auf die naechste Meldung starrt.
MAX_BACKOFF_S = 16
STARTUP_DEADLINE_S = 60


class LFSAssistantApp:
    """Hauptanwendung - orchestriert alle Komponenten"""

    def __init__(self):
        # --- First-time setup (blocks until wizard is closed) ---
        run_setup_if_needed()

        # Core-Komponenten
        self.event_bus = EventBus()
        self.settings = SettingsManager()
        self.thread_manager = ThreadManager(self.event_bus)
        self._shutdown_done = False

        self._wait_for_lfs_process()
        self._wait_for_insim()

        # LFS-Kommunikation
        self.lfs_connector = LFSConnector(self.event_bus, self.settings)
        self.message_sender = MessageSender(self.lfs_connector)

        # Fahrzeug-Management
        self.vehicle_manager = VehicleManager(self.event_bus)

        # Assistenzsysteme
        self.assistance_manager = AssistanceManager(self.event_bus, self.settings)

        # UI
        self.ui_manager = UIManager(self.event_bus, self.message_sender, self.settings)
        self.menu_system = MenuSystem(self.ui_manager, self.settings)

        # Audio Player
        self.audio_player = AudioPlayer(self.event_bus, self.settings)

        # Event-Handler registrieren
        self._setup_event_handlers()

        # Scheduled Tasks hinzufügen
        self._setup_scheduled_tasks()

    # ─── Startup ─────────────────────────────────────────────────────────

    def _wait_for_lfs_process(self):
        """Wartet, bis LFS.exe laeuft - mit Begruendung in der Meldung"""
        backoff = 1
        waited = 0
        while not helpers.is_lfs_running():
            if waited >= STARTUP_DEADLINE_S:
                logger.error("LFS.exe was not running after %d s - giving up.", waited)
                sys.exit("LFS is not running")
            logger.info("Waiting for LFS.exe to appear (start Live for Speed); "
                        "checking again in %d s.", backoff)
            time.sleep(backoff)
            waited += backoff
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    def _wait_for_insim(self):
        """Wartet auf eine antwortende InSim-Verbindung

        Jeder Versuch baut eine eigene Verbindung auf und laeuft in einen
        Timeout - vorher blockierte der erste Versuch fuer immer, wenn LFS
        nicht antwortete, und die Wiederholung war nie erreichbar
        (core/connection_test.py).
        """
        backoff = 2
        waited = 0
        while True:
            logger.info("Trying to connect to LFS on InSim port 29999...")
            if LfsConnectionTest().run_test():
                logger.info("InSim answered.")
                return
            if waited >= STARTUP_DEADLINE_S:
                logger.error("LFS did not answer on InSim port 29999. Is InSim "
                             "enabled? Type /insim 29999 in game, or add it to "
                             "data/script/autoexec.lfs.")
                sys.exit("Connection failed")
            logger.info("No answer on InSim port 29999 (is '/insim 29999' active?) - "
                        "retrying in %d s.", backoff)
            time.sleep(backoff)
            waited += backoff
            backoff = min(backoff * 2, MAX_BACKOFF_S)

    def _setup_event_handlers(self):
        """Registriert globale Event-Handler"""
        self.event_bus.subscribe('lfs_connected', self._on_lfs_connected)

    def _setup_scheduled_tasks(self):
        """Richtet geplante Aufgaben ein"""
        # Assistenzsysteme alle 100ms
        assistance_task = ScheduledTask(
            "assistance_processing",
            self.assistance_manager.process_all_systems,
            self.settings.get('assistance_refresh_rate')
        )
        self.thread_manager.add_task(assistance_task)

        # UI-Updates alle 200ms
        ui_task = ScheduledTask(
            "ui_updates",
            self.ui_manager.update_hud,
            self.settings.get('ui_refresh_rate')
        )
        self.thread_manager.add_task(ui_task)

    def _on_lfs_connected(self, data=None):
        """Wird aufgerufen wenn LFS-Verbindung hergestellt wurde"""
        logger.info("Connected to LFS.")

    # ─── Lifecycle ───────────────────────────────────────────────────────

    def start(self):
        """Startet die Anwendung"""
        logger.info("Starting LFS Assistant...")

        self.install_signal_handlers()
        try:
            # LFS-Verbindung herstellen
            self.lfs_connector.connect()

            # Thread-Manager starten
            self.thread_manager.start()

            # Hauptschleife
            self._run_main_loop()

        except KeyboardInterrupt:
            logger.info("Interrupted - shutting down.")
        except Exception as e:
            logger.critical("Unhandled error: %s: %s", type(e).__name__, e, exc_info=True)
        finally:
            self.shutdown()

    def install_signal_handlers(self):
        """SIGINT/SIGTERM sauber behandeln, auch waehrend pyinsim.run() blockiert

        Der Mainthread haengt in der asyncore-Schleife. Ein Signal-Handler
        laeuft dort trotzdem (PEP 475 setzt den Syscall nach dem Handler fort);
        closeall() leert die asyncore-Map, damit kehrt run() zurueck und der
        finally-Zweig in start() raeumt auf.
        """
        for name in ('SIGINT', 'SIGTERM', 'SIGBREAK'):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, self._on_signal)
            except (ValueError, OSError):
                # Kein Mainthread oder Plattform ohne dieses Signal.
                logger.debug("Could not install a %s handler.", name)

    def _on_signal(self, signum, frame):
        """Bricht die asyncore-Schleife ab, ohne die Sockets vorher zu schliessen

        KeyboardInterrupt statt closeall(): asyncore reicht genau diese
        Exception durch (`asyncore._reraised_exceptions`), start() faengt sie,
        und shutdown() findet die Verbindung noch offen vor - nur so koennen
        BFN_CLEAR und TINY_CLOSE ueberhaupt noch bei LFS ankommen.
        """
        logger.info("Received signal %s - shutting down.", signum)
        raise KeyboardInterrupt(f"signal {signum}")

    def _run_main_loop(self):
        """Hauptschleife der Anwendung"""
        try:
            # pyinsim run() ist blocking
            pyinsim.run()
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.error("Error in main loop: %s: %s", type(e).__name__, e, exc_info=True)

    def shutdown(self):
        """Fährt die Anwendung sauber herunter

        Reihenfolge: erst die Worker stoppen (sonst zeichnen sie waehrend des
        Aufraeumens neue Buttons), dann die Buttons in LFS loeschen, dann die
        Verbindung abmelden und schliessen.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        logger.info("Shutting down LFS Assistant...")

        try:
            self.thread_manager.stop()
        except Exception as e:
            logger.warning("Stopping worker threads failed: %s: %s", type(e).__name__, e)

        try:
            self.message_sender.remove_all()
        except Exception as e:
            logger.warning("Removing buttons failed: %s: %s", type(e).__name__, e)

        try:
            self.lfs_connector.disconnect()
        except Exception as e:
            logger.warning("Disconnecting from LFS failed: %s: %s", type(e).__name__, e)

        logger.info("Shutdown complete.")


if __name__ == '__main__':
    setup_logging()
    print("PACT Driving Assistant")      # Startbanner - der einzige print()
    app = LFSAssistantApp()
    app.start()
