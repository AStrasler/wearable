"""Live device session capture, driven by phase.txt (assistant edits it on your verbal cue).
Bonds, enables IMU + optical raw, tags every type-43 frame with the current phase + wall time.
Writes JSONL to argv[1] (default fixtures/session_capture.jsonl — gitignored). phase.txt='quit' stops.

Used for: gyro-scale calibration (known 720deg rotations) and optical phases (finger/dark/light/air).
"""
import asyncio
import json
import struct
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import CallbackFailure, is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
PHASE_FILE = "phase.txt"
OUT_PATH = sys.argv[1] if len(sys.argv) > 1 else "fixtures/session_capture.jsonl"

out = open(OUT_PATH, "w", buffering=1)
buf, need = b"", 0
phase = "init"
counts = {}
callback_failure = CallbackFailure()


def cur_phase():
    try:
        return open(PHASE_FILE).read().strip() or "none"
    except Exception:
        return "none"


def handle_frame(frame):
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        pkt = frame[4:length]
        t, seq, cmd, data = pkt[0], pkt[1], pkt[2], pkt[3:]
    except Exception as exc:
        if is_malformed_packet_error(exc):
            return
        raise
    if t == PacketType.REALTIME_RAW_DATA.value:
        counts[phase] = counts.get(phase, 0) + 1
        out.write(json.dumps({
            "phase": phase, "wall": time.time(), "cmd": cmd,
            "datalen": len(data), "data_hex": data.hex(),
        }) + "\n")


def data_cb(_, d):
    global buf, need
    try:
        f = bytes(d)
        if need == 0:
            if f and f[0] == 0xAA and len(f) >= 3:
                total = struct.unpack("<H", f[1:3])[0] + 4
                if len(f) >= total:
                    handle_frame(f[:total])
                else:
                    buf, need = f, total
            else:
                return
        else:
            buf += f
            if len(buf) >= need:
                frame, buf, need = buf[:need], b"", 0
                handle_frame(frame)
    except Exception as exc:
        callback_failure.report(exc)


async def send(client, cmd, payload=b"\x00"):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await client.write_gatt_char(CMD_TO, pkt, response=True)


async def main():
    global phase
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as client:
        print(f"Connected: {client.is_connected} -> {OUT_PATH}", flush=True)
        await client.start_notify(CMD_FROM, lambda _, d: None)
        await client.start_notify(DATA, data_cb)
        await send(client, CommandNumber.GET_BATTERY_LEVEL)  # bond
        await callback_failure.sleep(1)
        await send(client, CommandNumber.ENABLE_OPTICAL_DATA, b"\x01")
        await send(client, CommandNumber.TOGGLE_IMU_MODE, b"\x01")
        await send(client, CommandNumber.START_RAW_DATA, b"\x01")
        print("RAW STREAM ENABLED — watching phase.txt", flush=True)
        last = None
        while True:
            phase = cur_phase()
            if phase != last:
                print(f"[{time.strftime('%H:%M:%S')}] phase -> {phase} (counts so far: {counts})", flush=True)
                last = phase
            if phase == "quit":
                break
            await callback_failure.sleep(0.3)
        await send(client, CommandNumber.STOP_RAW_DATA, b"\x00")
        print(f"DONE. packets/phase: {counts}", flush=True)


asyncio.run(main())
