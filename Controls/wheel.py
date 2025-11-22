from core.event_bus import EventBus
from core.settings_manager import SettingsManager
try:
    from misc.vjoy import vj, setJoy
except ImportError:
    print("ERROR LOADING VJOY. VJOY IS MANDATORY FOR AUTO-BRAKE WITH CONTROLLER.")



class WheelController():
    """Logic for emulating wheel controls"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        self.event_bus = event_bus
        self.settings = settings

        try:
            vj.open()
            vj.close()
            self.vjoy_available = True
        except:
            self.vjoy_available = False



    def press_wheel_brake(self, brake_pressure: float):
        if self.vjoy_available:
            vj.open()
            scale = 16.39
            brake_value = -int((brake_pressure * 1000)) + 23
            throttle_value = 1000 + 23  # No throttle
            steer_value = 0 + 23  # No steering
            setJoy(brake_value, throttle_value, steer_value, scale)
            vj.close()

