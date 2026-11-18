"""Does acking a high-freq-sync chunk advance the strap's read/trim pointer?
Bond -> GET_DATA_RANGE (trim0) -> high-freq-sync -> for 30s ack each HISTORY_END with
[0x01]+data[10:18] (the app's form) -> GET_DATA_RANGE (trim1). If trim advances, acking works
(fix = drain to completion). If not, the biometric replay isn't trimmable -> need SET_READ_POINTER.
Saves every DATA notification to fixtures/diag_trim.bin (gitignored). Acks ARE sent (trim is a
rolling 14-day store + server-backed; user-approved this session)."""
import asyncio, struct, sys, time
sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, MetadataType, crc8  # noqa
from packet_errors import is_malformed_packet_error
import zlib  # noqa
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
OUT = "fixtures/diag_trim.bin"

meta_q: asyncio.Queue = asyncio.Queue()
ranges = []
n47 = 0
unix47 = []
histf = open(OUT, "wb")
buf, need = b"", 0


def parse_range(data):
    body = data[3:]
    return [struct.unpack_from("<I", body, i)[0] for i in range(0, len(body) - 3, 4)]


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_DATA_RANGE.value:
            ranges.append(parse_range(pkt.data))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def handle(frame):
    global n47
    histf.write(frame); histf.flush()
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        pkt = frame[4:length]
        t = pkt[0]
        if t == 47:
            n47 += 1
            if len(pkt) >= 11:
                unix47.append(struct.unpack_from("<I", pkt, 7)[0])  # data[4:8] = pkt[7:11]
        elif t == 49 and pkt[2] == MetadataType.HISTORY_END.value:
            meta_q.put_nowait(bytes(pkt[3:]))  # data
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


async def send(c, cmd, payload=b"\x00", resp=True):
    await c.write_gatt_char(CMD_TO, frame(cmd, payload), response=resp)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found"); return
    async with BleakClient(dev) as c:
        print(f"connected {c.is_connected}  now={int(time.time())}", flush=True)
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL.value); await asyncio.sleep(1.5)
        await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.0)
        r0 = ranges[-1] if ranges else None
        print(f"[range0] trim={r0[3] if r0 else '?'} write={r0[2] if r0 else '?'} count={r0[6] if r0 else '?'}", flush=True)

        now = int(time.time())
        await send(c, CommandNumber.GET_HELLO_HARVARD.value)
        await send(c, CommandNumber.SET_CLOCK.value, struct.pack("<I", now) + b"\x00\x00\x00\x00\x00")
        await send(c, CommandNumber.GET_ADVERTISING_NAME_HARVARD.value)
        await send(c, CommandNumber.ENTER_HIGH_FREQ_SYNC.value, b"")
        await send(c, CommandNumber.SEND_HISTORICAL_DATA.value, b"\x00")
        print("  acking chunks for 30s...", flush=True)
        acks = 0
        t0 = time.time()
        while time.time() - t0 < 30:
            try:
                data = await asyncio.wait_for(meta_q.get(), timeout=3)
            except asyncio.TimeoutError:
                continue
            end_data = data[10:18]
            await send(c, CommandNumber.HISTORICAL_DATA_RESULT.value, b"\x01" + end_data, resp=True)
            acks += 1
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS.value, b"\x00")
        await send(c, CommandNumber.EXIT_HIGH_FREQ_SYNC.value, b"\x00")
        await asyncio.sleep(1)
        await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.0)
        r1 = ranges[-1] if len(ranges) > 1 else None
        print(f"[range1] trim={r1[3] if r1 else '?'} write={r1[2] if r1 else '?'} count={r1[6] if r1 else '?'}", flush=True)
        print(f"\n  acks sent={acks}  type47 records={n47}", flush=True)
        if unix47:
            print(f"  type47 unix range: {min(unix47)} .. {max(unix47)}  (newest {max(unix47)-now:+d}s vs now)", flush=True)
        if r0 and r1:
            print(f"  TRIM moved: {r0[3]} -> {r1[3]}  (delta {r1[3]-r0[3]})", flush=True)
            print(f"  => trim advanced on ack? {'YES — acking works, fix=drain longer' if r1[3] != r0[3] else 'NO — acking does not trim biometric replay; need SET_READ_POINTER'}", flush=True)
        print(f"DONE -> {OUT}", flush=True)


asyncio.run(main())
