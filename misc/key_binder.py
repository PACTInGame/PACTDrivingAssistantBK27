import threading
from pynput import keyboard, mouse


class Keybinder:
    def __init__(self, event_bus):
        self.event_bus = event_bus
        self.event_bus.subscribe('await_keybinding', self._listen_for_key)
        self._listening = False
        self._current_setting = None
        self._keyboard_listener = None
        self._mouse_listener = None
        self._mouse_left_first_press = False

    def _listen_for_key(self, data):
        """Start listening for a key press in a separate thread."""
        setting = data.get('setting')
        if setting is None:
            return

        self._current_setting = setting
        thread = threading.Thread(target=self._start_listening, daemon=True)
        thread.start()

    def _start_listening(self):
        """Start the keyboard and mouse listeners."""
        if self._listening:
            return

        self._listening = True
        self._mouse_left_first_press = False

        # Start keyboard listener
        self._keyboard_listener = keyboard.Listener(
            on_press=self._on_key_press
        )
        self._keyboard_listener.start()

        # Start mouse listener
        self._mouse_listener = mouse.Listener(
            on_click=self._on_mouse_click
        )
        self._mouse_listener.start()

    def _on_key_press(self, key):
        """Handle keyboard key press events."""
        if not self._listening:
            return False

        button_name = self._get_key_name(key)
        self._emit_keybinding(button_name)
        return False  # Stop listener

    def _on_mouse_click(self, x, y, button, pressed):
        """Handle mouse click events."""
        if not self._listening or not pressed:
            return True

        button_name = self._get_mouse_button_name(button)

        # Special handling for left mouse button
        if button_name == 'mousel':
            if not self._mouse_left_first_press:
                self._mouse_left_first_press = True
                self._emit_mouse_message()
                return True  # Continue listening
            else:
                self._emit_keybinding(button_name)
                return False  # Stop listener
        else:
            self._emit_keybinding(button_name)
            return False  # Stop listener

    def _get_key_name(self, key):
        """Convert keyboard key to string representation."""
        try:
            # Handle special keys
            if hasattr(key, 'name'):
                return key.name
            # Handle regular character keys
            elif hasattr(key, 'char') and key.char is not None:
                return key.char
            else:
                return str(key)
        except AttributeError:
            return str(key)

    def _get_mouse_button_name(self, button):
        """Convert mouse button to string representation."""
        if button == mouse.Button.left:
            return 'mousel'
        elif button == mouse.Button.right:
            return 'mouser'
        elif button == mouse.Button.middle:
            return 'mousem'
        else:
            return str(button)

    def _emit_keybinding(self, button):
        """Emit the new keybinding event and stop listening."""
        self.stop_listening()

        if self._current_setting is not None:
            self.event_bus.emit('new_keybinding', {
                'button': button,
                'setting': self._current_setting
            })
            self._current_setting = None
    def _emit_mouse_message(self):
        self.event_bus.emit("notification", {'notification': 'Press Mouse L again to bind!'})

    def stop_listening(self):
        """Stop all active listeners."""
        self._listening = False
        self._mouse_left_first_press = False

        if self._keyboard_listener:
            self._keyboard_listener.stop()
            self._keyboard_listener = None

        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None
