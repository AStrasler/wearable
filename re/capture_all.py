"""Comprehensive capture: bond, then pull realtime + raw(accel/gyro) + historical.
Logs every char-03/05 packet (full hex + type) to capture.jsonl for offline decoding.
Tries several enable sequences to wake the raw stream.
"""
import asyncio
import json
import struct
import sys
import time

sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, MetadataType  # noqa: E402
from bleak import BleakClient, BleakScanner  # noqa: E402

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

cap = open("capture.jsonl", "w", buffering=1)
meta_q: asyncio.Queue = asyncio.Queue()
phase = "init"
typecounts = {}


def logpkt(char, raw):
    try:
        pkt = WhoopPacket.from_data(raw)
        tname = pkt.type.name if hasattr(pkt.type, "name") else str(pkt.type)
        typecounts[tname] = typecounts.get(tname, 0) + 1
        rec = {"phase": phase, "char": char, "type": tname,
               "seq": pkt.seq, "cmd": pkt.cmd, "data_hex": pkt.data.hex(),
               "full_hex": raw.hex()}
        cap.write(json.dumps(rec) + "\n")
        if pkt.type == PacketType.METADATA:
            meta_q.put_nowait(pkt)
        return pkt
    except Exception as e:
        cap.write(json.dumps({"phase": phase, "char": char, "err": str(e),
                              "full_hex": raw.hex()}) + "\n")
        return None


def cmd_cb(_, d):
    logpkt("cmd_from", bytes(d))


def data_cb(_, d):
    logpkt("data", bytes(d))


async def send(client, cmd, payload=b"\x00", resp=True):
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await client.write_gatt_char(CMD_TO, pkt, response=resp)
    print(f">>> {cmd.name} {payload.hex()}", flush=True)


async def run_historical(client):
    await send(client, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
    chunks = 0
    try:
        while True:
            m = await asyncio.wait_for(meta_q.get(), timeout=15)
            mt = MetadataType(m.cmd)
            if mt == MetadataType.HISTORY_COMPLETE:
                print(">>> HISTORY_COMPLETE", flush=True); break
            if mt == MetadataType.HISTORY_END:
                unix, subsec, unk0, trim = struct.unpack("<LHLL", m.data[:14])
                await send(client, CommandNumber.HISTORICAL_DATA_RESULT,
                           struct.pack("<BLL", 1, trim, 0))
                chunks += 1
                if chunks >= 40:
                    print(">>> stopping after 40 chunks", flush=True); break
    except asyncio.TimeoutError:
        print(f">>> historical stalled after {chunks} chunks", flush=True)
    return chunks


async def main():
    global phase
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    if dev is None:
        print("not found"); return
    async with BleakClient(dev) as client:
        print(f"Connected: {client.is_connected}", flush=True)
        await client.start_notify(CMD_FROM, cmd_cb)
        await client.start_notify(EVENTS, lambda _, d: None)
        await client.start_notify(DATA, data_cb)

        phase = "bond"
        await send(client, CommandNumber.GET_BATTERY_LEVEL)  # confirmed write -> bond
        await asyncio.sleep(1.5)

        phase = "realtime_hr"
        print("\n=== realtime HR 6s ===", flush=True)
        await send(client, CommandNumber.TOGGLE_REALTIME_HR, b"\x01")
        await asyncio.sleep(6)
        await send(client, CommandNumber.TOGGLE_REALTIME_HR, b"\x00")

        phase = "raw"
        print("\n=== raw stream: try enable sequence, 10s (MOVE YOUR ARM) ===", flush=True)
        # try multiple enables that might be required for raw accel/gyro
        for c, p in [(CommandNumber.ENABLE_OPTICAL_DATA, b"\x01"),
                     (CommandNumber.TOGGLE_IMU_MODE, b"\x01"),
                     (CommandNumber.START_RAW_DATA, b"\x01"),
                     (CommandNumber.SEND_R10_R11_REALTIME, b"\x01")]:
            try:
                await send(client, c, p)
            except Exception as e:
                print(f"    {c.name} err: {e}", flush=True)
            await asyncio.sleep(0.4)
        await asyncio.sleep(10)
        for c in [CommandNumber.STOP_RAW_DATA, CommandNumber.TOGGLE_IMU_MODE]:
            try:
                await send(client, c, b"\x00")
            except Exception as exc:
                print(f"    {c.name} stop failed: {exc}", flush=True)

        phase = "historical"
        print("\n=== historical offload ===", flush=True)
        n = await run_historical(client)

        print(f"\n=== TYPE COUNTS: {typecounts} ===", flush=True)
        print(f"historical chunks: {n}", flush=True)


asyncio.run(main())
