"""DECISIVE experiment: stop the persistent type-43 raw flood via TOGGLE_PERSISTENT_R20/R21.

Commands observed for interoperability that the whoomp reference never had:
  TOGGLE_PERSISTENT_R20 = 153,  TOGGLE_PERSISTENT_R21 = 154
Builder (observed): payload = 2 bytes [REVISION_1=0x01, on?1:0].
So DISABLE = [0x01, 0x00]. Hypothesis: R20/R21 = the two persistent raw records that show up
as the type-43 pair (1917B IMU+HR, 1921B optical). These are REVERSIBLE toggles (re-enable with
[0x01,0x01]); not destructive (no trim/reboot/firmware).

Method (one variable at a time):
  1. baseline: count type-43 frames by size (1917 vs 1921) over a window
  2. disable R20 -> observe which size stops
  3. disable R21 -> observe the other
  4. disconnect + reconnect fresh -> confirm the flood stays OFF across a reconnect (persistence)
Logs before/after + every COMMAND_RESPONSE.
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

TOGGLE_R20 = 153
TOGGLE_R21 = 154

databuf = bytearray()
resp_q: asyncio.Queue = asyncio.Queue()
event_log = []


def data_cb(_, d):
    databuf.extend(bytes(d))


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
            event_log.append((time.strftime("%H:%M:%S"), pkt.cmd, raw.hex()))
            print(f"    EVENT #{pkt.cmd} {raw.hex()}", flush=True)
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


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


async def send_cmd(client, cmd_num, payload, resp=True):
    """Send a raw command by integer cmd number with payload bytes."""
    pkt = struct.pack("<BBB", PacketType.COMMAND.value, 10, cmd_num) + payload
    import zlib
    from packet import crc8
    blen = struct.pack("<H", len(pkt) + 4)
    framed = b"\xaa" + blen + struct.pack("<B", crc8(blen)) + pkt + struct.pack("<L", zlib.crc32(pkt) & 0xFFFFFFFF)
    await client.write_gatt_char(CMD_TO, framed, response=resp)


async def window(secs, label):
    """Count type-43 frames by total length over `secs`."""
    databuf.clear()
    await asyncio.sleep(secs)
    sizes = Counter()
    total43 = 0
    for f in split_frames(bytes(databuf)):
        length = int.from_bytes(f[1:3], "little")
        pkt = f[4:length]
        if pkt and pkt[0] == PacketType.REALTIME_RAW_DATA.value:
            total43 += 1
            sizes[len(pkt[3:])] += 1   # payload (data) length: 1917 / 1921
    print(f"  [{label}] {secs}s: type-43={total43}  by-payload-len={dict(sizes)}", flush=True)
    return total43, dict(sizes)


async def drain_resp():
    out = []
    while not resp_q.empty():
        out.append(resp_q.get_nowait())
    return out


async def connect():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        return None
    c = BleakClient(dev)
    await c.connect()
    await c.start_notify(CMD_FROM, cmd_from_cb)
    await c.start_notify(EVENTS, events_cb)
    await c.start_notify(DATA, data_cb)
    # bond (confirmed write)
    await send_cmd(c, CommandNumber.GET_BATTERY_LEVEL.value, b"\x00", resp=True)
    await asyncio.sleep(1.5)
    return c


async def main():
    c = await connect()
    if c is None:
        print("strap not found — app force-quit + phone BT off + strap awake?")
        return
    print("connected + bonded #1\n", flush=True)

    await window(6, "BASELINE")

    print("\n>>> TOGGLE_PERSISTENT_R20 = OFF  [01 00]", flush=True)
    await drain_resp()
    await send_cmd(c, TOGGLE_R20, b"\x01\x00", resp=True)
    await asyncio.sleep(1.0)
    print(f"    resp: {await drain_resp()}", flush=True)
    await window(6, "after R20=off")

    print("\n>>> TOGGLE_PERSISTENT_R21 = OFF  [01 00]", flush=True)
    await drain_resp()
    await send_cmd(c, TOGGLE_R21, b"\x01\x00", resp=True)
    await asyncio.sleep(1.0)
    print(f"    resp: {await drain_resp()}", flush=True)
    after_both = await window(6, "after R20+R21=off")

    await c.disconnect()
    print("\n--- disconnected; reconnecting fresh to test PERSISTENCE ---\n", flush=True)
    await asyncio.sleep(3)
    c2 = await connect()
    if c2 is None:
        print("reconnect: strap not found — rerun re/diag_backfill.py to check the flood")
        return
    print("connected + bonded #2 (fresh, no toggles sent)\n", flush=True)
    fresh, fresh_sizes = await window(8, "FRESH RECONNECT")
    await c2.disconnect()

    print("\n=== RESULT ===", flush=True)
    print(f"  after both toggles: {after_both[0]} frames; fresh reconnect: {fresh} frames {fresh_sizes}", flush=True)
    if fresh == 0:
        print("  *** SUCCESS: persistent raw collection DISABLED and stays off across reconnect. ***", flush=True)
        print("  *** Re-enable later with TOGGLE_PERSISTENT_R20/R21 = [01 01]. ***", flush=True)
    else:
        print("  >>> still streaming on fresh connect — R20/R21 not the (only) persistent source.", flush=True)


asyncio.run(main())
