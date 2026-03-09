import time

import pyinsim


class StateHandler:
    """Verwaltet das Senden von Nachrichten und UI-Elementen an LFS"""

    def __init__(self, connector):
        self.connector = connector
        self.text_entry = False
        self.on_track = False
        self.track = ""
        self.in_game_cam = 0  # 0 = Follow, 1 = Heli, 2 = TV, 3 = Driver, 4 = Custom, 255 = Another
        self.in_game_interface = 0  # 0 = Game, 1 = Options, 2 = Host_Options, 3 = Garage, 4 = Car_select, 5 = Track_select,  6 = ShiftU
        self.submode_interface = 0
        self.time_menu_opened = time.time()
        self.connector.event_bus.subscribe('game_state_changed', self.insim_state)


    def insim_state(self, sta):
        """
        this method receives the is_sta packet from LFS. It contains information about the game state, that will
        be read and saved inside "flags" variable.
        """
        if self.connector.debug:
            print(f"Received game state: {sta}")
        def start_game_insim():
            self.on_track = True
            if time.time() - self.time_menu_opened >= 30:
                self.connector.start_outgauge()
            self.connector.insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_NPL)

        def start_menu_insim():
            self.time_menu_opened = time.time()
            self.on_track = False

        # Use proper bitwise operations for flag parsing
        flags_raw = sta.Flags
        self.in_game_cam = sta.InGameCam

        ISS_GAME = 1
        ISS_DIALOG = 16
        ISS_FRONT_END = 256
        ISS_TEXT_ENTRY = 32768

        is_in_game = bool(flags_raw & ISS_GAME)
        is_front_end = bool(flags_raw & ISS_FRONT_END)

        # on_track is True when ISS_GAME is set AND ISS_FRONT_END (entry screen) is NOT set.
        # ISS_DIALOG (in-game options menu) must NOT affect on_track.
        game = is_in_game and not is_front_end

        if not self.on_track and game:
            start_game_insim()
        elif self.on_track and not game:
            start_menu_insim()

        self.text_entry = bool(flags_raw & ISS_TEXT_ENTRY)
        self.dialog = bool(flags_raw & ISS_DIALOG)
        self.track = sta.Track
        state_data = {
            'on_track': self.on_track,
            'text_entry': self.text_entry,
            'dialog': self.dialog,
            'track': self.track,
            'in_game_cam': self.in_game_cam,
            'in_game_interface': self.in_game_interface,
            'submode_interface': self.submode_interface
        }
        self.connector.event_bus.emit('state_data', state_data)
