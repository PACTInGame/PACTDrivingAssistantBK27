"""WP5 -- screen context, button lifecycle and HUD placement.

Covers the acceptance list of WP5: every documented LFS screen maps to the
expected context, nothing is drawn in the main menu, the UI comes back after
SHIFT+B, the HUD clamp keeps every element on screen, the level-2 warning
really alternates, and the notification queue is bounded.
"""

import pytest

import pyinsim
from lfs.lfs_state import (SCREEN_ENTRY, SCREEN_GAME, SCREEN_GARAGE,
                           SCREEN_MAIN_MENU, SCREEN_OPTIONS, SCREEN_SHIFTU,
                           StateHandler)
from ui import ui_manager as ui_module
from ui.menu_system import MenuSystem
from ui.ui_manager import (ALL_BUTTONS_RANGE, BTN_HUD_GEAR, BTN_HUD_RPM,
                           BTN_HUD_SPEED, BTN_IDLE_BANNER, BTN_NOTIFICATION,
                           HUD_BOX_BOTTOM, HUD_BOX_LEFT, HUD_BOX_RIGHT,
                           HUD_BOX_TOP, MAX_QUEUED_NOTIFICATIONS,
                           NOTIFICATION_DISPLAY_S, RESERVED_BOTTOM,
                           RESERVED_LEFT, RESERVED_RIGHT, RESERVED_TOP,
                           SCREEN_MAX, UIManager, clamp_hud_position,
                           hud_overlaps_reserved_area)


# ─── Fake clock ──────────────────────────────────────────────────────────────

