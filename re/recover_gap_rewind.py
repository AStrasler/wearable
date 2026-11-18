"""RECOVER a server gap by REWINDING the strap's history read cursor (U), so the phone's normal
offload re-serves the old-but-still-in-flash type-47 records and re-uploads them (server dedupes by ts).

SAFE / reversible: only uses SET_READ_POINTER(33) = "move history read pointer (re-read WITHOUT trim)".
NEVER sends FORCE_TRIM(25) (destructive) and NEVER sends a forward HISTORICAL_DATA_RESULT ack here.
Worst case (wrong payload): U is unchanged and nothing happens.

Requires the phone's Bluetooth OFF + app quit so the Mac can own the strap.

Usage:  python recover_gap_rewind.py [TARGET_INDEX]
        TARGET_INDEX default = 72950 (the last-good read cursor / ~00:23 UTC, per the handoff).
After it confirms U rewound: turn the phone BT back on; the app's normal offload drains 72950->now
and uploads, backfilling the gap.
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

SET_READ_POINTER = 33   # "re-read without trim" (observed; not used by the official app)

resp_q: asyncio.Queue = asyncio.Queue()
datarange = []
t47 = []
buf, need = b"", 0
TARGET = int(sys.argv[1]) if len(sys.argv) > 1 else 72950


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
    await send(c, CommandNumber.GET_DATA_RANGE.value)
    await asyncio.sleep(1.5)
    r = datarange[-1] if datarange else None
    if not r or len(r) < 6:
        return None
    return {"W": r[2], "U": r[3], "T": r[5], "raw": r}


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — phone BT off + app quit?"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value); await asyncio.sleep(1.5)

        before = await read_range(c)
        if not before:
            print("no GET_DATA_RANGE response; aborting."); return
        oldest = max(0, before["W"] - before["T"])
        print(f"BEFORE: W={before['W']} U={before['U']} T={before['T']} (oldest retained idx>={oldest})")
        print(f"TARGET rewind index = {TARGET}")
        if TARGET < oldest:
            print(f"  !! TARGET {TARGET} < oldest retained {oldest} — those records were overwritten. Abort."); return
        if TARGET >= before["U"]:
            print(f"  note: TARGET {TARGET} >= current U {before['U']} — not a rewind (U already at/below target).")

        # Probe multiple candidate payloads for cmd 33, capturing the strap's response each time
        # (an error/echo response reveals whether the opcode is even processed) and re-reading U.
        candidates = [
            ("<u32 LE>",        struct.pack("<I", TARGET)),
            ("<u32 BE>",        struct.pack(">I", TARGET)),
            ("[0x01]+<u32 LE>", b"\x01" + struct.pack("<I", TARGET)),
            ("<i32 i32>(t,0)",  struct.pack("<ii", TARGET, 0)),
            ("[0x01]+<i32 i32>",b"\x01" + struct.pack("<ii", TARGET, 0)),
            ("<u16 LE>",        struct.pack("<H", TARGET & 0xFFFF)),
            ("empty",           b""),
        ]
        moved = False
        for label, pl in candidates:
            while not resp_q.empty(): resp_q.get_nowait()
            print(f"\n-> SET_READ_POINTER(33) payload={label} = {pl.hex() or '(empty)'}")
            await send(c, SET_READ_POINTER, pl); await asyncio.sleep(1.0)
            # drain any cmd-33 response
            resp33 = None
            t_end = time.time() + 1.0
            while time.time() < t_end:
                try:
                    rc, dd = await asyncio.wait_for(resp_q.get(), timeout=max(0.05, t_end - time.time()))
                except asyncio.TimeoutError:
                    break
                if rc == SET_READ_POINTER: resp33 = dd
            print(f"   cmd33 response: {resp33.hex() if resp33 else '(none)'}")
            after = await read_range(c)
            print(f"   U: {before['U']} -> {after['U']}  (W={after['W']})")
            if after and abs(after["U"] - TARGET) <= 5 and after["U"] != before["U"]:
                print(f"\n✅ U REWOUND to ~{after['U']} with payload {label}.")
                moved = True
                break
        if not moved:
            print(f"\n❌ No payload moved U off {before['U']}. SET_READ_POINTER(33) is not honored for a backward "
                  f"seek on this firmware (it's unbuilt by WHOOP's app — likely vestigial). No harm done.")
            await c.disconnect()
            return

        # Confirm the rewind actually re-serves old records (peek, NO ack so nothing is re-trimmed).
        print("\n-> peek: SEND_HISTORICAL_DATA[0x00] for 8s (NO ack — read-only confirm) ...")
        t47.clear()
        await send(c, CommandNumber.SEND_HISTORICAL_DATA.value, b"\x00", resp=True)
        await asyncio.sleep(8)
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS.value, b"\x00")
        if t47:
            print(f"   re-served {len(t47)} type-47 records, unix {min(t47)}..{max(t47)} "
                  f"({time.strftime('%H:%M', time.gmtime(min(t47)))}..{time.strftime('%H:%M', time.gmtime(max(t47)))} UTC)")
            print("   → turn the phone BT back ON; its normal offload will drain from here and re-upload (server dedupes).")
        else:
            print("   (no records re-served in the peek window — re-run, or U may need a lower target)")
        await c.disconnect()
    print("\nDONE (read pointer rewound; no trim/destructive op performed).")


asyncio.run(main())
