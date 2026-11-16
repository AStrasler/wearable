#!/usr/bin/env python3
"""Generate cross-language parity fixtures for the Swift WhoopProtocol library from
SYNTHETIC, protocol-valid frames — with NO dependency on any private capture files.

This is the open-source-safe fixture generator. It synthesizes WHOOP 4.0 BLE frames in
memory with OBVIOUSLY FAKE but protocol-valid biometric values (HR as a clean ramp,
round R-R intervals, timestamps anchored to a fixed synthetic epoch), runs the canonical
Python `whoop_protocol` decoder over them, and writes the 6 JSON fixtures the Swift
parity suite loads:

  Resources/frames.json                    [{"hex": "<complete frame hex>"}, ...]
  Resources/golden.json                    [parse_frame(frame) dict, ...]
  Resources/streams_golden.json            extract_streams(...) over the realtime frames
  Resources/historical_frames.json         [{"hex": ...}, ...] (type-43 raw replay)
  Resources/historical_golden.json         extract_historical_streams(...) over those
  Resources/biometric_streams_golden.json  extract_historical_streams(...) over V24 records

The Swift tests decode each frame with the SWIFT decoder and assert it equals the Python
golden. Parity holds BY CONSTRUCTION: golden is produced by the SAME Python parse_frame on
the SAME frames the Swift side decodes. The values being synthetic vs real does not affect
whether parity passes — it only guarantees no real biometric data is published.

HOW TO RUN (any cloner, public repo only):
  python3 -m venv /tmp/whoop_venv
  /tmp/whoop_venv/bin/pip install -e server/packages/whoop-protocol
  /tmp/whoop_venv/bin/python scripts/gen_synthetic_fixtures.py

`scripts/gen_golden.py` delegates to this module, so either entrypoint works.
"""
import json
import os
import struct
import zlib

from whoop_protocol.interpreter import (
    extract_historical_streams,
    extract_streams,
    parse_frame,
)

# Repo root = parent of this script's directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(_HERE)
_OUT_DIR = os.path.join(
    _REPO, "Packages", "WhoopProtocol", "Tests", "WhoopProtocolTests", "Resources")

# Fixed correlation refs for the cross-language stream parity fixture. These MUST match the
# constants in WhoopProtocolTests/StreamsParityTests.swift / StreamsTests.swift.
_DEVICE_CLOCK_REF = 31_538_447
_WALL_CLOCK_REF = 1_736_365_593

# Synthetic anchor epoch for HISTORICAL_DATA (V24) records: 2023-11-14T22:13:20Z. Obviously
# round and clearly not tied to any real capture. (REALTIME/EVENT timestamps use the device
# clock + wall ref above so the existing Swift constants keep working.)
_HIST_EPOCH = 1_700_000_000


# ---------------------------------------------------------------------------
# Frame assembly. A complete WHOOP frame is:
#   [0xAA][length u16 LE][crc8(length bytes)][type][seq][body...][crc32 u32 LE]
# where length = (#inner bytes) + 4, inner = frame[4:length] = type+seq+body, and the
# crc32 covers the inner bytes. `body` is everything from frame offset 6 onward, so callers
# place fields at their schema FRAME-absolute offsets simply by indexing the body from 6.
# ---------------------------------------------------------------------------

_CRC8_POLY = 0x07


def _crc8(buf: bytes) -> int:
    crc = 0
    for b in buf:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ _CRC8_POLY) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def build_frame(type_byte: int, seq: int, body: bytes) -> bytes:
    """Assemble a complete, CRC-valid frame. `body` = frame bytes from offset 6 onward."""
    inner = bytes([type_byte & 0xFF, seq & 0xFF]) + body
    length = len(inner) + 4
    header = bytes([0xAA]) + struct.pack("<H", length) + bytes([_crc8(struct.pack("<H", length))])
    crc32 = zlib.crc32(inner) & 0xFFFFFFFF
    return header + inner + struct.pack("<L", crc32)


def _le16(v: int) -> bytes:
    return struct.pack("<H", v & 0xFFFF)


def _le32(v: int) -> bytes:
    return struct.pack("<L", v & 0xFFFFFFFF)


def _f32(v: float) -> bytes:
    return struct.pack("<f", v)


# ---------------------------------------------------------------------------
# Per-type synthetic frame builders. All field offsets mirror schema/whoop_protocol.json.
# ---------------------------------------------------------------------------

