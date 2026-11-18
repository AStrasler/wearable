"""Set the strap's RTC to the current wall-clock time and verify it took.

Hypothesis: the strap's RTC is frozen at Jan 8 2025 (no app session in ~16 months) and it may
not be logging biometrics to flash as a result. SET_CLOCK payload = <u32 seconds><u32 subsec> LE
(observed 8-byte form: u32 seconds LE, u32 subseconds LE). Reversible,
non-destructive. Watches for a SET_RTC(16) event + re-reads GET_CLOCK / GET_HELLO_HARVARD."""
import asyncio
import struct
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, EventNumber  # noqa: E402
from packet_errors import CallbackFailure
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

resp_q: asyncio.Queue = asyncio.Queue()
events = []
callback_failure = CallbackFailure()


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d)); resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        callback_failure.report(exc)


def events_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.type == PacketType.EVENT:
            try: nm = EventNumber(pkt.cmd).name
            except ValueError: nm = f"#{pkt.cmd}"
            events.append((nm, bytes(d).hex())); print(f"    EVENT {pkt.cmd} {nm}", flush=True)
    except Exception as exc:
        callback_failure.report(exc)


async def send(c, cmd, payload=b"\x00", resp=False):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await c.write_gatt_char(CMD_TO, pkt, response=resp)


async def req(c, cmd, timeout=4.0):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(c, cmd)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, d = await callback_failure.get(resp_q, timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return d
    return None


def show(tag, clk, hello):
    dev = struct.unpack_from("<I", clk, 2)[0] if clk and len(clk) >= 6 else None
    print(f"  [{tag}] GET_CLOCK device={dev}  hello={hello.hex() if hello else None}", flush=True)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap not found — free it (app quit + phone BT off)?"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_from_cb)
        await c.start_notify(EVENTS, events_cb)
        await c.start_notify(DATA, lambda _, d: None)
        await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
        await callback_failure.sleep(1.5)
        print("(bonded)", flush=True)

        clk0 = await req(c, CommandNumber.GET_CLOCK)
        h0 = await req(c, CommandNumber.GET_HELLO_HARVARD)
        show("before", clk0, h0)

        now = int(time.time())
        print(f"\n>>> SET_CLOCK seconds={now} ({time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))}) subsec=0", flush=True)
        events.clear()
        await send(c, CommandNumber.SET_CLOCK, struct.pack("<II", now, 0), resp=True)
        await callback_failure.sleep(2.0)
        # drain any cmd response
        got = []
        while not resp_q.empty():
            got.append(resp_q.get_nowait())
        print(f"    cmd responses: {[(rc, d.hex()) for rc, d in got]}", flush=True)
        print(f"    events after SET_CLOCK: {events}", flush=True)

        clk1 = await req(c, CommandNumber.GET_CLOCK)
        h1 = await req(c, CommandNumber.GET_HELLO_HARVARD)
        show("after", clk1, h1)

        # interpret: did the device monotonic clock jump, or did a unix field in hello change?
        d0 = struct.unpack_from("<I", clk0, 2)[0] if clk0 and len(clk0) >= 6 else None
        d1 = struct.unpack_from("<I", clk1, 2)[0] if clk1 and len(clk1) >= 6 else None
        print(f"\n  device-clock before={d0} after={d1} (delta {None if None in (d0,d1) else d1-d0})", flush=True)
        if h0 and h1 and h0 != h1:
            print("  GET_HELLO_HARVARD CHANGED after SET_CLOCK (RTC field likely updated).", flush=True)
        print("\ndone — if a SET_RTC(16) event fired and a unix field updated, the RTC is now set.", flush=True)


asyncio.run(main())
