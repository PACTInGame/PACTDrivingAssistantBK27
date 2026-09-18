"""Reading LFS's own controller configuration (``misc/lfs_config.py``).

The format is undocumented and was reverse engineered (``reference/lfs-config-
files.md``), so the files here are **built** to that specification rather than
copied from an install: a test that only replays one machine's bytes proves the
reader works on that machine.

What matters is not the parsing but the two decisions on top of it -- which file
belongs to the driver, and what the stored numbers mean -- because both feed a
command that destroys an axis assignment when it is wrong.
"""

import os
import struct

import pytest

from misc.lfs_config import AXIS_FUNCTIONS, UNASSIGNED, axis_assignments


def write_controller_file(path, assignments, version=7, buttons=77):
    """A ``.csf`` as LFS writes it, with *assignments* as {function: (axis, invert)}."""
    width = 4 if version == 1 else 8
    data = bytearray(b'LFSCON')
    data += bytes([0, version])
    data += struct.pack('<f', 1080.0)          # wheel turn angle
    data += b'\x00' * (0x2c - len(data))
    data += struct.pack('<I', buttons)
    data += b'\x00' * (buttons * 4)
    data += struct.pack('<I', len(AXIS_FUNCTIONS))
    for function in AXIS_FUNCTIONS:
        axis, invert = assignments.get(function, (UNASSIGNED, 0))
        entry = struct.pack('<HH', axis, invert)
        data += entry + b'\x00' * (width - 4)
    with open(path, 'wb') as handle:
        handle.write(bytes(data))
    return path


@pytest.fixture
def lfs_dir(tmp_path):
    misc = tmp_path / 'data' / 'misc'
    misc.mkdir(parents=True)
    return str(tmp_path)


def misc_path(lfs_dir, name):
    return os.path.join(lfs_dir, 'data', 'misc', name)


# ─── The stored numbers are not the numbers /axis takes ──────────────────────

def test_the_offset_is_derived_from_the_brake_axis_not_assumed(lfs_dir):
    """The file stores a small index; ``/axis`` wants one higher on this
    install. Deriving it from the one number we are sure of means the same
    offset is used for every function, whatever it turns out to be."""
    write_controller_file(misc_path(lfs_dir, 'Wheel.csf'), {
        'steer': (7, 0), 'throttle': (8, 1), 'brake': (11, 1),
        'clutch': (12, 1)})

    resolved = axis_assignments(lfs_dir, known_brake_axis=12)

    assert resolved['throttle'].axis == 9
    assert resolved['steer'].axis == 8
    assert resolved['clutch'].axis == 13


def test_an_install_with_no_offset_at_all_still_works(lfs_dir):
    write_controller_file(misc_path(lfs_dir, 'Wheel.csf'),
                          {'throttle': (8, 1), 'brake': (12, 1)})

    resolved = axis_assignments(lfs_dir, known_brake_axis=12)

    assert resolved['throttle'].axis == 8


def test_the_polarity_comes_out_with_the_axis(lfs_dir):
    """Restoring the axis without it hands the driver an inverted throttle."""
    write_controller_file(misc_path(lfs_dir, 'Wheel.csf'),
                          {'throttle': (8, 1), 'brake': (11, 1)})

    assert axis_assignments(lfs_dir, 12)['throttle'].invert == 1


def test_unassigned_functions_are_left_out(lfs_dir):
    """``0xFFFF`` is what ``/axis -1 <function>`` writes; it is not axis 65535."""
    write_controller_file(misc_path(lfs_dir, 'Wheel.csf'),
                          {'brake': (11, 1), 'clutch': (UNASSIGNED, 0)})

    resolved = axis_assignments(lfs_dir, 12)

    assert 'clutch' not in resolved
    assert 'brake' in resolved


# ─── Picking the driver's own device out of the folder ───────────────────────

def test_the_file_whose_brake_matches_is_the_drivers(lfs_dir):
    """Every device gets its own complete snapshot, so the folder is full of
    other devices' opinions. The brake axis we already know picks one."""
    write_controller_file(misc_path(lfs_dir, 'A_Other_Stick.csf'),
                          {'throttle': (0, 0), 'brake': (1, 0)})
    write_controller_file(misc_path(lfs_dir, 'Z_Wheel.csf'),
                          {'throttle': (8, 1), 'brake': (11, 1)})

    resolved = axis_assignments(lfs_dir, known_brake_axis=12)

    assert resolved['throttle'].axis == 9


def test_no_matching_file_means_no_answer(lfs_dir):
    """Refusing beats returning a number nobody vouched for: the caller then
    leaves the throttle alone instead of destroying an axis assignment."""
    write_controller_file(misc_path(lfs_dir, 'Wheel.csf'),
                          {'throttle': (0, 0), 'brake': (1, 0)})

    assert axis_assignments(lfs_dir, known_brake_axis=12) is None


def test_a_missing_folder_is_not_an_error(tmp_path):
    assert axis_assignments(str(tmp_path / 'no-lfs-here'), 12) is None


# ─── Files a user can break ──────────────────────────────────────────────────

@pytest.mark.parametrize('content', [b'', b'LFS', b'NOTLFSCON' + b'\x00' * 100,
                                     b'LFSCON\x00\x07' + b'\x00' * 20])
def test_a_truncated_or_foreign_file_is_skipped(lfs_dir, content):
    with open(misc_path(lfs_dir, 'Broken.csf'), 'wb') as handle:
        handle.write(content)
    write_controller_file(misc_path(lfs_dir, 'Wheel.csf'),
                          {'throttle': (8, 1), 'brake': (11, 1)})

    assert axis_assignments(lfs_dir, 12)['throttle'].axis == 9


def test_a_file_with_a_different_axis_count_is_not_believed(lfs_dir):
    """Every version seen has exactly eleven axis functions. A different number
    means the layout changed and nothing in it can be trusted."""
    path = misc_path(lfs_dir, 'Wheel.csf')
    write_controller_file(path, {'throttle': (8, 1), 'brake': (11, 1)})
    with open(path, 'r+b') as handle:
        handle.seek(0x2c)
        buttons = struct.unpack('<I', handle.read(4))[0]
        handle.seek(0x2c + 4 + buttons * 4)
        handle.write(struct.pack('<I', 12))

    assert axis_assignments(lfs_dir, 12) is None


def test_the_2006_preset_layout_is_read_too(lfs_dir):
    """Version 1 files have four-byte axis entries, not eight."""
    write_controller_file(misc_path(lfs_dir, 'Old.con'),
                          {'throttle': (1, 1), 'brake': (2, 1)}, version=1,
                          buttons=52)

    assert axis_assignments(lfs_dir, known_brake_axis=3)['throttle'].axis == 2
