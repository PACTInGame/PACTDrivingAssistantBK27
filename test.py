import pyinsim
from MapBuilder import RoadMapper


class CarData:
    def __init__(self):
        self.x = 0
        self.y = 0
        self.z = 0
        self.heading = 0
        self.plid = 0

    def update(self, x, y, z, heading, plid):
        self.x = x
        self.y = y
        self.z = z
        self.heading = heading / 182.04
        self.plid = plid

    def print_data(self):
        print(f"Car PLID: {self.plid}, X: {self.x:.2f}, Y: {self.y:.2f}, Z: {self.z:.2f}, Heading: {self.heading:.2f}°")

insim = pyinsim.insim(b'127.0.0.1', 29999, Admin=b'', Flags=pyinsim.ISF_MCI | pyinsim.ISF_LOCAL, Interval=1000)

car_data = CarData()
mapper = RoadMapper()
mapper.start_recording()
def get_car_data(insim, mci):
    car = mci.Info[0]
    x = car.X / 65536
    y = car.Y / 65536
    z = car.Z / 65536
    heading = car.Heading
    if car.Speed > 10:
        car_data.update(x, y, z,heading, car.PLID)
        car_data.print_data()
        mapper.update_position(x, y, z, heading)


insim.bind(pyinsim.ISP_MCI, get_car_data)

pyinsim.run()