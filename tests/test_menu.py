"""WP6 -- menu dispatch tables, and the settings the menu writes.

The two halves of a menu are its button list and its action table. A button ID
that is drawn but has no action does nothing; an action for an ID the menu does
not draw can only be reached from the wrong menu. Both are checked here against
each other, for every menu, instead of by reading the dispatch chain.
"""

import pytest

import pyinsim
from ui.menu_system import BTN_CLOSE, BTN_OPEN_MENU, MenuSystem
from ui.ui_manager import UIManager


MENUS = ('main', 'driving', 'parking', 'system', 'cop', 'ai_traffic', 'keys',
         'await_key')


@pytest.fixture
def ui(bus, message_sender, settings) -> UIManager:
    return UIManager(bus, message_sender, settings)


@pytest.fixture
def menu(ui, settings) -> MenuSystem:
    return MenuSystem(ui, settings)


def clickable_ids(buttons):
    return {button_id for button_id, _x, _y, _w, _h, _text, style in buttons
            if style & pyinsim.ISB_CLICK}


# ─── Button list ↔ action table ──────────────────────────────────────────────

@pytest.mark.parametrize("name", MENUS)
def test_every_clickable_button_has_an_action(menu, name):
    drawn = clickable_ids(menu.buttons_for(name))
    handled = set(menu.actions_for(name)) | {BTN_CLOSE}

    assert drawn - handled == set(), f"{name}: drawn but dead"


@pytest.mark.parametrize("name", MENUS)
def test_every_action_belongs_to_a_button_that_menu_draws(menu, name):
    drawn = {button_id for button_id, *_rest in menu.buttons_for(name)}

    assert set(menu.actions_for(name)) - drawn == set(), \
        f"{name}: action for a button it never draws"


@pytest.mark.parametrize("name", MENUS)
def test_no_menu_claims_the_shared_ids(menu, name):
    """20 opens the menu and 40 closes it -- no menu may reuse them."""
    assert BTN_OPEN_MENU not in menu.actions_for(name)
    assert BTN_CLOSE not in menu.actions_for(name)


def test_an_id_from_another_menu_does_nothing(menu, settings):
    """ID 30 is 'Calibrate' in the driving menu and unused in the cop menu."""
    menu.open_cop_menu()
    before = dict(settings._settings)

    menu._handle_menu_click(30)

    assert menu.current_menu == 'cop'
    assert settings._settings == before


# ─── Navigation ──────────────────────────────────────────────────────────────

def test_the_floating_button_opens_the_main_menu(menu):
    menu.close_menu()

    menu._handle_menu_click(BTN_OPEN_MENU)

    assert menu.current_menu == 'main'


@pytest.mark.parametrize("button_id, expected", [
    (22, 'driving'), (23, 'parking'), (24, 'system'), (25, 'cop'),
    (26, 'keys'), (28, 'ai_traffic'),
])
def test_the_main_menu_reaches_every_submenu(menu, button_id, expected):
    menu.open_main_menu()

    menu._handle_menu_click(button_id)

    assert menu.current_menu == expected


def test_close_returns_to_the_main_menu_from_a_submenu(menu):
    menu.open_driving_menu()

    menu._handle_menu_click(BTN_CLOSE)

    assert menu.current_menu == 'main'


def test_close_from_the_main_menu_closes_everything(menu, fake_connector):
    menu.open_main_menu()
    fake_connector.reset()

    menu._handle_menu_click(BTN_CLOSE)

    assert menu.current_menu == 'none'
    assert fake_connector.drawn_ids() == {BTN_OPEN_MENU}


# ─── Toggles write the setting they claim to ─────────────────────────────────

@pytest.mark.parametrize("button_id, key", [
    (22, 'forward_collision_warning'),
    (24, 'blind_spot_warning'),
    (25, 'cross_traffic_warning'),
    (26, 'automatic_gearbox'),
    (27, 'auto_hold'),
    (28, 'adaptive_lights'),
    (29, 'high_beam_assist'),
])
def test_the_driving_menu_toggles(menu, settings, button_id, key):
    menu.open_driving_menu()
    before = settings.get(key)

    menu._handle_menu_click(button_id)

    assert settings.get(key) is (not before)
    assert menu.current_menu == 'driving'      # redrawn, so the colour updates


@pytest.mark.parametrize("button_id, key", [
    (23, 'collision_warning_distance'),
    (31, 'cross_traffic_warning_distance'),
])
def test_the_distance_settings_cycle_through_all_three_steps(menu, settings,
                                                             button_id, key):
    menu.open_driving_menu()
    seen = []
    for _ in range(4):
        seen.append(settings.get(key))
        menu._handle_menu_click(button_id)

    assert seen == [1, 2, 0, 1]


def test_the_unit_setting_cycles_between_the_two_allowed_values(menu, settings):
    menu.open_system_settings()

    menu._handle_menu_click(22)
    assert settings.get('unit') == 'imperial'
    menu._handle_menu_click(22)
    assert settings.get('unit') == 'metric'


def test_the_hud_arrows_move_and_stay_on_screen(menu, settings):
    menu.open_system_settings()
    before = settings.get('hud_height')

    menu._handle_menu_click(27)               # down

    assert settings.get('hud_height') == before + 2
    assert menu.current_menu == 'system'


# ─── PDC has exactly one representation (WP6) ────────────────────────────────

def test_the_boolean_is_derived_from_the_mode(settings):
    settings.set('park_distance_control_mode', 0)
    assert settings.get('park_distance_control') is False

    settings.set('park_distance_control_mode', 2)
    assert settings.get('park_distance_control') is True


