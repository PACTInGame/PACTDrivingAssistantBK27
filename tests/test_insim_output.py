"""WP3 -- the path from the app to the LFS socket.

Three defects live here: the send buffer was mutated from several threads
without a lock, buttons were re-sent every 50 ms whether or not anything
changed, and button text was encoded as latin-1 with a UTF-8 fallback that LFS
cannot read.
"""

import struct
import threading

import pytest

import pyinsim
from lfs.message_sender import MessageSender
from lfs.text_encoding import (BUTTON_TEXT_MAX_BYTES, button_text_capacity,
                               decode_button_text, encode_button_text)
from pyinsim.core import _TcpSocket


# ─── Thread-safe send buffer ─────────────────────────────────────────────────

class LoopbackTcpSocket(_TcpSocket):
    """A _TcpSocket whose actual write goes into a list instead of a socket.

    Only ``asyncore.dispatcher.send`` is replaced -- the buffer handling under
    test is the real one.
    """

    def __init__(self, chunk_size=7):
        # Deliberately not calling _TcpSocket.__init__: it creates a real
        # socket. Everything the buffer logic touches is set up by hand.
        self._send_lock = threading.RLock()
        self._send_buff = b''
        self._recv_buff = b''
        self.written = bytearray()
        self.chunk_size = chunk_size

    def _real_send(self, data):
        # Simulates a partial write, which is what makes the unlocked version
        # lose data: the drain used to slice the buffer it had read earlier.
        chunk = data[:self.chunk_size]
        self.written += chunk
        return len(chunk)


@pytest.fixture(autouse=True)
def _patch_dispatcher_send(monkeypatch):
    import asyncore

    original = asyncore.dispatcher.send

    def send(self, data):
        if isinstance(self, LoopbackTcpSocket):
            return self._real_send(data)
        return original(self, data)

    monkeypatch.setattr(asyncore.dispatcher, 'send', send)


def _packet_stream(count, plid_base=0):
    """`count` IS_BTN packets, each with a distinct, verifiable payload."""
    packets = []
    for index in range(count):
        text = f"button-{index:04d}".encode('ascii')
        packet = pyinsim.IS_BTN(ReqI=255, ClickID=index % 240, T=1, L=2, W=3, H=4,
                                Text=text)
        packets.append(packet.pack())
    return packets


def _split_packets(stream):
    """Split a raw InSim byte stream back into packets by the size prefix."""
    out = []
    index = 0
    while index < len(stream):
        size = stream[index] * 4
        assert size > 0, "size byte of zero -- the stream is desynchronised"
        out.append(bytes(stream[index:index + size]))
        index += size
    return out


def test_concurrent_sends_lose_no_bytes_and_stay_parseable():
    socket = LoopbackTcpSocket(chunk_size=13)
    packets = _packet_stream(800)
    per_thread = len(packets) // 8

    def producer(start):
        for packet in packets[start:start + per_thread]:
            socket.send(packet)

    drained = threading.Event()

    def drainer():
        while not drained.is_set() or socket.pending():
            socket.handle_write()

    drain_thread = threading.Thread(target=drainer)
    drain_thread.start()

    threads = [threading.Thread(target=producer, args=(index * per_thread,))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)
    drained.set()
    drain_thread.join(timeout=10.0)

    assert socket.pending() == 0
    parsed = _split_packets(socket.written)
    assert len(parsed) == len(packets)
    assert sorted(parsed) == sorted(packets)


def test_a_partial_write_keeps_the_remainder():
    socket = LoopbackTcpSocket(chunk_size=4)
    socket.send(b'0123456789')

    socket.handle_write()

    assert bytes(socket.written) == b'0123'
    assert socket.pending() == 6


def test_writable_is_false_with_an_empty_buffer():
    socket = LoopbackTcpSocket()

    assert socket.writable() is False
    socket.send(b'x')
    assert socket.writable() is True


