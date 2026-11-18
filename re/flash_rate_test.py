"""READ-ONLY decisive test: does the live type-43 flood get written to FLASH, or is flash
logging compact/sparse regardless of connection?

Compares GET_DATA_RANGE cursor growth over equal CONNECTED (flooding) vs DISCONNECTED windows.
  - connected_growth >> disconnected_growth  => the heavy live stream IS flashed (strap "stuck")
  - connected_growth ~= disconnected_growth  => flood is live-only; flash logging is compact/sparse
                                                regardless => strap is FINE (just stream-while-bonded)
No SET_READ_POINTER, no ack, no trim — purely reads GET_DATA_RANGE + counts frames.
"""
import asyncio
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
WIN = int(sys.argv[1]) if len(sys.argv) > 1 else 120

resp_q: asyncio.Queue = asyncio.Queue()
raw43 = 0
callback_failure = CallbackFailure()


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        callback_failure.report(exc)


def data_cb(_, d):
    global raw43
    raw = bytes(d)
    if len(raw) >= 5 and raw[0] == 0xAA and raw[4] == PacketType.REALTIME_RAW_DATA.value:
        raw43 += 1


async def send(c, cmd, payload=b"\x00", resp=False):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await c.write_gatt_char(CMD_TO, pkt, response=resp)


async def req(c, cmd, payload=b"\x00", timeout=4.0):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(c, cmd, payload)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, d = await callback_failure.get(resp_q, timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return d
    return None


def cursors(rng):
    if not rng or len(rng) < 7:
        return None
    body = rng[3:]  # skip 0a 01 01
    return [struct.unpack_from("<I", body, i)[0] for i in range(0, len(body) - 3, 4)]


async def connect():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        return None
    c = BleakClient(dev)
    await c.connect()
    await c.start_notify(CMD_FROM, cmd_from_cb)
    await c.start_notify(EVENTS, lambda _, d: None)
    await c.start_notify(DATA, data_cb)
    await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
    await callback_failure.sleep(1.5)
    return c


async def snap(c, tag):
    rng = cursors(await req(c, CommandNumber.GET_DATA_RANGE))
    print(f"  [{tag}] cursors={rng}", flush=True)
    return rng


def delta(a, b):
    if not a or not b:
        return None
    return [y - x for x, y in zip(a, b)]


async def main():
    global raw43
    c = await connect()
    if c is None:
        print("strap not found — free the strap (app quit + phone BT off)?"); return
    print(f"connected #1\n=== window={WIN}s ===", flush=True)
    a0 = await snap(c, "t0 connected")
    raw43 = 0
    print(f"  ... staying connected {WIN}s (counting type-43) ...", flush=True)
    await callback_failure.sleep(WIN)
    connected_43 = raw43
    a1 = await snap(c, "t1 connected")
    print(f"  type-43 frames while connected: {connected_43} (~{connected_43/WIN:.1f}/s)", flush=True)
    await c.disconnect()

    print(f"\n--- disconnected; waiting {WIN}s (NO client) ---", flush=True)
    await callback_failure.sleep(WIN)

    c2 = await connect()
    if c2 is None:
        print("reconnect failed"); return
    print("connected #2", flush=True)
    a2 = await snap(c2, "t2 after disconnect")
    await c2.disconnect()

    cg = delta(a1, a0)
    dg = delta(a2, a1)
    print("\n=== RESULT ===", flush=True)
    print(f"  CONNECTED   {WIN}s cursor growth: {cg}", flush=True)
    print(f"  DISCONNECTED {WIN}s cursor growth: {dg}", flush=True)
    print(f"  (type-43 streamed while connected: {connected_43})", flush=True)
    if cg and dg:
        # compare the largest-growing cursor (the write head)
        cmax = max(cg); dmax = max(dg)
        print(f"\n  connected write-growth={cmax}, disconnected write-growth={dmax}", flush=True)
        if cmax > dmax * 3 + 2:
            print("  >>> live flood IS being flashed (connected ≫ disconnected) — heavy raw to flash.", flush=True)
        else:
            print("  >>> flash growth ~same connected vs disconnected — the flood is LIVE-ONLY; flash", flush=True)
            print("      logging is compact/sparse regardless => strap is normal, not stuck.", flush=True)


asyncio.run(main())
