"""Restore the optical front-end (re-enable raw after the saturation latch) AND do two
read-only probes: GET_RESEARCH_PACKET(132) idx 0-3, and a NO-ACK historical peek (high-freq-sync
-> capture record sizes -> ABORT_HISTORICAL_TRANSMITS, NEVER ack => no trim) to see whether the
r24/r25 flags produced >=1188-byte full-IMU historical records.
Reports: whether 1921 optical frames resume (proxy for green LEDs back), 132 responses,
and the histogram of historical record (type-47) sizes seen before abort.
"""
import asyncio
import struct
import sys
import time
from collections import Counter

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, crc8  # noqa: E402
from packet_errors import is_malformed_packet_error
import zlib  # noqa: E402
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

raw_sizes = Counter()       # type-47 historical record data sizes
live_sizes = Counter()      # type-43 live sizes (1917/1921)
res132 = []
buf, need = b"", 0


def handle(frame):
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        pkt = frame[4:length]
        t, ver, cmd, data = pkt[0], pkt[1], pkt[2], pkt[3:]
        if t == 47:
            raw_sizes[len(pkt)] += 1   # full inner packet length (incl type/ver/cmd)
        elif t == 43:
            live_sizes[len(data)] += 1
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


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


def cmd_cb(_, d):
    raw = bytes(d)
    try:
        pkt = WhoopPacket.from_data(raw)
        if pkt.cmd == CommandNumber.GET_RESEARCH_PACKET.value:
            res132.append(pkt.data.hex())
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def frame(cmd_num, payload):
    pkt = struct.pack("<BBB", PacketType.COMMAND.value, 0, cmd_num) + payload
    blen = struct.pack("<H", len(pkt) + 4)
    return b"\xaa" + blen + struct.pack("<B", crc8(blen)) + pkt + struct.pack("<L", zlib.crc32(pkt) & 0xFFFFFFFF)


async def send(c, cmd, payload=b"\x00", resp=True):
    await c.write_gatt_char(CMD_TO, frame(cmd, payload), response=resp)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        print(f"connected {c.is_connected}", flush=True)
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value)  # bond
        await asyncio.sleep(1.2)

        # --- restore optical front-end ---
        await send(c, CommandNumber.ENABLE_OPTICAL_DATA.value, b"\x01")
        await send(c, CommandNumber.TOGGLE_OPTICAL_MODE.value, b"\x01")
        await send(c, CommandNumber.TOGGLE_IMU_MODE.value, b"\x01")
        await send(c, CommandNumber.START_RAW_DATA.value, b"\x01")
        print("re-enabled raw/optical; watching 8s for 1921 frames (green-LED proxy)...", flush=True)
        await asyncio.sleep(8)
        print(f"  live type-43 sizes after re-enable: {dict(live_sizes)}", flush=True)

        # --- GET_RESEARCH_PACKET idx 0..3 (read-only) ---
        for idx in range(4):
            await send(c, CommandNumber.GET_RESEARCH_PACKET.value, struct.pack("<B", idx))
            await asyncio.sleep(0.5)
        print(f"  GET_RESEARCH_PACKET responses: {res132}", flush=True)

        # --- NO-ACK historical peek: high-freq-sync, capture sizes, ABORT (no trim) ---
        live_sizes.clear()
        now = int(time.time())
        await send(c, CommandNumber.GET_HELLO_HARVARD.value)
        await send(c, CommandNumber.SET_CLOCK.value, struct.pack("<I", now) + b"\x00\x00\x00\x00\x00")
        await send(c, CommandNumber.GET_ADVERTISING_NAME_HARVARD.value)
        await send(c, CommandNumber.ENTER_HIGH_FREQ_SYNC.value, b"")
        await send(c, CommandNumber.SEND_HISTORICAL_DATA.value, b"\x00")
        print("  high-freq-sync started; capturing 12s of historical record sizes (NO ack)...", flush=True)
        await asyncio.sleep(12)
        # ABORT without ever acking => nothing trimmed
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS.value, b"\x00")
        await send(c, CommandNumber.EXIT_HIGH_FREQ_SYNC.value, b"\x00")
        await asyncio.sleep(1)
        print(f"  historical type-47 record sizes (inner pkt len): {dict(raw_sizes)}", flush=True)
        print(f"  >=1188-byte (full-IMU) historical records seen: {sum(v for k,v in raw_sizes.items() if k>=1188)}", flush=True)
        # leave optical enabled (do NOT STOP_RAW_DATA) so green LEDs stay on for the user
        print("DONE (raw left enabled; optical should be active)", flush=True)


asyncio.run(main())
