"""READ-ONLY device-config / feature-flag / research-packet enumeration.

TOP-PRIORITY RE: the strap streams heavy type-43 raw 24/7 from a PERSISTENT config
flag (survives reboot). It was NOT clearable by STOP_RAW_DATA / IMU/optical toggles /
reboot. The flag almost certainly lives in the device-config or feature-flag store,
which is gated behind a key-exchange handshake that has NEVER actually been run
(whoomp left it commented out; a naive single GET_DEVICE_CONFIG_VALUE/GET_FF_VALUE
returned a 64-byte all-zero buffer with status byte 0x00).

This script ONLY enumerates (read). It sends:
  - START_DEVICE_CONFIG_KEY_EXCHANGE(115) / SEND_NEXT_DEVICE_CONFIG(116) / GET_DEVICE_CONFIG_VALUE(121)
  - START_FF_KEY_EXCHANGE(117)            / SEND_NEXT_FF(118)            / GET_FF_VALUE(128)
  - GET_RESEARCH_PACKET(132)
It NEVER sends any SET_* (119/120/131) or any destructive command. Safe + reversible
(no state written). Every response is logged verbatim (hex + ascii) to config_dump.jsonl.
"""
import asyncio
import json
import struct
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import CallbackFailure
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

LOG = open("re/config_dump.jsonl", "a", buffering=1)
resp_q: asyncio.Queue = asyncio.Queue()   # (channel, resp_cmd, data_bytes, raw_bytes)
data43 = 0
callback_failure = CallbackFailure()


def ascii_render(b: bytes) -> str:
    return "".join(chr(c) if 32 <= c < 127 else "." for c in b)


def cmd_from_cb(_, d):
    raw = bytes(d)
    try:
        pkt = WhoopPacket.from_data(raw)
        resp_q.put_nowait(("cmd_from", pkt.cmd, bytes(pkt.data), raw))
    except Exception as exc:
        if callback_failure.report(exc):
            resp_q.put_nowait(("cmd_from", None, b"", raw))


def events_cb(_, d):
    raw = bytes(d)
    try:
        pkt = WhoopPacket.from_data(raw)
        resp_q.put_nowait(("events", pkt.cmd, bytes(pkt.data), raw))
    except Exception as exc:
        callback_failure.report(exc)


def data_cb(_, d):
    global data43
    raw = bytes(d)
    if len(raw) >= 5 and raw[0] == 0xAA and raw[4] == PacketType.REALTIME_RAW_DATA.value:
        data43 += 1


async def send(client, cmd, payload=b"\x00", resp=False):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await client.write_gatt_char(CMD_TO, pkt, response=resp)


async def probe(client, cmd, payload, label, wait=1.2):
    """Send one command, gather every response that arrives within `wait`s, print+log."""
    # drain stale
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(client, cmd, payload)
    await callback_failure.sleep(wait)
    got = []
    while not resp_q.empty():
        got.append(resp_q.get_nowait())
    print(f"\n>>> {label}  | sent cmd={cmd.value}({cmd.name}) payload={payload.hex()}", flush=True)
    if not got:
        print("    (no response)", flush=True)
    for chan, rcmd, data, raw in got:
        rcmd_name = ""
        try:
            rcmd_name = CommandNumber(rcmd).name if rcmd is not None else "?"
        except ValueError:
            rcmd_name = f"cmd{rcmd}"
        print(f"    [{chan}] resp_cmd={rcmd}({rcmd_name}) data={data.hex()}", flush=True)
        print(f"             ascii: {ascii_render(data)}", flush=True)
    LOG.write(json.dumps({
        "ts": time.time(), "label": label, "sent_cmd": cmd.value, "payload": payload.hex(),
        "responses": [{"chan": c, "resp_cmd": rc, "data": dt.hex(), "raw": rw.hex()} for c, rc, dt, rw in got],
    }) + "\n")
    return got


async def main():
    n_idx = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — is the OpenWhoop app force-quit + phone Bluetooth OFF?", flush=True)
        return
    async with BleakClient(dev) as client:
        print(f"connected: {client.is_connected}", flush=True)
        await client.start_notify(CMD_FROM, cmd_from_cb)
        await client.start_notify(EVENTS, events_cb)
        await client.start_notify(DATA, data_cb)
        # bond via one confirmed write
        await send(client, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
        await callback_failure.sleep(1.5)
        print("(bonded)", flush=True)

        # ---- EXPERIMENT A: device-config key-exchange ----
        print("\n========== A. DEVICE-CONFIG (115/116/121) ==========", flush=True)
        await probe(client, CommandNumber.START_DEVICE_CONFIG_KEY_EXCHANGE, b"\x00", "A.start devcfg p=00")
        await probe(client, CommandNumber.START_DEVICE_CONFIG_KEY_EXCHANGE, b"\x01", "A.start devcfg p=01")
        for i in range(n_idx):
            await probe(client, CommandNumber.SEND_NEXT_DEVICE_CONFIG, struct.pack("<B", i), f"A.next devcfg i={i}", wait=0.8)
        for k in range(n_idx):
            await probe(client, CommandNumber.GET_DEVICE_CONFIG_VALUE, struct.pack("<B", k), f"A.get devcfg key={k}", wait=0.8)

        # ---- EXPERIMENT B: feature-flag (FF) key-exchange ----
        print("\n========== B. FEATURE-FLAGS (117/118/128) ==========", flush=True)
        await probe(client, CommandNumber.START_FF_KEY_EXCHANGE, b"\x00", "B.start FF p=00")
        await probe(client, CommandNumber.START_FF_KEY_EXCHANGE, b"\x01", "B.start FF p=01")
        for i in range(n_idx):
            await probe(client, CommandNumber.SEND_NEXT_FF, struct.pack("<B", i), f"B.next FF i={i}", wait=0.8)
        for k in range(n_idx):
            await probe(client, CommandNumber.GET_FF_VALUE, struct.pack("<B", k), f"B.get FF key={k}", wait=0.8)

        # ---- EXPERIMENT C: research packet ----
        print("\n========== C. RESEARCH PACKET (132) ==========", flush=True)
        for i in range(8):
            await probe(client, CommandNumber.GET_RESEARCH_PACKET, struct.pack("<B", i), f"C.get research i={i}", wait=0.8)

        print(f"\n(type-43 frames seen during run: {data43})", flush=True)
        print("done — see re/config_dump.jsonl", flush=True)


asyncio.run(main())
