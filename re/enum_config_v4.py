"""Correct enumeration: SEND_NEXT_* is a CURSOR (payload always [0x01], NOT an index — from app
builders lh0/k0,l0 = b.a() = [0x01]). START_* payload also [0x01] (lh0/v0,w0). SET payload (lh0/
t0,u0) = [REVISION_1=0x01][32-byte key utf8][32-byte value utf8]. So enumerate ALL keys by
repeating SEND_NEXT([01]); then try GET_*_VALUE with the SET-style [0x01][32-byte key] payload to
read values. READ-ONLY (no SET)."""
import asyncio
import struct
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

resp_q: asyncio.Queue = asyncio.Queue()


def asc(b):
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d)); resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


async def send(c, cmd, payload=b"\x00", resp=False):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await c.write_gatt_char(CMD_TO, pkt, response=resp)


async def call(c, cmd, payload, timeout=2.5):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(c, cmd, payload)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, d = await asyncio.wait_for(resp_q.get(), timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return d
    return None


def parse_name(d):
    if d and len(d) >= 6 and d[1] == 0x01:
        body = d[5:]
        nul = body.find(b"\x00")
        nm = body[:nul] if nul >= 0 else body
        if nm and all(32 <= c < 127 for c in nm):
            return nm.decode()
    return None


def keypayload(name):
    return bytes([0x01]) + name.encode().ljust(32, b"\x00")


async def enum_store(c, start, nxt, get, tag):
    print(f"\n===== {tag} =====", flush=True)
    m = await call(c, start, b"\x01")
    count = struct.unpack_from("<H", m, 3)[0] if m and len(m) >= 5 else None
    print(f"  START -> {m.hex() if m else None} count={count}", flush=True)
    names = []
    for i in range(max((count or 0) + 3, 15)):
        m = await call(c, nxt, b"\x01")
        nm = parse_name(m)
        if nm:
            names.append(nm)
            print(f"  [{len(names)}] {nm}   (raw {m.hex()})", flush=True)
        else:
            # stop after the cursor is exhausted
            if names:
                print(f"  (cursor exhausted at {i}, raw {m.hex() if m else None})", flush=True)
                break
    print(f"  >>> {tag} keys ({len(names)}): {names}", flush=True)
    # try reading each value with SET-style key payload
    for nm in names:
        v = await call(c, get, keypayload(nm))
        print(f"  GET '{nm}': {v.hex() if v else None}  ascii={asc(v) if v else ''}", flush=True)
    return names


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_from_cb)
        await c.start_notify(EVENTS, lambda _, d: None)
        await c.start_notify(DATA, lambda _, d: None)
        await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
        await asyncio.sleep(1.5)
        print("(bonded)", flush=True)
        await enum_store(c, CommandNumber.START_DEVICE_CONFIG_KEY_EXCHANGE,
                         CommandNumber.SEND_NEXT_DEVICE_CONFIG,
                         CommandNumber.GET_DEVICE_CONFIG_VALUE, "DEVICE-CONFIG")
        await enum_store(c, CommandNumber.START_FF_KEY_EXCHANGE,
                         CommandNumber.SEND_NEXT_FF, CommandNumber.GET_FF_VALUE, "FEATURE-FLAGS")
        print("\ndone", flush=True)


asyncio.run(main())
