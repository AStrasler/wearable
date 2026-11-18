"""READ-ONLY: characterize what's REALLY in the first historical chunk — looking for the strap's
COMPACT 14-day metric records (HR/HRV/sleep/resp/skin-temp/activity), which are NOT the type-43
raw stream (that's live-only). CRC-validates every reassembled frame (skips junk), separates live
type-43 (ts≈now) from everything else, and lists distinct (type,payload_len) record shapes with a
sample hex + a candidate timestamp age. NEVER acks => no trim."""
import asyncio
import struct
import sys
import time
from collections import Counter, defaultdict

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 15

databuf = bytearray()
resp_q: asyncio.Queue = asyncio.Queue()


def data_cb(_, d):
    databuf.extend(bytes(d))


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


async def send(c, cmd, payload=b"\x00", resp=False):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await c.write_gatt_char(CMD_TO, pkt, response=resp)


async def req(c, cmd, timeout=4.0):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(c, cmd)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, d = await asyncio.wait_for(resp_q.get(), timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return d
    return None


def iter_valid_frames(blob):
    """Yield CRC-validated WhoopPackets; resync past junk."""
    i, n = 0, len(blob)
    while i + 8 <= n:
        if blob[i] != 0xAA:
            i += 1; continue
        length = int.from_bytes(blob[i + 1:i + 3], "little")
        end = i + length + 4
        if end > n or length < 8:
            i += 1; continue
        frame = blob[i:end]
        try:
            pkt = WhoopPacket.from_data(frame)
            yield pkt
            i = end
        except Exception:
            i += 1


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_from_cb)
        await c.start_notify(EVENTS, lambda _, d: None)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
        await asyncio.sleep(1.5)
        clk = await req(c, CommandNumber.GET_CLOCK)
        now = struct.unpack_from("<I", clk, 2)[0] if clk and len(clk) >= 6 else None
        print(f"now device clock: {now}", flush=True)
        databuf.clear()
        await send(c, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
        print(f"offload {SECS}s (NO ack)...", flush=True)
        await asyncio.sleep(SECS)
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"\x00")

    shapes = Counter()           # (type_name, payload_len) -> count
    sample = {}                  # shape -> sample hex (first 28 payload bytes)
    age_by_shape = defaultdict(list)
    live43 = 0
    for pkt in iter_valid_frames(bytes(databuf)):
        try:
            tn = PacketType(pkt.type).name
        except Exception:
            tn = f"type{pkt.type}"
        data = bytes(pkt.data)
        # candidate ts at data[4:8]
        age = None
        if len(data) >= 8:
            ts = struct.unpack_from("<I", data, 4)[0]
            if now and 30_000_000 < ts < 33_000_000:
                age = now - ts
        if pkt.type == PacketType.REALTIME_RAW_DATA.value and age is not None and age <= 120:
            live43 += 1
            continue
        shape = (tn, len(data))
        shapes[shape] += 1
        if shape not in sample:
            sample[shape] = data[:28].hex()
        if age is not None:
            age_by_shape[shape].append(age)

    print(f"\nlive type-43 (skipped): {live43}", flush=True)
    print("=== distinct record shapes in the historical chunk (non-live) ===", flush=True)
    for shape, n in shapes.most_common():
        ages = age_by_shape.get(shape, [])
        agestr = ""
        if ages:
            agestr = f" age(s)~{min(ages)}..{max(ages)} ({min(ages)/3600:.1f}-{max(ages)/3600:.1f}h)"
        print(f"  {shape[0]:18} len={shape[1]:5} x{n:3}{agestr}", flush=True)
        print(f"      sample: {sample[shape]}", flush=True)


asyncio.run(main())
