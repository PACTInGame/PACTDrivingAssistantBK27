import time
from typing import Dict, Any


from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from misc.helpers import is_reversing
from misc.language import LanguageManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle

# Licht-IDs des Events send_light_command (reference/events.md).
LIGHT_SIDE = 0
LIGHT_LOW_BEAM = 1
LIGHT_HIGH_BEAM = 2
LIGHT_FOG_FRONT = 3
LIGHT_FOG_REAR = 4
LIGHT_EXTRA = 5
LIGHT_INDICATOR_LEFT = 6
LIGHT_INDICATOR_RIGHT = 7
LIGHT_HAZARDS = 8

# Button-IDs aus der Karte in reference/ui.md §2. LightAssists fuehrt die
# Aktion aus und besitzt den Zustand; UIManager zeichnet ihn nur noch
# (known-issues #17) - die IDs muessen deshalb an beiden Stellen dieselben
# sein.
BTN_SIREN = 62
BTN_STROBE = 63


class LightAssists(AssistanceSystem):
    """Adaptive Lichtfunktionen"""

    # ─── Adaptives Bremslicht ─────────────────────────────────────────
    # Blinkfrequenz der Warnblinkanlage bei Notbremsung (ECE R48 erlaubt
    # 4 Hz; 0.15 s Halbperiode ≈ 3.3 Hz).
    BRAKE_LIGHT_FLASH_S = 0.15
    # Schwellen: Verzoegerung in m/s² bzw. Bremsdruck oberhalb dieser
    # Geschwindigkeit (km/h).
    BRAKE_LIGHT_DECEL = -8.0
    BRAKE_LIGHT_PEDAL = 0.85
    BRAKE_LIGHT_MIN_SPEED_KMH = 10.0

    # ─── Stroboskop ───────────────────────────────────────────────────
    # Ein Musterschritt alle 0.1 s. Frueher war es ein Schritt pro
    # process()-Aufruf, also haing die Blitzfrequenz an
    # assistance_refresh_rate (50-200 ms). Schneller als der Zyklus kann das
    # Muster nicht laufen - bei 200 ms Zyklus blitzt es entsprechend langsamer.
    STROBE_STEP_S = 0.1
    # Zyklen kommen nie exakt puenktlich. Ohne Toleranz wuerde ein Zyklus, der
    # eine Mikrosekunde zu frueh eintrifft, den Schritt um einen ganzen Zyklus
    # verschieben - bei 100 ms Zyklus also im Mittel auf die halbe
    # Blitzfrequenz. 10 % Toleranz faengt das ab.
    STROBE_STEP_TOLERANCE_S = 0.01

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("adaptive_lights", event_bus, settings)
        # Ueberschreibbar, damit Tests die Zeitsteuerung fahren koennen.
        self.clock = time.perf_counter
        self.indi_on = False
        self.adaptive_brake_light_timer = self.clock()
        self.is_siren_enabled_role = False
        self.copassist_enabled = False
        self.event_bus.subscribe("player_name_changed", self._on_player_name_changed)
        self.event_bus.subscribe("button_clicked", self._handle_button_click)
        self.event_bus.subscribe("state_data", self._handle_state_change)
        self.event_bus.subscribe("siren_toggle_requested", self._on_siren_toggle_requested)
        self.event_bus.subscribe("strobe_toggle_requested", self._on_strobe_toggle_requested)
        self.translator = LanguageManager()
        self.on_track = False
        self.player_name = "Unknown"
        self.strobe_active = False
        self.siren_active = False
        self.strobe_pattern = 0
        self._strobe_step_at = self.clock()
        # Zuletzt *selbst* angeforderter Lichtzustand des Fernlichtassistenten:
        # 'low', 'high' oder None. Solange er dem Wunschzustand entspricht,
        # wird nichts mehr gesendet - vorher ging pro Zyklus ein Befehl raus.
        self._last_beam_sent = None
        # TODO add to settings which lights to use (or 3 modes, all, indicators, and extras)
        self.strobe_actions = {
            0: {"light": 2, "on": True},
            1: {"light": 5, "on": True},
            2: {"light": 7, "on": True},
            3: {"light": 1, "on": True},
            4: {"light": 5, "on": False},
            5: {"light": 7, "on": False},
            6: {"light": 3, "on": True},
            7: {"light": 2, "on": True},
            8: {"light": 6, "on": True},
            9: {"light": 4, "on": True},
            10: {"light": 1, "on": True},
            11: {"light": 6, "on": False},
            12: {"light": 3, "on": False},
            13: {"light": 4, "on": False}


        }

    # ─── Sirene und Stroboskop: ein Besitzer ──────────────────────────
    #
    # Der Zustand liegt ausschliesslich hier. UIManager hielt frueher eine
    # zweite Kopie, die am selben button_clicked haing - der Chat-Befehl
    # schaltete nur eine der beiden um, und die Beschriftung stimmte danach
    # nicht mehr (known-issues #17). Jetzt: hier umschalten, per Event
    # veroeffentlichen, UI zeichnet.

    def _set_siren(self, active: bool):
        if self.siren_active == active:
            return False
        self.siren_active = active
        self.event_bus.emit("siren_state_changed", {"siren_active": self.siren_active})
        return True

    def _set_strobe(self, active: bool):
        if self.strobe_active == active:
            return False
        self.strobe_active = active
        self.event_bus.emit("strobe_state_changed", {"strobe_active": self.strobe_active})
        if not self.strobe_active:
            self.disable_siren()
        return True

    def _publish_siren_state(self):
        """Zustand erneut senden, damit eine neu gezeichnete UI ihn kennt"""
        self.event_bus.emit("siren_state_changed", {"siren_active": self.siren_active})
        self.event_bus.emit("strobe_state_changed", {"strobe_active": self.strobe_active})

    def _show_siren_ui(self, visible: bool):
        self.event_bus.emit("show_siren_ui", {"ui": bool(visible)})
        if visible:
            self._publish_siren_state()

    def _evaluate_role(self) -> bool:
        """Darf dieser Spieler Sirene und Stroboskop benutzen?

        Rollen-Tag im Spielernamen plus die Einstellung. Eine Stelle statt
        drei Kopien derselben Bedingung.
        """
        name = (self.player_name or '').lower()
        return (('[cop]' in name or '[tow]' in name or '[res]' in name)
                and bool(self.settings.get('cop_assistance')))

    def _apply_role(self):
        """Uebernimmt die Rolle und zieht die UI und die Lichter nach

        Lichter werden nur ausgeschaltet, wenn die Rolle wirklich verloren
        geht oder noch etwas an ist. Wer nie Cop war, bekommt keine
        Licht-Befehle - frueher schaltete jede Namensaenderung jedem Spieler
        die Zusatzlichter aus und das Abblendlicht ein.
        """
        had_role = self.is_siren_enabled_role
        self.is_siren_enabled_role = self._evaluate_role()
        self._show_siren_ui(self.is_siren_enabled_role)
        if self.is_siren_enabled_role:
            return
        if had_role or self.siren_active or self.strobe_active:
            self._set_siren(False)
            self._set_strobe(False)
            self.disable_siren()

    def _handle_state_change(self, data):
        new_on_track = data['on_track']
        if new_on_track != self.on_track:
            if new_on_track:
                self._apply_role()
            else:
                self.is_siren_enabled_role = False
                self._set_siren(False)
                self._set_strobe(False)
                self.disable_siren()
        self.on_track = new_on_track


    def _handle_button_click(self, btc):
        button_id = getattr(btc, 'ClickID', -1)
        if button_id == BTN_SIREN:
            self._set_siren(not self.siren_active)
        elif button_id == BTN_STROBE:
            self._set_strobe(not self.strobe_active)

    def _on_siren_toggle_requested(self, data):
        """Toggled die Sirene via Chat-Command. Nur wenn Cop-Modus aktiv."""
        lang = self.settings.get('language')
        if not self.is_siren_enabled_role or not self.settings.get('cop_assistance'):
            self.event_bus.emit("notification",
                                {'notification': '^1' + self.translator.get("Siren not available", lang)})
            return
        self._set_siren(not self.siren_active)
        status = self.translator.get("enabled", lang) if self.siren_active else self.translator.get("disabled", lang)
        color = "^2" if self.siren_active else "^1"
        self.event_bus.emit("notification",
                            {'notification': f'{color}{self.translator.get("Siren", lang)}: {status}'})

    def _on_strobe_toggle_requested(self, data):
        """Toggled die Stroboskoplichter via Chat-Command. Nur wenn Cop-Modus aktiv."""
        lang = self.settings.get('language')
        if not self.is_siren_enabled_role or not self.settings.get('cop_assistance'):
            self.event_bus.emit("notification",
                                {'notification': '^1' + self.translator.get("Strobe not available", lang)})
            return
        self._set_strobe(not self.strobe_active)
        status = self.translator.get("enabled", lang) if self.strobe_active else self.translator.get("disabled", lang)
        color = "^2" if self.strobe_active else "^1"
        self.event_bus.emit("notification",
                            {'notification': f'{color}{self.translator.get("Strobe", lang)}: {status}'})

    def _on_player_name_changed(self, data):
        # PName kommt seit WP4 dekodiert als str. Das fruehere str(bytes) hat
        # aus b'[COP] Bob' den Text "b'[COP] Bob'" gemacht - die Rollenpruefung
        # lief also gegen eine repr-Darstellung.
        player_name = data.get('player_name', '')
        if isinstance(player_name, (bytes, bytearray)):
            player_name = bytes(player_name).decode('latin-1', errors='replace')
        self.player_name = player_name
        self._apply_role()


    def disable_siren(self):
        """Schaltet die Stroboskop-Lichter aus

        Ohne Nebenwirkung auf das Abblendlicht: frueher stand hier
        ``{"light": 1, "on": True}``, also hat das Abschalten der Sirene dem
        Fahrer das Licht eingeschaltet - auch jedem Nicht-Cop bei jeder
        Streckeneinfahrt und bei jeder Namensaenderung.
        """
        self.event_bus.emit("send_light_command", {"light": LIGHT_FOG_FRONT, "on": False})
        self.event_bus.emit("send_light_command", {"light": LIGHT_FOG_REAR, "on": False})
        self.event_bus.emit("send_light_command", {"light": LIGHT_EXTRA, "on": False})
        self.event_bus.emit("send_light_command", {"light": LIGHT_HAZARDS, "on": False})

    # ─── Zyklus ───────────────────────────────────────────────────────

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        """Verarbeitet die Adaptive-Licht-Logik

        Kosten pro Zyklus: unveraendert ein Durchlauf ueber alle Fahrzeuge
        fuer den Fernlichtassistenten (zwei Vergleiche je Fahrzeug, Abbruch
        beim ersten Treffer), sonst nur Zeitvergleiche. Neu ist, dass in
        einem Zyklus ohne Zustandswechsel *kein* Licht-Befehl mehr rausgeht -
        vorher waren es zwei bis drei pro Zyklus, also 20-30 InSim-Pakete pro
        Sekunde.
        """
        if not self.is_enabled():
            return {'adaptive_lights': False}

        now = self.clock()
        # OutGauge folgt der Kamera. Bremslicht und Fernlichtassistent haengen
        # an Fahrdaten; beim Zuschauen waeren das die eines fremden Autos,
        # geschaltet wuerde aber das eigene (conventions.md §5.2).
        acts_on_own_car = own_vehicle.is_local_driver

        if acts_on_own_car and not self.strobe_active:
            self._process_adaptive_brake_light(own_vehicle, now)
            if self.settings.get('high_beam_assist'):
                self._process_high_beam_assist(own_vehicle, vehicles)

        # --- Sirenen-Management ---
        if (self.strobe_active and self.is_siren_enabled_role
                and self.settings.get('cop_assistance')):
            self._process_strobe(now)

        # cop_assistance im Menue umgeschaltet: Rolle neu bewerten. Vorher
        # zeigte das Einschalten die Sirenen-Buttons auch einem Spieler ohne
        # Rollen-Tag, bei dem sie nichts tun.
        if self.copassist_enabled != self.settings.get('cop_assistance'):
            self.copassist_enabled = self.settings.get('cop_assistance')
            self._apply_role()

        return {
            'adaptive_lights': True
        }

    def _process_adaptive_brake_light(self, own_vehicle: OwnVehicle, now: float):
        """Warnblinken bei Notbremsung

        ``Direction`` ist nur bei Bewegung sinnvoll und laeuft bei 65535 ueber:
        die fruehere Differenz ``heading - direction`` ohne Modulo hat das Auto
        in einem Kurssektor als rueckwaertsfahrend gemeldet und das Bremslicht
        dort abgeschaltet (misc/helpers.is_reversing).
        """
        if (now - self.adaptive_brake_light_timer) <= self.BRAKE_LIGHT_FLASH_S:
            return
        self.adaptive_brake_light_timer = now

        data = own_vehicle.data
        reverse = is_reversing(data.heading, data.direction)
        emergency = (data.acceleration < self.BRAKE_LIGHT_DECEL
                     or (own_vehicle.brake > self.BRAKE_LIGHT_PEDAL
                         and data.speed > self.BRAKE_LIGHT_MIN_SPEED_KMH))

        if emergency and not reverse:
            self.indi_on = not self.indi_on
            self.event_bus.emit("send_light_command",
                                {"light": LIGHT_HAZARDS, "on": self.indi_on})
        elif self.indi_on:
            self.indi_on = False
            self.event_bus.emit("send_light_command",
                                {"light": LIGHT_HAZARDS, "on": False})

    def _process_high_beam_assist(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]):
        """Fernlicht ab- und wieder aufblenden

        Zwei Regeln, die vorher fehlten:

        * **Fahrer mit ausgeschaltetem Licht bleiben ohne Licht.** Vorher
          schaltete der Assistent bei jedem Zyklus das Abblendlicht ein, wenn
          gar kein Licht an war - Fahren ohne Licht war schlicht unmoeglich.
        * **Ein Befehl pro Zustandswechsel.** Gesendet wird nur, wenn der
          gewuenschte Zustand vom tatsaechlichen abweicht *und* wir ihn nicht
          schon selbst angefordert haben. Blendet der Fahrer danach von Hand
          zurueck, bleibt es dabei, bis sich die Lage aendert.
        """
        if not own_vehicle.low_beam_light and not own_vehicle.full_beam_light:
            self._last_beam_sent = None
            return

        want_high = not self._any_vehicle_visible(vehicles)
        desired = 'high' if want_high else 'low'
        actual = 'high' if own_vehicle.full_beam_light else 'low'

        if desired == actual or desired == self._last_beam_sent:
            return

        self._last_beam_sent = desired
        self.event_bus.emit("send_light_command",
                            {"light": LIGHT_HIGH_BEAM if want_high else LIGHT_LOW_BEAM,
                             "on": True})

    def _any_vehicle_visible(self, vehicles: Dict[int, Vehicle]) -> bool:
        for vehicle in vehicles.values():
            if self._is_vehicle_visible(vehicle):
                return True
        return False

    def _process_strobe(self, now: float):
        """Ein Musterschritt pro STROBE_STEP_S, unabhaengig von der Zykluszeit"""
        if now - self._strobe_step_at < self.STROBE_STEP_S - self.STROBE_STEP_TOLERANCE_S:
            return
        self._strobe_step_at = now
        self.strobe_pattern = (self.strobe_pattern + 1) % len(self.strobe_actions)
        # Kopie: der Bus reicht das Dict an alle Abonnenten weiter, die
        # Mustertabelle darf dabei nicht veraendert werden.
        self.event_bus.emit("send_light_command", dict(self.strobe_actions[self.strobe_pattern]))

    def _is_vehicle_visible(self, other_vehicle: Vehicle) -> bool:
        """Prüft ob Fahrzeug sichtbar ist - keine Hindernisse werden berücksichtigt"""
        is_vehicle_ahead = other_vehicle.data.distance_to_player < 250 and other_vehicle.data.speed > 1
        player_in_cone = abs(other_vehicle.data.angle_to_player) < 15 or abs(other_vehicle.data.angle_to_player) > 345
        return is_vehicle_ahead and player_in_cone
