"""READ-ONLY: is the strap logging the biometric store RIGHT NOW (worn), in normal mode?
1) GET_DATA_RANGE twice 90s apart (NO high-freq-sync active) -> do the write cursor / count /
   newest_unix advance? = strap is logging normally.
2) No-ack historical peek (high-freq-sync -> capture type-47 -> ABORT, never ack => no trim):
   what's the NEWEST type-47 record actually available? (~now vs stale 00:55).
Distinguishes: (A) stuck-mode suppresses logging  vs  (B) the app's periodic sync interferes.
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

datarange = []
t47_unix = []
buf, need = b"", 0


def parse_range(data):
    # data = pkt.data: 0a 01 01 <u32 LE words...>
    words = []
    body = data[3:]
    for i in range(0, len(body) - 3, 4):
        words.append(struct.unpack_from("<I", body, i)[0])
    return words


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_DATA_RANGE.value:
            datarange.append(parse_range(pkt.data))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def handle(frame):
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        pkt = frame[4:length]
        if pkt[0] == 47:  # HISTORICAL_DATA
            data = pkt[3:]
            if len(data) >= 8:
                t47_unix.append(struct.unpack_from("<I", data, 4)[0])
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


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


def frame(cmd, payload):
    pkt = struct.pack("<BBB", PacketType.COMMAND.value, 0, cmd) + payload
    blen = struct.pack("<H", len(pkt) + 4)
    return b"\xaa" + blen + struct.pack("<B", crc8(blen)) + pkt + struct.pack("<L", zlib.crc32(pkt) & 0xFFFFFFFF)


async def send(c, cmd, payload=b"\x00"):
    await c.write_gatt_char(CMD_TO, frame(cmd, payload), response=True)


def fmt_unix(words):
    us = [w for w in words if 1_700_000_000 <= w <= 1_900_000_000]
    return us


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        print(f"connected {c.is_connected}  now={int(time.time())}", flush=True)
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value)  # bond
        await asyncio.sleep(1.5)

        # --- normal-mode logging test: GET_DATA_RANGE x2, 90s apart, NO high-freq-sync ---
        await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.0)
        print(f"[range #1] words={datarange[-1] if datarange else None}", flush=True)
        print(f"           unix vals={fmt_unix(datarange[-1]) if datarange else None}", flush=True)
        print("  waiting 90s in NORMAL mode (is it logging?)...", flush=True)
        await asyncio.sleep(90)
        await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.0)
        print(f"[range #2] words={datarange[-1] if len(datarange)>1 else None}", flush=True)
        print(f"           unix vals={fmt_unix(datarange[-1]) if len(datarange)>1 else None}", flush=True)
        if len(datarange) >= 2:
            a, b = datarange[-2], datarange[-1]
            diff = [(i, a[i], b[i], b[i]-a[i]) for i in range(min(len(a), len(b))) if a[i] != b[i]]
            print(f"  CHANGED words over 90s (idx, before, after, delta): {diff}", flush=True)
            print("  => if cursors/count/newest_unix ADVANCED, the strap IS logging normally.", flush=True)

        # --- no-ack peek: newest available type-47 record ---
        t47_unix.clear()
        now = int(time.time())
        await send(c, CommandNumber.GET_HELLO_HARVARD.value)
        await send(c, CommandNumber.SET_CLOCK.value, struct.pack("<I", now) + b"\x00\x00\x00\x00\x00")
        await send(c, CommandNumber.GET_ADVERTISING_NAME_HARVARD.value)
        await send(c, CommandNumber.ENTER_HIGH_FREQ_SYNC.value, b"")
        await send(c, CommandNumber.SEND_HISTORICAL_DATA.value, b"\x00")
        print("  high-freq-sync peek 15s (NO ack)...", flush=True)
        await asyncio.sleep(15)
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS.value, b"\x00")
        await send(c, CommandNumber.EXIT_HIGH_FREQ_SYNC.value, b"\x00")
        await asyncio.sleep(1)
        if t47_unix:
            newest = max(t47_unix)
            print(f"  type-47 records seen: {len(t47_unix)}  newest_unix={newest} "
                  f"({newest-now:+d}s vs now)  oldest={min(t47_unix)}", flush=True)
        else:
            print("  type-47 records seen: 0 (strap offered NO biometric records)", flush=True)
        print("DONE (read-only; nothing trimmed)", flush=True)


asyncio.run(main())