def realtime_data(seq: int, device_ts: int, hr: int, rr_list=None, subsec: int = 0) -> bytes:
    """REALTIME_DATA (type 40). Body offsets (frame-absolute):
    ts@6 u32, subsec@10 u16, hr@12 u8, rr_count@13 u8, rr[i]@14+2i u16."""
    rr_list = rr_list or []
    body = bytearray()
    body += _le32(device_ts)          # 6..9
    body += _le16(subsec)             # 10..11
    body += bytes([hr & 0xFF])        # 12
    body += bytes([len(rr_list)])     # 13
    for rr in rr_list:                # 14...
        body += _le16(rr)
    # pad to a realistic minimum length so the frame isn't degenerate
    if len(body) < 18:
        body += bytes(18 - len(body))
    return build_frame(40, seq, bytes(body))


def event_simple(seq: int, event_num: int, event_ts: int) -> bytes:
    """EVENT (type 48). event@6 u8, (gap@7), event_timestamp@8 u32."""
    body = bytearray()
    body += bytes([event_num & 0xFF])  # 6
    body += bytes([0x00])              # 7 (reserved)
    body += _le32(event_ts)            # 8..11
    return build_frame(48, seq, bytes(body))


def event_battery(seq: int, event_ts: int, soc_pct: float, mv: int, charging: int) -> bytes:
    """EVENT BATTERY_LEVEL (event=3). Adds soc@17 u16(/10), mv@21 u16, charging@26 u8."""
    body = bytearray(28)  # frame offsets 6..33; body index = off-6
    body[0] = 3                                  # event @6
    body[2:6] = _le32(event_ts)                  # event_timestamp @8
    body[11:13] = _le16(int(round(soc_pct * 10)))  # soc @17
    body[15:17] = _le16(mv)                      # mv @21
    body[20] = charging & 1                      # charging @26
    return build_frame(48, seq, bytes(body))


def command_response_battery(seq: int, soc_pct: float) -> bytes:
    """COMMAND_RESPONSE (type 36), cmd=GET_BATTERY_LEVEL(26).
    resp_cmd@6, payload = frame[7:length]; pay[2:4] = soc*10."""
    cmd = 26
    pay = bytearray(8)
    pay[2:4] = _le16(int(round(soc_pct * 10)))
    body = bytes([cmd]) + bytes(pay)
    return build_frame(36, seq, body)


def metadata(seq: int, meta_type: int, payload: bytes = b"") -> bytes:
    """METADATA (type 49). meta_type@6 u8, then payload from @7."""
    return build_frame(49, seq, bytes([meta_type & 0xFF]) + payload)


def console_log(seq: int, text: str) -> bytes:
    """CONSOLE_LOGS (type 50). Text decoded from frame[11:length-1]."""
    raw = text.encode("utf-8")
    # body must place text starting at frame offset 11 -> body index 5; trailing byte trimmed.
    body = bytes(5) + raw + b"\x00"
    return build_frame(50, seq, body)


def raw_imu(seq: int, device_ts: int, hr: int, rr_list=None) -> bytes:
    """REALTIME_RAW_DATA (type 43), IMU variant (data_len == 1917 => length == 1924).
    cmd@6, record_hdr@7 u32, timestamp@11 u32, subsec@15 u16, unknown_hdr@17 u32,
    hr@21 u8, rr_count@22 u8, rr[i]@23+2i u16, accel/gyro i16 blocks, tail to 1917."""
    rr_list = rr_list or []
    body = bytearray(1917 - 6)  # frame offsets 6..1916; length will be (1911)+? -> data_len 1917
    body[0] = 0                                  # cmd @6
    body[1:5] = _le32(0xCAFEBABE)                # record_hdr @7
    body[5:9] = _le32(device_ts)                 # timestamp @11
    body[9:11] = _le16(0)                        # subsec @15
    body[11:15] = _le32(0)                       # unknown_hdr @17
    body[15] = hr & 0xFF                         # hr @21
    body[16] = len(rr_list)                      # rr_count @22
    for i, rr in enumerate(rr_list[:4]):         # rr @23...
        body[17 + i * 2:19 + i * 2] = _le16(rr)
    # Clean synthetic accel/gyro i16 samples (constant gravity on Z, zero gyro).
    def fill_axis(off_frame, value, n=100):
        base = off_frame - 6
        for i in range(n):
            body[base + i * 2:base + i * 2 + 2] = struct.pack("<h", value)
    fill_axis(89, 0)      # accelX
    fill_axis(289, 0)     # accelY
    fill_axis(489, 4096)  # accelZ ~ +1g (1/4096 LSB/g)
    fill_axis(692, 0)     # gyroX
    fill_axis(892, 0)     # gyroY
    fill_axis(1092, 0)    # gyroZ
    # body length must make data_len == length-7 == 1917 -> length == 1924 -> inner == 1920
    # inner = type+seq+body, so body must be 1918 bytes. frame offsets 6..1923.
    target_body = 1918
    if len(body) < target_body:
        body += bytes(target_body - len(body))
    else:
        body = body[:target_body]
    return build_frame(43, seq, bytes(body))


