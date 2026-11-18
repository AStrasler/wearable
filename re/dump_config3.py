"""READ-ONLY enumeration v3 — robust request/response correlation.

Run-1/2 flapped because the type-43 raw flood saturates the link and DELAYS command
responses across probe windows. Fixes here:
  - send STOP_RAW_DATA(82) first to quiet the live stream (transient, reversible),
  - correlate each response to its request by matching resp_cmd (no time-window bucketing),
  - SEND_NEXT payload IS an index (run-1 proved index 1 is the only populated slot); sweep 0..5,
  - read each value BOTH by NAME (string payload) and by index, to learn the value-read format.
Still 100% read-only: no SET_* (119/120/131), nothing destructive.
"""
import asyncio
import json
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

LOG = open("re/config_dump.jsonl", "a", buffering=1)
resp_q: asyncio.Queue = asyncio.Queue()
data43 = 0


def asc(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def cmd_from_cb(_, d):
    raw = bytes(d)
    try:
        pkt = WhoopPacket.from_data(raw)
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def data_cb(_, d):
    global data43
    raw = bytes(d)
    if len(raw) >= 5 and raw[0] == 0xAA and raw[4] == PacketType.REALTIME_RAW_DATA.value:
        data43 += 1


async def send(client, cmd, payload=b"\x00", resp=False):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await client.write_gatt_char(CMD_TO, pkt, response=resp)


async def call(client, cmd, payload, label, timeout=3.0):
    """Send, then wait until a response with resp_cmd==cmd.value arrives (or timeout)."""
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(client, cmd, payload)
    deadline = time.time() + timeout
    match = None
    others = []
    while time.time() < deadline:
        try:
            rc, d = await asyncio.wait_for(resp_q.get(), timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value and match is None:
            match = d
            break
        else:
            others.append((rc, d))
    LOG.write(json.dumps({"ts": time.time(), "label": label, "cmd": cmd.value,
                          "payload": payload.hex(),
                          "match": match.hex() if match else None,
                          "others": [(c, x.hex()) for c, x in others]}) + "\n")
    return match, others


def parse_name(data: bytes):
    if data and len(data) >= 6 and data[1] == 0x01:
        body = data[5:]
        nul = body.find(b"\x00")
        name = body[:nul] if nul >= 0 else body
        if name and all(32 <= c < 127 for c in name):
            return name.decode()
    return None


async def enumerate_store(client, start_cmd, next_cmd, get_cmd, tag):
    print(f"\n===== {tag} =====", flush=True)
    m, o = await call(client, start_cmd, b"\x01", f"{tag}.start")
    count = struct.unpack_from("<H", m, 3)[0] if (m and len(m) >= 5) else None
    print(f"  START -> {m.hex() if m else None}  count={count}", flush=True)

    names = {}
    for i in range(8):
        m, o = await call(client, next_cmd, struct.pack("<B", i), f"{tag}.next[{i}]")
        nm = parse_name(m) if m else None
        if nm:
            names[i] = nm
            print(f"  idx {i}: {nm}   (raw {m.hex()})", flush=True)
        else:
            print(f"  idx {i}: -   (raw {m.hex() if m else None})", flush=True)
    print(f"  >>> {tag} populated: {names}", flush=True)

    for i, nm in names.items():
        # by index
        m, o = await call(client, get_cmd, struct.pack("<B", i), f"{tag}.getidx[{i}]")
        print(f"  GET idx {i}  -> status={m[1] if m and len(m)>1 else '?'}  {m.hex() if m else None}", flush=True)
        # by name string
        m2, o2 = await call(client, get_cmd, nm.encode() + b"\x00", f"{tag}.getname[{nm}]")
        print(f"  GET '{nm}' -> status={m2[1] if m2 and len(m2)>1 else '?'}  {m2.hex() if m2 else None}", flush=True)
        if m2:
            print(f"             ascii={asc(m2)}", flush=True)
    return names


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — app force-quit + phone BT off?", flush=True)
        return
    async with BleakClient(dev) as client:
        print(f"connected: {client.is_connected}", flush=True)
        await client.start_notify(CMD_FROM, cmd_from_cb)
        await client.start_notify(EVENTS, lambda _, d: None)
        await client.start_notify(DATA, data_cb)
        await send(client, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
        await asyncio.sleep(1.5)
        print("(bonded)", flush=True)

        # quiet the link: transient stop of the live raw stream
        global data43
        data43 = 0
        await asyncio.sleep(2)
        print(f"  type-43 in 2s BEFORE stop: {data43}", flush=True)
        await send(client, CommandNumber.STOP_RAW_DATA, b"\x01", resp=True)
        await asyncio.sleep(1.0)
        data43 = 0
        await asyncio.sleep(2)
        print(f"  type-43 in 2s AFTER STOP_RAW_DATA: {data43}", flush=True)

        await enumerate_store(client, CommandNumber.START_FF_KEY_EXCHANGE,
                              CommandNumber.SEND_NEXT_FF, CommandNumber.GET_FF_VALUE, "FF")
        await enumerate_store(client, CommandNumber.START_DEVICE_CONFIG_KEY_EXCHANGE,
                              CommandNumber.SEND_NEXT_DEVICE_CONFIG,
                              CommandNumber.GET_DEVICE_CONFIG_VALUE, "DEVCFG")

        print("\n===== RESEARCH PACKET (132) idx 0..6 =====", flush=True)
        for i in range(7):
            m, o = await call(client, CommandNumber.GET_RESEARCH_PACKET, struct.pack("<B", i),
                              f"research[{i}]")
            print(f"  i={i}: status={m[1] if m and len(m)>1 else '?'}  {m.hex() if m else None}   ascii={asc(m) if m else ''}", flush=True)

        print("\ndone", flush=True)


asyncio.run(main())
