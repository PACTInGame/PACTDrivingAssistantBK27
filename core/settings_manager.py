import json
import logging
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from misc.helpers import resolve_path
from misc.language import SUPPORTED_LANGUAGES

logger = logging.getLogger(__name__)

# Dateiversion. Wird beim Laden geprueft; aeltere Dateien laufen durch
# ``_migrate`` und werden danach mit der aktuellen Version zurueckgeschrieben.
SETTINGS_VERSION = 1
VERSION_KEY = '_version'

# Menueklicks kommen aus dem Paket-Thread. Jeder ``set()`` schrieb frueher die
# komplette Datei synchron - beim Durchklicken der HUD-Pfeile also ein
# Dateisystem-Roundtrip pro Klick im Hot Path. Stattdessen wird die Datei
# gesammelt und verzoegert geschrieben.
SAVE_DEBOUNCE_S = 0.5


@dataclass(frozen=True)
class Setting:
    """Schema einer Einstellung: Standardwert, Typ und zulaessiger Bereich.

    ``kind`` ist einer von ``bool``, ``int``, ``float``, ``str``. Werte aus der
    Datei werden dagegen geprueft und repariert - eine handeditierte ``0`` bei
    ``assistance_refresh_rate`` hat die App vorher in einen Busy-Loop geschickt.
    """
    default: Any
    kind: type
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Optional[Tuple[Any, ...]] = None
    comment: str = ""


_SCHEMA: Dict[str, Setting] = {
    # ─── Assistenz-Schalter ───────────────────────────────────────────
    'forward_collision_warning': Setting(True, bool),
    'blind_spot_warning': Setting(True, bool),
    'cross_traffic_warning': Setting(True, bool),
    'automatic_gearbox': Setting(False, bool),
    'auto_hold': Setting(True, bool),
    'adaptive_lights': Setting(True, bool),
    'high_beam_assist': Setting(True, bool),
    'cop_assistance': Setting(True, bool),
    'ai_traffic': Setting(True, bool),

    # ─── Schwellen und Modi ───────────────────────────────────────────
    'collision_warning_distance': Setting(1, int, choices=(0, 1, 2),
                                          comment="0 = Early, 1 = Normal, 2 = Late"),
    'cross_traffic_warning_distance': Setting(1, int, choices=(0, 1, 2),
                                              comment="0 = Early, 1 = Medium, 2 = Late"),
    'automatic_emergency_brake': Setting(1, int, choices=(0, 1, 2),
                                         comment="0 = Off, 1 = Warn, 2 = Warn & Brake "
                                                 "- inert, siehe control-intervention.md"),
    'parking_emergency_brake': Setting(True, bool),

    # PDC hat genau eine Darstellung: den Modus. Der frueher parallel
    # gespeicherte Schalter ``park_distance_control`` wird daraus abgeleitet
    # (siehe _DERIVED) - vorher konnten sich beide widersprechen, und die
    # ausgelieferte Kombination (Schalter an, Modus 0) tat genau das: das
    # Menue zeigte PDC als aktiv, angezeigt wurde nie etwas.
    'park_distance_control_mode': Setting(1, int, choices=(0, 1, 2),
                                          comment="0 = Off, 1 = Visual, 2 = Visual & Audio"),

    # ─── Darstellung ──────────────────────────────────────────────────
    'language': Setting('de', str, choices=SUPPORTED_LANGUAGES),
    'unit': Setting('metric', str, choices=('metric', 'imperial')),
    'hud_active': Setting(True, bool),
    # Bildschirmkoordinaten, kein Mass: 0…200 ist die LFS-Button-Flaeche.
    # Die feinere Begrenzung macht ui.clamp_hud_position.
    'hud_height': Setting(119, int, minimum=0, maximum=200),
    'hud_width': Setting(90, int, minimum=0, maximum=200),

    # ─── Zeitverhalten ────────────────────────────────────────────────
    # assistance_refresh_rate ist zugleich das MCI-``Interval`` (LFSConnector)
    # und die Periode des Assistenz-Threads. LFS akzeptiert 40…8000 ms; unter
    # 50 ms haelt der Zyklus das Budget nicht mehr ein, ueber 200 ms wird die
    # Kollisionswarnung zu traege (CLAUDE.md §1).
    'assistance_refresh_rate': Setting(100, int, minimum=50, maximum=200),
    'ui_refresh_rate': Setting(50, int, minimum=20, maximum=500),

    # ─── Eingabe ──────────────────────────────────────────────────────
    'user_handbrake_key': Setting("q", str),
    'user_shift_up_key': Setting("s", str),
    'user_shift_down_key': Setting("x", str),
    'user_clutch_key': Setting("c", str),
    'user_ignition_key': Setting("i", str),
    'user_brake_key': Setting("down", str),

    'user_axis_steering': Setting(8, int, minimum=0, maximum=31),
    'user_axis_throttle': Setting(9, int, minimum=0, maximum=31),
    'user_axis_brake': Setting(12, int, minimum=0, maximum=31),
    'user_axis_clutch': Setting(13, int, minimum=0, maximum=31),
    'vjoy_axis_1': Setting(15, int, minimum=0, maximum=31),

    # Vom Nutzer gewaehlter Eingabemodus. Wird *nicht* mehr aus IS_NPL
    # ueberschrieben - der erkannte Modus steht kameraunabhaengig in
    # ``vehicle.data.control_mode`` und im Event ``player_name_changed``
    # (reference/conventions.md §5.4).
    'own_control_mode': Setting(0, int, choices=(0, 1, 2),
                                comment="0 = Mouse, 1 = Keyboard, 2 = Joystick"),
}


