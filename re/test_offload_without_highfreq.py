"""READ-ONLY experiment: does plain SEND_HISTORICAL_DATA return type-47 biometrics
WITHOUT entering high-freq-sync?

Decides whether the iOS connect handshake's ENTER_HIGH_FREQ_SYNC is necessary at all.
Motivation: the 2026-05-25 sync-stall investigation (strap stranded in high-freq-sync stops
logging -> permanent data loss) + the APK finding that WHOOP's own Gen4 (WHOOP 4.0) offload
NEVER enters high-freq-sync (it uses plain SEND_HISTORICAL_DATA; 96/97 are Smart-Alarm-only).
Our docs claim type-47 "requires the high-freq-sync handshake" -- but that was likely an
artifact of the then-frozen RTC. This isolates the variable.

Design -- ONE variable (high-freq-sync) changed between two phases, SAME data window:
  0) connect, bond, stop the R10/R11 type-43 live flood (matches the app; reversible) for a
     clean histogram, then GET_DATA_RANGE -> confirm the strap is LOGGING now (newest unix ~now).
  A) NO high-freq-sync:   SEND_HISTORICAL_DATA, capture 15s, ABORT.
  B) WITH high-freq-sync: ENTER_HIGH_FREQ_SYNC, SEND_HISTORICAL_DATA, capture 15s, ABORT, EXIT.
Never acks (no HISTORICAL_DATA_RESULT) -> no trim -> the biometric store is NOT mutated. No
SET_CLOCK (no clock mutation). Always EXITs high-freq-sync + disconnects cleanly.

Verdict:
  - type-47 in A          -> HIGH-FREQ-SYNC NOT NEEDED (drop it; full WHOOP-faithful).
  - none in A, some in B   -> HIGH-FREQ-SYNC REQUIRED on this strap (keep it, make it safe:
                              bounded-duration ENTER + explicit EXIT).
  - none in either         -> strap not serving fresh data (caught-up / stuck / not logging):
                              INCONCLUSIVE -- check the GET_DATA_RANGE markers (stale = frozen).
"""
import asyncio, struct, sys, time
from collections import Counter
sys.path.insert(0, "whoomp/scripts")
sys.path.insert(0, "re")
from packet import WhoopPacket, PacketType, CommandNumber  # noqa
from packet_errors import is_malformed_packet_error
from bleak import BleakClient, BleakScanner  # noqa
from device_config import DEVICE_UUID as ADDR  # noqa

CMD_TO = "61080002-8d6d-82b8-614a-1c8cb0f8dcc6"
CMD_FROM = "61080003-8d6d-82b8-614a-1c8cb0f8dcc6"
DATA = "61080005-8d6d-82b8-614a-1c8cb0f8dcc6"

datarange = []
counts = Counter()      # current-phase frame-type histogram
t47_unix = []           # current-phase type-47 record unix timestamps
buf, need = b"", 0


def cmd_cb(_, d):
    try:
        pkt = WhoopPacket.from_data(bytes(d))
        if pkt.cmd == CommandNumber.GET_DATA_RANGE.value:
            body = pkt.data[3:]
            datarange.append([struct.unpack_from("<I", body, i)[0] for i in range(0, len(body) - 3, 4)])
    except Exception as exc:
        if not is_malformed_packet_error(exc):
            raise


def handle(frame):
    if len(frame) <= 4:
        return
    t = frame[4]
    counts[t] += 1
    if t == 47:                       # HISTORICAL_DATA (the biometric store the app uses)
        data = frame[4:][3:]          # strip [type,seq,cmd] -> record payload
        if len(data) >= 8:
            t47_unix.append(struct.unpack_from("<I", data, 4)[0])


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
            handle(buf[:need])
            buf, need = b"", 0


async def send(c, cmd, payload=b"\x00"):
    await c.write_gatt_char(CMD_TO, WhoopPacket(PacketType.COMMAND, 10, cmd, data=payload).framed_packet(), response=True)


def reset():
    counts.clear()
    t47_unix.clear()