def test_a_reentrant_send_during_the_drain_is_not_lost():
    """`asyncore.dispatcher.send` can trigger handle_close -> a callback that
    sends again on the same thread. A plain Lock would deadlock, and slicing
    the previously read buffer would drop those bytes."""
    socket = LoopbackTcpSocket(chunk_size=4)
    socket.send(b'AAAA')

    original = socket._real_send
    reentered = []

    def send_and_reenter(data):
        if not reentered:
            reentered.append(True)
            socket.send(b'BBBB')
        return original(data)

    socket._real_send = send_and_reenter
    socket.handle_write()
    socket.handle_write()

    assert bytes(socket.written) == b'AAAABBBB'


def test_flush_pushes_everything_out_without_the_event_loop():
    socket = LoopbackTcpSocket(chunk_size=3)
    socket.send(b'shutdown-packet')

    assert socket.flush(timeout=1.0) is True
    assert bytes(socket.written) == b'shutdown-packet'


# ─── Text encoding ───────────────────────────────────────────────────────────

@pytest.mark.parametrize('text', [
    "Blind Spot Warning",
    "Fußgängerüberweg",                 # latin-1
    "Sürüş Ayarları",                   # Turkish, needs ^T
    "Çarpışma Uyarısı",
    "På många banor",
    "Uzun Far Asistanı",
])
def test_text_round_trips_through_the_encoder(text):
    assert decode_button_text(encode_button_text(text)) == text


def test_a_turkish_string_switches_the_code_page_instead_of_going_out_as_utf8():
    """Any code page that carries the character is correct -- 's with cedilla'
    lives in both cp1250 (^E) and cp1254 (^T). What must not happen is UTF-8,
    which LFS does not read."""
    encoded = encode_button_text("Sürüş")

    assert b'^' in encoded                                   # a code page was selected
    assert encoded != "Sürüş".encode('utf-8')
    assert decode_button_text(encoded) == "Sürüş"


def test_a_long_turkish_string_does_not_ping_pong_between_code_pages():
    encoded = encode_button_text("Çarpışma Uyarısı Ayarları")

    assert encoded.count(b'^') == 1
    assert decode_button_text(encoded) == "Çarpışma Uyarısı Ayarları"


def test_colour_codes_survive_untouched():
    encoded = encode_button_text("^1Warnung")

    assert encoded.startswith(b'^1')
    assert decode_button_text(encoded) == "^1Warnung"


def test_a_stray_combining_mark_is_dropped_not_mangled():
    assert decode_button_text(encode_button_text("lä̈ge")) == "läge"


def test_a_control_character_cannot_truncate_the_button_text_inside_lfs():
    assert encode_button_text("a\x00b") == b'ab'


def test_the_protocol_limit_is_never_exceeded():
    encoded = encode_button_text("x" * 1000)

    assert len(encoded) <= BUTTON_TEXT_MAX_BYTES
    assert encoded.endswith(b'..')


def test_truncation_never_leaves_a_dangling_caret():
    encoded = encode_button_text("abcdef^", max_chars=99)

    assert not encoded.endswith(b'^')


def test_truncation_marks_the_cut():
    encoded = encode_button_text("abcdefghij", max_chars=4)

    assert decode_button_text(encoded) == "abcd.."


def test_short_text_is_never_truncated():
    assert decode_button_text(encode_button_text("abcd", max_chars=4)) == "abcd"


def test_the_capacity_model_leaves_the_shipped_strings_alone():
    """The off-track banner is the widest text the app itself draws."""
    banner = "PACT Driving Assist Active."

    assert button_text_capacity(25, 5) >= len(banner)


def test_bytes_are_passed_through_unchanged():
    assert encode_button_text(b'^7PDC') == b'^7PDC'


def test_none_and_numbers_do_not_raise():
    assert encode_button_text(None) == b''
    assert encode_button_text(42) == b'42'


def test_an_encoded_button_actually_packs():
    packed = pyinsim.IS_BTN(ClickID=1, Text=encode_button_text("Çarpışma")).pack()

    assert len(packed) % 4 == 0
    assert packed[0] * 4 == len(packed)


# ─── Button registry ─────────────────────────────────────────────────────────

