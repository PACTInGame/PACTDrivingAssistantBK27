import logging
from typing import Callable, Dict, List, Tuple

import pyinsim
from core.settings_manager import SettingsManager
from misc import key_binder
from misc.language import LanguageManager
from ui.ui_manager import (MENU_RANGE, UIManager, clamp_hud_position,
                           hud_overlaps_reserved_area)

logger = logging.getLogger(__name__)

# Schrittweite der HUD-Pfeile im Systemmenue.
HUD_STEP = 2

# Der schwebende Oeffner und der Schliessen-Button gehoeren keinem Menue,
# sondern allen (reference/ui.md §2).
BTN_OPEN_MENU = 20
BTN_CLOSE = 40

# Eine Button-Zeile: (id, links, oben, breit, hoch, text, stil)
Button = Tuple[int, int, int, int, int, str, int]


class MenuSystem:
    """Verwaltet das Menüsystem

    Jedes Menue besteht aus zwei Haelften, die zusammengehoeren:

    * ``_buttons_<name>()`` liefert die gezeichneten Buttons,
    * ``_actions[<name>]`` bildet Button-ID auf Aktion ab.

    Frueher war das Klick-Handling eine lange ``if current_menu == … and
    button_id == …``-Kette ueber die mehrfach vergebenen IDs 20–40. Eine ID
    konnte damit im falschen Menue landen und ein neuer Eintrag einen
    bestehenden still verdecken. Ueber die Tabelle kann eine ID nur noch in
    genau dem Menue wirken, das sie auch zeichnet - ``tests/test_menu.py``
    prueft beide Haelften gegeneinander.
    """

    def __init__(self, ui_manager: UIManager, settings: SettingsManager):
        self.ui_manager = ui_manager
        self.settings = settings
        self.current_menu = 'none'
        self.menu_stack: List[str] = []
        self.on_track = False
        self.buttons_allowed = False
        self.translator = LanguageManager()
        self.keybinder = key_binder.Keybinder(self.ui_manager.event_bus, self.settings)
        self.ai_traffic_active = False
        # Welche Taste gerade neu belegt wird ('await_key').
        self.pending_key_setting = ''
        # Zuletzt aktiver PDC-Modus, damit der Ein/Aus-Schalter die Wahl
        # zwischen 'Visual' und 'Visual & Audio' nicht vergisst.
        self._last_pdc_mode = self.settings.get('park_distance_control_mode') or 1

        self._painters: Dict[str, Callable[[], None]] = {
            'main': self.open_main_menu,
            'driving': self.open_driving_menu,
            'parking': self.open_parking_menu,
            'system': self.open_system_settings,
            'cop': self.open_cop_menu,
            'ai_traffic': self.open_ai_traffic_menu,
            'keys': self.open_keys_settings,
        }
        self._actions: Dict[str, Dict[int, Callable[[], None]]] = self._build_actions()

        self.ui_manager.event_bus.subscribe('state_data', self._state_change)
        self.ui_manager.event_bus.subscribe('button_clicked', self._handle_ui_action)
        self.ui_manager.event_bus.subscribe('player_name_changed', self._handle_player_change)
        self.ui_manager.event_bus.subscribe('new_keybinding', self._rebind_key)
        self.ui_manager.event_bus.subscribe('ai_traffic_state_changed', self._on_ai_traffic_state_changed)
        self.ui_manager.event_bus.subscribe('buttons_cleared', self._on_buttons_cleared)

    # ─── Sprache ──────────────────────────────────────────────────────

    @property
    def language(self) -> str:
        """Die aktuell eingestellte Sprache - immer live aus den Settings.

        Sie war frueher eine Kopie aus dem Konstruktor, die nur
        ``change_language`` nachzog. Jeder andere Weg zur Spracheinstellung
        (Chat-Kommando, handeditierte settings.json) erreichte das Menue
        dadurch nicht.
        """
        return self.settings.get('language')

    def _on_ai_traffic_state_changed(self, data):
        """Callback from AIDriver when traffic state changes."""
        self.ai_traffic_active = data.get('active', False)

    def _on_buttons_cleared(self, data=None):
        """SHIFT+B: LFS hat unsere Buttons geworfen (reference/ui.md §1.5)

        Das Menue wird nur bei Aenderungen gezeichnet, kaeme also erst beim
        naechsten Zustandswechsel zurueck. Die Registry im MessageSender ist
        an dieser Stelle bereits verworfen, der Repaint geht also wirklich
        raus.
        """
        if not self.on_track:
            return
        self._repaint_current_menu()

    def _repaint_current_menu(self):
        painter = self._painters.get(self.current_menu)
        if painter is not None:
            painter()
        elif self.current_menu == 'none':
            self.create_open_menu_button()
        elif self.current_menu == 'await_key':
            self.open_awaiting_key(self.pending_key_setting)

    def _rebind_key(self, data):
        setting = data['setting']
        new_key = data['button']
        self.settings.set(setting, new_key)
        logger.info("Rebound %s to %r", setting, new_key)
        if self.current_menu == 'await_key':
            self.open_keys_settings()

    def _handle_player_change(self, data):
        """IS_NPL hat den Fahrer/das Auto gewechselt

        Der erkannte Eingabemodus wird hier **nicht** mehr in
        ``own_control_mode`` geschrieben: das ist die Wahl des Nutzers und
        wurde bei jedem Namens- oder Autowechsel ueberschrieben. Der erkannte
        Modus steht kameraunabhaengig in ``vehicle.data.control_mode`` und
        wird mit diesem Event verteilt (reference/conventions.md §5.4).
        """
        logger.debug("Player changed: %r, LFS control mode %s",
                     data.get('player_name'), data.get('control_mode'))

    def _state_change(self, data):
        # Im Hauptmenue und in der Serverliste darf nichts gezeichnet werden
        # (reference/ui.md §1.1); dort ist on_track ohnehin False.
        self.buttons_allowed = bool(data.get('buttons_allowed', True))
        new_on_track = bool(data.get('on_track', False))
        if new_on_track != self.on_track:
            self.on_track = new_on_track
            if new_on_track and self.current_menu == 'none':
                self.create_open_menu_button()
            elif not new_on_track:
                self._clear_menu_buttons()
            else:
                self.close_menu()

    # ─── Zeichnen ─────────────────────────────────────────────────────

    def buttons_for(self, menu: str) -> List[Button]:
        """Die Buttons, die ``menu`` zeichnet - ohne sie zu senden.

        Oeffentlich, weil der Test die Aktionstabelle dagegen prueft.
        """
        builder = getattr(self, f'_buttons_{menu}', None)
        if builder is None:
            return []
        return builder()

    def actions_for(self, menu: str) -> Dict[int, Callable[[], None]]:
        """Die Aktionstabelle eines Menues"""
        return self._actions.get(menu, {})

    def _open(self, menu: str, buttons: List[Button]):
        """Setzt den Menuezustand und zeichnet die Buttons"""
        self.current_menu = menu
        self._clear_menu_buttons()
        for button_id, x, y, w, h, text, style in buttons:
            self.ui_manager.message_sender.create_button(button_id, x, y, w, h, text, style)

    # ─── Main Menu ────────────────────────────────────────────────────

    def _buttons_main(self) -> List[Button]:
        lang = self.language
        return [
            (21, 0, 80, 20, 5, self.translator.get("Main Menu", lang),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 20, 5, self.translator.get("Driving", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 0, 90, 20, 5, self.translator.get("Parking", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 0, 95, 20, 5, self.translator.get("System", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 100, 20, 5, self.translator.get("Cop Mode", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (26, 0, 105, 20, 5, self.translator.get("Keys and Axes", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (28, 0, 110, 20, 5, self.translator.get("AI Traffic", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 0, 115, 20, 5,
             self.translator.get("Language", lang) + f": {lang}",
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (BTN_CLOSE, 0, 120, 20, 5, "^1" + self.translator.get("Close", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

    def open_main_menu(self):
        """Öffnet das Hauptmenü"""
        self._open('main', self._buttons_main())

    # ─── Driving Menu ─────────────────────────────────────────────────

    def _buttons_driving(self) -> List[Button]:
        lang = self.language
        fcw = "^2" if self.settings.get('forward_collision_warning') else "^1"
        bsw = "^2" if self.settings.get('blind_spot_warning') else "^1"
        ctw = "^2" if self.settings.get('cross_traffic_warning') else "^1"
        agb = "^2" if self.settings.get('automatic_gearbox') else "^1"
        ah = "^2" if self.settings.get('auto_hold') else "^1"
        al = "^2" if self.settings.get('adaptive_lights') else "^1"
        hba = "^2" if self.settings.get('high_beam_assist') else "^1"

        distance = self.settings.get('collision_warning_distance')
        distance_text = "^2" + self.translator.get("Early", lang) if distance == 0 else "^3" + self.translator.get("Medium", lang) if distance == 1 else "^1" + self.translator.get("Late", lang)

        ctw_distance = self.settings.get('cross_traffic_warning_distance')
        ctw_distance_text = "^2" + self.translator.get("Early", lang) if ctw_distance == 0 else "^3" + self.translator.get("Medium", lang) if ctw_distance == 1 else "^1" + self.translator.get("Late", lang)

        return [
            (21, 0, 70, 25, 5, self.translator.get("Driving Settings", lang),
             pyinsim.ISB_LIGHT),
            (22, 0, 75, 25, 5, fcw + self.translator.get("Collision Warning", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 25, 75, 15, 5, distance_text,
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (24, 0, 80, 25, 5, bsw + self.translator.get("Blind Spot Warn.", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 85, 25, 5, ctw + self.translator.get("Cross Traffic Warn.", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (31, 25, 85, 15, 5, ctw_distance_text,
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (26, 0, 90, 25, 5, agb + self.translator.get("Automatic Gearbox", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (30, 25, 90, 15, 5, self.translator.get("Calibrate", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 0, 95, 25, 5, ah + self.translator.get("Auto Hold", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (28, 0, 100, 25, 5, al + self.translator.get("Adaptive Lights", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (29, 0, 105, 25, 5, hba + self.translator.get("High Beam Assist", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (BTN_CLOSE, 0, 110, 25, 5, "^1" + self.translator.get("Close", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

    def open_driving_menu(self):
        """Öffnet das Fahrer-Menü"""
        self._open('driving', self._buttons_driving())

    # ─── Parking Menu ─────────────────────────────────────────────────

    def _buttons_parking(self) -> List[Button]:
        lang = self.language
        # ``park_distance_control`` ist seit WP6 aus dem Modus abgeleitet -
        # es gibt nur noch einen gespeicherten Wert, also koennen Schalter
        # und Modus sich nicht mehr widersprechen.
        pdc_on = self.settings.get('park_distance_control')
        pdc = "^2" if pdc_on else "^1"
        pdc_mode = self.settings.get('park_distance_control_mode')
        pdc_mode_text = (
            self.translator.get("Visual", lang) if pdc_mode == 1
            else self.translator.get("Visual & Audio", lang) if pdc_mode == 2
            else "^1" + self.translator.get("Off", lang)
        )

        return [
            (21, 0, 80, 25, 5, self.translator.get("Parking Settings", lang),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 25, 5, pdc + self.translator.get("Park Distance Control", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 25, 85, 20, 5, pdc_mode_text,
             (pyinsim.ISB_DARK | pyinsim.ISB_CLICK) if pdc_on else pyinsim.ISB_LIGHT),
            (BTN_CLOSE, 0, 90, 25, 5, "^1" + self.translator.get("Close", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

    def open_parking_menu(self):
        """Öffnet das Parken-Menü"""
        self._open('parking', self._buttons_parking())

    # ─── System Settings Menu ─────────────────────────────────────────

    def _buttons_system(self) -> List[Button]:
        lang = self.language
        unit = self.settings.get('unit')
        unit_text = "^2" + self.translator.get("Metric", lang) if unit == "metric" else "^2" + self.translator.get("Imperial", lang)
        hud_on = self.settings.get('hud_active')
        hud_text = "^2" if hud_on else "^1"
        hud_w, hud_h = clamp_hud_position(self.settings.get('hud_width'),
                                          self.settings.get('hud_height'))
        # Rot heisst: der HUD liegt im von LFS reservierten Rechteck
        # (L 0…110, T 30…170). LFS raeumt dort seine eigene UI weg, also
        # verschwinden Einstiegs- und Garagenmenues (reference/ui.md §1.3).
        # Verschoben wird nichts - die ausgelieferte Standardposition liegt
        # selbst in diesem Bereich.
        hud_position_colour = "^1" if hud_overlaps_reserved_area(hud_w, hud_h) else "^7"

        return [
            (21, 0, 75, 25, 5, self.translator.get("System Settings", lang),
             pyinsim.ISB_LIGHT),
            (22, 0, 80, 20, 5, self.translator.get("Unit", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (23, 20, 80, 10, 5, unit_text,
             pyinsim.ISB_LIGHT),
            (24, 0, 85, 20, 5, hud_text + self.translator.get("Head-Up Display", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (25, 0, 90, 25, 5,
             f"{hud_position_colour}{self.translator.get('HUD Position', lang)}"
             f"  (V:{hud_h}  H:{hud_w})",
             pyinsim.ISB_LIGHT),
            (26, 25, 90, 5, 5, "^7" + self.translator.get("Up", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (27, 30, 90, 5, 5, "^7" + self.translator.get("Down", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (28, 35, 90, 5, 5, "^7" + self.translator.get("Left", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (29, 40, 90, 5, 5, "^7" + self.translator.get("Right", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (BTN_CLOSE, 0, 95, 25, 5, "^1" + self.translator.get("Close", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

    def open_system_settings(self):
        """Öffnet die Systemeinstellungen"""
        self._open('system', self._buttons_system())

    # ─── Cop Mode Menu ────────────────────────────────────────────────

    def _buttons_cop(self) -> List[Button]:
        lang = self.language
        cop = "^2" if self.settings.get('cop_assistance') else "^1"

        return [
            (21, 0, 80, 25, 5, self.translator.get("Cop Mode Settings", lang),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 25, 5, cop + self.translator.get("Cop Assistance", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (BTN_CLOSE, 0, 90, 25, 5, "^1" + self.translator.get("Close", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

    def open_cop_menu(self):
        """Öffnet das Cop-Mode-Menü"""
        self._open('cop', self._buttons_cop())

    # ─── AI Traffic Menu ──────────────────────────────────────────────

    def _buttons_ai_traffic(self) -> List[Button]:
        lang = self.language
        if self.ai_traffic_active:
            toggle_color = "^2"
            toggle_text = self.translator.get("Stop AI Traffic", lang)
        else:
            toggle_color = "^1"
            toggle_text = self.translator.get("Start AI Traffic", lang)

        return [
            (21, 0, 80, 25, 5, self.translator.get("AI Traffic", lang),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 25, 5, toggle_color + toggle_text,
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
            (BTN_CLOSE, 0, 90, 25, 5, "^1" + self.translator.get("Close", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

    def open_ai_traffic_menu(self):
        """Öffnet das AI-Traffic-Menü"""
        self._open('ai_traffic', self._buttons_ai_traffic())

    # ─── Keys and Axes Menu ───────────────────────────────────────────

    # Button-ID → Einstellung, in genau der Reihenfolge der gezeichneten Zeilen.
    KEY_BINDINGS = (
        (22, 'user_handbrake_key', "Handbrake Key"),
        (23, 'user_shift_up_key', "Shift Up Key"),
        (24, 'user_shift_down_key', "Shift Down Key"),
        (25, 'user_clutch_key', "Clutch Key"),
        (26, 'user_ignition_key', "Ignition Key"),
    )

    def _buttons_keys(self) -> List[Button]:
        lang = self.language
        buttons: List[Button] = [
            (21, 0, 75, 25, 5, self.translator.get("Keys and Axes", lang),
             pyinsim.ISB_LIGHT),
        ]
        for index, (button_id, setting, label) in enumerate(self.KEY_BINDINGS):
            top = 80 + index * 5
            buttons.append((button_id, 0, top, 20, 5, self.translator.get(label, lang),
                            pyinsim.ISB_DARK | pyinsim.ISB_CLICK))
            # Die Anzeige der belegten Taste liegt 5 IDs hoeher (27–31).
            key = self.settings.get(setting) or ''
            buttons.append((button_id + 5, 20, top, 5, 5, f"{str(key).upper()}",
                            pyinsim.ISB_LIGHT))
        buttons.append((BTN_CLOSE, 0, 105, 25, 5, "^1" + self.translator.get("Close", lang),
                        pyinsim.ISB_DARK | pyinsim.ISB_CLICK))
        return buttons

    def open_keys_settings(self):
        """Öffnet die Einstellungen für Tastenbelegung und Achsen"""
        self._open('keys', self._buttons_keys())

    # ─── Await Key Binding ────────────────────────────────────────────

    def _buttons_await_key(self) -> List[Button]:
        lang = self.language
        setting = self.pending_key_setting
        text = (f"^7{self.translator.get('Key', lang)} {setting}, "
                f"{self.translator.get('currently bound to', lang)} "
                f"'{self.settings.get(setting)}'.")

        return [
            (21, 0, 80, 25, 5, self.translator.get("Rebind Key", lang),
             pyinsim.ISB_LIGHT),
            (22, 0, 85, 25, 5, self.translator.get("Press a key to bind...", lang),
             pyinsim.ISB_LIGHT),
            (23, 0, 90, 50, 5, text,
             pyinsim.ISB_LIGHT),
            (BTN_CLOSE, 0, 95, 25, 5, "^1" + self.translator.get("Cancel", lang),
             pyinsim.ISB_DARK | pyinsim.ISB_CLICK),
        ]

    def open_awaiting_key(self, setting):
        """Show user prompt to press a key for binding"""
        self.pending_key_setting = setting
        self._open('await_key', self._buttons_await_key())

    # ─── Helpers ──────────────────────────────────────────────────────

    def _clear_menu_buttons(self):
        """Löscht alle Menü-Buttons

        Ueber die Button-Registry: es geht genau ein Paket pro wirklich
        sichtbarem Button raus, nicht 21 Aufrufe.
        """
        self.ui_manager.message_sender.remove_range(*MENU_RANGE)

    def create_open_menu_button(self):
        self.ui_manager.message_sender.create_button(
            BTN_OPEN_MENU, 0, 100, 20, 10,
            self.translator.get("Main Menu", self.language),
            pyinsim.ISB_DARK | pyinsim.ISB_CLICK,
        )

    def close_menu(self):
        """Schließt alle Menüs"""
        self.current_menu = 'none'
        self._clear_menu_buttons()
        self.create_open_menu_button()

    def _move_hud(self, dx: int, dy: int):
        """Verschiebt den HUD und haelt ihn dabei auf dem Schirm

        Vorher wurden nur die Eckwerte 0 und 200 begrenzt - der HUD-Block
        reicht aber von x-3 bis x+29 und von y-6 bis y+13, also fielen
        PDC-Anzeige, Sirenen-Buttons und die Notification-Zeile heraus
        (known-issues #27).
        """
        x, y = clamp_hud_position(self.settings.get('hud_width') + dx,
                                  self.settings.get('hud_height') + dy)
        self.settings.set('hud_width', x)
        self.settings.set('hud_height', y)
        if hud_overlaps_reserved_area(x, y):
            logger.info("HUD at (%d, %d) is inside the area LFS reserves for its "
                        "own UI (L 0-110, T 30-170).", x, y)
        self.open_system_settings()

    def change_language(self):
        """Wechselt die Sprache"""
        available_langs = self.translator.supported_languages
        try:
            current_index = available_langs.index(self.language)
        except ValueError:
            # Eine handeditierte settings.json kann alles enthalten; das
            # Schema faengt das ab, aber ein Absturz hier waere trotzdem einer.
            current_index = -1
        new_language = available_langs[(current_index + 1) % len(available_langs)]
        self.settings.set('language', new_language)
        self.open_main_menu()

    def _toggle(self, key: str, repaint: Callable[[], None]):
        """Schaltet einen Boolean um und zeichnet das Menue neu

        Das Neuzeichnen gehoert dazu: die ^1/^2-Farbe im Label *ist* die
        Zustandsanzeige (reference/ui.md §4).
        """
        self.settings.set(key, not self.settings.get(key))
        repaint()

    def _cycle(self, key: str, values: Tuple, repaint: Callable[[], None]):
        """Schaltet eine Einstellung zyklisch durch ``values``"""
        current = self.settings.get(key)
        try:
            index = values.index(current)
        except ValueError:
            index = -1
        self.settings.set(key, values[(index + 1) % len(values)])
        repaint()

    def _notify(self, colour: str, key: str):
        self.ui_manager.event_bus.emit(
            "notification", {'notification': colour + self.translator.get(key, self.language)})

    # ─── Aktionen ─────────────────────────────────────────────────────

    def _build_actions(self) -> Dict[str, Dict[int, Callable[[], None]]]:
        """Baut die Tabelle ``menu → {button_id: action}``

        Einmal im Konstruktor, nicht pro Klick - die Werte sind gebundene
        Methoden bzw. kleine Lambdas ueber ``_toggle``/``_cycle``.
        """
        driving = self.open_driving_menu
        system = self.open_system_settings

        actions: Dict[str, Dict[int, Callable[[], None]]] = {
            'main': {
                22: self.open_driving_menu,
                23: self.open_parking_menu,
                24: self.open_system_settings,
                25: self.open_cop_menu,
                26: self.open_keys_settings,
                27: self.change_language,
                28: self.open_ai_traffic_menu,
            },
            'driving': {
                22: lambda: self._toggle('forward_collision_warning', driving),
                23: lambda: self._cycle('collision_warning_distance', (0, 1, 2), driving),
                24: lambda: self._toggle('blind_spot_warning', driving),
                25: lambda: self._toggle('cross_traffic_warning', driving),
                26: lambda: self._toggle('automatic_gearbox', driving),
                27: lambda: self._toggle('auto_hold', driving),
                28: lambda: self._toggle('adaptive_lights', driving),
                29: lambda: self._toggle('high_beam_assist', driving),
                30: self._calibrate_gearbox,
                31: lambda: self._cycle('cross_traffic_warning_distance', (0, 1, 2), driving),
            },
            'parking': {
                22: self._toggle_pdc,
                23: self._cycle_pdc_mode,
            },
            'system': {
                22: lambda: self._cycle('unit', ('metric', 'imperial'), system),
                24: lambda: self._toggle('hud_active', system),
                26: lambda: self._move_hud(0, -HUD_STEP),
                27: lambda: self._move_hud(0, HUD_STEP),
                28: lambda: self._move_hud(-HUD_STEP, 0),
                29: lambda: self._move_hud(HUD_STEP, 0),
            },
            'cop': {
                22: lambda: self._toggle('cop_assistance', self.open_cop_menu),
            },
            'ai_traffic': {
                22: self._toggle_ai_traffic,
            },
            'keys': {},
            'await_key': {},     # nur Abbrechen (BTN_CLOSE)
        }
        for button_id, setting, _label in self.KEY_BINDINGS:
            actions['keys'][button_id] = self._make_rebind_action(setting)
        return actions

    def _make_rebind_action(self, setting: str) -> Callable[[], None]:
        def action():
            self.ui_manager.event_bus.emit('await_keybinding', {'setting': setting})
            self.open_awaiting_key(setting)
        return action

    def _calibrate_gearbox(self):
        self.ui_manager.event_bus.emit('gearbox_calibrate', {})
        self._notify('^3', "Gearbox calibration requested...")
        self.close_menu()

    def _toggle_pdc(self):
        """PDC an/aus

        Einzige gespeicherte Groesse ist der Modus; ``park_distance_control``
        wird daraus abgeleitet. Der zuletzt gewaehlte Modus wird gemerkt, damit
        Aus/Ein die Audio-Einstellung nicht verwirft. Beim Ausschalten muss die
        Anzeige weg, sonst bleiben die PDC-Buttons bis zum naechsten
        Zustandswechsel stehen.
        """
        if self.settings.get('park_distance_control'):
            self._last_pdc_mode = self.settings.get('park_distance_control_mode')
            self.settings.set('park_distance_control_mode', 0)
            self.ui_manager.remove_pdc_display()
        else:
            self.settings.set('park_distance_control_mode', self._last_pdc_mode)
        self.open_parking_menu()

    def _cycle_pdc_mode(self):
        """Wechselt zwischen 1 = Visual und 2 = Visual & Audio

        Modus 0 gehoert dem Ein/Aus-Schalter, deshalb faehrt dieser Button ihn
        nicht an.
        """
        if not self.settings.get('park_distance_control'):
            return
        self._cycle('park_distance_control_mode', (1, 2), self.open_parking_menu)

    def _toggle_ai_traffic(self):
        if self.ai_traffic_active:
            self.ui_manager.event_bus.emit('ai_traffic_stop', {})
            self._notify('^3', "AI Traffic stopping...")
        else:
            self.ui_manager.event_bus.emit('ai_traffic_start', {})
            # AIDriver validates the track and emits its own notifications.
            # ai_traffic_active is updated by the synchronous callback
            # _on_ai_traffic_state_changed which fires during the emit above.
            if self.ai_traffic_active:
                self._notify('^2', "AI Traffic started.")
                self._notify('^3', "Camera needs to be on own vehicle.")
        self.open_ai_traffic_menu()

    # ─── Click Handling ───────────────────────────────────────────────

    def _handle_ui_action(self, data):
        """Verarbeitet UI-Aktionen"""
        try:
            button_id = int(getattr(data, 'ClickID'))
        except (AttributeError, TypeError, ValueError):
            # Kein IS_BTC oder ein Feld, das LFS so nicht schicken sollte.
            return
        if BTN_OPEN_MENU <= button_id <= BTN_CLOSE:
            self._handle_menu_click(button_id)

    def _handle_menu_click(self, button_id: int):
        """Verarbeitet Menü-Klicks über die Aktionstabelle des aktiven Menüs"""

        # ── Open menu from the floating button ──
        if self.current_menu == 'none':
            if button_id == BTN_OPEN_MENU:
                self.open_main_menu()
            return

        # ── Close button — always returns to main menu (except from main) ──
        if button_id == BTN_CLOSE:
            self._handle_close()
            return

        action = self._actions.get(self.current_menu, {}).get(button_id)
        if action is None:
            logger.debug("Button %d has no action in menu %r", button_id, self.current_menu)
            return
        action()

    def _handle_close(self):
        if self.current_menu == 'main':
            self.close_menu()
        elif self.current_menu == 'await_key':
            self.keybinder.stop_listening()
            self._notify('^1', "Keybinding cancelled.")
            self.open_keys_settings()
        else:
            self.open_main_menu()
