"""Verify the CORRECT whoomp framing yields valid command responses + realtime data."""
import asyncio
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa: E402
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

t0 = time.time()
resp_q = asyncio.Queue()


def on_cmd(_, data):
    try:
        pkt = WhoopPacket.from_data(data)
        resp_q.put_nowait(pkt)
        print(f"  [{time.time()-t0:5.1f}s] RESP: {pkt}")
    except Exception as e:
        print(f"  cmd parse err: {e} raw={bytes(data).hex()}")


def on_data(_, data):
    try:
        pkt = WhoopPacket.from_data(data)
        if pkt.type == PacketType.REALTIME_DATA:
            print(f"  [{time.time()-t0:5.1f}s] {pkt}")
    except Exception as e:
        print(f"  data parse err: {e} raw={bytes(data).hex()[:40]}")


def on_event(_, data):
    try:
        pkt = WhoopPacket.from_data(data)
        print(f"  [{time.time()-t0:5.1f}s] EVENT: {pkt}")
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


async def send(client, cmd, payload=b"\x00"):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    print(f">>> {cmd.name}: {pkt.hex()}")
    await client.write_gatt_char(CMD_TO, pkt, response=False)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    if dev is None:
        print("Device not found."); return
    async with BleakClient(dev) as client:
        print(f"Connected: {client.is_connected}\n")
        await client.start_notify(CMD_FROM, on_cmd)
        await client.start_notify(EVENTS, on_event)
        await client.start_notify(DATA, on_data)

        await send(client, CommandNumber.GET_BATTERY_LEVEL); await asyncio.sleep(1.5)
        await send(client, CommandNumber.REPORT_VERSION_INFO); await asyncio.sleep(1.5)
        await send(client, CommandNumber.GET_CLOCK); await asyncio.sleep(1.5)

        print("\n--- toggling realtime HR for 6s ---")
        await send(client, CommandNumber.TOGGLE_REALTIME_HR, b"\x01")
        await asyncio.sleep(6)
        await send(client, CommandNumber.TOGGLE_REALTIME_HR, b"\x00")
        await asyncio.sleep(1)
        print("\nDone.")


asyncio.run(main())
