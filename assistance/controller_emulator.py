from typing import Dict, Any

from Controls.wheel import WheelController
from assistance.base_system import AssistanceSystem
from core.event_bus import EventBus
from core.settings_manager import SettingsManager
from vehicles.own_vehicle import OwnVehicle
from vehicles.vehicle import Vehicle


class ControllerEmulator(AssistanceSystem):
    """Logic for emulating controls"""

    def __init__(self, event_bus: EventBus, settings: SettingsManager):
        super().__init__("controller_emulator", event_bus, settings)
        self.event_bus = event_bus
        self.event_bus.subscribe('needed_deceleration_update', self._update_decel_value)
        self._wanted_deceleration = 0
        self.controller_type = 2 # TODO fetch controller type from lfs # 0= keyboard, 1=mouse, 2=wheel
        self.emulating_input = False
        if self.controller_type == 2:
            self.wheel_driver = WheelController(event_bus, settings)


    def _update_decel_value(self, data):
        self._wanted_deceleration = data['deceleration']

    def process(self, own_vehicle: OwnVehicle, vehicles: Dict[int, Vehicle]) -> Dict[str, Any]:
        initial_brake_pressure = self._wanted_deceleration * 0.1  # -10 m/s² corresponds to 1.0 brake pressure
        #print("----------------")
        #print("Initial brake pressure:", initial_brake_pressure)
        #print("Current acceleration:", own_vehicle.data.acceleration)
        #print("Wanted deceleration:", self._wanted_deceleration)
        # Calculate delta between current deceleration and wanted deceleration
        wanted = -self._wanted_deceleration
        delta = -(wanted - own_vehicle.data.acceleration)
        #print("Delta:", delta)
        # if delta is positive, we need to increase brake pressure

        # Increase brake pressure smoothly
        brake_pressure = initial_brake_pressure + (delta * 0.05)
        if brake_pressure > 1.0:
            brake_pressure = 1.0
        elif brake_pressure < 0:
            brake_pressure = 0.0
        #print("Final brake pressure:", brake_pressure)
        if self.controller_type == 2:
            if brake_pressure > 0.5:
                if not self.emulating_input:
                    self.emulating_input = True
                    self.event_bus.emit('send_lfs_command', {'command': f"/axis {self.settings.get('vjoy_axis_1')} brake"})
                #print("Pressing wheel brake")
                self.wheel_driver.press_wheel_brake(brake_pressure)

            elif brake_pressure < 0.5 and self.emulating_input:
                self.emulating_input = False
                self.event_bus.emit('send_lfs_command', {'command': f"/axis {self.settings.get('user_axis_brake')} brake"})





