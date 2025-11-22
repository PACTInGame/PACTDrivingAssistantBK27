import pyinsim

# Init new InSim object
insim = pyinsim.insim(b'127.0.0.1', 29999, Admin=b'')

# Function to handle AI Info responses
def handle_ai_info(insim, aii):
    print(f"AI PLID {aii.PLID}: RPM={aii.RPM}, Gear={aii.Gear}")

# Bind the AI Info handler
insim.bind(pyinsim.ISP_AII, handle_ai_info)

# Request AI info once
inputs = [
    pyinsim.AIInputVal(Input=pyinsim.CS_SEND_AI_INFO)
]
insim.send(pyinsim.ISP_AIC, PLID=1, Inputs=inputs)

# Or request repeated AI info updates every
inputs = [
    pyinsim.AIInputVal(Input=pyinsim.CS_REPEAT_AI_INFO, Time=100)
]
insim.send(pyinsim.ISP_AIC, PLID=1, Inputs=inputs)

# Control the AI
inputs = [
    pyinsim.AIInputVal(Input=pyinsim.CS_IGNITION, Value=1),  # Steer right
    pyinsim.AIInputVal(Input=pyinsim.CS_CHUP, Value=1),  # Steer right

    pyinsim.AIInputVal(Input=pyinsim.CS_MSX, Value=40000),      # Steer right
    pyinsim.AIInputVal(Input=pyinsim.CS_THROTTLE, Value=50000)  # Half throttle
]
insim.send(pyinsim.ISP_AIC, PLID=1, Inputs=inputs)

# Start pyinsim
pyinsim.run()