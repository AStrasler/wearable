"""Probe ALL data sources: standard HR profile + custom service.

Corrects the off-by-one in whoop-reader: the real custom service is 61080001,
so the command (write) char is 61080002. We subscribe to every notify char,
send the known commands, and log everything raw + timestamped.
"""
import asyncio
import time
from bleak import BleakClient, BleakScanner
from whoop_reader.protocol import build_command

from device_config import DEVICE_UUID as ADDR

# Standard profile
HR_MEAS = "00002a37-0000-1000-8000-00805f9b34fb"

# Custom service (corrected): service is 61080001
CMD_CHAR = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"  # write / write-without-response
NOTIFY_CHARS = {
    "61080003": "61080003-8d6d-82b8-614a-1c8cb0f8dcc6",
    "61080004": "61080004-8d6d-82b8-614a-1c8cb0f8dcc6",
    "61080005": "61080005-8d6d-82b8-614a-1c8cb0f8dcc6",
    "61080007": "61080007-8d6d-82b8-614a-1c8cb0f8dcc6",
}

# Known command codes from reverse-engineering
COMMANDS = {
    "GET_HELLO": 0x05,
    "GET_BATTERY": 0x01,
    "GET_DEVICE_INFO": 0x02,
    "START_REALTIME": 0x03,
}

t0 = time.time()
counts = {}


def make_cb(label):
    def cb(_, data: bytearray):
        counts[label] = counts.get(label, 0) + 1
        # Only print first few of each to avoid flooding
        if counts[label] <= 4:
            print(f"  [{time.time()-t0:6.2f}s] {label:9} len={len(data):3} raw={bytes(data).hex()}")
    return cb


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    if dev is None:
        print("Device not found."); return
    async with BleakClient(dev) as client:
        print(f"Connected: {client.is_connected}\n")

        # Subscribe to standard HR + all custom notify chars
        await client.start_notify(HR_MEAS, make_cb("HR(2A37)"))
        for label, uuid in NOTIFY_CHARS.items():
            try:
                await client.start_notify(uuid, make_cb(label))
            except Exception as e:
                print(f"  subscribe {label} failed: {e}")

        print("Subscribed. Sending commands...\n")
        for name, code in COMMANDS.items():
            frame = build_command(code)
            print(f">>> {name} (0x{code:02X}): {frame.hex()}")
            try:
                await client.write_gatt_char(CMD_CHAR, frame, response=False)
            except Exception as e:
                print(f"    write failed: {e}")
            await asyncio.sleep(2.5)

        print("\nListening 12s for streaming data...\n")
        await asyncio.sleep(12)

        # Stop realtime
        try:
            await client.write_gatt_char(CMD_CHAR, build_command(0x04), response=False)
        except Exception as exc:
            print(f"stop realtime failed: {exc}", flush=True)

        print("\n=== notification counts ===")
        for label, n in sorted(counts.items()):
            print(f"  {label:9} {n}")


asyncio.run(main())