def test_the_boolean_is_not_stored_separately(settings):
    assert 'park_distance_control' not in settings._settings


def test_switching_pdc_off_and_on_keeps_the_chosen_mode(menu, settings):
    settings.set('park_distance_control_mode', 2)      # visual + audio
    menu.open_parking_menu()

    menu._handle_menu_click(22)                        # off
    assert settings.get('park_distance_control_mode') == 0
    assert settings.get('park_distance_control') is False

    menu._handle_menu_click(22)                        # on again
    assert settings.get('park_distance_control_mode') == 2
    assert settings.get('park_distance_control') is True


def test_the_mode_button_cycles_only_the_active_modes(menu, settings):
    settings.set('park_distance_control_mode', 1)
    menu.open_parking_menu()

    menu._handle_menu_click(23)
    assert settings.get('park_distance_control_mode') == 2
    menu._handle_menu_click(23)
    assert settings.get('park_distance_control_mode') == 1


def test_the_mode_button_does_nothing_while_pdc_is_off(menu, settings):
    settings.set('park_distance_control_mode', 0)
    menu.open_parking_menu()

    menu._handle_menu_click(23)

    assert settings.get('park_distance_control_mode') == 0


def test_switching_pdc_off_removes_its_display(menu, settings):
    """Otherwise the PDC buttons stay up until the next state change."""
    removed = []
    menu.ui_manager.remove_pdc_display = lambda: removed.append(True)
    settings.set('park_distance_control_mode', 2)
    menu.open_parking_menu()

    menu._handle_menu_click(22)

    assert settings.get('park_distance_control') is False
    assert removed == [True]


# ─── The user's own control mode survives (WP6) ──────────────────────────────

def test_a_player_change_no_longer_overwrites_the_users_control_mode(
        menu, settings, bus):
    settings.set('own_control_mode', 2)             # user picked joystick

    bus.emit('player_name_changed', {'player_name': 'Tester', 'control_mode': 1})

    assert settings.get('own_control_mode') == 2


# ─── Language is read live (WP6) ─────────────────────────────────────────────

def test_the_menu_follows_a_language_change_made_elsewhere(menu, settings):
    assert menu.language == 'de'

    settings.set('language', 'fr')          # e.g. a chat command

    assert menu.language == 'fr'
    labels = [text for _id, _x, _y, _w, _h, text, _s in menu.buttons_for('main')]
    assert "Menu Principal" in labels


def test_the_language_button_cycles_and_persists(menu, settings):
    menu.open_main_menu()
    order = menu.translator.supported_languages
    expected = order[(order.index('de') + 1) % len(order)]

    menu._handle_menu_click(27)

    assert settings.get('language') == expected
    assert menu.language == expected


def test_an_unsupported_stored_language_does_not_break_the_cycle(menu, settings):
    settings._settings['language'] = 'xx'      # bypassing validation on purpose

    menu.change_language()

    assert settings.get('language') in menu.translator.supported_languages


# ─── Key rebinding ───────────────────────────────────────────────────────────

def test_a_key_button_asks_for_a_binding_and_shows_the_prompt(menu, recorder):
    seen = recorder('await_keybinding')
    menu.open_keys_settings()

    menu._handle_menu_click(23)               # shift up

    assert menu.current_menu == 'await_key'
    assert seen.last('await_keybinding') == {'setting': 'user_shift_up_key'}


def test_a_new_binding_is_stored_and_returns_to_the_key_menu(menu, settings, bus):
    menu.open_keys_settings()
    menu._handle_menu_click(22)               # handbrake

    bus.emit('new_keybinding', {'setting': 'user_handbrake_key', 'button': 'j'})

    assert settings.get('user_handbrake_key') == 'j'
    assert menu.current_menu == 'keys'


def test_the_key_menu_shows_the_bound_keys(menu, settings):
    settings.set('user_clutch_key', 'v')

    labels = {button_id: text
              for button_id, _x, _y, _w, _h, text, _s in menu.buttons_for('keys')}

    assert labels[30] == 'V'                  # 25 is the clutch row, 30 its value


def test_cancelling_a_rebind_returns_to_the_key_menu(menu):
    menu.open_keys_settings()
    menu._handle_menu_click(22)

    menu._handle_menu_click(BTN_CLOSE)

    assert menu.current_menu == 'keys'


# ─── Repaint after SHIFT+B (WP5 contract, kept by the table refactor) ────────

@pytest.mark.parametrize("name", MENUS)
def test_every_menu_repaints_itself_after_the_buttons_are_cleared(
        menu, fake_connector, name):
    menu.on_track = True
    if name == 'await_key':
        menu.open_awaiting_key('user_clutch_key')
    else:
        menu._painters[name]()
    expected = {button_id for button_id, *_rest in menu.buttons_for(name)}
    fake_connector.reset()

    menu._on_buttons_cleared()

    assert fake_connector.drawn_ids() == expected
    assert menu.current_menu == name


# ─── Click handling is hostile-input safe ────────────────────────────────────

def test_a_packet_without_a_click_id_is_ignored(menu):
    class Empty:
        pass

    menu._handle_ui_action(Empty())           # must not raise


def test_a_button_outside_the_menu_range_is_ignored(menu):
    class Click:
        ClickID = 61                          # notification line

    menu.open_main_menu()
    menu._handle_ui_action(Click())

    assert menu.current_menu == 'main'
