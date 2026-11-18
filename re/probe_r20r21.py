"""Capture the EXACT response to TOGGLE_PERSISTENT_R20/R21 to learn if this firmware
supports them. Status 0a01=ok (applied), 0a03=unsupported (gen-gated). Also try the
ENABLE direction [01 01] and a couple payload variants to rule out a payload mistake.
Reversible toggles only; nothing destructive. Restores enable state at the end."""
import asyncio
import struct
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, crc8  # noqa: E402
from packet_errors import is_malformed_packet_error
import zlib  # noqa: E402
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

resp_q: asyncio.Queue = asyncio.Queue()
events = []


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def events_cb(_, d):
    raw = bytes(d)
    try:
        pkt = WhoopPacket.from_data(raw)
        if pkt.type == PacketType.EVENT:
            events.append((pkt.cmd, raw.hex()))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def frame(cmd_num, payload):
    pkt = struct.pack("<BBB", 35, 10, cmd_num) + payload
    blen = struct.pack("<H", len(pkt) + 4)
    return b"\xaa" + blen + struct.pack("<B", crc8(blen)) + pkt + struct.pack("<L", zlib.crc32(pkt) & 0xFFFFFFFF)


async def shot(client, cmd_num, payload, label, timeout=3.0):
    while not resp_q.empty():
        resp_q.get_nowait()
    events.clear()
    await client.write_gatt_char(CMD_TO, frame(cmd_num, payload), response=False)
    deadline = time.time() + timeout
    match = None
    others = []
    while time.time() < deadline:
        try:
            rc, d = await asyncio.wait_for(resp_q.get(), timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd_num and match is None:
            match = d
        else:
            others.append((rc, d.hex()))
    stat = match[1] if match and len(match) > 1 else None
    print(f"  {label:30} cmd={cmd_num} p={payload.hex()} -> resp={match.hex() if match else None} (status={stat})  events={events}  others={others}", flush=True)
    return match


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_from_cb)
        await c.start_notify(EVENTS, events_cb)
        await c.start_notify(DATA, lambda _, d: None)
        await c.write_gatt_char(CMD_TO, frame(CommandNumber.GET_BATTERY_LEVEL.value, b"\x00"), response=True)
        await asyncio.sleep(1.5)
        print("(bonded)\n", flush=True)

        # reference: a known-supported command's status
        await shot(c, CommandNumber.GET_CLOCK.value, b"\x00", "GET_CLOCK (ref ok)")
        await shot(c, CommandNumber.GET_HELLO.value, b"\x00", "GET_HELLO (ref unsupported)")
        print("", flush=True)
        # R20/R21 in both payload conventions
        await shot(c, 153, b"\x01\x00", "R20 disable [01 00]")
        await shot(c, 154, b"\x01\x00", "R21 disable [01 00]")
        await shot(c, 153, b"\x00", "R20 disable [00] (1-byte)")
        await shot(c, 154, b"\x00", "R21 disable [00] (1-byte)")
        # battery-pack-info (also new in this app enum) for completeness
        await shot(c, 151, b"\x00", "GET_BATTERY_PACK_INFO[151]")
        print("\ndone", flush=True)


asyncio.run(main())
