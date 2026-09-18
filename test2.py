import pyautogui
import time
from pynput import mouse

# Variables to store the click position
click_position = None
listener = None


def on_click(x, y, button, pressed):
    """Callback function that captures the first mouse click"""
    global click_position, listener

    if pressed and click_position is None:
        click_position = (x, y)
        print(f"Click position captured: {x}, {y}")
        print("Starting auto-clicker in 2 seconds...")
        print("Press Ctrl+C to stop the script")

        # Stop the listener after capturing the position
        listener.stop()
        return False


def auto_click():
    """Periodically clicks at the captured position"""
    while True:
        time.sleep(30)  # Wait 30 seconds
        if click_position:
            pyautogui.click(click_position[0], click_position[1])
            print(f"Clicked at {click_position} - {time.strftime('%H:%M:%S')}")


# Main execution
print("Click anywhere to set the auto-click location...")

# Start listening for mouse clicks
with mouse.Listener(on_click=on_click) as listener:
    listener.join()

# Wait a moment before starting
time.sleep(2)

# Start the auto-clicking loop
try:
    auto_click()
except KeyboardInterrupt:
    print("\nAuto-clicker stopped.")