import json
import os
from typing import Any, Dict

class SettingsManager:
    """Verwaltet alle Einstellungen mit Persistierung"""

    def __init__(self, settings_file: str = "settings.json"):
        self.settings_file = settings_file
        self._settings: Dict[str, Any] = {}
        self._defaults: Dict[str, Any] = {
            'forward_collision_warning': True,
            'blind_spot_warning': True,
            'cross_traffic_warning': True,
            'automatic_gearbox': False,
            'language': 'en',
            'collision_warning_distance': 1,
            'ui_refresh_rate': 200,
            'assistance_refresh_rate': 100,
        }
        self.load()

    def get(self, key: str, default: Any = None) -> Any:
        """Holt einen Einstellungswert"""
        return self._settings.get(key, default or self._defaults.get(key))

    def set(self, key: str, value: Any):
        """Setzt einen Einstellungswert"""
        self._settings[key] = value
        self.save()

    def load(self):
        """Lädt Einstellungen aus Datei"""
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, 'r') as f:
                    self._settings = json.load(f)
            except Exception as e:
                print(f"Error loading settings: {e}")
                self._settings = self._defaults.copy()
        else:
            self._settings = self._defaults.copy()

    def save(self):
        """Speichert Einstellungen in Datei"""
        try:
            with open(self.settings_file, 'w') as f:
                json.dump(self._settings, f, indent=2)
        except Exception as e:
            print(f"Error saving settings: {e}")