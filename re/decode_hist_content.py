"""READ-ONLY: offload the first (oldest-untrimmed) flash chunk WITHOUT acking (no trim) and
DECODE its records to answer: does flash contain accelerometer, at what cadence, and how recent?

For each reassembled type-43 frame:
  data = pkt[3:]  (after type/seq/cmd); IMU variant = len 1917, optical = 1921
  IMU header: ts(device clock)=u32@4, HR=u8@14; accelZ = 100x int16 LE @482 (APK-confirmed)
Reports consecutive-record ts deltas (=> flash sampling cadence), HR, accelZ mean/spread
(=> accel present + plausible), and ts vs current GET_CLOCK (=> recent or old data).
NEVER acks => never trims. Bounded capture.
"""
import asyncio
import struct
import sys
import time
import statistics

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
SECS = int(sys.argv[1]) if len(sys.argv) > 1 else 12

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
        await asyncio.sleep(1.5)
        clk = await req(c, CommandNumber.GET_CLOCK)
        now_clock = struct.unpack_from("<I", clk, 2)[0] if clk and len(clk) >= 6 else None
        print(f"current device clock: {now_clock}", flush=True)
        databuf.clear()
        await send(c, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
        print(f"offloading first chunk {SECS}s (NO ack/trim)...", flush=True)
        await asyncio.sleep(SECS)
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"\x00")

    imu = []   # (ts, hr, accelZ_mean, accelZ_std)
    opt = 0
    for f in split_frames(bytes(databuf)):
        length = int.from_bytes(f[1:3], "little")
        pkt = f[4:length]
        if not pkt or pkt[0] != PacketType.REALTIME_RAW_DATA.value:
            continue
        data = pkt[3:]
        if len(data) >= 1290 and len(data) < 1920:   # 1917 IMU
            ts = struct.unpack_from("<I", data, 4)[0]
            hr = data[14]
            az = [struct.unpack_from("<h", data, 482 + 2 * k)[0] for k in range(100)]
            imu.append((ts, hr, round(statistics.mean(az), 1), round(statistics.pstdev(az), 1)))
        elif len(data) >= 1920:  # 1921 optical
            opt += 1

    print(f"\ncaptured {len(databuf)} bytes; IMU(1917) records={len(imu)}, optical(1921)={opt}", flush=True)
    if imu:
        tss = [r[0] for r in imu]
        deltas = [b - a for a, b in zip(tss, tss[1:])]
        print(f"  IMU record device-clock ts: first={tss[0]} last={tss[-1]} span={tss[-1]-tss[0]}s", flush=True)
        print(f"  inter-record deltas (s): {deltas[:20]}", flush=True)
        if now_clock:
            print(f"  age vs now: oldest is {now_clock - tss[0]}s before now (~{(now_clock-tss[0])/3600:.1f}h)", flush=True)
        print(f"  HR values: {[r[1] for r in imu][:20]}", flush=True)
        print(f"  accelZ mean per record: {[r[2] for r in imu][:20]}", flush=True)
        print(f"  accelZ std  per record: {[r[3] for r in imu][:20]}", flush=True)
        print("  >>> accel present if accelZ means are ~thousands (1g≈3900 LSB) and vary with motion.", flush=True)


asyncio.run(main())
