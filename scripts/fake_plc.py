r"""Fake S7-1200 for testing STECH iVIEW with NO physical device.   Run:  python scripts\fake_plc.py

It behaves like the PLC program in plc_testing\SimProcess.scl: a Modbus TCP server with the SAME 12 holding
registers (DB1 "PSIM_Data_DB"), so the app registers you create for the real PLC work here unchanged.

  Add device in the app:  protocol Modbus TCP, IP 127.0.0.1, port 5020   (real PLC later: IP 192.168.1.100, port 502)

While it runs, type a command + Enter:
  f   toggle the FAULT scenario (all six statuses = 1, temperature/pressure/level high, flow low)
  a   toggle AUTO mode (60 s normal, then 30 s fault, repeating)
  1-6 toggle one system fault:  1=FAS 2=FGS 3=PAGA 4=ANPR 5=Access 6=UVSS
  q   quit
Options:  --port 5020   --host 0.0.0.0   --fault (start in fault)   --auto (start in auto mode)
"""
import argparse
import asyncio
import math
import sys
import threading
import time

try:
    from pymodbus.server import StartAsyncTcpServer
    from pymodbus.simulator import DataType, SimData, SimDevice
except ImportError:
    raise SystemExit("This script needs a newer pymodbus.  Run:  python -m pip install -U \"pymodbus>=3.11,<4\"")

NAMES = ["FAS", "FGS", "PAGA", "ANPR", "Access", "UVSS"]
# word index = Modbus holding address (0-based) = DB1 byte offset / 2
STATUS0, TEMP, PRESS, FLOW, LEVEL, HEARTBEAT, MODE = 0, 6, 7, 8, 9, 10, 11
N_WORDS = 12


def to_word(value: int) -> int:
    return value & 0xFFFF  # int16 -> unsigned register word (two's complement, like a real PLC)


class State:
    fault = False
    auto = False
    sys_fault = [False] * 6


def build_registers(angle: float, cur: dict, st: State, cycle: int, beat: int, step: bool = True) -> list[int]:
    fault = st.fault or (st.auto and cycle >= 60)
    normal = {"t": 60 + 25 * math.sin(angle), "p": 6 + 2 * math.sin(angle * 2),      # same waves as SimProcess.scl
              "f": 50 + 10 * math.sin(angle / 2), "l": 60 + 20 * math.sin(angle / 2 + 1.0)}
    bad = {"t": 110.0, "p": 12.5, "f": 5.0, "l": 95.0}
    for k in cur if step else ():  # ease towards the target once per second, like the PLC does (no sudden jumps)
        cur[k] += ((bad[k] if fault else normal[k]) - cur[k]) * 0.4
    regs = [0] * N_WORDS
    for i in range(6):
        regs[STATUS0 + i] = 1 if (fault or st.sys_fault[i]) else 0
    regs[TEMP] = to_word(round(cur["t"] * 10))
    regs[PRESS] = to_word(round(cur["p"] * 100))
    regs[FLOW] = to_word(round(cur["f"] * 10))
    regs[LEVEL] = to_word(round(cur["l"] * 10))
    regs[HEARTBEAT] = beat & 0xFFFF
    regs[MODE] = 1 if fault else 0
    return regs


def console(st: State, stop: threading.Event):
    for line in sys.stdin:
        cmd = line.strip().lower()
        if cmd == "q":
            stop.set()
            return
        if cmd == "f":
            st.fault = not st.fault
        elif cmd == "a":
            st.auto = not st.auto
        elif cmd in list("123456"):
            st.sys_fault[int(cmd) - 1] = not st.sys_fault[int(cmd) - 1]
        else:
            continue
        print(f"fault={st.fault} auto={st.auto} systems={[n for n, v in zip(NAMES, st.sys_fault) if v]}", flush=True)


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=5020)
    ap.add_argument("--fault", action="store_true")
    ap.add_argument("--auto", action="store_true")
    args = ap.parse_args()
    st = State()
    st.fault, st.auto = args.fault, args.auto

    stop = threading.Event()
    threading.Thread(target=console, args=(st, stop), daemon=True).start()

    sim = {"angle": 0.0, "cur": {"t": 60.0, "p": 6.0, "f": 50.0, "l": 60.0}, "cycle": 0, "beat": 0, "last": time.monotonic()}

    async def on_access(function_code, start_address, address, count, current_registers, set_values):
        """Runs on every request: moves the simulation forward by the seconds that passed, then serves fresh values."""
        now = time.monotonic()
        for _ in range(min(int(now - sim["last"]), 30)):  # one simulation step per elapsed second
            sim["angle"] += 0.2
            sim["cycle"] = (sim["cycle"] + 1) % 90
            sim["beat"] = (sim["beat"] + 1) % 65536
            sim["last"] += 1
            build_registers(sim["angle"], sim["cur"], st, sim["cycle"], sim["beat"])
        if int(now - sim["last"]) >= 30:
            sim["last"] = now
        current_registers[0:N_WORDS] = build_registers(sim["angle"], sim["cur"], st, sim["cycle"], sim["beat"], step=False)
        return None

    # one device (unit id 1) with 12 holding registers, like DB1 in the PLC
    device = SimDevice(1, simdata=[SimData(0, count=N_WORDS, values=0, datatype=DataType.REGISTERS)], action=on_access)

    print(f"Fake PLC listening on {args.host}:{args.port}  (Modbus TCP, holding registers 0-{N_WORDS - 1})")
    print("Commands: f=fault  a=auto  1-6=one system  q=quit")
    server = asyncio.create_task(StartAsyncTcpServer(context=device, address=(args.host, args.port)))
    while not stop.is_set():
        await asyncio.sleep(0.5)
    server.cancel()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
