"""READ-ONLY diagnosis: is the strap logging type-47 V24 biometrics RIGHT NOW (worn)?
1) dump current feature-flag values (did the sigproc/r19 flags get left OFF?)
2) GET_DATA_RANGE x2 ~20s apart -> does newest_unix advance (= logging)? + tally type-43 (flood on/off)
3) no-ack high-freq-sync peek -> decode the newest type-47 records: unix, hr, skin_contact, |g|.
No ack/trim sent (ABORT). Nothing modified.
"""
import asyncio, struct, sys, time
from collections import Counter
sys.path.insert(0, "whoomp/scripts")
sys.path.insert(0, "re")
from packet import WhoopPacket, PacketType, CommandNumber, crc8  # noqa
from packet_errors import is_malformed_packet_error
import zlib  # noqa
from bleak import BleakClient, BleakScanner  # noqa
from device_config import DEVICE_UUID as ADDR  # noqa

CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

resp_q: asyncio.Queue = asyncio.Queue()
datarange = []
type_counts = Counter()
t47 = []           # (unix, hr, skin_contact, grav_mag)
buf, need = b"", 0
FLAGS = ["sigproc_10_sec_dp", "enable_r19_packets", "enable_r19_v4_packets",
         "enable_sigproc_walk_detector", "enable_write_r24_packets", "enable_write_r25_packets"]


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_DATA_RANGE.value:
            body = pkt.data[3:]
            datarange.append([struct.unpack_from("<I", body, i)[0] for i in range(0, len(body)-3, 4)])
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def handle(frame):
    if len(frame) <= 4:
        return
    type_counts[frame[4]] += 1
    if frame[4] == 47:
        pkt = frame[4:]            # [type,seq,cmd,data...]
        data = pkt[3:]
        if len(data) >= 49:
            unix = struct.unpack_from("<I", data, 4)[0]
            hr = data[14]
            skin = data[48]
            gx, gy, gz = (struct.unpack_from("<f", data, o)[0] for o in (33, 37, 41))
            t47.append((unix, hr, skin, (gx*gx+gy*gy+gz*gz)**0.5))


def data_cb(_, d):
    global buf, need
    f = bytes(d)
    if need == 0:
        if f and f[0] == 0xAA and len(f) >= 3:
            total = struct.unpack("<H", f[1:3])[0] + 4
            if len(f) >= total: handle(f[:total])
            else: buf, need = f, total
    else:
        buf += f
        if len(buf) >= need:
            handle(buf[:need]); buf, need = b"", 0


async def send(c, cmd, payload=b"\x00"):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet(), response=True)


async def call(c, cmd, payload=b"\x00", timeout=2.5):
    while not resp_q.empty(): resp_q.get_nowait()
    await send(c, cmd, payload)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, dd = await asyncio.wait_for(resp_q.get(), timeout=max(0.05, deadline-time.time()))
        except asyncio.TimeoutError:
            break
        if rc == cmd.value: return dd
    return None


def ascval(d):  # GET_FF_VALUE: current value byte = first printable after the 32-byte key
    body = d[3+32:] if d and len(d) > 35 else b""
    for b in body:
        if b in (0x31, 0x32):  # '1' or '2'
            return chr(b)
    return "?"


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — phone BT off + app quit?"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL); await asyncio.sleep(1.5)
        now = int(time.time())
        print(f"connected. now={now} ({time.strftime('%H:%M:%S', time.gmtime(now))} UTC)\n", flush=True)

        print("=== 1) feature-flag current values ('1'=ON '2'=OFF) ===", flush=True)
        for nm in FLAGS:
            v = await call(c, CommandNumber.GET_FF_VALUE, bytes([0x01]) + nm.encode().ljust(32, b"\x00"))
            print(f"  {nm} = {ascval(v) if v else 'no-resp'}", flush=True)

        print("\n=== 2) is it LOGGING now? GET_DATA_RANGE x2, 20s apart + type-43 tally ===", flush=True)
        await call(c, CommandNumber.GET_DATA_RANGE); r0 = datarange[-1] if datarange else None
        type_counts.clear()
        await asyncio.sleep(20)
        await call(c, CommandNumber.GET_DATA_RANGE); r1 = datarange[-1] if len(datarange) > 1 else None
        print(f"  type-43 in 20s: {type_counts.get(43,0)} (flood {'ON' if type_counts.get(43,0)>5 else 'off'})", flush=True)
        if r0 and r1:
            ch = [(i, r0[i], r1[i], r1[i]-r0[i]) for i in range(min(len(r0),len(r1))) if r0[i] != r1[i]]
            print(f"  GET_DATA_RANGE changed words (idx,before,after,Δ over 20s): {ch}", flush=True)
            uni = [w for w in r1 if 1_770_000_000 <= w <= 1_900_000_000]
            print(f"  range unix markers: {[(w, time.strftime('%H:%M:%S', time.gmtime(w))) for w in uni]}", flush=True)

        print("\n=== 3) no-ack peek: newest type-47 records being served ===", flush=True)
        t47.clear()
        await send(c, CommandNumber.ENTER_HIGH_FREQ_SYNC, b"")
        await send(c, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
        await asyncio.sleep(15)
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"\x00")
        await send(c, CommandNumber.EXIT_HIGH_FREQ_SYNC, b"\x00")
        if t47:
            newest = max(r[0] for r in t47)
            oldest = min(r[0] for r in t47)
            hrs = [r[1] for r in t47]; skins = Counter(r[2] for r in t47)
            print(f"  type-47 records: {len(t47)}  unix {oldest}..{newest} "
                  f"({time.strftime('%H:%M', time.gmtime(oldest))}..{time.strftime('%H:%M', time.gmtime(newest))} UTC, "
                  f"newest {newest-now:+d}s vs now)", flush=True)
            print(f"  HR values: min={min(hrs)} max={max(hrs)} (zeros={sum(1 for h in hrs if h==0)})", flush=True)
            print(f"  skin_contact counts: {dict(skins)} (0=off-wrist, nonzero=contact)", flush=True)
        else:
            print("  NO type-47 records served.", flush=True)
        await c.disconnect()
    print("\nDONE (read-only).", flush=True)


asyncio.run(main())
