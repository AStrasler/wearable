"""Option-B decisive test: does REBOOT_STRAP clear the stuck raw-streaming mode?

Hypothesis: the strap is stuck logging/streaming heavy type-43 raw (a prior RE session's
START_RAW_DATA state persisted). whoomp uses REBOOT_STRAP(29) as the reset. If reboot returns
the strap to normal mode, the type-43 flood should STOP on the post-reboot reconnect (and it
should resume compact 24/7 HR logging). Safe + reversible — the band reboots in seconds.
"""
import asyncio
import struct
import sys

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

raw43 = 0
resp_q: asyncio.Queue = asyncio.Queue()


def data_cb(_, d):
    global raw43
    raw = bytes(d)
    if len(raw) >= 5 and raw[0] == 0xAA and raw[4] == PacketType.REALTIME_RAW_DATA.value:
        raw43 += 1


def resp_cb(_, d):
    try:
        resp_q.put_nowait(WhoopPacket.from_data(bytes(d)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


async def send(client, cmd, payload, resp=True):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await client.write_gatt_char(CMD_TO, pkt, response=resp)


async def connect():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        return None
    c = BleakClient(dev)
    await c.connect()
    await c.start_notify(CMD_FROM, resp_cb)
    await c.start_notify(EVENTS, lambda _, d: None)
    await c.start_notify(DATA, data_cb)
    await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00")
    await asyncio.sleep(1.5)
    return c


async def observe(label, secs=8):
    global raw43
    raw43 = 0
    await asyncio.sleep(secs)
    print(f"  [{label}] type-43 in {secs}s: {raw43}", flush=True)
    return raw43


async def get_data_range(c):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(c, CommandNumber.GET_DATA_RANGE, b"\x00")
    try:
        pkt = await asyncio.wait_for(resp_q.get(), timeout=5)
        print(f"  GET_DATA_RANGE data: {pkt.data.hex()}", flush=True)
    except asyncio.TimeoutError:
        print("  GET_DATA_RANGE: timeout", flush=True)


async def main():
    c = await connect()
    if c is None:
        print("strap not found (app closed / phone BT off / awake?)"); return
    print("Connected #1 (bonded)\n", flush=True)
    await observe("BASELINE (pre-reboot)")
    await get_data_range(c)

    print("\n>>> sending REBOOT_STRAP(29) — band will reboot + drop the link", flush=True)
    try:
        await send(c, CommandNumber.REBOOT_STRAP, b"\x00", resp=False)
    except Exception as e:
        print(f"  (write raised, expected on reboot: {e})", flush=True)
    try:
        await c.disconnect()
    except Exception as exc:
        print(f"disconnect after reboot failed: {exc}", flush=True)

    print(">>> waiting 30s for the strap to reboot + re-advertise...", flush=True)
    await asyncio.sleep(30)

    print(">>> reconnecting (fresh, post-reboot)...", flush=True)
    c2 = await connect()
    if c2 is None:
        print("post-reboot: strap not found yet — rerun re/diag_backfill.py in ~30s to check the flood"); return
    print("Connected #2 post-reboot (bonded)\n", flush=True)
    n = await observe("POST-REBOOT (no command sent)")
    await get_data_range(c2)
    try:
        await c2.disconnect()
    except Exception as exc:
        print(f"disconnect after observation failed: {exc}", flush=True)

    print("\n=== RESULT ===", flush=True)
    if n == 0:
        print("  >>> REBOOT STOPPED the type-43 flood — stuck raw mode cleared. Strap should now log compact 24/7 HR.", flush=True)
    else:
        print(f"  >>> still flooding ({n}) after reboot — the mode is persistent flash config, not a RAM flag.", flush=True)


asyncio.run(main())
