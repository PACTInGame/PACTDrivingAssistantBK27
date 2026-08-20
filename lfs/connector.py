import logging

import pyinsim
from typing import Dict, Any, Callable
import time

from AI_Control import AICarController
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from lfs.lfs_state import StateHandler
from lfs.text_encoding import (MSL_TEXT_MAX_BYTES, MST_TEXT_MAX_BYTES,
                               encode_button_text, encode_lfs_text)
from misc import helpers

logger = logging.getLogger(__name__)


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
        self.event_bus.subscribe("send_light_command", self.send_light_command)
        self.event_bus.subscribe("request_axm_update", self._request_axm_update)
        self.event_bus.subscribe("siren_state_changed", self._siren_state_changed)
        self.AIController = None


    def _siren_state_changed(self, data):
        if not (self.insim and self.is_connected):
            return
        siren = data.get('siren_active', False)
        siren_config =  (pyinsim.LCS_SET_SIREN, pyinsim.LCS_Mask_Siren)


        set_flag, mask_flag = siren_config
        UVal = set_flag | (mask_flag if siren else 0)

        self.insim.send(pyinsim.ISP_SMALL, SubT=pyinsim.SMALL_LCS, UVal=UVal)

    def _request_axm_update(self, data):
        """Fordert ein AXM-Layout-Update an"""
        if not (self.insim and self.is_connected):
            return
        self.insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_AXM)

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
            pyinsim.ISP_BFN: self._handle_button_function,
            pyinsim.ISP_CIM: self._handle_interface_mode,
        }

    def connect(self):
        """Stellt Verbindung zu LFS her"""

        try:
            interval = self.settings.get('assistance_refresh_rate')
            self.insim = pyinsim.insim(
                b'127.0.0.1', 29999,
                Admin=b'',
                Prefix=b"$",
                Flags=pyinsim.ISF_MCI | pyinsim.ISF_AXM_LOAD | pyinsim.ISF_AXM_EDIT | pyinsim.ISF_LOCAL,
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
            self.insim.send(pyinsim.ISP_TINY, ReqI=255, SubT=pyinsim.TINY_AXM)
            self.AIController = AICarController(self.insim)
            self.event_bus.emit('AI_Controller_initialized', self.AIController)

        except Exception as e:
            logger.error("Failed to connect to LFS: %s: %s", type(e).__name__, e,
                         exc_info=True)
            self.is_connected = False

    def start_outgauge(self):
        """Startet OutGauge-Verbindung"""
        try:
            self.outgauge = pyinsim.outgauge('127.0.0.1', 30000, self._outgauge_handler, 30.0)
        except Exception as e:
            logger.error("Failed to connect OutGauge: %s: %s", type(e).__name__, e)

    def start_outsim(self):
        """Startet OutSim-Verbindung"""
        try:
            self.outsim = pyinsim.outsim('127.0.0.1', 29998, self._outsim_handler, 30.0)
        except Exception as e:
            logger.error("Failed to connect OutSim: %s: %s", type(e).__name__, e)

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

    def _handle_button_click(self, insim, btc):
        """Verarbeitet Button-Klicks"""
        self.event_bus.emit('button_clicked', btc)

    def _handle_message(self, insim, mso):
        """Verarbeitet Chat-Nachrichten"""
        logger.debug("MSO: %r", getattr(mso, 'Msg', b''))
        self.event_bus.emit('message_received', mso)

    def _handle_layout(self, insim, axm):
        """Verarbeitet Layout-Pakete"""
        self.event_bus.emit('layout_received', axm)

    def _handle_button_function(self, insim, bfn):
        """Verarbeitet eingehende IS_BFN-Pakete

        LFS schickt BFN_USER_CLEAR (der Nutzer hat mit SHIFT+B alle Buttons
        geloescht) und BFN_REQUEST (er will sie zurueck). Beides bedeutet:
        was wir gesendet haben, ist nicht mehr auf dem Schirm. Die
        Button-Registry im MessageSender muss das erfahren, sonst unterdrueckt
        sie den naechsten Repaint als 'unveraendert'.
        """
        subtype = getattr(bfn, 'SubT', None)
        if subtype in (pyinsim.BFN_USER_CLEAR, pyinsim.BFN_REQUEST):
            self.event_bus.emit('buttons_cleared', {'sub_type': subtype})

    def _handle_interface_mode(self, insim, cim):
        """Verarbeitet IS_CIM (Conn Interface Mode)

        Sagt, in welchem LFS-Bildschirm die lokale Verbindung gerade ist
        (Optionen, Garage samt Untermenues, Auto-/Streckenwahl, SHIFT+U).
        Braucht kein ISF_*-Flag - LFS schickt es bei jedem Moduswechsel.
        Nur der StateHandler wertet es aus (reference/ui.md §1.2).
        """
        self.event_bus.emit('interface_mode_changed', cim)

    def _outgauge_handler(self, outgauge, packet):
        """Handler für OutGauge-Pakete (hochfrequent)"""
        self.event_bus.emit('outgauge_data', packet)

    def _outsim_handler(self, outsim, packet):
        """Handler für OutSim-Pakete"""
        self.event_bus.emit('outsim_data', packet)

    def send_command_to_lfs(self, command: str):
        """Sendet einen Befehl an LFS (IS_MST, max. 63 Zeichen)"""
        if not (self.insim and self.is_connected):
            logger.warning("Not connected - dropping command %r", command)
            return False
        try:
            self.insim.send(pyinsim.ISP_MST,
                            Msg=encode_lfs_text(command, max_bytes=MST_TEXT_MAX_BYTES))
        except Exception as e:
            logger.error("Sending command %r failed: %s: %s", command, type(e).__name__, e)
            return False
        return True

    def send_local_message_to_lfs(self, message: str):
        """Sendet eine lokale Chat-Nachricht (IS_MSL, max. 127 Zeichen)

        Gleiche Kodierung wie bei Buttons: LFS liest kein UTF-8, und die
        Tooltips kommen uebersetzt aus misc/language.py.
        """
        if not (self.insim and self.is_connected):
            logger.warning("Not connected - dropping message %r", message)
            return False
        try:
            self.insim.send(pyinsim.ISP_MSL,
                            Msg=encode_lfs_text(message, max_bytes=MSL_TEXT_MAX_BYTES))
        except Exception as e:
            logger.error("Sending message failed: %s: %s", type(e).__name__, e)
            return False
        return True

    def send_light_command(self, data):
        """Schaltet ein Licht ein oder aus

        Args:
            light: 0=Standlicht, 1=Abblendlicht, 2=Fernlicht, 3=Nebelscheinwerfer,
                   4=Nebelschlussleuchte, 5=Extra, 6=Blinker links, 7=Blinker rechts, 8=Warnblinkanlage
            on: True zum Einschalten, False zum Ausschalten
        """
        if not (self.insim and self.is_connected):
            return
        light = data.get('light')
        on = data.get('on', False)
        # Mapping: light_id -> (SET_flag, MASK_flag)
        light_config = {
            0: (pyinsim.LCL_SET_LIGHTS, pyinsim.LCL_Mask_SideLight),      # Standlicht
            1: (pyinsim.LCL_SET_LIGHTS, pyinsim.LCL_Mask_LowBeam),        # Abblendlicht
            2: (pyinsim.LCL_SET_LIGHTS, pyinsim.LCL_Mask_HighBeam),       # Fernlicht
            3: (pyinsim.LCL_SET_FOG_FRONT, pyinsim.LCL_Mask_FogFront),    # Nebelscheinwerfer
            4: (pyinsim.LCL_SET_FOG_REAR, pyinsim.LCL_Mask_FogRear),      # Nebelschlussleuchte
            5: (pyinsim.LCL_SET_EXTRA, pyinsim.LCL_Mask_Extra),           # Extra
            6: (pyinsim.LCL_SET_SIGNALS, pyinsim.LCL_Mask_Left),          # Blinker links
            7: (pyinsim.LCL_SET_SIGNALS, pyinsim.LCL_Mask_Right),         # Blinker rechts
            8: (pyinsim.LCL_SET_SIGNALS, pyinsim.LCL_Mask_Signals),       # Warnblinkanlage
        }

        if light not in light_config:
            logger.warning("Ignoring light command with unknown light id %r", light)
            return

        set_flag, mask_flag = light_config[light]
        UVal = set_flag | (mask_flag if on else 0)

        self.insim.send(pyinsim.ISP_SMALL, SubT=pyinsim.SMALL_LCL, UVal=UVal)

    def send_button(self, click_id: int, style: int, t: int, l: int, w: int, h: int, text, inst: int = 0):
        """Sendet einen Button an LFS (T < 170 überlappt UI von LFS)

        ``text`` kommt normalerweise schon kodiert (bytes) vom MessageSender,
        der als einziger die Registry fuehrt. Ein str wird hier noch einmal
        durch denselben Encoder geschickt, damit ein direkter Aufruf nicht in
        UTF-8 rausgeht - LFS liest kein UTF-8 (siehe lfs/text_encoding.py).
        """
        if not (self.insim and self.is_connected):
            return False
        if not isinstance(text, (bytes, bytearray)):
            text = encode_button_text(text)
        try:
            self.insim.send(
                pyinsim.ISP_BTN,
                ReqI=255,
                ClickID=click_id,
                BStyle=style | 3,
                Inst=inst,
                T=t, L=l, W=w, H=h,
                Text=bytes(text),
                TypeIn=0
            )
        except Exception as e:
            logger.error("Sending button %s failed: %s: %s", click_id, type(e).__name__, e)
            return False
        return True

    def delete_button(self, click_id: int):
        """Löscht einen Button in LFS"""
        if not (self.insim and self.is_connected):
            return False
        try:
            self.insim.send(pyinsim.ISP_BFN, ReqI=255, SubT=pyinsim.BFN_DEL_BTN,
                            ClickID=click_id, MaxClick=click_id)
        except Exception as e:
            logger.error("Deleting button %s failed: %s: %s", click_id, type(e).__name__, e)
            return False
        return True

    def clear_all_buttons(self):
        """Löscht alle Buttons dieser InSim-Verbindung mit einem Paket (BFN_CLEAR)"""
        if not (self.insim and self.is_connected):
            return False
        try:
            self.insim.send(pyinsim.ISP_BFN, ReqI=255, SubT=pyinsim.BFN_CLEAR)
        except Exception as e:
            logger.error("Clearing buttons failed: %s: %s", type(e).__name__, e)
            return False
        return True

    def disconnect(self):
        """Meldet die InSim-Verbindung sauber ab und schliesst alle Sockets.

        Reihenfolge zaehlt: erst ISP_TINY/TINY_CLOSE, damit LFS die Verbindung
        selbst aufraeumt (und mit ihr alle unsere Buttons), dann die UDP-Sockets,
        dann pyinsim.closeall(). Vorher war das ein `pass` - Buttons blieben in
        LFS stehen und der Socket blieb offen (known-issues #2).
        """
        if self.insim is not None:
            try:
                self.insim.send(pyinsim.ISP_TINY, ReqI=0, SubT=pyinsim.TINY_CLOSE)
            except Exception as e:
                logger.warning("TINY_CLOSE could not be sent: %s: %s", type(e).__name__, e)

        self.is_connected = False

        # Der asyncore-Loop laeuft hier nicht mehr - ohne dieses Flush blieben
        # BFN_CLEAR und TINY_CLOSE im Sendepuffer liegen und die Buttons in LFS
        # stehen.
        if self.insim is not None and hasattr(self.insim, 'flush'):
            try:
                if not self.insim.flush(1.0):
                    logger.warning("Not everything could be sent to LFS before closing.")
            except Exception as e:
                logger.warning("Flushing the InSim buffer failed: %s: %s",
                               type(e).__name__, e)

        for name in ('outgauge', 'outsim', 'insim'):
            connection = getattr(self, name, None)
            if connection is None:
                continue
            try:
                connection.close()
            except Exception as e:
                logger.warning("Closing %s failed: %s: %s", name, type(e).__name__, e)

        try:
            pyinsim.closeall()
        except Exception as e:
            logger.warning("pyinsim.closeall() failed: %s: %s", type(e).__name__, e)

        self.insim = None
        self.outgauge = None
        self.outsim = None
