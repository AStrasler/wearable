"""Enumerate the device's command surface: send every safe GET/info command, log responses.
Also listen for events (TEMPERATURE_LEVEL etc.). NON-destructive commands only.
"""
import asyncio, struct, sys, json, time
sys.path.insert(0, "whoomp/scripts")
from packet import WhoopPacket, PacketType, CommandNumber, EventNumber  # noqa
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa

from device_config import DEVICE_UUID as ADDR
CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
EVENTS = "61080004-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

out = open("command_probe.jsonl", "w", buffering=1)
responses = {}
events_seen = {}

# Safe, read-only commands to probe (avoid reboot/trim/firmware/shipmode/alarm-run/DFU/SET_*)
SAFE = [
    "LINK_VALID", "GET_MAX_PROTOCOL_VERSION", "REPORT_VERSION_INFO", "GET_CLOCK",
    "GET_BATTERY_LEVEL", "GET_DATA_RANGE", "GET_HELLO_HARVARD", "GET_LED_DRIVE",
    "GET_TIA_GAIN", "GET_BIAS_OFFSET", "GET_ALARM_TIME", "GET_ADVERTISING_NAME_HARVARD",
    "GET_ALL_HAPTICS_PATTERN", "GET_BODY_LOCATION_AND_STATUS", "GET_EXTENDED_BATTERY_INFO",
    "GET_DEVICE_CONFIG_VALUE", "GET_FF_VALUE", "GET_RESEARCH_PACKET", "GET_ADVERTISING_NAME",
    "GET_HELLO",
]


def cmd_cb(_, d):
    raw = bytes(d)
    try:
        pkt = WhoopPacket.from_data(raw)
        cmdname = CommandNumber(pkt.cmd).name if pkt.cmd in [c.value for c in CommandNumber] else f"cmd{pkt.cmd}"
        responses.setdefault(cmdname, []).append(pkt.data.hex())
        out.write(json.dumps({"chan": "cmd_from", "resp_cmd": cmdname, "type": pkt.type.name if hasattr(pkt.type,'name') else pkt.type,
                              "data_hex": pkt.data.hex(), "str": str(pkt)}) + "\n")
        print(f"  <= {cmdname}: {str(pkt)[:120]}", flush=True)
    except Exception as e:
        out.write(json.dumps({"chan": "cmd_from", "err": str(e), "hex": raw.hex()}) + "\n")


def event_cb(_, d):
    raw = bytes(d)
    try:
        pkt = WhoopPacket.from_data(raw)
        if pkt.type == PacketType.EVENT:
            try: en = EventNumber(pkt.cmd).name
            except ValueError: en = f"event{pkt.cmd}"
            events_seen[en] = events_seen.get(en, 0) + 1
            out.write(json.dumps({"chan": "events", "event": en, "data_hex": pkt.data.hex()}) + "\n")
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


async def send(client, cmdname, payload=b"\x00"):
    cmd = CommandNumber[cmdname]
    pkt = WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet()
    await client.write_gatt_char(CMD_TO, pkt, response=True)
    print(f">>> {cmdname} (payload {payload.hex()})", flush=True)


async def main():
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=15.0)
    if dev is None:
        print("not found"); return
    async with BleakClient(dev) as client:
        print(f"Connected: {client.is_connected}\n", flush=True)
        await client.start_notify(CMD_FROM, cmd_cb)
        await client.start_notify(EVENTS, event_cb)
        await client.start_notify(DATA, lambda _, d: None)
        await send(client, "GET_BATTERY_LEVEL")  # bond
        await asyncio.sleep(1.5)
        for name in SAFE:
            try:
                await send(client, name)
            except Exception as e:
                print(f"    send {name} failed: {e}", flush=True)
            await asyncio.sleep(0.8)
        print("\nlistening 15s for periodic events (temp etc.)...", flush=True)
        await asyncio.sleep(15)
        print(f"\n=== RESPONSES received for: {sorted(responses.keys())} ===", flush=True)
        print(f"=== EVENTS seen: {events_seen} ===", flush=True)


asyncio.run(main())