def raw_optical(seq: int, device_ts: int) -> bytes:
    """REALTIME_RAW_DATA (type 43), optical variant (data_len == 1921 => length == 1928).
    ppg s24 samples at off 42, stride 4, 419 samples."""
    body = bytearray()
    body += bytes([0])            # cmd @6
    body += _le32(0xCAFEBABE)     # record_hdr @7
    body += _le32(device_ts)      # timestamp @11
    body += _le16(0)              # subsec @15
    body += _le32(0)              # unknown_hdr @17
    # config header @21..41 (frame), then ppg @42
    while len(body) < (42 - 6):
        body += b"\x00"
    # 419 PPG samples: clean synthetic ~1 Hz sine, s24 little-endian + aux byte.
    import math
    for i in range(419):
        v = int(2000 * math.sin(i * 2 * math.pi / 50))
        u = v & 0xFFFFFF
        body += bytes([u & 0xFF, (u >> 8) & 0xFF, (u >> 16) & 0xFF, 0x00])
    # data_len must be 1921 -> length 1928 -> inner 1924 -> body 1922 (frame 6..1927)
    target_body = 1922
    if len(body) < target_body:
        body += bytes(target_body - len(body))
    else:
        body = body[:target_body]
    return build_frame(43, seq, bytes(body))


def historical_v24(seq_version: int, unix_ts: int, hr: int, rr_list,
                   spo2_red: int, spo2_ir: int, skin_temp: int, resp_raw: int,
                   ppg_green: int = 1500, ppg_red_ir: int = 30000,
                   skin_contact: int = 64, signal_quality: int = 3000) -> bytes:
    """HISTORICAL_DATA (type 47) V24 record. seq byte == version (24). All field offsets
    mirror schema versions["24"]. Gravity is a synthetic unit vector (mag ~1g)."""
    rr_list = rr_list or []
    body = bytearray(84)  # frame offsets 6..89; body index = off-6
    body[11 - 6:11 - 6 + 4] = _le32(unix_ts)          # unix @11
    body[21 - 6] = hr & 0xFF                           # heart_rate @21
    body[22 - 6] = len(rr_list)                        # rr_count @22
    for i, rr in enumerate(rr_list[:4]):               # rr @23...
        body[23 - 6 + i * 2:23 - 6 + i * 2 + 2] = _le16(rr)
    body[33 - 6:33 - 6 + 2] = _le16(ppg_green)         # ppg_green @33
    body[35 - 6:35 - 6 + 2] = _le16(ppg_red_ir)        # ppg_red_ir @35
    # gravity unit vector (synthetic): mostly +Z, small X/Y => magnitude ~1.0
    gx, gy, gz = 0.05, 0.10, 0.993734
    body[40 - 6:40 - 6 + 4] = _f32(gx)                 # gravity_x @40
    body[44 - 6:44 - 6 + 4] = _f32(gy)                 # gravity_y @44
    body[48 - 6:48 - 6 + 4] = _f32(gz)                 # gravity_z @48
    body[55 - 6] = skin_contact & 0xFF                 # skin_contact @55
    body[56 - 6:56 - 6 + 4] = _f32(gx)                 # gravity2_x @56
    body[60 - 6:60 - 6 + 4] = _f32(gy)                 # gravity2_y @60
    body[64 - 6:64 - 6 + 4] = _f32(gz)                 # gravity2_z @64
    body[68 - 6:68 - 6 + 2] = _le16(spo2_red)          # spo2_red @68
    body[70 - 6:70 - 6 + 2] = _le16(spo2_ir)           # spo2_ir @70
    body[72 - 6:72 - 6 + 2] = _le16(skin_temp)         # skin_temp_raw @72
    body[74 - 6:74 - 6 + 2] = _le16(800)               # ambient @74
    body[76 - 6:76 - 6 + 2] = _le16(100)               # led_drive_1 @76
    body[78 - 6:78 - 6 + 2] = _le16(100)               # led_drive_2 @78
    body[80 - 6:80 - 6 + 2] = _le16(resp_raw)          # resp_rate_raw @80
    body[82 - 6:82 - 6 + 2] = _le16(signal_quality)    # signal_quality @82
    return build_frame(47, seq_version, bytes(body))


