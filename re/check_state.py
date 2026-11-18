"""Verify the strap's current data/optical state: is it flooding type-43 raw (the heavy
~3.8KB/s stream) or just doing normal compact biometric logging? Read LED drive, measure the
type-43 rate while bonded, then attempt a graceful quiet (STOP_RAW_DATA + optical/IMU off) and
re-measure. Reversible commands only.
"""
import asyncio, struct, sys, time
from collections import Counter
sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa
from packet_errors import CallbackFailure, is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

types = Counter()
sizes43 = Counter()
ledresp = []
buf, need = b"", 0
callback_failure = CallbackFailure()


def handle(frame):
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        pkt = frame[4:length]
        t = pkt[0]
        types[t] += 1
        if t == 43:
            sizes43[len(pkt) - 3] += 1
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def data_cb(_, d):
    global buf, need
    try:
        f = bytes(d)
        if need == 0:
            if f and f[0] == 0xAA and len(f) >= 3:
                total = struct.unpack("<H", f[1:3])[0] + 4
                if len(f) >= total:
                    handle(f[:total])
                else:
                    buf, need = f, total
        else:
            buf += f
            if len(buf) >= need:
                frame, buf, need = buf[:need], b"", 0
                handle(frame)
    except Exception as exc:
        callback_failure.report(exc)


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_LED_DRIVE.value:
            ledresp.append(pkt.data.hex())
    except Exception as exc:
        callback_failure.report(exc)


async def send(c, cmd, payload=b"\x00"):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet(), response=True)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        print(f"connected {c.is_connected}", flush=True)
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value)  # bond
        await callback_failure.sleep(1.2)
        await send(c, CommandNumber.GET_LED_DRIVE.value)
        await callback_failure.sleep(0.5)
        print(f"LED drive (0=optical off): {ledresp}", flush=True)

        # measure WITHOUT sending any enable — pure current state
        types.clear(); sizes43.clear()
        t0 = time.time(); await callback_failure.sleep(12); dt = time.time() - t0
        n43 = types.get(43, 0)
        print(f"[BEFORE] {dt:.0f}s: frame types={dict(types)}  type-43={n43} ({n43/dt:.1f}/s) sizes={dict(sizes43)}", flush=True)

        # attempt graceful quiet (reversible)
        print("sending STOP_RAW_DATA + TOGGLE_OPTICAL_MODE 0 + TOGGLE_IMU_MODE 0 ...", flush=True)
        await send(c, CommandNumber.STOP_RAW_DATA.value, b"\x01")
        await send(c, CommandNumber.TOGGLE_OPTICAL_MODE.value, b"\x00")
        await send(c, CommandNumber.TOGGLE_IMU_MODE.value, b"\x00")
        await send(c, CommandNumber.ENABLE_OPTICAL_DATA.value, b"\x00")
        await callback_failure.sleep(1)
        types.clear(); sizes43.clear()
        t0 = time.time(); await callback_failure.sleep(10); dt = time.time() - t0
        n43 = types.get(43, 0)
        print(f"[AFTER ] {dt:.0f}s: frame types={dict(types)}  type-43={n43} ({n43/dt:.1f}/s)", flush=True)
        await send(c, CommandNumber.GET_LED_DRIVE.value)
        await callback_failure.sleep(0.5)
        print(f"LED drive after: {ledresp[-1:]}", flush=True)
        print("DONE (disconnecting — type-43 stream is live-only, stops when no client is bonded)", flush=True)


asyncio.run(main())
