"""Controlled OPTICAL capture: bonds, enables raw, tags every REALTIME_RAW_DATA packet
(both 1917 IMU + 1921 optical) with the phase from phase.txt. -> optical_capture.jsonl
"""
import asyncio, json, struct, sys, time
sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa
from packet_errors import CallbackFailure, is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
PHASE = "phase.txt"
out = open("optical_capture.jsonl", "w", buffering=1)
buf, need = b"", 0
counts = {}
callback_failure = CallbackFailure()


def cur():
    try: return open(PHASE).read().strip() or "none"
    except Exception: return "none"


def handle(frame, phase):
    global counts
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        inner = frame[4:length]; t = inner[0]; data = inner[3:]
        if t == PacketType.REALTIME_RAW_DATA.value:
            counts[(phase, len(data))] = counts.get((phase, len(data)), 0) + 1
            out.write(json.dumps({"phase": phase, "datalen": len(data),
                                  "data_hex": data.hex(), "wall": time.time()}) + "\n")
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def cb(_, d):
    global buf, need
    try:
        f = bytes(d); ph = cur()
        if need == 0:
            if f and f[0] == 0xAA and len(f) >= 3:
                total = struct.unpack("<H", f[1:3])[0] + 4
                if len(f) >= total:
                    handle(f[:total], ph)
                else:
                    buf, need = f, total
        else:
            buf += f
            if len(buf) >= need:
                frame, buf, need = buf[:need], b"", 0
                handle(frame, ph)
    except Exception as exc:
        callback_failure.report(exc)


async def send(c, name, p=b"\x01"):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, CommandNumber[name], p).framed_packet(), response=True)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    if not dev: print("not found"); return
    async with BleakClient(dev) as c:
        print("connected", flush=True)
        await c.start_notify(CMD_FROM, lambda _, d: None)
        await c.start_notify(DATA, cb)
        await send(c, "GET_BATTERY_LEVEL")  # bond
        await callback_failure.sleep(1)
        await send(c, "ENABLE_OPTICAL_DATA"); await send(c, "TOGGLE_IMU_MODE"); await send(c, "START_RAW_DATA")
        print("raw+optical streaming, watching phase.txt", flush=True)
        last = None; tick = 0
        while True:
            ph = cur()
            if ph != last:
                print(f"[{time.strftime('%H:%M:%S')}] phase -> {ph}  counts={counts}", flush=True)
                last = ph
            if ph == "quit": break
            tick += 1
            if tick % 20 == 0:  # re-arm raw stream every ~8s in case it paused
                try:
                    await send(c, "START_RAW_DATA")
                except Exception as exc:
                    print(f"re-arm raw stream failed: {exc}", flush=True)
            await callback_failure.sleep(0.4)
        print("DONE", counts, flush=True)


asyncio.run(main())
