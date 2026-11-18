"""Confirm: (1) does the R10/R11 flood return on a FRESH connect (persistence)? (2) with the flood
off, is the type-47 offload FAST (airtime freed)? Read-only: high-freq-sync PEEK with NO ack (ABORT),
nothing trimmed.
"""
import asyncio, struct, sys, time
from collections import Counter
sys.path.insert(0, "whoomp/scripts")
from packet import crc8  # noqa
import zlib  # noqa
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
counts = Counter(); t47_unix = []
buf, need = b"", 0


def handle(fr):
    if len(fr) > 4:
        counts[fr[4]] += 1
        if fr[4] == 47 and len(fr) >= 15:
            # type-47 V24: unix at data[4:8]; data = pkt after [type,seq,cmd] = fr[7:]; unix=fr[11:15]
            t47_unix.append(struct.unpack_from("<I", fr, 11)[0])


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


def fr_cmd(cmd, payload=b"\x00"):
    inner = struct.pack("<BBB", 35, 0, cmd) + payload
    blen = struct.pack("<H", len(inner) + 4)
    return b"\xaa" + blen + struct.pack("<B", crc8(blen)) + inner + struct.pack("<L", zlib.crc32(inner) & 0xFFFFFFFF)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        await c.start_notify(DATA, data_cb)
        async def send(cmd, p=b"\x00"):
            await c.write_gatt_char(CMD_TO, fr_cmd(cmd, p), response=True)
        await send(26); await asyncio.sleep(1.5)  # bond
        print("connected+bonded", flush=True)

        # (1) persistence: did the flood come back on this fresh connect (after last run turned it off)?
        counts.clear(); await asyncio.sleep(8)
        print(f"  PERSISTENCE: type-43 on fresh connect (8s) = {counts.get(43,0)} "
              f"({'still OFF — persists ✅' if counts.get(43,0)==0 else 'came BACK -> app must send 63[00] each connect'})", flush=True)

        # ensure off, then time the offload
        await send(63, b"\x00"); await asyncio.sleep(0.5)
        now = int(time.time())
        await send(35)  # GET_HELLO_HARVARD
        await send(10, struct.pack("<I", now) + b"\x00"*5)  # SET_CLOCK
        await send(96, b"")   # ENTER_HIGH_FREQ_SYNC
        await send(22, b"\x00")  # SEND_HISTORICAL_DATA
        counts.clear(); t47_unix.clear()
        await asyncio.sleep(15)
        await send(20, b"\x00")  # ABORT_HISTORICAL_TRANSMITS
        await send(97, b"\x00")  # EXIT_HIGH_FREQ_SYNC
        n47 = counts.get(47, 0)
        print(f"  OFFLOAD SPEED (flood off, 15s, no-ack): type-47={n47} ({n47/15:.1f}/s)  "
              f"other={ {k:v for k,v in counts.items() if k!=47} }", flush=True)
        if t47_unix:
            print(f"    type-47 unix span: {min(t47_unix)} -> {max(t47_unix)}", flush=True)
        print("  (compare: WITH the flood earlier the no-ack peek got ~51 type-47 in 15s = 3.4/s)", flush=True)
        await c.disconnect()
    print("DONE (read-only; nothing trimmed)", flush=True)


asyncio.run(main())