class RecordingConnector:
    """The slice of LFSConnector that MessageSender uses."""

    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.buttons = []
        self.deletes = []
        self.clears = 0

    def send_button(self, click_id, style, t, l, w, h, text, inst=0):
        self.buttons.append((click_id, style, t, l, w, h, text, inst))
        return True

    def delete_button(self, click_id):
        self.deletes.append(click_id)
        return True

    def clear_all_buttons(self):
        self.clears += 1
        return True

    def send_command_to_lfs(self, command):
        pass

    def send_local_message_to_lfs(self, message):
        pass


@pytest.fixture
def connector(bus):
    return RecordingConnector(bus)


@pytest.fixture
def sender(connector):
    return MessageSender(connector)


def test_an_unchanged_button_produces_no_packet(sender, connector):
    for _ in range(20):                  # one second of the 50 ms UI cycle
        sender.create_button(1, 90, 119, 13, 5, "120", 32)

    assert len(connector.buttons) == 1
    assert sender.buttons_suppressed == 19


def test_a_changed_text_produces_exactly_one_packet(sender, connector):
    sender.create_button(1, 90, 119, 13, 5, "120", 32)
    sender.create_button(1, 90, 119, 13, 5, "121", 32)
    sender.create_button(1, 90, 119, 13, 5, "121", 32)

    assert len(connector.buttons) == 2


def test_a_moved_button_produces_a_packet(sender, connector):
    sender.create_button(1, 90, 119, 13, 5, "120")
    sender.create_button(1, 91, 119, 13, 5, "120")

    assert len(connector.buttons) == 2


def test_a_hud_idle_for_one_second_emits_no_packets(sender, connector):
    """WP3's send-rate acceptance: an idle HUD must be silent."""
    def paint():
        sender.create_button(1, 90, 119, 13, 5, "120", 32)
        sender.create_button(2, 103, 119, 13, 5, "3", 32)
        sender.create_button(3, 116, 119, 13, 5, "^7km/h", 32)

    paint()
    connector.buttons.clear()
    for _ in range(20):                  # 1 s at ui_refresh_rate = 50 ms
        paint()

    assert connector.buttons == []


def test_removing_an_unknown_button_sends_nothing(sender, connector):
    assert sender.remove_button(77) is False
    assert connector.deletes == []


def test_removing_a_live_button_sends_exactly_one_delete(sender, connector):
    sender.create_button(61, 0, 0, 26, 5, "Auto Hold")

    assert sender.remove_button(61) is True
    assert sender.remove_button(61) is False
    assert connector.deletes == [61]


def test_leaving_the_track_deletes_only_live_buttons(sender, connector):
    """One packet per visible button, whatever the range asked for."""
    for button_id in (1, 2, 3, 41, 60):
        sender.create_button(button_id, 0, 0, 10, 5, "x")

    removed = sender.remove_range(0, 238)

    assert removed == 5
    assert sorted(connector.deletes) == [1, 2, 3, 41, 60]


def test_remove_range_ignores_buttons_outside_the_range(sender, connector):
    sender.create_button(1, 0, 0, 10, 5, "x")
    sender.create_button(100, 0, 0, 10, 5, "x")

    sender.remove_range(0, 10)

    assert connector.deletes == [1]
    assert sender.is_live(100) is True


def test_remove_all_clears_with_a_single_packet(sender, connector):
    for button_id in range(0, 30):
        sender.create_button(button_id, 0, 0, 10, 5, "x")

    sender.remove_all()

    assert connector.clears == 1
    assert connector.deletes == []
    assert sender.active_buttons == {}


def test_a_deleted_button_is_sent_again_when_it_comes_back(sender, connector):
    sender.create_button(1, 0, 0, 10, 5, "x")
    sender.remove_button(1)
    connector.buttons.clear()

    sender.create_button(1, 0, 0, 10, 5, "x")

    assert len(connector.buttons) == 1


def test_shift_b_makes_the_next_repaint_send_again(sender, connector, bus):
    """LFS cleared our buttons -- the registry must not suppress the repaint."""
    sender.create_button(1, 0, 0, 10, 5, "x")
    connector.buttons.clear()

    bus.emit('buttons_cleared', {'sub_type': pyinsim.BFN_USER_CLEAR})
    sender.create_button(1, 0, 0, 10, 5, "x")

    assert len(connector.buttons) == 1


