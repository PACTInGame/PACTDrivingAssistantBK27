"""Contact packets must survive the production dispatch path without patches."""

import struct
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import pyinsim

from lfs.connector import LFSConnector


@pytest.mark.parametrize('layout,time', [(40, 65535), (44, 0xFEDCBA98)])
def test_contact_dispatch_preserves_fields(layout, time):
    car = struct.pack('<3Bb6B2b2h', 1, 2, 0, -30,
                      255, 254, 240, 200, 255, 240, -128, 127, -32768, 32767)
    fields = (layout // 4, pyinsim.ISP_CON, 0, 0, 123)
    header = (struct.pack('<4B2HI', *fields, 42, time) if layout == 44 else
              struct.pack('<4B2H', *fields, time))
    received = []
    connection = SimpleNamespace(_callbacks={
        pyinsim.ISP_CON: [lambda _, packet: received.append(packet)]})
    pyinsim.core._InSim._handle_insim_packet(connection, header + car + car)
    packet, = received
    assert type(packet) is pyinsim.IS_CON
    assert packet.Time == time
    assert packet.SpW == (42 if layout == 44 else 0)
    assert packet.SpClose == 123
    assert packet.con_layout == layout
    for contact in (packet.A, packet.B):
        assert (contact.Steer, contact.ThrBrk, contact.CluHan, contact.GearSp) == (-30, 255, 254, 240)
        assert (contact.Speed, contact.Direction, contact.Heading) == (200, 255, 240)
        assert (contact.AccelF, contact.AccelR, contact.X, contact.Y) == (-128, 127, -32768, 32767)


@pytest.mark.parametrize('data', [b'', bytes([9]) + bytes(35),
                                  bytes([11]) + bytes(39), bytes([10]) + bytes(43)])
def test_contact_rejects_invalid_lengths(data):
    with pytest.raises(ValueError, match='unexpected size'):
        pyinsim.IS_CON().unpack(data)


def test_connect_keeps_outgauge_without_opening_unused_outsim(bus, settings, monkeypatch):
    insim = Mock()
    gauges = Mock()
    outsim = Mock(side_effect=AssertionError('unused OutSim must not start'))
    monkeypatch.setattr(pyinsim, 'insim', Mock(return_value=insim))
    monkeypatch.setattr(pyinsim, 'outgauge', gauges)
    monkeypatch.setattr(pyinsim, 'outsim', outsim)
    connector = LFSConnector(bus, settings)
    connector.connect()
    assert connector.is_connected
    gauges.assert_called_once()
    outsim.assert_not_called()
    assert connector.outsim is None
