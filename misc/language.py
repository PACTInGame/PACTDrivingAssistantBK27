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
            # ─── Main Menu ────────────────────────────────────────────
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
                "tr": "Sürüs",
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
            },
            "Auto Hold": {
                "en": "Auto Hold",
                "de": "Auto Hold",
                "it": "Freno Auto",
                "fr": "Frein Auto",
                "tr": "Otomatik El Freni",
                "no": "Auto Hold",
                "dk": "Auto Hold",
                "se": "Auto Hold"
            },
            # ─── Main Menu Entries ────────────────────────────────────
            "Cop Mode": {
                "en": "Cop Mode",
                "de": "Polizei-Modus",
                "it": "Modalità Polizia",
                "fr": "Mode Police",
                "tr": "Polis Modu",
                "no": "Politimodus",
                "dk": "Politimodus",
                "se": "Polisläge"
            },
            "Keys and Axes": {
                "en": "Keys and Axes",
                "de": "Tasten & Achsen",
                "it": "Tasti e Assi",
                "fr": "Touches et Axes",
                "tr": "Tuşlar ve Eksenler",
                "no": "Taster og Akser",
                "dk": "Taster og Akser",
                "se": "Knappar och Axlar"
            },
            "AI Traffic": {
                "en": "AI Traffic",
                "de": "KI-Verkehr",
                "it": "Traffico IA",
                "fr": "Trafic IA",
                "tr": "YZ Trafik",
                "no": "KI-Trafikk",
                "dk": "AI-Trafik",
                "se": "AI-Trafik"
            },
            # ─── Driving Menu ─────────────────────────────────────────
            "Driving Settings": {
                "en": "Driving Settings",
                "de": "Fahreinstellungen",
                "it": "Impost. di Guida",
                "fr": "Réglages Conduite",
                "tr": "Sürüş Ayarları",
                "no": "Kjøreinnstillinger",
                "dk": "Køreindstillinger",
                "se": "Körinställningar"
            },
            "Collision Warning": {
                "en": "Collision Warning",
                "de": "Kollisionswarnung",
                "it": "Avviso Collisione",
                "fr": "Alerte Collision",
                "tr": "Çarpışma Uyarısı",
                "no": "Kollisjonsvarsel",
                "dk": "Kollisionsadvarsel",
                "se": "Kollisionsvarning"
            },
            "Blind Spot Warn.": {
                "en": "Blind Spot Warn.",
                "de": "Totwinkelwarnung",
                "it": "Avviso Ang. Cieco",
                "fr": "Alerte Angle Mort",
                "tr": "Kör Nokta Uyarısı",
                "no": "Blindsonevarsel",
                "dk": "Blindvinkeladv.",
                "se": "Dödavinkelvarning"
            },
            "Cross Traffic Warn.": {
                "en": "Cross Traffic Warn.",
                "de": "Querverkehrswarnung",
                "it": "Avviso Traffico Lat.",
                "fr": "Alerte Trafic Lat.",
                "tr": "Çapraz Trafik Uyar.",
                "no": "Krysstrafikkvars.",
                "dk": "Tværtrafikadvarsel",
                "se": "Tvärtrafik varning"
            },
            "Automatic Gearbox": {
                "en": "Automatic Gearbox",
                "de": "Automatikgetriebe",
                "it": "Cambio Automatico",
                "fr": "Boîte Automatique",
                "tr": "Otomatik Vites",
                "no": "Automatgir",
                "dk": "Automatgear",
                "se": "Automatlåda"
            },
            "Calibrate": {
                "en": "Calibrate",
                "de": "Kalibrieren",
                "it": "Calibra",
                "fr": "Calibrer",
                "tr": "Kalibre Et",
                "no": "Kalibrer",
                "dk": "Kalibrer",
                "se": "Kalibrera"
            },
            "Adaptive Lights": {
                "en": "Adaptive Lights",
                "de": "Adaptives Licht",
                "it": "Luci Adattive",
                "fr": "Phares Adaptatifs",
                "tr": "Adaptif Farlar",
                "no": "Adaptive Lys",
                "dk": "Adaptive Lys",
                "se": "Adaptiv Belysning"
            },
            "High Beam Assist": {
                "en": "High Beam Assist",
                "de": "Fernlichtassistent",
                "it": "Assist. Abbaglianti",
                "fr": "Assist. Feux Route",
                "tr": "Uzun Far Asistanı",
                "no": "Fjernlysassistent",
                "dk": "Fjernlysassistent",
                "se": "Helljusassistent"
            },
            "Early": {
                "en": "Early",
                "de": "Früh",
                "it": "Presto",
                "fr": "Tôt",
                "tr": "Erken",
                "no": "Tidlig",
                "dk": "Tidlig",
                "se": "Tidig"
            },
            "Medium": {
                "en": "Medium",
                "de": "Mittel",
                "it": "Medio",
                "fr": "Moyen",
                "tr": "Orta",
                "no": "Middels",
                "dk": "Middel",
                "se": "Medel"
            },
            "Late": {
                "en": "Late",
                "de": "Spät",
                "it": "Tardi",
                "fr": "Tard",
                "tr": "Geç",
                "no": "Sent",
                "dk": "Sent",
                "se": "Sen"
            },
            # ─── Parking Menu ─────────────────────────────────────────
            "Parking Settings": {
                "en": "Parking Settings",
                "de": "Parkeinstellungen",
                "it": "Impost. Parcheggio",
                "fr": "Réglages Station.",
                "tr": "Park Ayarları",
                "no": "Parkinnstillinger",
                "dk": "Parkindstillinger",
                "se": "Parkinställningar"
            },
            "Park Distance Control": {
                "en": "Park Distance Control",
                "de": "Einparkhilfe",
                "it": "Sensori Parcheggio",
                "fr": "Radar de Recul",
                "tr": "Park Sensörü",
                "no": "Parkeringssensor",
                "dk": "Parkeringssensor",
                "se": "Parkeringssensor"
            },
            "Visual": {
                "en": "Visual",
                "de": "Visuell",
                "it": "Visivo",
                "fr": "Visuel",
                "tr": "Görsel",
                "no": "Visuell",
                "dk": "Visuel",
                "se": "Visuell"
            },
            "Visual & Audio": {
                "en": "Visual & Audio",
                "de": "Visuell & Audio",
                "it": "Visivo & Audio",
                "fr": "Visuel & Audio",
                "tr": "Görsel & Ses",
                "no": "Visuell & Lyd",
                "dk": "Visuel & Lyd",
                "se": "Visuell & Ljud"
            },
            # ─── System Settings ──────────────────────────────────────
            "System Settings": {
                "en": "System Settings",
                "de": "Systemeinstellungen",
                "it": "Impost. Sistema",
                "fr": "Réglages Système",
                "tr": "Sistem Ayarları",
                "no": "Systeminnstillinger",
                "dk": "Systemindstillinger",
                "se": "Systeminställningar"
            },
            "Unit": {
                "en": "Unit",
                "de": "Einheit",
                "it": "Unità",
                "fr": "Unité",
                "tr": "Birim",
                "no": "Enhet",
                "dk": "Enhed",
                "se": "Enhet"
            },
            "Metric": {
                "en": "Metric",
                "de": "Metrisch",
                "it": "Metrico",
                "fr": "Métrique",
                "tr": "Metrik",
                "no": "Metrisk",
                "dk": "Metrisk",
                "se": "Metrisk"
            },
            "Imperial": {
                "en": "Imperial",
                "de": "Imperial",
                "it": "Imperiale",
                "fr": "Impérial",
                "tr": "İmparatorluk",
                "no": "Imperialt",
                "dk": "Imperielt",
                "se": "Imperialt"
            },
            "Head-Up Display": {
                "en": "Head-Up Display",
                "de": "Head-Up Display",
                "it": "Head-Up Display",
                "fr": "Affichage Tête Haute",
                "tr": "Head-Up Display",
                "no": "Head-Up Display",
                "dk": "Head-Up Display",
                "se": "Head-Up Display"
            },
            # ─── Cop Mode Menu ────────────────────────────────────────
            "Cop Mode Settings": {
                "en": "Cop Mode Settings",
                "de": "Polizei-Einstellungen",
                "it": "Impost. Polizia",
                "fr": "Réglages Police",
                "tr": "Polis Modu Ayarları",
                "no": "Politiinnstillinger",
                "dk": "Politiindstillinger",
                "se": "Polisinställningar"
            },
            "Cop Assistance": {
                "en": "Cop Assistance",
                "de": "Polizei-Assistent",
                "it": "Assist. Polizia",
                "fr": "Assist. Police",
                "tr": "Polis Asistanı",
                "no": "Politiassistent",
                "dk": "Politiassistent",
                "se": "Polisassistent"
            },
            # ─── AI Traffic Menu ──────────────────────────────────────
            "Stop AI Traffic": {
                "en": "Stop AI Traffic",
                "de": "KI-Verkehr stoppen",
                "it": "Ferma Traffico IA",
                "fr": "Arrêter Trafic IA",
                "tr": "YZ Trafiği Durdur",
                "no": "Stopp KI-Trafikk",
                "dk": "Stop AI-Trafik",
                "se": "Stoppa AI-Trafik"
            },
            "Start AI Traffic": {
                "en": "Start AI Traffic",
                "de": "KI-Verkehr starten",
                "it": "Avvia Traffico IA",
                "fr": "Démarrer Trafic IA",
                "tr": "YZ Trafiği Başlat",
                "no": "Start KI-Trafikk",
                "dk": "Start AI-Trafik",
                "se": "Starta AI-Trafik"
            },
            # ─── Keys and Axes Menu ───────────────────────────────────
            "Handbrake Key": {
                "en": "Handbrake Key",
                "de": "Handbremse Taste",
                "it": "Tasto Freno a Mano",
                "fr": "Touche Frein à Main",
                "tr": "El Freni Tuşu",
                "no": "Håndbrekkstast",
                "dk": "Håndbremsetast",
                "se": "Handbromsknapp"
            },
            "Shift Up Key": {
                "en": "Shift Up Key",
                "de": "Hochschalten Taste",
                "it": "Tasto Marcia Su",
                "fr": "Touche Rapport +",
                "tr": "Vites Yükselt Tuşu",
                "no": "Gir Opp Tast",
                "dk": "Gear Op Tast",
                "se": "Växla Upp Knapp"
            },
            "Shift Down Key": {
                "en": "Shift Down Key",
                "de": "Runterschalten Taste",
                "it": "Tasto Marcia Giù",
                "fr": "Touche Rapport -",
                "tr": "Vites Düşür Tuşu",
                "no": "Gir Ned Tast",
                "dk": "Gear Ned Tast",
                "se": "Växla Ned Knapp"
            },
            "Clutch Key": {
                "en": "Clutch Key",
                "de": "Kupplung Taste",
                "it": "Tasto Frizione",
                "fr": "Touche Embrayage",
                "tr": "Debriyaj Tuşu",
                "no": "Clutchtast",
                "dk": "Koblingstart",
                "se": "Kopplingsknapp"
            },
            "Ignition Key": {
                "en": "Ignition Key",
                "de": "Zündung Taste",
                "it": "Tasto Accensione",
                "fr": "Touche Contact",
                "tr": "Kontak Tuşu",
                "no": "Tenningtast",
                "dk": "Tændingstast",
                "se": "Tändningsknapp"
            },
            # ─── Await Key Binding ────────────────────────────────────
            "Rebind Key": {
                "en": "Rebind Key",
                "de": "Taste neu belegen",
                "it": "Riassegna Tasto",
                "fr": "Réassigner Touche",
                "tr": "Tuş Yeniden Ata",
                "no": "Tilordne Tast",
                "dk": "Gentildel Tast",
                "se": "Byt Knapp"
            },
            "Press a key to bind...": {
                "en": "Press a key to bind...",
                "de": "Taste drücken...",
                "it": "Premi un tasto...",
                "fr": "Appuyez sur une touche...",
                "tr": "Bir tuşa basın...",
                "no": "Trykk en tast...",
                "dk": "Tryk en tast...",
                "se": "Tryck en knapp..."
            },
            "Cancel": {
                "en": "Cancel",
                "de": "Abbrechen",
                "it": "Annulla",
                "fr": "Annuler",
                "tr": "İptal",
                "no": "Avbryt",
                "dk": "Annuller",
                "se": "Avbryt"
            },
            # ─── Notifications (menu_system.py) ──────────────────────
            "Keybinding cancelled.": {
                "en": "Keybinding cancelled.",
                "de": "Tastenbelegung abgebrochen.",
                "it": "Assegnazione annullata.",
                "fr": "Attribution annulée.",
                "tr": "Tuş ataması iptal edildi.",
                "no": "Tasttilordning avbrutt.",
                "dk": "Tasttildeling annulleret.",
                "se": "Knappbindning avbruten."
            },
            "Gearbox calibration requested...": {
                "en": "Gearbox calibration requested...",
                "de": "Getriebe-Kalibrierung angefordert...",
                "it": "Calibrazione cambio richiesta...",
                "fr": "Calibration boîte demandée...",
                "tr": "Vites kalibrasyonu istendi...",
                "no": "Girkalibrering forespurt...",
                "dk": "Gearkalibrering anmodet...",
                "se": "Växellådskalibrering begärd..."
            },
            "AI Traffic stopping...": {
                "en": "AI Traffic stopping...",
                "de": "KI-Verkehr wird gestoppt...",
                "it": "Traffico IA in arresto...",
                "fr": "Arrêt du trafic IA...",
                "tr": "YZ Trafiği durduruluyor...",
                "no": "KI-Trafikk stopper...",
                "dk": "AI-Trafik stopper...",
                "se": "AI-Trafik stoppas..."
            },
            "AI Traffic started.": {
                "en": "AI Traffic started.",
                "de": "KI-Verkehr gestartet.",
                "it": "Traffico IA avviato.",
                "fr": "Trafic IA démarré.",
                "tr": "YZ Trafiği başlatıldı.",
                "no": "KI-Trafikk startet.",
                "dk": "AI-Trafik startet.",
                "se": "AI-Trafik startad."
            },
            "Camera needs to be on own vehicle.": {
                "en": "Camera needs to be on own vehicle.",
                "de": "Kamera muss auf eigenem Fahrzeug sein.",
                "it": "La telecamera deve essere sul proprio veicolo.",
                "fr": "La caméra doit être sur votre véhicule.",
                "tr": "Kamera kendi aracınızda olmalıdır.",
                "no": "Kameraet må være på eget kjøretøy.",
                "dk": "Kameraet skal være på eget køretøj.",
                "se": "Kameran måste vara på eget fordon."
            },
            # ─── Notifications (auto_hold.py) ────────────────────────
            # "Auto Hold" is already defined above

            # ─── Notifications (key_binder.py) ───────────────────────
            "Press Mouse L again to bind!": {
                "en": "Press Mouse L again to bind!",
                "de": "Maus L erneut drücken!",
                "it": "Premi di nuovo Mouse L!",
                "fr": "Appuyez encore sur Souris G !",
                "tr": "Bağlamak için tekrar Sol Tık!",
                "no": "Trykk Mus V igjen!",
                "dk": "Tryk Mus V igen!",
                "se": "Tryck Mus V igen!"
            },
            # ─── Notifications (gearbox.py) ──────────────────────────
            "Gearbox Calibration Started": {
                "en": "Gearbox Calibration Started",
                "de": "Getriebe-Kalibrierung gestartet",
                "it": "Calibrazione Cambio Avviata",
                "fr": "Calibration Boîte Lancée",
                "tr": "Vites Kalibrasyonu Başladı",
                "no": "Girkalibrering Startet",
                "dk": "Gearkalibrering Startet",
                "se": "Växellådskalibrering Startad"
            },
            "Keep the rpm at idle!": {
                "en": "Keep the rpm at idle!",
                "de": "Drehzahl im Leerlauf halten!",
                "it": "Mantieni il minimo!",
                "fr": "Maintenez le ralenti !",
                "tr": "Rölantide tutun!",
                "no": "Hold turtallet på tomgang!",
                "dk": "Hold omdrejningerne i tomgang!",
                "se": "Håll varvtalet på tomgång!"
            },
            "Recording idle rpm!": {
                "en": "Recording idle rpm!",
                "de": "Leerlaufdrehzahl wird gemessen!",
                "it": "Registrazione minimo!",
                "fr": "Enregistrement ralenti !",
                "tr": "Rölanti kaydediliyor!",
                "no": "Registrerer tomgang!",
                "dk": "Registrerer tomgang!",
                "se": "Registrerar tomgång!"
            },
            "Gearbox Calibration Aborted": {
                "en": "Gearbox Calibration Aborted",
                "de": "Getriebe-Kalibrierung abgebrochen",
                "it": "Calibrazione Cambio Interrotta",
                "fr": "Calibration Boîte Annulée",
                "tr": "Vites Kalibrasyonu İptal Edildi",
                "no": "Girkalibrering Avbrutt",
                "dk": "Gearkalibrering Afbrudt",
                "se": "Växellådskalibrering Avbruten"
            },
            "Vehicle moved during calibration!": {
                "en": "Vehicle moved during calibration!",
                "de": "Fahrzeug hat sich bewegt!",
                "it": "Veicolo mosso durante calibraz.!",
                "fr": "Véhicule bougé pendant calibr. !",
                "tr": "Araç kalibrasyon sırasında hareket etti!",
                "no": "Kjøretøy beveget seg!",
                "dk": "Køretøj flyttede sig!",
                "se": "Fordon rörde sig!"
            },
            "Vehicle must be stationary to calibrate!": {
                "en": "Vehicle must be stationary to calibrate!",
                "de": "Fahrzeug muss stillstehen!",
                "it": "Veicolo fermo per calibrare!",
                "fr": "Véhicule à l'arrêt pour calibrer !",
                "tr": "Araç durmalı!",
                "no": "Kjøretøy må stå stille!",
                "dk": "Køretøj skal holde stille!",
                "se": "Fordon måste stå stilla!"
            },
            "Idle RPM set to": {
                "en": "Idle RPM set to",
                "de": "Leerlaufdrehzahl gesetzt auf",
                "it": "RPM minimo impostato a",
                "fr": "Régime ralenti réglé à",
                "tr": "Rölanti devri ayarlandı:",
                "no": "Tomgang satt til",
                "dk": "Tomgang sat til",
                "se": "Tomgång satt till"
            },
            "Rev it to the redline!": {
                "en": "Rev it to the redline!",
                "de": "Auf Maximaldrehzahl drehen!",
                "it": "Porta al fuorigiri!",
                "fr": "Montez au rupteur !",
                "tr": "Kırmızı bölgeye çıkın!",
                "no": "Gi gass til rødmerket!",
                "dk": "Giv gas til rødmarkeringen!",
                "se": "Gasa till rödmarkeringen!"
            },
            "Recording redline!": {
                "en": "Recording redline!",
                "de": "Maximaldrehzahl wird gemessen!",
                "it": "Registrazione fuorigiri!",
                "fr": "Enregistrement rupteur !",
                "tr": "Kırmızı bölge kaydediliyor!",
                "no": "Registrerer rødmerke!",
                "dk": "Registrerer rødmarkering!",
                "se": "Registrerar rödmarkering!"
            },
            "Redline RPM set to": {
                "en": "Redline RPM set to",
                "de": "Maximaldrehzahl gesetzt auf",
                "it": "RPM fuorigiri impostato a",
                "fr": "Régime rupteur réglé à",
                "tr": "Kırmızı bölge devri ayarlandı:",
                "no": "Rødmerke satt til",
                "dk": "Rødmarkering sat til",
                "se": "Rödmarkering satt till"
            },
            "Shift into the highest gear!": {
                "en": "Shift into the highest gear!",
                "de": "Höchsten Gang einlegen!",
                "it": "Inserisci la marcia più alta!",
                "fr": "Passez le rapport le plus haut !",
                "tr": "En yüksek vitese geçin!",
                "no": "Legg inn høyeste gir!",
                "dk": "Skift til højeste gear!",
                "se": "Lägg i högsta växeln!"
            },
            "Recording highest gear!": {
                "en": "Recording highest gear!",
                "de": "Höchster Gang wird gemessen!",
                "it": "Registrazione marcia max!",
                "fr": "Enregistrement rapport max !",
                "tr": "En yüksek vites kaydediliyor!",
                "no": "Registrerer høyeste gir!",
                "dk": "Registrerer højeste gear!",
                "se": "Registrerar högsta växel!"
            },
            "Max gear set to": {
                "en": "Max gear set to",
                "de": "Höchster Gang gesetzt auf",
                "it": "Marcia max impostata a",
                "fr": "Rapport max réglé à",
                "tr": "Maks. vites ayarlandı:",
                "no": "Høyeste gir satt til",
                "dk": "Højeste gear sat til",
                "se": "Högsta växel satt till"
            },
            "Gearbox Calibration Completed": {
                "en": "Gearbox Calibration Completed",
                "de": "Getriebe-Kalibrierung abgeschlossen",
                "it": "Calibrazione Cambio Completata",
                "fr": "Calibration Boîte Terminée",
                "tr": "Vites Kalibrasyonu Tamamlandı",
                "no": "Girkalibrering Fullført",
                "dk": "Gearkalibrering Fuldført",
                "se": "Växellådskalibrering Klar"
            },
            "Reset possible in menu!": {
                "en": "Reset possible in menu!",
                "de": "Zurücksetzen im Menü möglich!",
                "it": "Reset possibile nel menu!",
                "fr": "Réinit. possible dans le menu !",
                "tr": "Menüden sıfırlanabilir!",
                "no": "Tilbakestilling mulig i menyen!",
                "dk": "Nulstilling mulig i menuen!",
                "se": "Återställning möjlig i menyn!"
            },
            # ─── Notifications (AI_Driver.py) ────────────────────────
            "Traffic not avail. on this map": {
                "en": "Traffic not avail. on this map",
                "de": "Verkehr nicht verfügbar",
                "it": "Traffico non disponibile",
                "fr": "Trafic indisponible",
                "tr": "Trafik bu haritada yok",
                "no": "Trafikk ikke tilgjengelig",
                "dk": "Trafik ikke tilgængelig",
                "se": "Trafik ej tillgänglig"
            },
            "Wrong track config for traffic": {
                "en": "Wrong track config for traffic",
                "de": "Falsche Streckenkonfig.",
                "it": "Config. pista errata",
                "fr": "Mauvaise config. circuit",
                "tr": "Yanlış pist yapılandırması",
                "no": "Feil banekonfigurasjon",
                "dk": "Forkert banekonfiguration",
                "se": "Fel bankonfiguration"
            },
            # ─── Notifications (navigation.py) ───────────────────────
            "Destination Reached": {
                "en": "Destination Reached",
                "de": "Ziel erreicht",
                "it": "Destinazione Raggiunta",
                "fr": "Destination Atteinte",
                "tr": "Hedefe Ulaşıldı",
                "no": "Mål Nådd",
                "dk": "Destination Nået",
                "se": "Mål Nått"
            },
            "Go Straight": {
                "en": "Go Straight",
                "de": "Geradeaus",
                "it": "Dritto",
                "fr": "Tout Droit",
                "tr": "Düz Gidin",
                "no": "Rett Frem",
                "dk": "Lige Ud",
                "se": "Rakt Fram"
            },
            "Turn Left": {
                "en": "Turn Left",
                "de": "Links abbiegen",
                "it": "Gira a Sinistra",
                "fr": "Tournez à Gauche",
                "tr": "Sola Dönün",
                "no": "Sving Venstre",
                "dk": "Drej Venstre",
                "se": "Sväng Vänster"
            },
            "Turn Right": {
                "en": "Turn Right",
                "de": "Rechts abbiegen",
                "it": "Gira a Destra",
                "fr": "Tournez à Droite",
                "tr": "Sağa Dönün",
                "no": "Sving Høyre",
                "dk": "Drej Højre",
                "se": "Sväng Höger"
            },
            "Off": {
                "en": "Off",
                "de": "Aus",
                "it": "Off",
                "fr": "Arrêt",
                "tr": "Kapalı",
                "no": "Av",
                "dk": "Fra",
                "se": "Av"
            },
            "Up": {
                "en": "Up",
                "de": "Hoch",
                "it": "Su",
                "fr": "Haut",
                "tr": "Yukarı",
                "no": "Opp",
                "dk": "Op",
                "se": "Upp"
            },
            "Down": {
                "en": "Down",
                "de": "Runter",
                "it": "Giù",
                "fr": "Bas",
                "tr": "Aşağı",
                "no": "Ned",
                "dk": "Ned",
                "se": "Ner"
            },
            "Left": {
                "en": "Left",
                "de": "Links",
                "it": "Sinistra",
                "fr": "Gauche",
                "tr": "Sol",
                "no": "Venstre",
                "dk": "Venstre",
                "se": "Vänster"
            },
            "Right": {
                "en": "Right",
                "de": "Rechts",
                "it": "Destra",
                "fr": "Droite",
                "tr": "Sağ",
                "no": "Høyre",
                "dk": "Højre",
                "se": "Höger"
            },
            "Key": {
                "en": "Key",
                "de": "Taste",
                "it": "Tasto",
                "fr": "Touche",
                "tr": "Tuş",
                "no": "Tast",
                "dk": "Tast",
                "se": "Knapp"
            },
            "currently bound to": {
                "en": "currently bound to",
                "de": "aktuell belegt mit",
                "it": "attualmente assegnato a",
                "fr": "actuellement lié à",
                "tr": "şu anda bağlı:",
                "no": "for tiden tilordnet",
                "dk": "i øjeblikket tildelt",
                "se": "för närvarande bunden till"
            },
            "HUD Position": {
                "en": "HUD Position",
                "de": "HUD Position",
                "it": "Posizione HUD",
                "fr": "Position HUD",
                "tr": "HUD Konumu",
                "no": "HUD Posisjon",
                "dk": "HUD Position",
                "se": "HUD Position"
            },
            # ─── Chat Command Translations ────────────────────────────
            "enabled": {
                "en": "enabled",
                "de": "aktiviert",
                "it": "attivato",
                "fr": "activé",
                "tr": "etkin",
                "no": "aktivert",
                "dk": "aktiveret",
                "se": "aktiverad"
            },
            "disabled": {
                "en": "disabled",
                "de": "deaktiviert",
                "it": "disattivato",
                "fr": "désactivé",
                "tr": "devre dışı",
                "no": "deaktivert",
                "dk": "deaktiveret",
                "se": "inaktiverad"
            },
            "Siren": {
                "en": "Siren",
                "de": "Sirene",
                "it": "Sirena",
                "fr": "Sirène",
                "tr": "Siren",
                "no": "Sirene",
                "dk": "Sirene",
                "se": "Siren"
            },
            "Strobe": {
                "en": "Strobe",
                "de": "Blaulicht",
                "it": "Strobo",
                "fr": "Stroboscope",
                "tr": "Çakar",
                "no": "Blålys",
                "dk": "Blålys",
                "se": "Blixtljus"
            },
            "Siren not available": {
                "en": "Siren not available",
                "de": "Sirene nicht verfügbar",
                "it": "Sirena non disponibile",
                "fr": "Sirène non disponible",
                "tr": "Siren kullanılamıyor",
                "no": "Sirene ikke tilgjengelig",
                "dk": "Sirene ikke tilgængelig",
                "se": "Siren inte tillgänglig"
            },
            "Strobe not available": {
                "en": "Strobe not available",
                "de": "Blaulicht nicht verfügbar",
                "it": "Strobo non disponibile",
                "fr": "Stroboscope non disponible",
                "tr": "Çakar kullanılamıyor",
                "no": "Blålys ikke tilgjengelig",
                "dk": "Blålys ikke tilgængeligt",
                "se": "Blixtljus inte tillgängligt"
            },

            # ─── Tooltips ─────────────────────────────────────────────
            "tooltip_help_command": {
                "en": "Type '$help' in chat to see all available commands.",
                "de": "Schreibe '$help' in den Chat, um alle verfügbaren Commands zu sehen.",
                "it": "Scrivi '$help' nella chat per vedere tutti i comandi disponibili.",
                "fr": "Tapez '$help' dans le chat pour afficher toutes les commandes.",
                "tr": "Mevcut komutları görmek için sohbete '$help' yazın.",
                "no": "Skriv '$help' i chatten for å se alle tilgjengelige kommandoer.",
                "dk": "Skriv '$help' i chatten for at se alle tilgængelige kommandoer.",
                "se": "Skriv '$help' i chatten för att se alla tillgängliga kommandon."
            },
            "tooltip_key_binding": {
                "en": "You can bind commands to wheel or keyboard buttons in the LFS options.",
                "de": "Du kannst in den LFS-Optionen Commands auf Lenkrad- oder Tastaturtasten legen.",
                "it": "Nelle opzioni di LFS puoi assegnare i comandi ai pulsanti del volante o della tastiera.",
                "fr": "Vous pouvez assigner des commandes aux touches du volant ou du clavier dans les options LFS.",
                "tr": "LFS ayarlarından komutları direksiyon veya klavye tuşlarına atayabilirsiniz.",
                "no": "Du kan tilordne kommandoer til ratt- eller tastaturknapper i LFS-innstillingene.",
                "dk": "Du kan tildele kommandoer til rat- eller tastaturknapper i LFS-indstillingerne.",
                "se": "Du kan tilldela kommandon till ratt- eller tangentbordsknappar i LFS-inställningarna."
            },
            "tooltip_fun_fact": {
                "en": "Fun Fact: The PACT Driving Assistant has been around since 2018. This is version 10.",
                "de": "Fun Fact: Den PACT Driving Assistant gibt es seit 2018. Dies ist Version 10.",
                "it": "Curiosità: il PACT Driving Assistant esiste dal 2018. Questa è la versione 10.",
                "fr": "Le saviez-vous ? Le PACT Driving Assistant existe depuis 2018. Ceci est la version 10.",
                "tr": "Biliyor muydunuz? PACT Driving Assistant 2018'den beri var. Bu, 10. sürüm.",
                "no": "Visste du? PACT Driving Assistant har eksistert siden 2018. Dette er versjon 10.",
                "dk": "Vidste du? PACT Driving Assistant har eksisteret siden 2018. Dette er version 10.",
                "se": "Visste du? PACT Driving Assistant har funnits sedan 2018. Det här är version 10."
            },
            "tooltip_bug_report": {
                "en": "Found a bug? Report it in the PACT Driving Assistant thread on the LFS forum.",
                "de": "Einen Bug gefunden? Melde ihn im PACT Driving Assistant Thread im LFS-Forum.",
                "it": "Hai trovato un bug? Segnalalo nel thread del PACT Driving Assistant sul forum LFS.",
                "fr": "Un bug ? Signalez-le dans le fil PACT Driving Assistant sur le forum LFS.",
                "tr": "Bir hata mı buldunuz? LFS forumundaki PACT Driving Assistant başlığında bildirin.",
                "no": "Funnet en feil? Meld den i PACT Driving Assistant-tråden på LFS-forumet.",
                "dk": "Fundet en fejl? Rapportér den i PACT Driving Assistant-tråden på LFS-forummet.",
                "se": "Hittat en bugg? Rapportera den i PACT Driving Assistant-tråden på LFS-forumet."
            },
            "tooltip_ai_traffic": {
                "en": "On many tracks you can drive with AI traffic in single player mode.",
                "de": "Auf vielen Strecken kannst du im Einzelspieler-Modus mit KI-Verkehr fahren.",
                "it": "Su molti circuiti puoi guidare con il traffico IA in modalità giocatore singolo.",
                "fr": "Sur de nombreux circuits, vous pouvez rouler avec du trafic IA en mode solo.",
                "tr": "Birçok pistte tek oyunculu modda yapay zekâ trafiğiyle sürüş yapabilirsiniz.",
                "no": "På mange baner kan du kjøre med AI-trafikk i enkeltspillermodus.",
                "dk": "På mange baner kan du køre med AI-trafik i enkeltspillertilstand.",
                "se": "På många banor kan du köra med AI-trafik i enspelarlä̈ge."
            },
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