class SettingsManager:
    """Verwaltet alle Einstellungen mit Persistierung

    Drei Zusagen, auf die sich der Rest der App verlassen darf:

    * **Jeder bekannte Schluessel liefert immer einen gueltigen Wert.** Fehlt er
      in der Datei, kommt der Standardwert; ist er kaputt, wird er repariert
      und die Reparatur protokolliert. Ein ``default``-Argument an ``get()``
      greift nur fuer Schluessel, die das Schema gar nicht kennt - frueher
      verdeckte es den Standardwert, wodurch jedes neu hinzugefuegte
      Assistenzsystem bei Bestandsnutzern dauerhaft aus blieb.
    * **Unbekannte Schluessel gehen nicht verloren.** Eine Datei aus einer
      neueren Version ueberlebt einen Downgrade.
    * **Schreiben ist gebuendelt und atomar.** ``set()`` kommt aus dem
      Paket-Thread; geschrieben wird verzoegert ueber tmp + ``os.replace``.
    """

    def __init__(self, settings_file: str = "settings.json"):
        self.settings_file = resolve_path(settings_file)
        self._schema = _SCHEMA
        self._defaults: Dict[str, Any] = {key: spec.default
                                          for key, spec in _SCHEMA.items()}
        # Abgeleitete Schluessel: gespeichert wird nur die Quelle.
        self._derived: Dict[str, Tuple[Callable[[], Any], Callable[[Any], None]]] = {
            'park_distance_control': (self._get_park_distance_control,
                                      self._set_park_distance_control),
        }
        self._settings: Dict[str, Any] = {}
        self._lock = threading.RLock()
        self._save_timer: Optional[threading.Timer] = None
        self._dirty = False
        self.load()

    # ─── Zugriff ──────────────────────────────────────────────────────

    @property
    def known_keys(self) -> frozenset:
        """Alle Schluessel, die die App kennt - gespeicherte und abgeleitete.

        ``AssistanceSystem.is_enabled`` schlaegt den eigenen Systemnamen hier
        nach; ein System, dessen Name hier fehlt, kann nie eingeschaltet werden.
        """
        return frozenset(self._schema) | frozenset(self._derived)

    def get(self, key: str, default: Any = None) -> Any:
        """Holt einen Einstellungswert

        ``default`` ist ausdruecklich *nur* der Notnagel fuer Schluessel, die
        das Schema nicht kennt. Fuer bekannte Schluessel gewinnt immer der
        gespeicherte Wert bzw. der Standardwert aus dem Schema.
        """
        derived = self._derived.get(key)
        if derived is not None:
            return derived[0]()
        with self._lock:
            if key in self._settings:
                return self._settings[key]
        spec = self._schema.get(key)
        if spec is not None:
            return spec.default
        return default

    def set(self, key: str, value: Any):
        """Setzt einen Einstellungswert"""
        derived = self._derived.get(key)
        if derived is not None:
            derived[1](value)
            return

        value = self._repair(key, value, origin='set')
        with self._lock:
            if key in self._settings and self._settings[key] == value \
                    and type(self._settings[key]) is type(value):
                return
            self._settings[key] = value
        logger.debug("Setting %s to %r", key, value)
        self._schedule_save()

    # ─── Abgeleitete Schluessel ───────────────────────────────────────

    def _get_park_distance_control(self) -> bool:
        return self.get('park_distance_control_mode') != 0

    def _set_park_distance_control(self, value: Any):
        """PDC an/aus, ausgedrueckt im Modus.

        Beim Einschalten bleibt ein bereits gewaehlter Modus erhalten;
        aus dem Aus heraus kommt der Standardmodus.
        """
        if value:
            if self.get('park_distance_control_mode') == 0:
                self.set('park_distance_control_mode',
                         self._schema['park_distance_control_mode'].default)
        else:
            self.set('park_distance_control_mode', 0)

    # ─── Validierung ──────────────────────────────────────────────────

    def _repair(self, key: str, value: Any, origin: str) -> Any:
        """Prueft einen Wert gegen das Schema und repariert ihn notfalls"""
        spec = self._schema.get(key)
        if spec is None:
            return value            # unbekannter Schluessel: unveraendert behalten

        coerced = _coerce(value, spec.kind)
        if coerced is None:
            logger.warning("Setting '%s' (%s): %r is not a %s - using the default %r.",
                           key, origin, value, spec.kind.__name__, spec.default)
            return spec.default

        if spec.choices is not None and coerced not in spec.choices:
            logger.warning("Setting '%s' (%s): %r is not one of %s - using the default %r.",
                           key, origin, coerced, list(spec.choices), spec.default)
            return spec.default

        clamped = coerced
        if spec.minimum is not None and clamped < spec.minimum:
            clamped = spec.kind(spec.minimum)
        if spec.maximum is not None and clamped > spec.maximum:
            clamped = spec.kind(spec.maximum)
        if clamped != coerced:
            logger.warning("Setting '%s' (%s): %r is outside %s…%s - clamped to %r.",
                           key, origin, coerced, spec.minimum, spec.maximum, clamped)
        return clamped

    # ─── Laden, Migration, Speichern ──────────────────────────────────

    def load(self):
        """Laedt Einstellungen aus Datei und mischt fehlende Standardwerte ein

        Reihenfolge: lesen → migrieren → Standardwerte ergaenzen → validieren.
        Danach steht in ``_settings`` jeder bekannte Schluessel mit einem
        gueltigen Wert, plus alles, was die Datei sonst noch enthielt.
        """
        stored = self._read_file()
        changed = self._migrate(stored)

        merged: Dict[str, Any] = dict(self._defaults)
        merged.update(stored)               # Nutzerwerte und unbekannte Schluessel gewinnen
        for key in list(merged):
            if key == VERSION_KEY:
                continue
            repaired = self._repair(key, merged[key], origin='settings.json')
            if repaired != merged[key] or type(repaired) is not type(merged[key]):
                merged[key] = repaired
                changed = True
        # Abgeleitete Schluessel gehoeren nicht in die Datei.
        for key in self._derived:
            if merged.pop(key, None) is not None:
                changed = True
        if merged.get(VERSION_KEY) != SETTINGS_VERSION:
            merged[VERSION_KEY] = SETTINGS_VERSION
            changed = True
        if set(merged) != set(stored):
            changed = True

        with self._lock:
            self._settings = merged
        if changed:
            self._schedule_save()

    def _read_file(self) -> Dict[str, Any]:
        """Liest die JSON-Datei; eine kaputte Datei wird beiseitegelegt"""
        if not os.path.exists(self.settings_file):
            return {}
        try:
            with open(self.settings_file, 'r', encoding='utf-8') as handle:
                data = json.load(handle)
        except Exception as e:
            logger.error("Error loading settings from %s: %s: %s - falling back to defaults.",
                         self.settings_file, type(e).__name__, e)
            self._quarantine_file()
            return {}
        if not isinstance(data, dict):
            logger.error("Settings file %s does not contain an object - "
                         "falling back to defaults.", self.settings_file)
            self._quarantine_file()
            return {}
        return data

    def _quarantine_file(self):
        """Benennt eine unlesbare Datei um, statt sie kommentarlos zu ueberschreiben"""
        broken = self.settings_file + '.corrupt'
        try:
            os.replace(self.settings_file, broken)
        except OSError as e:
            logger.warning("Could not keep a copy of the broken settings file: %s", e)
        else:
            logger.warning("Kept the unreadable settings file as %s", broken)

    def _migrate(self, data: Dict[str, Any]) -> bool:
        """Hebt eine aeltere Datei auf die aktuelle Version

        Rueckgabe: True, wenn etwas geaendert wurde.
        """
        version = data.get(VERSION_KEY)
        if isinstance(version, bool) or not isinstance(version, int):
            version = 0
        if version >= SETTINGS_VERSION:
            return False

        if version < 1:
            self._migrate_pdc_to_mode(data)
        if data:
            # Eine leere/fehlende Datei ist keine Migration, sondern ein
            # Erstlauf - dafuer gibt es keine Meldung.
            logger.info("Migrated settings from version %d to %d.",
                        version, SETTINGS_VERSION)
        data[VERSION_KEY] = SETTINGS_VERSION
        return True

    @staticmethod
    def _migrate_pdc_to_mode(data: Dict[str, Any]) -> bool:
        """v0 → v1: PDC hatte einen Schalter *und* einen Modus.

        Die Absicht des Nutzers steckt in der Kombination: Schalter aus heisst
        Modus 0; Schalter an mit Modus 0 ist der widerspruechliche
        Auslieferungszustand und wird zum Standardmodus.
        """
        if 'park_distance_control' not in data:
            return False
        enabled = bool(data.pop('park_distance_control'))
        mode = data.get('park_distance_control_mode')
        if not isinstance(mode, int) or isinstance(mode, bool) or mode not in (0, 1, 2):
            mode = None
        if not enabled:
            data['park_distance_control_mode'] = 0
        elif not mode:
            data['park_distance_control_mode'] = \
                _SCHEMA['park_distance_control_mode'].default
        else:
            data['park_distance_control_mode'] = mode
        return True

    def _schedule_save(self):
        """Merkt die Datei als schmutzig und startet den Sammel-Timer"""
        with self._lock:
            self._dirty = True
            if self._save_timer is not None:
                return
            timer = threading.Timer(SAVE_DEBOUNCE_S, self._flush_timer)
            # Kein Daemon: ein noch ausstehender Schreibvorgang muss den
            # Programmende ueberleben, sonst geht der letzte Menueklick verloren.
            timer.daemon = False
            timer.name = "settings-save"
            self._save_timer = timer
            timer.start()

    def _flush_timer(self):
        with self._lock:
            self._save_timer = None
        self.flush()

    def flush(self):
        """Schreibt ausstehende Aenderungen sofort"""
        with self._lock:
            if not self._dirty:
                return
            self._dirty = False
            snapshot = dict(self._settings)
        self._write(snapshot)

    def save(self):
        """Speichert Einstellungen in Datei (sofort)"""
        with self._lock:
            self._dirty = False
            snapshot = dict(self._settings)
        self._write(snapshot)

    def _write(self, snapshot: Dict[str, Any]):
        """Schreibt atomar: erst in eine temporaere Datei, dann umbenennen

        Ein Absturz oder ein voller Datentraeger hinterlaesst so nie eine halb
        geschriebene settings.json.
        """
        tmp_path = self.settings_file + '.tmp'
        try:
            directory = os.path.dirname(self.settings_file)
            if directory:
                os.makedirs(directory, exist_ok=True)
            with open(tmp_path, 'w', encoding='utf-8') as handle:
                json.dump(snapshot, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.settings_file)
        except Exception as e:
            logger.error("Error saving settings to %s: %s: %s",
                         self.settings_file, type(e).__name__, e)
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _coerce(value: Any, kind: type) -> Any:
    """Bringt einen JSON-Wert auf den erwarteten Typ, oder None bei Unsinn

    ``bool`` ist in Python eine ``int``-Unterklasse - deshalb ueberall die
    ausdrueckliche Trennung, sonst wuerde ``True`` als Zahl 1 durchgehen.
    """
    if kind is bool:
        return value if isinstance(value, bool) else None
    if kind is int:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        return None
    if kind is float:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        return None
    if kind is str:
        return value if isinstance(value, str) else None
    return value
