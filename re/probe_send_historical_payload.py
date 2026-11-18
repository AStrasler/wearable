"""Test whether SEND_HISTORICAL_DATA(22) accepts a START argument (index / unix / page / range) in its
payload — WHOOP's app always sends empty/[0x00], but the firmware may honor a start position. For each
candidate payload we serve and decode WHAT unix timestamps come out; if they fall in the GAP window
(00:23-02:48 UTC) instead of the recent U position, we've found a time/index-addressed read.

SAFE: read-only. SEND_HISTORICAL + ABORT, never an ack/trim, never FORCE_TRIM. Requires phone BT off.
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
GAP_LO, GAP_HI = 1779754981, 1779763694   # 00:23..02:48 UTC
GAP_IDX = 72950
datarange = []
t47 = []
buf, need = b"", 0


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_DATA_RANGE.value:
            body = pkt.data[3:]
            datarange.append([struct.unpack_from("<I", body, i)[0] for i in range(0, len(body) - 3, 4)])
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
    await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.3)
    r = datarange[-1] if datarange else None
    return (r[2], r[3], r[5], r) if r and len(r) > 5 else (None, None, None, r)


async def serve(c, label, payload, secs=7):
    t47.clear()
    await send(c, CommandNumber.SEND_HISTORICAL_DATA.value, payload, resp=True)
    await asyncio.sleep(secs)
    await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS.value, b"\x00"); await asyncio.sleep(0.6)
    if not t47:
        print(f"   [{label:22}] payload={payload.hex() or '(empty)':18} served 0"); return False
    lo, hi = min(t47), max(t47); ingap = sum(1 for u in t47 if GAP_LO <= u <= GAP_HI)
    fmt = lambda u: time.strftime('%H:%M', time.gmtime(u))
    print(f"   [{label:22}] payload={payload.hex() or '(empty)':18} served {len(t47):3} unix {fmt(lo)}..{fmt(hi)} ingap={ingap}{'  <<< GAP!!' if ingap else ''}")
    return ingap > 0


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — phone BT off + app quit?"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value); await asyncio.sleep(1.5)
        await send(c, CommandNumber.SEND_R10_R11_REALTIME.value, b"\x00"); await asyncio.sleep(1.0)
        W, U, T, raw = await read_range(c)
        print(f"baseline: W={W} U={U} T={T}")
        print(f"GET_DATA_RANGE words: {raw}")
        print(f"gap target: idx {GAP_IDX}, unix {GAP_LO}..{GAP_HI} (00:23..02:48 UTC)\n")

        cands = [
            ("control [00]",          b"\x00"),
            ("empty",                 b""),
            ("idx u32LE",             struct.pack("<I", GAP_IDX)),
            ("unix u32LE",            struct.pack("<I", GAP_LO)),
            ("[01]+idx u32LE",        b"\x01" + struct.pack("<I", GAP_IDX)),
            ("[01]+unix u32LE",       b"\x01" + struct.pack("<I", GAP_LO)),
            ("idx,end u32LE",         struct.pack("<II", GAP_IDX, U or 75123)),
            ("unix range u32LE",      struct.pack("<II", GAP_LO, GAP_HI)),
            ("unix u32BE",            struct.pack(">I", GAP_LO)),
            ("idx u32BE",             struct.pack(">I", GAP_IDX)),
            ("[01]+unix+unix",        b"\x01" + struct.pack("<II", GAP_LO, GAP_HI)),
        ]
        found = False
        for label, pl in cands:
            if await serve(c, label, pl):
                print(f"\n🎯 SEND_HISTORICAL with payload {label} ({pl.hex()}) SERVES THE GAP! That's the recovery key.")
                found = True
                break
        if not found:
            print("\n❌ No SEND_HISTORICAL payload reached the gap (all served from U / recent or nothing).")
        await c.disconnect()
    print("\nDONE (read-only; no trim).")


asyncio.run(main())
