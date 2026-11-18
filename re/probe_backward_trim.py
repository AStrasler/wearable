"""Test whether HISTORICAL_DATA_RESULT(23) with a BACKWARD trim value rewinds the read cursor U,
so the strap re-serves the gap (00:23-02:48 UTC, indices ~72950..U) that is below the current U.

SAFETY ANALYSIS (why this can't lose the gap data):
- The ack value (trim) only ever affects indices <= trim. We send trim = 72950, which is the BOTTOM of
  the gap, so the gap (72950..75123) and pending (75123..W) are all >= trim and untouched.
- Even if firmware "frees" slots <= trim, the ring physically retains them until the WRITE cursor wraps
  around (W=75738 vs wrap point ~72950+131072=204022 — eons away). Nothing is erased now.
- Best case: U rewinds to 72950 and SEND_HISTORICAL re-serves the gap. Worst case: clamped/no-op.
- We NEVER send FORCE_TRIM(25), and never a FORWARD trim (which would skip/lose pending records).
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
GAP_LO, GAP_HI = 1779754981, 1779763694   # 00:23..02:48 UTC
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 72950

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
    await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.2)
    r = datarange[-1] if datarange else None
    return (r[2], r[3], r[5]) if r and len(r) > 5 else (None, None, None)


async def serve(c, label, secs=8):
    t47.clear()
    await send(c, CommandNumber.SEND_HISTORICAL_DATA.value, b"\x00", resp=True)
    await asyncio.sleep(secs)
    await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS.value, b"\x00"); await asyncio.sleep(0.5)
    if not t47:
        print(f"   [{label}] served 0"); return 0
    lo, hi = min(t47), max(t47); ingap = sum(1 for u in t47 if GAP_LO <= u <= GAP_HI)
    fmt = lambda u: time.strftime('%H:%M', time.gmtime(u))
    print(f"   [{label}] served {len(t47)}, unix {fmt(lo)}..{fmt(hi)} UTC ingap={ingap}{'  <<< GAP!' if ingap else ''}")
    return ingap


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — phone BT off + app quit?"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value); await asyncio.sleep(1.5)
        await send(c, CommandNumber.SEND_R10_R11_REALTIME.value, b"\x00"); await asyncio.sleep(1.0)

        W, U0, T = await read_range(c)
        print(f"BEFORE: W={W} U={U0} T={T}")
        if TARGET >= U0:
            print(f"TARGET {TARGET} >= U {U0}: that's a FORWARD trim (would lose pending) — refusing."); return
        print(f"-> HISTORICAL_DATA_RESULT(23) BACKWARD trim = [01]+<u32 {TARGET}>+<u32 0>")
        await send(c, CommandNumber.HISTORICAL_DATA_RESULT.value, b"\x01" + struct.pack("<II", TARGET, 0))
        await asyncio.sleep(2.0)
        W2, U1, T2 = await read_range(c)
        print(f"AFTER : W={W2} U={U1} T={T2}   (U {U0} -> {U1})")
        if U1 is not None and U1 < U0 and abs(U1 - TARGET) <= 50:
            print(f"\n✅ U REWOUND {U0} -> {U1}. Serving to confirm it re-serves the gap...")
            ingap = await serve(c, "after-rewind")
            if ingap:
                print(f"\n🎯 RECOVERY WORKS. U is now at the gap; turn the phone BT ON and its normal "
                      f"offload will re-serve 00:23->now and re-upload (server dedupes).")
            else:
                print("   U moved but served data not in gap window — re-check target index.")
        else:
            print(f"\n❌ Backward trim NOT honored (U unchanged / clamped). No harm done; gap data intact in flash.")
        await c.disconnect()
    print("\nDONE.")


asyncio.run(main())
