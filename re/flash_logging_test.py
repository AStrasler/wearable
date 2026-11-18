"""DECISIVE read-only test: what does the strap log to FLASH while no client is connected,
and at what rate? Answers "is true 24/7 compact HR feasible on this firmware?".

Steps (no destructive writes; no trim/ack):
  1. connect, GET_CLOCK + GET_DATA_RANGE  (note device clock + history cursors)
  2. disconnect, wait GAP seconds with NO client (strap logs to flash on its own)
  3. reconnect, GET_CLOCK + GET_DATA_RANGE  (compute how much the history grew over the gap)
  4. SEND_HISTORICAL_DATA, capture the FIRST chunk's frames WITHOUT acking/trimming, then
     ABORT_HISTORICAL_TRANSMITS — inspect whether freshly-logged flash records are type-43 raw
     (~1920 B) or a compact record. (capture only; never acks => never trims/erases)
"""
import asyncio
import struct
import sys
import time
from collections import Counter

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

GAP = int(sys.argv[1]) if len(sys.argv) > 1 else 150

resp_q: asyncio.Queue = asyncio.Queue()
databuf = bytearray()
capture = False


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def data_cb(_, d):
    if capture:
        databuf.extend(bytes(d))


async def send(client, cmd, payload=b"\x00", resp=False):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await client.write_gatt_char(CMD_TO, pkt, response=resp)


async def req(client, cmd, payload=b"\x00", timeout=4.0):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(client, cmd, payload)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, d = await asyncio.wait_for(resp_q.get(), timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return d
    return None


def u32s(body):
    return [struct.unpack_from("<I", body, i)[0] for i in range(0, len(body) - 3, 4)]


def split_frames(blob):
    i, n = 0, len(blob)
    while i + 4 <= n:
        if blob[i] != 0xAA:
            i += 1
            continue
        length = int.from_bytes(blob[i + 1:i + 3], "little")
        end = i + length + 4
        if end > n:
            break
        yield blob[i:end]
        i = end


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
    await asyncio.sleep(1.5)
    return c


async def snapshot(c, tag):
    clk = await req(c, CommandNumber.GET_CLOCK)
    rng = await req(c, CommandNumber.GET_DATA_RANGE)
    dev_clock = struct.unpack_from("<I", clk, 2)[0] if clk and len(clk) >= 6 else None
    body = rng[2:] if rng and len(rng) > 2 else b""
    print(f"  [{tag}] device_clock={dev_clock}", flush=True)
    print(f"  [{tag}] DATA_RANGE raw={rng.hex() if rng else None}", flush=True)
    print(f"  [{tag}] DATA_RANGE u32s={u32s(body)}", flush=True)
    return dev_clock, (rng.hex() if rng else None)


async def main():
    global capture
    c = await connect()
    if c is None:
        print("strap not found — app force-quit + phone BT off + on wrist?")
        return
    print("connected #1\n=== BEFORE gap ===", flush=True)
    clk0, rng0 = await snapshot(c, "before")
    await c.disconnect()

    print(f"\n--- disconnected; waiting {GAP}s with NO client (strap logs to flash) ---", flush=True)
    await asyncio.sleep(GAP)

    c2 = await connect()
    if c2 is None:
        print("reconnect failed — rerun"); return
    print("\nconnected #2\n=== AFTER gap ===", flush=True)
    clk1, rng1 = await snapshot(c2, "after")
    if clk0 and clk1:
        print(f"\n  >>> clock advanced {clk1 - clk0}s over the gap (expected ~{GAP})", flush=True)

    # capture the first history chunk WITHOUT acking (no trim)
    print("\n=== peek first history chunk (capture only, NO ack/trim) ===", flush=True)
    databuf.clear()
    capture = True
    await send(c2, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
    await asyncio.sleep(8)
    capture = False
    await send(c2, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"\x00")
    frames = list(split_frames(bytes(databuf)))
    types = Counter()
    sizes = Counter()
    for f in frames:
        length = int.from_bytes(f[1:3], "little")
        pkt = f[4:length]
        if not pkt:
            continue
        try:
            tn = PacketType(pkt[0]).name
        except ValueError:
            tn = f"type{pkt[0]}"
        types[tn] += 1
        if pkt[0] == PacketType.REALTIME_RAW_DATA.value:
            sizes[len(pkt[3:])] += 1
    print(f"  captured {len(databuf)} bytes, {len(frames)} frames", flush=True)
    print(f"  frame types: {dict(types)}", flush=True)
    print(f"  type-43 payload sizes: {dict(sizes)}", flush=True)
    await c2.disconnect()
    print("\ndone (no trim performed; history intact)", flush=True)


asyncio.run(main())