def test_reconnecting_makes_the_next_repaint_send_again(sender, connector, bus):
    sender.create_button(1, 0, 0, 10, 5, "x")
    connector.buttons.clear()

    bus.emit('lfs_connected')
    sender.create_button(1, 0, 0, 10, 5, "x")

    assert len(connector.buttons) == 1


def test_the_connector_receives_encoded_bytes(sender, connector):
    sender.create_button(1, 0, 0, 26, 5, "Sürüş")

    text = connector.buttons[0][6]
    assert isinstance(text, bytes)
    assert decode_button_text(text) == "Sürüş"


def test_two_strings_that_look_the_same_do_not_produce_two_packets(sender, connector):
    """NFC normalisation happens in the encoder, so the registry compares
    what LFS actually renders."""
    sender.create_button(1, 0, 0, 26, 5, "läge")
    sender.create_button(1, 0, 0, 26, 5, "läge")

    assert len(connector.buttons) == 1


def test_concurrent_repaints_from_two_threads_send_each_change_once(sender, connector):
    barrier = threading.Barrier(4)

    def repaint():
        barrier.wait()
        for _ in range(200):
            sender.create_button(7, 0, 0, 10, 5, "stable")

    threads = [threading.Thread(target=repaint) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10.0)

    assert len(connector.buttons) == 1


# ─── Chat text uses the same code pages ──────────────────────────────────────

def test_chat_text_is_encoded_and_length_limited():
    from lfs.text_encoding import MSL_TEXT_MAX_BYTES, encode_lfs_text

    long_turkish = "Bir hata mı buldunuz? " * 20
    encoded = encode_lfs_text(long_turkish, max_bytes=MSL_TEXT_MAX_BYTES)

    assert len(encoded) <= MSL_TEXT_MAX_BYTES
    assert encoded != long_turkish.encode('utf-8')
    assert decode_button_text(encoded).startswith("Bir hata mı buldunuz?")


def test_a_command_stays_ascii_and_fits_is_mst():
    from lfs.text_encoding import MST_TEXT_MAX_BYTES, encode_lfs_text

    encoded = encode_lfs_text("/axload AI_Traffic", max_bytes=MST_TEXT_MAX_BYTES)

    assert encoded == b"/axload AI_Traffic"


def test_the_connector_drops_traffic_when_it_is_not_connected(bus, settings):
    from lfs.connector import LFSConnector

    connector = LFSConnector(bus, settings)          # never connect()ed

    assert connector.send_command_to_lfs("/restart") is False
    assert connector.send_local_message_to_lfs("hi") is False
    assert connector.send_button(1, 0, 0, 0, 10, 5, "x") is False
    assert connector.delete_button(1) is False
    assert connector.clear_all_buttons() is False
    # These must not raise either -- they arrive as events from other threads.
    connector.send_light_command({'light': 1, 'on': True})
    connector._siren_state_changed({'siren_active': True})
    connector._request_axm_update({})


# ─── Connector shutdown and inbound IS_BFN ───────────────────────────────────

