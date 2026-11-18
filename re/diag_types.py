"""Enumerate ALL packet types streaming on char 05 with various enables on."""
import asyncio, struct, sys, collections
sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"
buf, need = b"", 0
types = collections.Counter()
sizes = collections.defaultdict(set)


def handle(frame):
    try:
        length = struct.unpack("<H", frame[1:3])[0]
        pkt = frame[4:length]
        t = pkt[0]
        try: tn = PacketType(t).name
        except ValueError: tn = f"type{t}"
        types[tn] += 1
        sizes[tn].add(len(pkt) - 3)
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def cb(_, d):
    global buf, need
    f = bytes(d)
    if need == 0:
        if f and f[0] == 0xAA and len(f) >= 3:
            total = struct.unpack("<H", f[1:3])[0] + 4
            if len(f) >= total: handle(f[:total])
            else: buf, need = f, total
    else:
        buf += f
        if len(buf) >= need: handle(buf[:need]); buf, need = b"", 0


async def send(c, cmd, p=b"\x00"):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, cmd, p).framed_packet(), response=True)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    async with BleakClient(dev) as c:
        print("connected", c.is_connected, flush=True)
        await c.start_notify(DATA, cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL); await asyncio.sleep(1)
        for cmd, p in [(CommandNumber.ENABLE_OPTICAL_DATA, b"\x01"),
                       (CommandNumber.START_RAW_DATA, b"\x01"),
                       (CommandNumber.TOGGLE_IMU_MODE, b"\x01"),
                       (CommandNumber.SEND_R10_R11_REALTIME, b"\x01"),
                       (CommandNumber.TOGGLE_REALTIME_HR, b"\x01")]:
            try: await send(c, cmd, p)
            except Exception as e: print("err", cmd.name, e, flush=True)
            await asyncio.sleep(0.3)
        print("enabled all; capturing 10s (move your arm)...", flush=True)
        await asyncio.sleep(10)
        for cmd in [CommandNumber.STOP_RAW_DATA, CommandNumber.TOGGLE_REALTIME_HR]:
            try: await send(c, cmd, b"\x00")
            except Exception as exc: print(f"stop command {cmd.name} failed: {exc}", flush=True)
        print("TYPES:", dict(types), flush=True)
        print("SIZES:", {k: sorted(v) for k, v in sizes.items()}, flush=True)


asyncio.run(main())
