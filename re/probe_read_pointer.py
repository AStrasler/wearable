"""Probe SET_READ_POINTER(33) by SERVING after setting it (the test I missed before): set the pointer,
then SEND_HISTORICAL_DATA[0x00] and decode WHAT unix timestamps come out. If they fall in the GAP
window (00:23-02:48 UTC) instead of "now", the pointer moved the serve-start below U → recovery works.

SAFE: read-only. NEVER sends HISTORICAL_DATA_RESULT (no trim) or FORCE_TRIM. Aborts each stream.
Requires phone BT off + app quit.
"""
import asyncio, struct, sys, time
sys.path.insert(0, "whoomp/scripts")
sys.path.insert(0, "re")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa
from device_config import DEVICE_UUID as ADDR  # noqa

CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
SET_READ_POINTER = 33

GAP_LO, GAP_HI = 1779754981, 1779763694   # 00:23..02:48 UTC (the window we want back)

resp_q: asyncio.Queue = asyncio.Queue()
datarange = []
t47 = []
buf, need = b"", 0


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_DATA_RANGE.value:
            body = pkt.data[3:]
            datarange.append([struct.unpack_from("<I", body, i)[0] for i in range(0, len(body) - 3, 4)])
        resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def handle(frame):
    if len(frame) > 4 and frame[4] == 47:
        data = frame[4:][3:]
        if len(data) >= 49:
            t47.append(struct.unpack_from("<I", data, 4)[0])


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


async def send(c, cmd, payload=b"\x00", resp=True):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet(), response=resp)


async def read_range(c):
    datarange.clear()
    await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.2)
    r = datarange[-1] if datarange else None
    return (r[2], r[3], r[5]) if r and len(r) > 5 else (None, None, None)


async def serve_and_decode(c, label, secs=7):
    """SEND_HISTORICAL, capture type-47 unix for `secs`, ABORT (no ack). Report the served window."""
    t47.clear()
    await send(c, CommandNumber.SEND_HISTORICAL_DATA.value, b"\x00", resp=True)
    await asyncio.sleep(secs)
    await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS.value, b"\x00"); await asyncio.sleep(0.5)
    if not t47:
        print(f"   [{label}] served 0 type-47"); return None
    lo, hi = min(t47), max(t47)
    ingap = sum(1 for u in t47 if GAP_LO <= u <= GAP_HI)
    fmt = lambda u: time.strftime('%H:%M', time.gmtime(u))
    tag = "  <<< IN GAP WINDOW!" if ingap > 0 else ""
    print(f"   [{label}] served {len(t47)} type-47, unix {fmt(lo)}..{fmt(hi)} UTC (ingap={ingap}){tag}")
    return (lo, hi, ingap)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — phone BT off + app quit?"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value); await asyncio.sleep(1.5)
        await send(c, CommandNumber.SEND_R10_R11_REALTIME.value, b"\x00"); await asyncio.sleep(1.0)  # quiet the radio

        W, U, T = await read_range(c)
        print(f"baseline: W={W} U={U} T={T}; gap target index ~72950 (00:23 UTC)\n")

        # CONTROL: plain serve from U (expect ~recent timestamps, NOT in gap).
        print("CONTROL (no SET_READ_POINTER):")
        await serve_and_decode(c, "control")

        # Candidate SET_READ_POINTER payloads. After each: serve and see what unix comes out.
        TARGET = 72950
        cands = [
            ("[01]+u32LE",      b"\x01" + struct.pack("<I", TARGET)),
            ("[01]+u32LE+u32LE",b"\x01" + struct.pack("<II", TARGET, 0)),
            ("u32LE",           struct.pack("<I", TARGET)),
            ("[01]+u32BE",      b"\x01" + struct.pack(">I", TARGET)),
            ("[02]+u32LE",      b"\x02" + struct.pack("<I", TARGET)),
            ("[01]+i32+i32",    b"\x01" + struct.pack("<ii", TARGET, TARGET)),
        ]
        for label, pl in cands:
            while not resp_q.empty(): resp_q.get_nowait()
            print(f"\nSET_READ_POINTER(33) {label} = {pl.hex()}")
            await send(c, SET_READ_POINTER, pl); await asyncio.sleep(1.0)
            r33 = None
            t_end = time.time() + 1.0
            while time.time() < t_end:
                try: rc, dd = await asyncio.wait_for(resp_q.get(), timeout=max(0.05, t_end - time.time()))
                except asyncio.TimeoutError: break
                if rc == SET_READ_POINTER: r33 = dd
            print(f"   resp={r33.hex() if r33 else '(none)'}")
            res = await serve_and_decode(c, label)
            if res and res[2] > 0:
                print(f"\n🎯 RECOVERY PATH FOUND: SET_READ_POINTER {label} → serves gap data. Stopping.")
                break
        await c.disconnect()
    print("\nDONE (read-only; no trim).")


asyncio.run(main())