def report(phase):
    hist = {t: counts[t] for t in sorted(counts)}
    print(f"  [{phase}] frame-type histogram: {hist}", flush=True)
    if t47_unix:
        now = int(time.time())
        newest, oldest = max(t47_unix), min(t47_unix)
        print(f"  [{phase}] TYPE-47: {len(t47_unix)} records, unix {oldest}..{newest} "
              f"({time.strftime('%H:%M:%S', time.gmtime(oldest))}..{time.strftime('%H:%M:%S', time.gmtime(newest))} UTC, "
              f"newest {newest - now:+d}s vs now)", flush=True)
    else:
        print(f"  [{phase}] TYPE-47: NONE", flush=True)
    return len(t47_unix)


async def main():
    print(f"scanning for {ADDR} ...", flush=True)
    dev = await BleakScanner.find_device_by_address(ADDR, timeout=20.0)
    if dev is None:
        print("strap NOT found -- phone BT off + app quit? strap worn/awake?"); return
    async with BleakClient(dev) as c:
        await c.start_notify(CMD_FROM, cmd_cb)
        await c.start_notify(DATA, data_cb)
        await send(c, CommandNumber.GET_BATTERY_LEVEL); await asyncio.sleep(1.5)
        now = int(time.time())
        print(f"connected. now={now} ({time.strftime('%H:%M:%S', time.gmtime(now))} UTC)\n", flush=True)

        # Stop the R10/R11 type-43 live flood (the app does this on every connect; reversible) so the
        # offload histogram is clean and the radio isn't starved.
        await send(c, CommandNumber.SEND_R10_R11_REALTIME, b"\x00"); await asyncio.sleep(1.5)

        await send(c, CommandNumber.GET_DATA_RANGE); await asyncio.sleep(1.0)
        r = datarange[-1] if datarange else None
        uni = [w for w in (r or []) if 1_700_000_000 <= w <= 1_900_000_000]
        print("=== strap state ===", flush=True)
        print(f"  GET_DATA_RANGE unix markers: {[(w, time.strftime('%H:%M:%S', time.gmtime(w))) for w in uni]}", flush=True)
        print("  (newest marker ~now => logging; stale => stuck/frozen)\n", flush=True)

        # --- PHASE A: NO high-freq-sync ---
        print("=== PHASE A: SEND_HISTORICAL_DATA *without* high-freq-sync (15s, no-ack) ===", flush=True)
        reset()
        await send(c, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
        await asyncio.sleep(15)
        await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"\x00"); await asyncio.sleep(1.0)
        a47 = report("A")
        print("", flush=True)

        # --- PHASE B: WITH high-freq-sync (positive control) ---
        print("=== PHASE B: ENTER_HIGH_FREQ_SYNC + SEND_HISTORICAL_DATA (15s, no-ack) ===", flush=True)
        reset()
        entered = False
        try:
            await send(c, CommandNumber.ENTER_HIGH_FREQ_SYNC, b"")
            entered = True
            await send(c, CommandNumber.SEND_HISTORICAL_DATA, b"\x00")
            await asyncio.sleep(15)
        finally:
            await send(c, CommandNumber.ABORT_HISTORICAL_TRANSMITS, b"\x00"); await asyncio.sleep(0.3)
            if entered:
                await send(c, CommandNumber.EXIT_HIGH_FREQ_SYNC, b"\x00"); await asyncio.sleep(0.3)
        b47 = report("B")
        print("", flush=True)

        await c.disconnect()

    print("=== VERDICT ===", flush=True)
    if a47 > 0:
        print(f"  type-47 WITHOUT high-freq-sync = {a47}  ->  HIGH-FREQ-SYNC NOT NEEDED.", flush=True)
        print("  Plain SEND_HISTORICAL_DATA returns biometrics. Drop ENTER_HIGH_FREQ_SYNC (WHOOP-faithful).", flush=True)
    elif b47 > 0:
        print(f"  type-47 only WITH high-freq-sync (A={a47}, B={b47})  ->  HIGH-FREQ-SYNC REQUIRED here.", flush=True)
        print("  Keep it, but make it safe: bounded-duration ENTER + explicit EXIT on teardown.", flush=True)
    else:
        print(f"  NO type-47 in either phase (A={a47}, B={b47})  ->  INCONCLUSIVE.", flush=True)
        print("  Strap may be caught-up / not logging / stuck. Check GET_DATA_RANGE markers above", flush=True)
        print("  (stale newest = frozen/stuck); re-run once the strap is confirmed logging (~now).", flush=True)
    print("\nDONE (read-only; no ack/trim; high-freq-sync exited; flood left off as the app leaves it).", flush=True)


asyncio.run(main())
