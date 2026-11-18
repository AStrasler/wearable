"""Analyze the labeled optical session: per phase, decode the s24 PPG channel + the unknown
byte[3] aux, and characterize finger/dark/light/air/finger2 to (a) confirm the PPG channel and
(b) figure out what byte[3] encodes (exposure/saturation/LED-state?).

data_hex = the `data` region (after type/seq/cmd). PPG array: data offset 35, stride 4,
sample = bytes[0:3] signed-24 LE, byte[3] = aux. ~419 samples/packet.
"""
import json
import statistics
import struct
import sys
from collections import Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else "fixtures/optical_session.jsonl"
OFF, STRIDE, NSAMP = 35, 4, 419


def s24(b):
    v = b[0] | (b[1] << 8) | (b[2] << 16)
    return v - 0x1000000 if v & 0x800000 else v


def decode_ppg(data):
    vals, aux = [], []
    for i in range(NSAMP):
        p = OFF + i * STRIDE
        if p + 4 > len(data):
            break
        vals.append(s24(data[p:p + 3]))
        aux.append(data[p + 3])
    return vals, aux


def fft_peak_hz(vals, fs):
    # crude DFT magnitude over 0.5-3 Hz to find pulsatility fundamental
    import math
    n = len(vals)
    if n < 32:
        return None, 0
    mean = sum(vals) / n
    x = [v - mean for v in vals]
    best_f, best_mag = None, 0
    f = 0.5
    while f <= 3.0:
        re = sum(x[k] * math.cos(-2 * math.pi * f * k / fs) for k in range(n))
        im = sum(x[k] * math.sin(-2 * math.pi * f * k / fs) for k in range(n))
        mag = (re * re + im * im) ** 0.5
        if mag > best_mag:
            best_mag, best_f = mag, f
        f += 0.05
    return best_f, best_mag


def main():
    phases = {}
    for line in open(PATH):
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if r.get("datalen") != 1921:
            continue
        phases.setdefault(r["phase"], []).append(r)

    print("optical (1921) packets/phase:", {k: len(v) for k, v in phases.items()})
    # also report 1917 counts (whether optical stream kept pairing)
    p17 = Counter()
    for line in open(PATH):
        try:
            r = json.loads(line)
            if r.get("datalen") == 1917:
                p17[r["phase"]] += 1
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
            continue
    print("IMU (1917) packets/phase:   ", dict(p17))

    for ph, recs in phases.items():
        allv, allaux = [], []
        # use a middle packet for pulsatility (one packet = ~419 samples ~ 1s)
        for r in recs:
            v, a = decode_ppg(bytes.fromhex(r["data_hex"]))
            allv.extend(v)
            allaux.extend(a)
        if not allv:
            print(f"\n[{ph}] no PPG samples")
            continue
        ac = statistics.pstdev(allv) if len(allv) > 1 else 0
        rng = max(allv) - min(allv)
        dc = statistics.mean(allv)
        # pulsatility from the first full packet
        v0, _ = decode_ppg(bytes.fromhex(recs[len(recs) // 2]["data_hex"]))
        pf, pm = fft_peak_hz(v0, 419 / 0.96)
        auxc = Counter(allaux)
        top_aux = auxc.most_common(6)
        print(f"\n[{ph}] pkts={len(recs)} samples={len(allv)}")
        print(f"  PPG: DC(mean)={dc:.0f}  AC(std)={ac:.0f}  range={rng}")
        print(f"  pulsatility: peak~{pf} Hz (mag {pm:.0f})  => {pf*60:.0f} bpm" if pf else "  pulsatility: n/a")
        print(f"  byte[3] aux top values: {top_aux}")
        print(f"  byte[3] distinct={len(auxc)} min={min(allaux)} max={max(allaux)}")


if __name__ == "__main__":
    main()
