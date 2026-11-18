"""READ-ONLY: during a historical offload the strap ALSO floods the live type-43 stream, so a
raw capture mixes both. Separate them BY TIMESTAMP: live records carry ts≈now; historical(flash)
records carry the older ts they were logged at. This answers definitively:
  - Are there type-43 records with OLD timestamps?  => flash stores RAW IMU/optical (accel avail)
  - Only ts≈now type-43 + the historical chunk is just METADATA/EVENT/CONSOLE? => flash is COMPACT
    (no raw accel in history)
Classifies every type-43 by age; decodes accelZ for historical IMU; tallies all frame types +
metadata markers. NEVER acks => no trim.
"""
import asyncio
import struct
import sys
import time
import statistics
from collections import Counter

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, MetadataType  # noqa: E402
from packet_errors import CallbackFailure
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 15

databuf = bytearray()
resp_q: asyncio.Queue = asyncio.Queue()
callback_failure = CallbackFailure()


def data_cb(_, d):
    databuf.extend(bytes(d))


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        callback_failure.report(exc)


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
            rc, d = await callback_failure.get(resp_q, timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return d
    return None


def split_frames(blob):
    i, n = 0, len(blob)
    while i + 4 <= n:
        if blob[i] != 0xAA:
            i += 1; continue
        length = int.from_bytes(blob[i + 1:i + 3], "little")
        end = i + length + 4
        if end > n:
            break
        yield blob[i:end]; i = end


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_from_cb)
        await c.start_notify(EVENTS, lambda _, d: None)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
        await callback_failure.sleep(1.5)
        clk = await req(c, CommandNumber.GET_CLOCK)
        now = struct.unpack_from("<I", clk, 2)[0] if clk and len(clk) >= 6 else None
        print(f"now device clock: {now}", flush=True)
        databuf.clear()
        await send(c, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
        print(f"offloading {SECS}s (NO ack)...", flush=True)
        await callback_failure.sleep(SECS)
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"\x00")

    types = Counter()
    live43 = 0
    hist43 = []     # (age_s, kind, hr, accelZ_mean, accelZ_std)
    metas = []
    for f in split_frames(bytes(databuf)):
        length = int.from_bytes(f[1:3], "little")
        pkt = f[4:length]
        if not pkt:
            continue
        t = pkt[0]
        try:
            types[PacketType(t).name] += 1
        except ValueError:
            types[f"type{t}"] += 1
        data = pkt[3:]
        if t == PacketType.METADATA.value and data:
            metas.append((data[0], data[1:15].hex()))
        if t == PacketType.REALTIME_RAW_DATA.value and len(data) >= 16:
            ts = struct.unpack_from("<I", data, 4)[0]
            age = (now - ts) if now else None
            is_imu = len(data) < 1920
            if age is not None and age <= 120:
                live43 += 1
            else:
                hr = data[14]
                if is_imu and len(data) >= 682:
                    az = [struct.unpack_from("<h", data, 482 + 2 * k)[0] for k in range(100)]
                    hist43.append((age, "imu", hr, round(statistics.mean(az), 1), round(statistics.pstdev(az), 1)))
                else:
                    hist43.append((age, "opt", hr, None, None))

    print(f"\ncaptured {len(databuf)} bytes; frame types: {dict(types)}", flush=True)
    print(f"metadata markers: {[(MetadataType(m).name if m in (1,2,3) else m, h) for m,h in metas]}", flush=True)
    print(f"LIVE type-43 (ts≈now, age<=120s): {live43}", flush=True)
    print(f"HISTORICAL type-43 (older ts): {len(hist43)}", flush=True)
    for age, kind, hr, m, s in hist43[:30]:
        print(f"   age={age}s ({age/3600:.1f}h) {kind} HR={hr} accelZ_mean={m} std={s}", flush=True)
    if not hist43:
        print("   >>> NO old-timestamp type-43 => flash history holds NO raw IMU/optical;", flush=True)
        print("       historical = compact (metadata/event/console + any compact HR record).", flush=True)


asyncio.run(main())
