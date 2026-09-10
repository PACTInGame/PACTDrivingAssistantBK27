from typing import Any, Dict

from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.input_guard import InputGuard
from misc.key_tap import get_key_tapper
from misc.language import LanguageManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class AutoHold(AssistanceSystem):
    """Automatic Parking Brake"""

    # ─── Ausloeseschwellen ────────────────────────────────────────────
    # Stillstand: OutGauge liefert die Geschwindigkeit in m/s, umgerechnet in
    # km/h. 0.05 km/h ist 1.4 cm/s - langsamer als jedes Kriechen.
    STANDSTILL_SPEED_KMH = 0.05
    # Bremse muss wirklich getreten sein, nicht nur beruehrt.
    MIN_BRAKE = 0.05

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("auto_hold", event_bus, settings)
        self.current_warning_level = 0
        self.own_rectangle = None
        self.translator = LanguageManager()
        # Der Schutz vor Tastendruecken an der falschen Stelle liegt komplett
        # im InputGuard (reference/ui.md §1.4). Er abonniert state_data und
        # outgauge_data selbst; frueher hat AutoHold nur dialog/text_entry
        # geprueft und weder Shift noch das Vordergrundfenster noch ob die
        # OutGauge-Daten ueberhaupt das eigene Auto beschreiben.
        self.guard = InputGuard(event_bus)
        # Der Tastendruck laeuft ueber den gemeinsamen KeyTapper: druecken und
        # loslassen passieren auf dessen eigenem Thread, der Assistenzzyklus
        # zahlt nur das Einreihen. Frueher hielt pyautogui.PAUSE die Taste
        # 100 ms - mit time.sleep im 100-ms-Thread, also 220 ms Blockade pro
        # Ausloesung (known-issues.md #43).
        self.tapper = get_key_tapper()

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Auto-Hold-Logik

        Kosten pro Zyklus: zwei Vergleiche. Der InputGuard wird nur in dem
        Zyklus befragt, in dem tatsaechlich gedrueckt wuerde.
        """
        if not self.is_enabled():
            return {'auto_hold_active': False}
        auto_hold = False
        if (own_vehicle.data.speed < self.STANDSTILL_SPEED_KMH
                and own_vehicle.brake > self.MIN_BRAKE):
            auto_hold = True
            if not own_vehicle.handbrake_light:
                if self.guard.may_inject(own_vehicle) is not None:
                    return {'auto_hold_active': auto_hold}
                # Taste live lesen: eine im Menue neu belegte Handbremse wirkt
                # sofort und nicht erst nach einem Neustart.
                user_handbrake_key = self.settings.get('user_handbrake_key')
                if not self.tapper.tap(user_handbrake_key):
                    # Kein Tastendruck zustande gekommen (unbekannte Taste,
                    # kein pyautogui) - dann auch keine Meldung, die eine
                    # angezogene Handbremse behauptet.
                    return {'auto_hold_active': auto_hold}
                self.event_bus.emit("notification", {'notification': self.translator.get('Auto Hold', self.settings.get('language'))})

        return {
            'auto_hold_active': auto_hold
        }

    def shutdown(self):
        """Keine Taste darf gedrueckt bleiben, wenn der Prozess endet

        Der KeyTapper ist prozessweit geteilt, ``release_all()`` ist idempotent
        und deckt auch das Getriebe mit ab (reference/control-intervention.md
        §1, Fail-Safe).
        """
        self.tapper.release_all()
