"""Enable the strap's biometric data-product feature flags, set the clock, and reboot to boot
fresh — then (after a disconnected wear period) we offload to see if biometric records now write
to flash. Hypothesis: enable_write_r24/r25_packets gate writing biometric data-products to the
14-day flash store; enable_r19_packets / sigproc_10_sec_dp are sigproc data products.

SET_FF_VALUE payload (observed) = [REVISION_1=0x01][32-byte key utf8][32-byte value utf8],
value "1"=ON / "2"=OFF. All reversible (re-run with ON=False to restore). REBOOT is user-approved.
"""
import asyncio
import struct
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, EventNumber  # noqa: E402
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

# Flags to enable (True = ON = value "1")
TARGET_FLAGS = ["enable_write_r24_packets", "enable_write_r25_packets",
                "enable_r19_packets", "sigproc_10_sec_dp", "enable_sigproc_walk_detector"]
ON = (len(sys.argv) > 1 and sys.argv[1] == "on") or len(sys.argv) == 1  # default ON; "off" to restore

resp_q: asyncio.Queue = asyncio.Queue()
events = []


def cmd_from_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d)); resp_q.put_nowait((pkt.cmd, bytes(pkt.data)))
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def events_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.type == PacketType.EVENT:
            try: nm = EventNumber(pkt.cmd).name
            except Exception: nm = f"#{pkt.cmd}"
            events.append(nm)
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def ff_payload(name, on):
    val = b"1" if on else b"2"
    return bytes([0x01]) + name.encode().ljust(32, b"\x00") + val.ljust(32, b"\x00")


async def send(c, cmd, payload=b"\x00", resp=True):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await c.write_gatt_char(CMD_TO, pkt, response=resp)


async def call(c, cmd, payload, timeout=2.5):
    while not resp_q.empty():
        resp_q.get_nowait()
    await send(c, cmd, payload)
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            rc, d = await asyncio.wait_for(resp_q.get(), timeout=deadline - time.time())
        except asyncio.TimeoutError:
            break
        if rc == cmd.value:
            return d
    return None


async def connect():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        return None
    c = BleakClient(dev)
    await c.connect()
    await c.start_notify(CMD_FROM, cmd_from_cb)
    await c.start_notify(EVENTS, events_cb)
    await c.start_notify(DATA, lambda _, d: None)
    await send(c, CommandNumber.GET_BATTERY_LEVEL, b"\x00", resp=True)
    await asyncio.sleep(1.5)
    return c


async def main():
    c = await connect()
    if c is None:
        print("strap not found — free it (app quit + phone BT off)?"); return
    print(f"connected + bonded. Setting flags {'ON' if ON else 'OFF'}: {TARGET_FLAGS}\n", flush=True)

    for name in TARGET_FLAGS:
        events.clear()
        r = await call(c, CommandNumber.SET_FF_VALUE, ff_payload(name, ON))
        print(f"  SET_FF {name}={'1' if ON else '2'} -> resp={r.hex() if r else None}  events={events}", flush=True)
        await asyncio.sleep(0.3)

    # set clock to now (so any new records timestamp correctly)
    now = int(time.time())
    await call(c, CommandNumber.SET_CLOCK, struct.pack("<II", now, 0))
    print(f"\n  SET_CLOCK {now}", flush=True)

    # reboot to boot fresh with the flags applied
    print("\n>>> REBOOT_STRAP (user-approved) — link will drop", flush=True)
    try:
        await send(c, CommandNumber.REBOOT_STRAP, b"\x00", resp=False)
    except Exception as e:
        print(f"  (write raised on reboot, expected: {e})", flush=True)
    try:
        await c.disconnect()
    except Exception as exc:
        print(f"disconnect after reboot failed: {exc}", flush=True)

    print(">>> waiting 35s for reboot + re-advertise...", flush=True)
    await asyncio.sleep(35)
    c2 = await connect()
    if c2 is None:
        print("post-reboot: strap not found yet — wait ~30s and we'll proceed to the wear test anyway")
        return
    print("reconnected post-reboot + bonded.", flush=True)
    # confirm flags survived reboot by re-enumerating (names only; values unreliable)
    m = await call(c2, CommandNumber.START_FF_KEY_EXCHANGE, b"\x01")
    print(f"  FF START post-reboot -> {m.hex() if m else None}", flush=True)
    await c2.disconnect()
    print("\n=== DONE ===", flush=True)
    print("Flags set + strap rebooted. NOW: wear the strap, phone BT OFF, nothing connected,", flush=True)
    print("for ~25-30 min (move around), then we offload to check for new R24/R25/biometric records.", flush=True)


asyncio.run(main())
