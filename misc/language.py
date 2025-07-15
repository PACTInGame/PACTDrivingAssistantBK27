class LanguageManager:
    """
    A language management class that handles translations for multiple languages.
    Supports easy extension and fallback to English if translation is not found.
    """

    def __init__(self):
        """Initialize the language manager with default translations."""
        self.supported_languages = ['en', 'de', 'it', 'fr', 'tr', 'no', 'dk', 'se']
        self.default_language = 'en'

        # Translation dictionary - organized by English key, then by language code
        self.translations = {
            "Main Menu": {
                "en": "Main Menu",
                "de": "Hauptmenü",
                "it": "Menu Principale",
                "fr": "Menu Principal",
                "tr": "Ana Menü",
                "no": "Hovedmeny",
                "dk": "Hovedmenu",
                "se": "Huvudmeny"
            },
            "Driving": {
                "en": "Driving",
                "de": "Fahren",
                "it": "Guida",
                "fr": "Conduite",
                "tr": "Sürüş",
                "no": "Kjøring",
                "dk": "Kørsel",
                "se": "Körning"
            },
            "Parking": {
                "en": "Parking",
                "de": "Parken",
                "it": "Parcheggio",
                "fr": "Stationnement",
                "tr": "Park Etme",
                "no": "Parkering",
                "dk": "Parkering",
                "se": "Parkering"
            },
            "Language": {
                "en": "Language",
                "de": "Sprache",
                "it": "Lingua",
                "fr": "Langue",
                "tr": "Dil",
                "no": "Språk",
                "dk": "Sprog",
                "se": "Språk"
            },
            "System": {
                "en": "System",
                "de": "System",
                "it": "Sistema",
                "fr": "Système",
                "tr": "Sistem",
                "no": "System",
                "dk": "System",
                "se": "System"
            },
            "Close": {
                "en": "Close",
                "de": "Schließen",
                "it": "Chiudi",
                "fr": "Fermer",
                "tr": "Kapat",
                "no": "Lukk",
                "dk": "Luk",
                "se": "Stäng"
            }
        }

    def get(self, english_key, language_code=None):
        """
        Get the translated string for the given English key and language.

        Args:
            english_key (str): The English string to translate
            language_code (str): The target language code (en, de, it, fr, tr, no, dk, se)
                                If None, returns the English version

        Returns:
            str: The translated string, or the English version if translation not found
        """
        # Default to English if no language code provided
        if language_code is None:
            language_code = self.default_language

        # Validate language code
        if language_code not in self.supported_languages:
            language_code = self.default_language

        # Check if the English key exists in translations
        if english_key not in self.translations:
            # Return the original key if no translation entry exists
            return english_key

        # Get the translation for the specific language
        translation_dict = self.translations[english_key]

        result = translation_dict.get(language_code, translation_dict.get(self.default_language, english_key))
        # Return translation if available, otherwise fallback to English
        return result


    def get_supported_languages(self):
        """
        Get list of supported language codes.

        Returns:
            list: List of supported language codes
        """
        return self.supported_languages.copy()

    def set_default_language(self, language_code):
        """
        Set the default fallback language.

        Args:
            language_code (str): Language code to use as default
        """
        if language_code in self.supported_languages:
            self.default_language = language_code

    def get_all_translations(self, english_key):
        """
        Get all available translations for a given English key.

        Args:
            english_key (str): The English string to get translations for

        Returns:
            dict: Dictionary with language codes as keys and translations as values
        """
        return self.translations.get(english_key, {}).copy()

    def load_translations_from_file(self, filepath):
        """
        Load translations from a file (placeholder for future implementation).

        Args:
            filepath (str): Path to the translation file
        """
        # This method can be implemented to load translations from JSON, CSV, etc.
        # For now, it's a placeholder for extensibility
        pass

    def save_translations_to_file(self, filepath):
        """
        Save current translations to a file (placeholder for future implementation).

        Args:
            filepath (str): Path where to save the translations
        """
        # This method can be implemented to save translations to JSON, CSV, etc.
        # For now, it's a placeholder for extensibility
        pass