class Clock:
    """Monotonic clock the test drives by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(ui_module.time, 'perf_counter', fake)
    return fake


@pytest.fixture
def state(fake_connector) -> StateHandler:
    return StateHandler(fake_connector)


@pytest.fixture
def ui(bus, message_sender, settings, clock) -> UIManager:
    return UIManager(bus, message_sender, settings)


@pytest.fixture
def menu(ui, settings) -> MenuSystem:
    return MenuSystem(ui, settings)


def on_track_state(**overrides):
    data = {'on_track': True, 'screen': SCREEN_GAME, 'buttons_allowed': True,
            'text_entry': False, 'dialog': False, 'track': 'SO6R',
            'in_game_cam': 3, 'in_game_interface': 0, 'submode_interface': 0}
    data.update(overrides)
    return data


# ─── Screen contexts (known-issues #25) ──────────────────────────────────────

def test_main_menu_sets_neither_game_nor_front_end(bus, recorder, state,
                                                   make_sta_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(base_flags=0))

    published = seen.last('state_data')
    assert published['screen'] == SCREEN_MAIN_MENU
    assert published['buttons_allowed'] is False
    assert published['on_track'] is False


def test_entry_screen_is_its_own_context(bus, recorder, state, make_sta_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(on_track=False))

    published = seen.last('state_data')
    assert published['screen'] == SCREEN_ENTRY
    assert published['buttons_allowed'] is True


def test_in_game_is_the_normal_context(bus, recorder, state, make_sta_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(on_track=True))

    published = seen.last('state_data')
    assert published['screen'] == SCREEN_GAME
    assert published['on_track'] is True
    assert published['ui_visible'] is True


def test_garage_is_recognised_from_is_cim(bus, recorder, state, make_sta_packet,
                                          make_cim_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(on_track=True))
    bus.emit('interface_mode_changed',
             make_cim_packet(mode=pyinsim.CIM_GARAGE, sub_mode=pyinsim.GRG_TYRES))

    published = seen.last('state_data')
    assert published['screen'] == SCREEN_GARAGE
    # The two placeholder fields are finally filled (known-issues #25).
    assert published['in_game_interface'] == pyinsim.CIM_GARAGE
    assert published['submode_interface'] == pyinsim.GRG_TYRES
    assert published['on_track'] is True


def test_options_and_selection_screens_hide_normal_buttons(
        bus, recorder, state, make_sta_packet, make_cim_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(on_track=True))

    for mode in (pyinsim.CIM_OPTIONS, pyinsim.CIM_HOST_OPTIONS,
                 pyinsim.CIM_CAR_SELECT, pyinsim.CIM_TRACK_SELECT):
        bus.emit('interface_mode_changed', make_cim_packet(mode=mode))
        published = seen.last('state_data')
        assert published['screen'] == SCREEN_OPTIONS, mode
        assert published['buttons_allowed'] is False, mode


def test_shift_u_free_view_is_its_own_context(bus, recorder, state,
                                              make_sta_packet, make_cim_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed',
             make_sta_packet(on_track=True, flags=pyinsim.ISS_SHIFTU))
    assert seen.last('state_data')['screen'] == SCREEN_SHIFTU
    assert seen.last('state_data')['shift_u'] is True

    bus.emit('interface_mode_changed',
             make_cim_packet(mode=pyinsim.CIM_SHIFTU, sub_mode=pyinsim.FVM_EDIT))
    assert seen.last('state_data')['screen'] == SCREEN_SHIFTU
    assert seen.last('state_data')['submode_interface'] == pyinsim.FVM_EDIT


def test_dialog_and_text_entry_do_not_change_on_track(bus, recorder, state,
                                                      make_sta_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(
        on_track=True, flags=pyinsim.ISS_DIALOG | pyinsim.ISS_TEXT_ENTRY))

    published = seen.last('state_data')
    assert published['on_track'] is True
    assert published['dialog'] is True
    assert published['text_entry'] is True


def test_multiplayer_flag_is_published(bus, recorder, state, make_sta_packet):
    seen = recorder('state_data')
    bus.emit('game_state_changed',
             make_sta_packet(on_track=True, flags=pyinsim.ISS_MULTI))

    assert seen.last('state_data')['multiplayer'] is True


def test_every_legacy_state_data_key_survives(bus, recorder, state, make_sta_packet):
    """Seven components read these keys - the payload may only grow."""
    seen = recorder('state_data')
    bus.emit('game_state_changed', make_sta_packet(on_track=True))

    published = seen.last('state_data')
    for key in ('on_track', 'text_entry', 'dialog', 'track', 'in_game_cam',
                'in_game_interface', 'submode_interface'):
        assert key in published, key


def test_a_cim_packet_before_any_sta_is_not_published(bus, recorder, state,
                                                      make_cim_packet):
    seen = recorder('state_data')
    bus.emit('interface_mode_changed', make_cim_packet(mode=pyinsim.CIM_GARAGE))

    assert seen.count('state_data') == 0


def test_malformed_state_packets_do_not_raise(bus, recorder, state, make_sta_packet):
    class Bare:
        pass

    seen = recorder('state_data')
    bus.emit('game_state_changed', Bare())
    bus.emit('interface_mode_changed', Bare())

    assert seen.last('state_data')['screen'] == SCREEN_MAIN_MENU


def test_isp_cim_is_bound_in_the_connector():
    from lfs.connector import LFSConnector
    handlers = LFSConnector.__dict__
    assert '_handle_interface_mode' in handlers


# ─── Nothing is drawn on the main menu (WP5 scope 4) ─────────────────────────

def test_no_button_is_created_in_main_menu_context(bus, ui, fake_connector):
    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_MAIN_MENU,
                                          buttons_allowed=False))

    assert fake_connector.buttons == []


def test_the_idle_banner_appears_only_on_the_entry_screen(bus, ui, fake_connector):
    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_ENTRY))
    assert BTN_IDLE_BANNER in fake_connector.drawn_ids()

    fake_connector.reset()
    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_MAIN_MENU,
                                          buttons_allowed=False))

    assert fake_connector.buttons == []
    assert BTN_IDLE_BANNER in fake_connector.deletes


def test_the_banner_no_longer_collides_with_the_hud_speed_field(bus, ui,
                                                                fake_connector):
    """ID 1 used to be both the HUD speed field and the off-track banner."""
    assert BTN_IDLE_BANNER != BTN_HUD_SPEED

    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_ENTRY))
    banner = fake_connector.last_button(BTN_IDLE_BANNER)
    assert banner is not None

    bus.emit('state_data', on_track_state())
    ui.update_hud()

    assert BTN_IDLE_BANNER in fake_connector.deletes
    assert {BTN_HUD_SPEED, BTN_HUD_RPM, BTN_HUD_GEAR} <= fake_connector.drawn_ids()


def test_leaving_the_track_clears_every_button_and_resets_state(bus, ui,
                                                                fake_connector):
    bus.emit('state_data', on_track_state())
    ui.siren_ui_visible = True
    ui.update_hud()
    fake_connector.reset()

    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_ENTRY))

    assert BTN_HUD_SPEED in fake_connector.deletes
    assert ui.siren_ui_visible is False
    assert list(ui.notifications) == []


def test_the_hud_is_not_drawn_while_lfs_hides_normal_buttons(bus, ui,
                                                             fake_connector):
    bus.emit('state_data', on_track_state())
    ui.update_hud()
    fake_connector.reset()

    bus.emit('state_data', on_track_state(screen=SCREEN_OPTIONS,
                                          buttons_allowed=False))
    ui.update_hud()

    assert fake_connector.buttons == []


# ─── SHIFT+B repaint (known-issues #26) ──────────────────────────────────────

def test_the_hud_repaints_after_the_user_cleared_the_buttons(
        bus, ui, message_sender, fake_connector):
    bus.emit('state_data', on_track_state())
    ui.update_hud()
    ui.update_hud()                       # unchanged -> suppressed by registry
    assert len(fake_connector.buttons) == 3

    bus.emit('buttons_cleared', {'sub_type': pyinsim.BFN_USER_CLEAR})
    ui.update_hud()

    assert {BTN_HUD_SPEED, BTN_HUD_RPM, BTN_HUD_GEAR} <= fake_connector.drawn_ids()
    assert len(fake_connector.buttons) == 6


def test_the_idle_banner_repaints_after_the_user_cleared_the_buttons(
        bus, ui, fake_connector):
    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_ENTRY))
    fake_connector.reset()

    bus.emit('buttons_cleared', {'sub_type': pyinsim.BFN_REQUEST})

    assert BTN_IDLE_BANNER in fake_connector.drawn_ids()


def test_the_menu_repaints_after_the_user_cleared_the_buttons(
        bus, ui, menu, fake_connector):
    bus.emit('state_data', on_track_state())
    fake_connector.reset()
    menu.open_driving_menu()
    drawn_before = fake_connector.drawn_ids()
    fake_connector.reset()

    bus.emit('buttons_cleared', {'sub_type': pyinsim.BFN_USER_CLEAR})

    assert drawn_before == fake_connector.drawn_ids()
    assert 21 in fake_connector.drawn_ids()       # menu title is back


def test_the_menu_does_not_repaint_off_track(bus, ui, menu, fake_connector):
    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_ENTRY))
    fake_connector.reset()

    bus.emit('buttons_cleared', {'sub_type': pyinsim.BFN_USER_CLEAR})

    assert 21 not in fake_connector.drawn_ids()


def test_bfn_from_lfs_is_republished_as_buttons_cleared(bus, recorder,
                                                        make_bfn_packet):
    from lfs.connector import LFSConnector

    seen = recorder('buttons_cleared')
    connector = LFSConnector.__new__(LFSConnector)
    connector.event_bus = bus

    connector._handle_button_function(None, make_bfn_packet(pyinsim.BFN_USER_CLEAR))
    connector._handle_button_function(None, make_bfn_packet(pyinsim.BFN_REQUEST))
    connector._handle_button_function(None, make_bfn_packet(pyinsim.BFN_DEL_BTN))

    assert seen.count('buttons_cleared') == 2


# ─── HUD placement (known-issues #27) ────────────────────────────────────────

def _hud_box(x, y):
    return (x + HUD_BOX_LEFT, x + HUD_BOX_RIGHT, y + HUD_BOX_TOP, y + HUD_BOX_BOTTOM)


@pytest.mark.parametrize("x,y", [
    (0, 0), (200, 200), (-50, -50), (500, 500), (90, 119), (3, 6), (171, 187),
])
def test_the_clamp_keeps_every_hud_element_on_screen(x, y):
    left, right, top, bottom = _hud_box(*clamp_hud_position(x, y))

    assert 0 <= left <= SCREEN_MAX
    assert 0 <= right <= SCREEN_MAX
    assert 0 <= top <= SCREEN_MAX
    assert 0 <= bottom <= SCREEN_MAX


def test_the_clamp_leaves_a_valid_position_alone():
    assert clamp_hud_position(90, 119) == (90, 119)


def test_the_clamp_survives_a_hand_edited_settings_file():
    assert clamp_hud_position("nonsense", None) == (3, 6)


def test_the_reserved_area_detector_matches_the_documented_rectangle():
    # Far right of the reserved rectangle: no overlap.
    assert hud_overlaps_reserved_area(150, 119) is False
    # Above it.
    assert hud_overlaps_reserved_area(90, 6) is False
    # The shipped default sits inside it - that is why WP5 warns instead of
    # relocating it (reference/ui.md §1.3).
    assert hud_overlaps_reserved_area(90, 119) is True


def test_the_reserved_rectangle_constants_match_insim_txt():
    assert (RESERVED_LEFT, RESERVED_RIGHT) == (0, 110)
    assert (RESERVED_TOP, RESERVED_BOTTOM) == (30, 170)


def test_moving_the_hud_never_leaves_the_screen(menu, settings):
    settings.set('hud_width', 190)
    settings.set('hud_height', 195)

    for _ in range(50):
        menu._move_hud(2, 2)

    x, y = settings.get('hud_width'), settings.get('hud_height')
    assert (x, y) == clamp_hud_position(x, y)
    left, right, top, bottom = _hud_box(x, y)
    assert 0 <= left and right <= SCREEN_MAX
    assert 0 <= top and bottom <= SCREEN_MAX


def test_every_drawn_hud_element_stays_on_screen(bus, ui, settings,
                                                 fake_connector):
    settings.set('hud_width', 500)          # hand-edited nonsense
    settings.set('hud_height', -20)
    ui.siren_ui_visible = True
    bus.emit('pdc_changed', {0: 1, 1: 2, 2: 3, 3: 1, 4: 2, 5: 3})
    bus.emit('notification', {'notification': 'hello'})
    bus.emit('blind_spot_warning_changed', {'left': True, 'right': True})
    bus.emit('state_data', on_track_state())
    ui.update_hud()

    assert fake_connector.buttons
    for click_id, _style, top, left, width, height, _text, _inst in fake_connector.buttons:
        assert 0 <= left and left + width <= SCREEN_MAX, click_id
        assert 0 <= top and top + height <= SCREEN_MAX, click_id


def test_the_menu_marks_a_hud_inside_the_reserved_area(menu, settings,
                                                       fake_connector):
    settings.set('hud_width', 90)
    settings.set('hud_height', 119)
    menu.open_system_settings()
    label = fake_connector.last_button(25)
    assert label is not None and label[6].startswith(b'^1')

    fake_connector.reset()
    settings.set('hud_width', 150)
    menu.open_system_settings()

    label = fake_connector.last_button(25)
    assert label is not None and label[6].startswith(b'^7')


# ─── Warning flashing (WP5 scope 6/7) ────────────────────────────────────────

def _hud_styles(ui, fake_connector, clock, passes=8, step=0.05):
    styles = []
    for _ in range(passes):
        ui.update_hud()
        entry = fake_connector.last_button(BTN_HUD_SPEED)
        if entry is not None:
            styles.append(entry[1])
        clock.advance(step)
    return styles


def test_level_two_warning_alternates_styles(bus, ui, fake_connector, clock):
    bus.emit('state_data', on_track_state())
    bus.emit('collision_warning_changed', {'level': 2})

    styles = _hud_styles(ui, fake_connector, clock)

    assert pyinsim.ISB_DARK in styles
    assert pyinsim.ISB_LIGHT in styles


def test_level_three_warning_alternates_styles(bus, ui, fake_connector, clock):
    bus.emit('state_data', on_track_state())
    bus.emit('collision_warning_changed', {'level': 3})

    styles = _hud_styles(ui, fake_connector, clock)

    assert pyinsim.ISB_DARK in styles
    assert pyinsim.ISB_LIGHT in styles


def test_level_one_warning_does_not_flash(bus, ui, fake_connector, clock):
    bus.emit('state_data', on_track_state())
    bus.emit('collision_warning_changed', {'level': 1})

    styles = _hud_styles(ui, fake_connector, clock)

    assert set(styles) == {pyinsim.ISB_DARK}
    assert fake_connector.last_button(BTN_HUD_SPEED)[6] == b'^1- - -'


def test_cross_traffic_level_two_alternates_and_yields_to_fcw(bus, ui,
                                                              fake_connector,
                                                              clock):
    bus.emit('state_data', on_track_state())
    bus.emit('cross_traffic_warning_changed', {'level': 2, 'side': 'right'})

    styles = _hud_styles(ui, fake_connector, clock)
    assert pyinsim.ISB_LIGHT in styles
    assert fake_connector.last_button(BTN_HUD_SPEED)[6] == b'^1< < <'

    bus.emit('collision_warning_changed', {'level': 1})
    ui.update_hud()

    assert fake_connector.last_button(BTN_HUD_SPEED)[6] == b'^1- - -'


def test_the_blink_phase_does_not_depend_on_the_ui_refresh_rate(bus, ui,
                                                                fake_connector,
                                                                clock):
    """Ten passes inside one blink interval must all show the same style."""
    bus.emit('state_data', on_track_state())
    bus.emit('collision_warning_changed', {'level': 3})

    styles = _hud_styles(ui, fake_connector, clock, passes=10, step=0.01)

    assert len(set(styles)) == 1


# ─── Redline / shift light (WP5 scope 6) ─────────────────────────────────────

def test_the_rpm_readout_uses_the_lfs_shift_light(bus, ui, fake_connector,
                                                  make_outgauge_packet):
    bus.emit('state_data', on_track_state())

    bus.emit('outgauge_data', make_outgauge_packet(rpm=3000.0))
    ui.update_hud()
    assert fake_connector.last_button(BTN_HUD_RPM)[6] == b'3.0 rpm'

    bus.emit('outgauge_data', make_outgauge_packet(
        rpm=7000.0, show_lights_mask=pyinsim.DL_SHIFT))
    ui.update_hud()
    assert fake_connector.last_button(BTN_HUD_RPM)[6] == b'^17.0 rpm'


def test_a_new_maximum_rpm_alone_never_turns_the_readout_red(
        bus, ui, fake_connector, make_outgauge_packet):
    """The old heuristic went red at every new highest RPM ever seen."""
    bus.emit('state_data', on_track_state())

    for rpm in (1000.0, 2000.0, 3000.0, 9000.0):
        bus.emit('outgauge_data', make_outgauge_packet(rpm=rpm))
        ui.update_hud()
        assert not fake_connector.last_button(BTN_HUD_RPM)[6].startswith(b'^1')


# ─── Notification queue (WP5 scope 8) ────────────────────────────────────────

def test_the_notification_queue_never_exceeds_its_cap(bus, ui):
    for index in range(MAX_QUEUED_NOTIFICATIONS * 5):
        bus.emit('notification', {'notification': f'msg {index}'})

    assert len(ui.notifications) == MAX_QUEUED_NOTIFICATIONS
    # The newest messages survive, the oldest are dropped.
    assert ui.notifications[-1] == f'msg {MAX_QUEUED_NOTIFICATIONS * 5 - 1}'


def test_notifications_are_shown_one_at_a_time(bus, ui, fake_connector, clock):
    bus.emit('state_data', on_track_state())
    bus.emit('notification', {'notification': 'first'})
    bus.emit('notification', {'notification': 'second'})

    ui.update_hud()
    assert fake_connector.last_button(BTN_NOTIFICATION)[6] == b'first'

    clock.advance(NOTIFICATION_DISPLAY_S / 2)
    ui.update_hud()
    assert fake_connector.last_button(BTN_NOTIFICATION)[6] == b'first'

    clock.advance(NOTIFICATION_DISPLAY_S)
    ui.update_hud()
    assert fake_connector.last_button(BTN_NOTIFICATION)[6] == b'second'

    clock.advance(NOTIFICATION_DISPLAY_S)
    ui.update_hud()
    assert BTN_NOTIFICATION in fake_connector.deletes


def test_the_notification_queue_is_cleared_on_track_exit(bus, ui):
    bus.emit('state_data', on_track_state())
    for index in range(5):
        bus.emit('notification', {'notification': f'msg {index}'})

    bus.emit('state_data', on_track_state(on_track=False, screen=SCREEN_ENTRY))

    assert list(ui.notifications) == []
    assert ui.current_notification is None


def test_an_empty_notification_is_ignored(bus, ui):
    bus.emit('notification', {'notification': ''})
    bus.emit('notification', {})
    bus.emit('notification', None)

    assert list(ui.notifications) == []


# ─── Button-ID map (WP5 scope 3) ─────────────────────────────────────────────

def test_the_button_id_map_has_no_collisions():
    ids = [value for name, value in vars(ui_module).items()
           if name.startswith('BTN_') and isinstance(value, int)]

    assert len(ids) == len(set(ids))


def test_hide_hud_and_remove_pdc_only_delete_live_buttons(bus, ui, message_sender,
                                                          fake_connector):
    bus.emit('state_data', on_track_state())
    ui.update_hud()
    fake_connector.reset()

    ui.hide_hud()
    ui.remove_pdc_display()

    # Three HUD buttons are live; the PDC range holds nothing. The old code
    # called remove_button() 30 times for this.
    assert sorted(fake_connector.deletes) == [BTN_HUD_SPEED, BTN_HUD_RPM,
                                              BTN_HUD_GEAR]


def test_all_buttons_range_covers_the_whole_map():
    first, last = ALL_BUTTONS_RANGE
    ids = [value for name, value in vars(ui_module).items()
           if name.startswith('BTN_') and isinstance(value, int)]

    assert all(first <= button_id <= last for button_id in ids)
