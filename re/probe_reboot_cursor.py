"""Last-resort recovery probe: does REBOOT_STRAP(29) reset the history READ cursor U?
If reboot rewinds U toward the oldest retained record, a post-reboot offload re-serves the gap.
Expectation: U PERSISTS across reboot (offload bookmark survives power-cycle) => no help. But cheap+safe
to verify (reboot retains data + write cursor; done safely before). NO trim/destructive ops.
Requires phone BT off + app quit.
"""
import asyncio, struct, sys, time
sys.path.insert(0, "whoomp/scripts")
sys.path.insert(0, "re")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa
from device_config import DEVICE_UUID as ADDR  # noqa

CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
REBOOT_STRAP = 29
datarange = []


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_DATA_RANGE.value:
            body = pkt.data[3:]
            datarange.append([struct.unpack_from("<I", body, i)[0] for i in range(0, len(body) - 3, 4)])
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


async def send(c, cmd, payload=b"\x00", resp=True):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet(), response=resp)


async def read_range(c):
    datarange.clear()
    await send(c, CommandNumber.GET_DATA_RANGE.value); await asyncio.sleep(1.3)
    r = datarange[-1] if datarange else None
    return (r[2], r[3], r[5]) if r and len(r) > 5 else (None, None, None)


async def connect_once(timeout=20.0):
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=timeout)
    if dev is None: return None
    c = BleakClient(dev); await c.connect()
    await c.start_notify(CMD_FROM, cmd_cb)
    await send(c, CommandNumber.GET_BATTERY_LEVEL.value); await asyncio.sleep(1.5)
    return c


async def main():
    c = await connect_once()
    if not c: print("strap not found — phone BT off?"); return
    W0, U0, T0 = await read_range(c)
    print(f"BEFORE reboot: W={W0} U={U0} T={T0}")
    print("-> REBOOT_STRAP(29); waiting for re-advertise...")
    try: await send(c, REBOOT_STRAP, b"\x00", resp=False)
    except Exception as exc: print(f"reboot write raised (link may drop): {exc}", flush=True)
    try: await c.disconnect()
    except Exception as exc: print(f"disconnect after reboot failed: {exc}", flush=True)

    c2 = None
    for attempt in range(8):
        await asyncio.sleep(8)
        try:
            c2 = await connect_once(timeout=12.0)
            if c2:
                print(f"   reconnected after ~{(attempt+1)*8}s")
                break
        except Exception as e:
            print(f"   reconnect attempt {attempt+1} failed ({e})")
    if not c2:
        print("could not reconnect after reboot (strap may still be booting). Re-run or let the phone reconnect.")
        return
    W1, U1, T1 = await read_range(c2)
    print(f"AFTER  reboot: W={W1} U={U1} T={T1}   (U {U0} -> {U1})")
    if U1 is not None and U0 is not None and U1 < U0 - 50:
        print(f"\n🎯 REBOOT REWOUND U ({U0} -> {U1})! A post-reboot offload re-serves older data — "
              f"turn the phone BT ON to recover the gap (it re-serves from U and re-uploads).")
    else:
        print(f"\n❌ U persisted across reboot ({U0} -> {U1}). Reboot is not a recovery path. "
              f"Gap data remains in flash but unreachable via the offload (forward-only from a persistent U).")
    try: await c2.disconnect()
    except Exception as exc: print(f"disconnect after probe failed: {exc}", flush=True)
    print("\nDONE (no trim/destructive ops).")


asyncio.run(main())