class ClosableInsim:
    """FakeInsim plus the close/flush surface `disconnect()` uses."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self.flushed = False

    def send(self, packet_type, **fields):
        self.sent.append((packet_type, fields))

    def bind(self, packet_type, handler):
        pass

    def flush(self, timeout=1.0):
        self.flushed = True
        return True

    def close(self):
        self.closed = True


def test_disconnect_says_goodbye_flushes_and_closes(bus, settings, monkeypatch):
    from lfs.connector import LFSConnector

    connector = LFSConnector(bus, settings)
    insim = ClosableInsim()
    connector.insim = insim
    connector.is_connected = True
    monkeypatch.setattr('lfs.connector.pyinsim.closeall', lambda: None)

    connector.disconnect()

    assert (pyinsim.ISP_TINY, {'ReqI': 0, 'SubT': pyinsim.TINY_CLOSE}) in insim.sent
    assert insim.flushed is True
    assert insim.closed is True
    assert connector.insim is None
    assert connector.is_connected is False


def test_disconnect_survives_a_socket_that_is_already_gone(bus, settings, monkeypatch):
    from lfs.connector import LFSConnector

    class Broken(ClosableInsim):
        def send(self, packet_type, **fields):
            raise OSError("socket already closed")

        def flush(self, timeout=1.0):
            raise OSError("socket already closed")

    connector = LFSConnector(bus, settings)
    connector.insim = Broken()
    connector.is_connected = True
    monkeypatch.setattr('lfs.connector.pyinsim.closeall', lambda: None)

    connector.disconnect()                       # must not raise

    assert connector.insim is None


@pytest.mark.parametrize('sub_type', [pyinsim.BFN_USER_CLEAR, pyinsim.BFN_REQUEST])
def test_an_inbound_bfn_is_republished_as_buttons_cleared(bus, settings, recorder,
                                                          sub_type):
    from lfs.connector import LFSConnector

    events = recorder('buttons_cleared')
    connector = LFSConnector(bus, settings)

    connector._handle_button_function(None, type('BFN', (), {'SubT': sub_type})())

    assert events.count('buttons_cleared') == 1


def test_a_bfn_without_a_subtype_is_ignored(bus, settings, recorder):
    from lfs.connector import LFSConnector

    events = recorder('buttons_cleared')
    connector = LFSConnector(bus, settings)

    connector._handle_button_function(None, object())

    assert events.count('buttons_cleared') == 0


# ─── Registry vs. a failing socket ───────────────────────────────────────────

class FailingConnector(RecordingConnector):
    def __init__(self, event_bus):
        super().__init__(event_bus)
        self.accept = False

    def send_button(self, *args, **kwargs):
        if not self.accept:
            return False
        return super().send_button(*args, **kwargs)

    def delete_button(self, click_id):
        if not self.accept:
            return False
        return super().delete_button(click_id)


def test_a_button_that_could_not_be_sent_is_retried(bus):
    connector = FailingConnector(bus)
    sender = MessageSender(connector)

    assert sender.create_button(1, 0, 0, 10, 5, "x") is False
    assert sender.is_live(1) is False

    connector.accept = True
    assert sender.create_button(1, 0, 0, 10, 5, "x") is True
    assert len(connector.buttons) == 1


def test_a_delete_that_could_not_be_sent_keeps_the_button_in_the_registry(bus):
    connector = FailingConnector(bus)
    connector.accept = True
    sender = MessageSender(connector)
    sender.create_button(1, 0, 0, 10, 5, "x")

    connector.accept = False
    assert sender.remove_button(1) is False
    assert sender.is_live(1) is True

    connector.accept = True
    assert sender.remove_button(1) is True


# ─── Encoder fuzz ────────────────────────────────────────────────────────────

def _no_dangling_caret(data: bytes) -> bool:
    """LFS reads ``^`` as 'consume the next byte' -- one must never be last."""
    index = 0
    while index < len(data):
        if data[index] == 0x5E:
            if index + 1 >= len(data):
                return False
            index += 2
        else:
            index += 1
    return True


def test_the_encoder_never_produces_an_illegal_stream():
    """Random text against every byte budget: the two rules that must hold
    whatever the input are 'inside the limit' and 'no dangling caret'."""
    import random

    from lfs.text_encoding import encode_lfs_text

    alphabet = "abcXYZ 019.^1^7:-/ßäşıÇ日"
    random.seed(7)
    for _ in range(3000):
        text = ''.join(random.choice(alphabet)
                       for _ in range(random.randint(0, 60)))
        max_chars = random.choice([None, 1, 4, 10, 30])
        max_bytes = random.choice([239, 127, 63, 12, 5])

        encoded = encode_lfs_text(text, max_chars, max_bytes)

        assert len(encoded) <= max_bytes, (text, max_chars, max_bytes, encoded)
        assert _no_dangling_caret(encoded), (text, max_chars, max_bytes, encoded)
        decode_button_text(encoded)          # must not raise


def test_a_literal_double_caret_is_not_cut_in_half():
    assert encode_button_text("-X^^") == b"-X^^"
