"""Hunt the optical/PPG + temperature stream: try each enable command, log what NEW
packet shapes appear on the data channel. Baseline IMU packets are data-len 1917/1921.
"""
import asyncio, struct, sys, time
from collections import Counter
sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
_buf = {"b": b"", "need": 0}
seen = Counter()       # (type, datalen, leadhex) -> count
window = Counter()


def on_frame(frame):
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        inner = frame[4:length]
        t = inner[0]; data = inner[3:]
        try: tn = PacketType(t).name
        except ValueError: tn = f"t{t}"
        key = (tn, len(data), data[:6].hex())
        seen[key] += 1; window[key] += 1
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def cb_data(_, d):
    f = bytes(d)
    if _buf["need"] == 0:
        if f and f[0] == 0xAA and len(f) >= 3:
            total = struct.unpack("<H", f[1:3])[0] + 4
            if len(f) >= total: on_frame(f[:total])
            else: _buf["b"], _buf["need"] = f, total
    else:
        _buf["b"] += f
        if len(_buf["b"]) >= _buf["need"]:
            on_frame(_buf["b"][:_buf["need"]]); _buf["b"], _buf["need"] = b"", 0


async def send(c, name, payload=b"\x00"):
    pkt = WhoopPacket(PacketType.COMMAND, 10, CommandNumber[name], data=payload).framed_packet()
    await c.write_gatt_char(CMD_TO, pkt, response=True)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    if not dev: print("not found"); return
    async with BleakClient(dev) as c:
        print("connected", c.is_connected, flush=True)
        await c.start_notify(CMD_FROM, lambda _, d: None)
        await c.start_notify(EVENTS, lambda _, d: None)
        await c.start_notify(DATA, cb_data)
        await send(c, "GET_BATTERY_LEVEL"); await asyncio.sleep(1)  # bond
        # baseline: start raw IMU so we know normal shapes
        steps = [
            ("BASELINE start_raw", [("ENABLE_OPTICAL_DATA", b"\x01"), ("TOGGLE_IMU_MODE", b"\x01"), ("START_RAW_DATA", b"\x01")]),
            ("TOGGLE_OPTICAL_MODE=1", [("TOGGLE_OPTICAL_MODE", b"\x01")]),
            ("TOGGLE_R7_DATA_COLLECTION=1", [("TOGGLE_R7_DATA_COLLECTION", b"\x01")]),
            ("SEND_R10_R11_REALTIME=1", [("SEND_R10_R11_REALTIME", b"\x01")]),
            ("research sigproc_pdaf", [("SET_RESEARCH_PACKET", b"sigproc_pdaf\x00")]),
            ("research enable_r19_packets", [("SET_RESEARCH_PACKET", b"enable_r19_packets\x00")]),
            ("TOGGLE_LABRADOR_DATA_GENERATION=1", [("TOGGLE_LABRADOR_DATA_GENERATION", b"\x01")]),
            ("TOGGLE_LABRADOR_RAW_SAVE=1", [("TOGGLE_LABRADOR_RAW_SAVE", b"\x01")]),
            ("REALTIME_IMU via TOGGLE_IMU_MODE_HISTORICAL", [("TOGGLE_IMU_MODE_HISTORICAL", b"\x01")]),
        ]
        for label, cmds in steps:
            window.clear()
            for name, pl in cmds:
                try: await send(c, name, pl)
                except Exception as e: print(f"  ! {name}: {e}", flush=True)
                await asyncio.sleep(0.3)
            await asyncio.sleep(4)
            shapes = ", ".join(f"{k[0]}/len{k[1]}/{k[2]}×{v}" for k, v in window.most_common(6))
            print(f"[{label}] -> {shapes or 'no packets'}", flush=True)
        print("\n=== ALL distinct shapes seen (type/datalen/lead6hex × count) ===", flush=True)
        for k, v in seen.most_common():
            print(f"  {k[0]:18} len={k[1]:5} lead={k[2]} ×{v}", flush=True)


asyncio.run(main())