# ---------------------------------------------------------------------------
# Synthetic data series. Obviously-fake clean shapes.
# ---------------------------------------------------------------------------

def _hr_ramp(i: int, lo: int = 60, hi: int = 80) -> int:
    """A clean triangular ramp 60..80..60 — visibly synthetic."""
    span = hi - lo
    period = span * 2
    x = i % period
    return lo + (x if x <= span else period - x)


def build_all():
    frames = []            # [{"hex": ...}] for frames.json
    golden = []            # [parse_frame dict] for golden.json
    realtime_parsed = []   # subset that feeds extract_streams
    biometric_parsed = []  # V24 records that feed biometric streams

    def emit(frame, collect_rt=False, collect_bio=False):
        out = parse_frame(frame)
        if not out.get("ok"):
            raise RuntimeError(f"synthetic frame failed to parse: {frame.hex()}")
        frames.append({"hex": frame.hex()})
        golden.append(out)
        if collect_rt:
            realtime_parsed.append(out)
        if collect_bio:
            biometric_parsed.append(out)
        return out

    # --- REALTIME_DATA: 20 frames, HR ramp, every other carries one R-R interval ---
    for i in range(20):
        ts = _DEVICE_CLOCK_REF + i
        hr = _hr_ramp(i)
        rr = [60000 // hr] if i % 2 == 0 else []  # round-ish R-R in ms
        emit(realtime_data(seq=i % 256, device_ts=ts, hr=hr, rr_list=rr, subsec=i * 1000),
             collect_rt=True)

    # --- EVENT: a few distinct simple events + dense BATTERY_LEVEL events ---
    emit(event_simple(seq=1, event_num=46, event_ts=_WALL_CLOCK_REF), collect_rt=True)  # RAW_ON
    emit(event_simple(seq=2, event_num=9, event_ts=_WALL_CLOCK_REF + 5), collect_rt=True)  # WRIST_ON
    emit(event_simple(seq=3, event_num=15, event_ts=_WALL_CLOCK_REF + 10), collect_rt=True)  # BOOT
    for i in range(6):
        # Fractional SoC (like the real 0.1%-resolution gauge) so the value round-trips as a
        # JSON float on BOTH sides; whole numbers serialize as ints and break .double parity.
        emit(event_battery(seq=10 + i, event_ts=_WALL_CLOCK_REF + 60 * (i + 1),
                           soc_pct=50.5 + i, mv=3800 + i * 10, charging=i % 2),
             collect_rt=True)

    # --- COMMAND_RESPONSE: GET_BATTERY_LEVEL ---
    emit(command_response_battery(seq=20, soc_pct=42.5), collect_rt=True)

    # --- METADATA: HISTORY_START / END / COMPLETE ---
    emit(metadata(seq=0, meta_type=1))  # HISTORY_START
    end_pay = _le32(_HIST_EPOCH) + _le16(0) + _le32(0) + _le32(12345)
    emit(metadata(seq=0, meta_type=2, payload=end_pay))  # HISTORY_END
    emit(metadata(seq=0, meta_type=3))  # HISTORY_COMPLETE

    # --- CONSOLE_LOGS ---
    emit(console_log(seq=0, text="synthetic console log line"))

    # --- REALTIME_RAW_DATA: one IMU + one optical (cover both variants) ---
    emit(raw_imu(seq=5, device_ts=_DEVICE_CLOCK_REF, hr=70, rr_list=[857]))
    emit(raw_optical(seq=6, device_ts=_DEVICE_CLOCK_REF + 1))

    # --- HISTORICAL_DATA V24: exactly 60 records (BiometricStreamsParityTests requires 60),
    #     all with HR>0 so every biometric stream is populated. ---
    for i in range(60):
        unix_ts = _HIST_EPOCH + i
        hr = _hr_ramp(i, 60, 70)
        rr = [60000 // hr]
        emit(historical_v24(
            seq_version=24, unix_ts=unix_ts, hr=hr, rr_list=rr,
            spo2_red=18000 + i, spo2_ir=17000 + i,
            skin_temp=900 + i, resp_raw=3000 + i,
            ppg_green=1500 + i, ppg_red_ir=30000 + i),
            collect_bio=True)

    return frames, golden, realtime_parsed, biometric_parsed


def main():
    os.makedirs(_OUT_DIR, exist_ok=True)
    frames, golden, realtime_parsed, biometric_parsed = build_all()

    type_counts = {}
    for g in golden:
        tn = g["type_name"]
        type_counts[tn] = type_counts.get(tn, 0) + 1

    # Coverage guard: the parity suite must exercise every core packet type.
    expected = {"REALTIME_DATA", "COMMAND_RESPONSE", "EVENT", "METADATA",
                "CONSOLE_LOGS", "REALTIME_RAW_DATA", "HISTORICAL_DATA"}
    missing = expected - set(type_counts)
    if missing:
        raise RuntimeError(f"synthetic parity fixture missing packet types: {sorted(missing)}")
    historical_count = sum(1 for g in golden if g["type_name"] == "HISTORICAL_DATA")
    if historical_count != 60:
        raise RuntimeError("BiometricStreamsParityTests requires exactly 60 V24 records")

    with open(os.path.join(_OUT_DIR, "frames.json"), "w") as fh:
        json.dump(frames, fh, indent=0)
    with open(os.path.join(_OUT_DIR, "golden.json"), "w") as fh:
        json.dump(golden, fh, indent=0)

    # Stream parity over the realtime/event/battery frames.
    streams = extract_streams(realtime_parsed, _DEVICE_CLOCK_REF, _WALL_CLOCK_REF)
    if not streams["hr"]:
        raise RuntimeError("stream fixture produced no HR rows")
    if not streams["events"]:
        raise RuntimeError("stream fixture produced no EVENT rows")
    if not streams["battery"]:
        raise RuntimeError("stream fixture produced no battery rows")
    with open(os.path.join(_OUT_DIR, "streams_golden.json"), "w") as fh:
        json.dump(streams, fh, indent=0)

    # Historical stream parity (type-43 raw replay path): reuse the synthetic IMU frame plus
    # a couple of EVENT frames so historical_golden has HR + (passthrough) events.
    hist_frames = []
    hist_parsed = []
    for f in (raw_imu(seq=5, device_ts=_DEVICE_CLOCK_REF, hr=70, rr_list=[857]),
              raw_imu(seq=6, device_ts=_DEVICE_CLOCK_REF + 1, hr=72, rr_list=[833]),
              event_simple(seq=7, event_num=46, event_ts=_WALL_CLOCK_REF)):
        pr = parse_frame(f)
        if pr.get("ok") and pr["type_name"] in ("REALTIME_RAW_DATA", "EVENT"):
            hist_frames.append({"hex": f.hex()})
            hist_parsed.append(pr)
    hstreams = extract_historical_streams(hist_parsed, _DEVICE_CLOCK_REF, _WALL_CLOCK_REF)
    if not hstreams["hr"]:
        raise RuntimeError("historical fixture produced no HR rows")
    with open(os.path.join(_OUT_DIR, "historical_frames.json"), "w") as fh:
        json.dump(hist_frames, fh)
    with open(os.path.join(_OUT_DIR, "historical_golden.json"), "w") as fh:
        json.dump(hstreams, fh)

    # Biometric (type-47 V24) stream parity.
    bstreams = extract_historical_streams(biometric_parsed, _DEVICE_CLOCK_REF, _WALL_CLOCK_REF)
    if not bstreams["hr"]:
        raise RuntimeError("biometric fixture produced no HR rows")
    if not bstreams["spo2"]:
        raise RuntimeError("biometric fixture produced no SpO2 rows")
    if not bstreams["gravity"]:
        raise RuntimeError("biometric fixture produced no gravity rows")
    with open(os.path.join(_OUT_DIR, "biometric_streams_golden.json"), "w") as fh:
        json.dump(bstreams, fh, indent=0)

    print(f"wrote {len(frames)} synthetic frames to {_OUT_DIR}")
    for tn in sorted(type_counts):
        print(f"  {tn}: {type_counts[tn]}")
    print(f"streams_golden: hr={len(streams['hr'])} rr={len(streams['rr'])} "
          f"events={len(streams['events'])} battery={len(streams['battery'])}")
    print(f"historical_golden: frames={len(hist_frames)} hr={len(hstreams['hr'])} rr={len(hstreams['rr'])}")
    print(f"biometric_streams_golden: hr={len(bstreams['hr'])} rr={len(bstreams['rr'])} "
          f"spo2={len(bstreams['spo2'])} skin_temp={len(bstreams['skin_temp'])} "
          f"resp={len(bstreams['resp'])} gravity={len(bstreams['gravity'])}")


if __name__ == "__main__":
    main()
