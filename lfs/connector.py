import pyinsim
from typing import Dict, Any, Callable
import time

from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from lfs.lfs_state import StateHandler
from misc import helpers


class LFSConnector:
    """Verwaltet die Verbindung zu Live for Speed"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        self.debug = False
        self.event_bus = event_bus
        self.settings = settings
        self.insim = None
        self.outgauge = None
        self.outsim = None
        self.is_connected = False
        self._packet_handlers: Dict[int, Callable] = {}
        self._setup_handlers()

    def _setup_handlers(self):
        """Registriert Standard-Packet-Handler und startet listener"""

        self.state_handler = StateHandler(self)

        self._packet_handlers = {
            pyinsim.ISP_NPL: self._handle_new_player,
            pyinsim.ISP_PLL: self._handle_player_left,
            pyinsim.ISP_STA: self._handle_state,
            pyinsim.ISP_BTC: self._handle_button_click,
            pyinsim.ISP_MSO: self._handle_message,
            pyinsim.ISP_MCI: self._handle_mci,
            pyinsim.ISP_AXM: self._handle_layout,
        }

    def connect(self):
        """Stellt Verbindung zu LFS her"""
        while not helpers.is_lfs_running():
            print("Waiting for Live for Speed to start...")
            time.sleep(1)
        try:
            interval = self.settings.get('assistance_refresh_rate')
            self.insim = pyinsim.insim(
                b'127.0.0.1', 29999,
                Admin=b'',
                Prefix=b"$",
                Flags= pyinsim.ISF_MCI | pyinsim.ISF_AXM_LOAD | pyinsim.ISF_AXM_EDIT | pyinsim.ISF_LOCAL,
                Interval=interval
            )

            # Registriere alle Handler
            for packet_type, handler in self._packet_handlers.items():
                self.insim.bind(packet_type, handler)

            self.start_outgauge()
            self.start_outsim()

            self.is_connected = True
            self.event_bus.emit('lfs_connected')

            self.insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_SST)



        except Exception as e:
            print(f"Failed to connect to LFS: {e}")
            self.is_connected = False


    def start_outgauge(self):
        """Startet OutGauge-Verbindung"""
        try:
            self.outgauge = pyinsim.outgauge('127.0.0.1', 30000, self._outgauge_handler, 30.0)
        except Exception as e:
            print(f"Failed to connect OutGauge: {e}")

    def start_outsim(self):
        """Startet OutSim-Verbindung"""
        try:
            self.outsim = pyinsim.outsim('127.0.0.1', 29998, self._outsim_handler, 30.0)
        except Exception as e:
            print(f"Failed to connect OutSim: {e}")

    def _handle_mci(self, insim, mci):
        """Verarbeitet MCI (Multi Car Info) Pakete"""
        self.event_bus.emit('vehicle_data_received', mci)

    def _handle_new_player(self, insim, npl):
        """Verarbeitet neue Spieler"""
        self.event_bus.emit('player_joined', npl)

    def _handle_player_left(self, insim, pll):
        """Verarbeitet Spieler die verlassen"""
        self.event_bus.emit('player_left', pll)

    def _handle_state(self, insim, sta):
        """Verarbeitet Spielstatus"""
        self.event_bus.emit('game_state_changed', sta)
        if self.debug:
            print(f"Game state changed: {sta}")

    def _handle_button_click(self, insim, btc):
        """Verarbeitet Button-Klicks"""
        self.event_bus.emit('button_clicked', btc)

    def _handle_message(self, insim, mso):
        """Verarbeitet Chat-Nachrichten"""
        self.event_bus.emit('message_received', mso)

    def _handle_layout(self, insim, axm):
        """Verarbeitet Layout-Pakete"""
        self.event_bus.emit('layout_received', axm)

    def _outgauge_handler(self, outgauge, packet):
        """Handler für OutGauge-Pakete (hochfrequent)"""
        self.event_bus.emit('outgauge_data', packet)

    def _outsim_handler(self, outsim, packet):
        """Handler für OutSim-Pakete"""
        self.event_bus.emit('outsim_data', packet)

    def send_button(self, click_id: int, style: int, t: int, l: int, w: int, h: int, text: str, inst: int = 0):
        """Sendet einen Button an LFS (T < 170 überlappt UI von LFS)"""
        #print(f"ClickID: {click_id}, Style: {style}, Position: ({t}, {l}), Size: ({w}, {h}), Text: '{text}'")
        try:
            text = text.encode("latin-1")
        except UnicodeEncodeError:
            text = text.encode()
        if self.insim and self.is_connected:
            self.insim.send(
                pyinsim.ISP_BTN,
                ReqI=255,
                ClickID=click_id,
                BStyle=style | 3,
                Inst=inst,
                T=t, L=l, W=w, H=h,
                Text=text,
                TypeIn=0
            )

    def delete_button(self, click_id: int):
        """Löscht einen Button in LFS"""
        if self.insim and self.is_connected:
            self.insim.send(pyinsim.ISP_BFN, ReqI=255, ClickID=click_id)

