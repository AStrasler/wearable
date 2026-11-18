"""Turn OFF the sigproc/R19 data-product feature flags a prior RE session latched ON, reboot,
then MEASURE whether the persistent type-43 REALTIME_RAW_DATA flood stops while the type-47 V24
biometric store keeps logging. Reversible (re-run with `on` to restore). REBOOT user-approved.

Step 1 (this run): SET the 4 flood-suspect flags OFF (keep enable_write_r24/r25 ON for retention),
SET_CLOCK, REBOOT, reconnect, and tally DATA-char frame types for 25s + GET_DATA_RANGE x2 to see if
the strap is still logging V24 (write cursor / newest_unix advancing).
"""
import asyncio, struct, sys, time
from collections import Counter
sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, crc8  # noqa
from packet_errors import is_malformed_packet_error
import zlib  # noqa
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

# default: turn the flood suspects OFF. `python fix_raw_flood.py on` restores them.
RESTORE = len(sys.argv) > 1 and sys.argv[1] == "on"
FLOOD_FLAGS = ["sigproc_10_sec_dp", "enable_r19_packets", "enable_r19_v4_packets",
               "enable_sigproc_walk_detector"]

resp_q: asyncio.Queue = asyncio.Queue()
type_counts = Counter()
buf, need = b"", 0
datarange = []


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
    if len(frame) > 4:
        type_counts[frame[4]] += 1


def data_cb(_, d):
    global buf, need
    f = bytes(d)
    if need == 0:
        if f and f[0] == 0xAA and len(f) >= 3:
            total = struct.unpack("<H", f[1:3])[0] + 4
            if len(f) >= total:
                handle(f[:total])
            else:
                buf, need = f, total
    else:
        buf += f
        if len(buf) >= need:
            handle(buf[:need]); buf, need = b"", 0


def ff_payload(name, on):
    return bytes([0x01]) + name.encode().ljust(32, b"\x00") + (b"1" if on else b"2").ljust(32, b"\x00")


async def send(c, cmd, payload=b"\x00", resp=True):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet(), response=resp)


async def call(c, cmd, payload=b"\x00", timeout=2.5):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(c, cmd, payload, resp=True)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, dd = await asyncio.wait_for(resp_q.get(), timeout=max(0.05, deadline - time.time()))
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return dd
    return None


async def connect():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        return None
    c = BleakClient(dev)
    await c.connect()
    await c.start_notify(CMD_FROM, cmd_cb)
    await c.start_notify(DATA, data_cb)
    await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
    await asyncio.sleep(1.5)
    return c


async def main():
    c = await connect()
    if c is None:
        print("strap not found — is the phone's BT off + app quit?"); return
    val = "1(ON)" if RESTORE else "2(OFF)"
    print(f"connected+bonded. Setting flood flags -> {val}: {FLOOD_FLAGS}\n", flush=True)
    for name in FLOOD_FLAGS:
        r = await call(c, CommandNumber.SET_FF_VALUE, ff_payload(name, RESTORE))
        print(f"  SET_FF {name}={val} -> resp={r.hex() if r else None}", flush=True)
        await asyncio.sleep(0.3)
    now = int(time.time())
    await call(c, CommandNumber.SET_CLOCK, struct.pack("<II", now, 0))
    print(f"  SET_CLOCK {now}\n>>> REBOOT_STRAP (user-approved) — link drops", flush=True)
    try:
        await send(c, CommandNumber.REBOOT_STRAP, b"\x00", resp=False)
    except Exception as e:
        print(f"  (reboot write raised, expected: {e})", flush=True)
    try:
        await c.disconnect()
    except Exception as exc:
        print(f"disconnect after reboot failed: {exc}", flush=True)

    print(">>> waiting 40s for reboot + re-advertise...", flush=True)
    await asyncio.sleep(40)
    c2 = await connect()
    if c2 is None:
        print("post-reboot: strap not found yet — wait ~30s, re-run with no args to just measure"); return
    print("reconnected post-reboot.\n", flush=True)

    await call(c2, CommandNumber.GET_DATA_RANGE)
    r0 = datarange[-1] if datarange else None
    type_counts.clear()
    print("MEASURING 25s of DATA-char frames (is the type-43 flood gone?)...", flush=True)
    await asyncio.sleep(25)
    await call(c2, CommandNumber.GET_DATA_RANGE)
    r1 = datarange[-1] if len(datarange) > 1 else None

    print(f"\n  DATA frame-type counts over 25s: {dict(type_counts)}", flush=True)
    t43 = type_counts.get(43, 0)
    print(f"  >>> type-43 REALTIME_RAW_DATA in 25s: {t43}  ({t43/25:.1f}/s)  "
          f"{'FLOOD STILL ON' if t43 > 10 else 'FLOOD STOPPED ✅' if t43 == 0 else 'much reduced'}", flush=True)
    if r0 and r1:
        delta = [(i, r0[i], r1[i], r1[i] - r0[i]) for i in range(min(len(r0), len(r1))) if r0[i] != r1[i]]
        print(f"  GET_DATA_RANGE changed words (idx,before,after,delta): {delta}", flush=True)
        print(f"  >>> if a cursor/unix advanced, V24 logging CONTINUES; if nothing changed, logging may have stopped", flush=True)
    await c2.disconnect()
    print("\n=== DONE (reversible: `python re/fix_raw_flood.py on` to restore) ===", flush=True)


asyncio.run(main())
