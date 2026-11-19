#!/usr/bin/env python3
"""Diagnose why Zeroboot's vmstate parser can't anchor on this snapshot.

Zeroboot (src/vmm/vmstate.rs) finds the layout by searching for the IOAPIC base
0xFEC00000 as a u64, then validating EFER (0xD01 or 0x501) at a FIXED relative
offset (REF_EFER-REF_IOAPIC = 0x2564). If the real layout differs, detection
fails with "IOAPIC base address 0xFEC00000 not found".

This prints where the anchors actually are so we can fix the reference offsets
(or the detection). Usage:
  python3 tools/inspect_vmstate.py /tmp/zb-workdir-agent/snapshot/vmstate
"""
import struct
import sys

REF_IOAPIC = 0x0591
REF_EFER = 0x2AF5
REL_EFER = REF_EFER - REF_IOAPIC  # 0x2564, what the parser assumes

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/zb-workdir-agent/snapshot/vmstate"
data = open(path, "rb").read()
n = len(data)
u64 = lambda o: struct.unpack_from("<Q", data, o)[0]
u32 = lambda o: struct.unpack_from("<I", data, o)[0]

print(f"vmstate: {path}  size={n} (0x{n:x})\n")

io64 = [i for i in range(n - 8) if u64(i) == 0xFEC00000]
io32 = [i for i in range(n - 4) if u32(i) == 0xFEC00000]
print(f"0xFEC00000 as u64 (parser's test) at: {[hex(x) for x in io64]}")
print(f"0xFEC00000 as u32              at: {[hex(x) for x in io32]}")
if io32 and not io64:
    print("  -> stored as u32 (next 4 bytes nonzero): parser's r64 test can never match.")

efer = [i for i in range(n - 8) if u64(i) in (0xD01, 0x501)]
print(f"\nEFER (0xD01/0x501) as u64 at: {[hex(x) for x in efer]}")

print("\nFor each IOAPIC candidate, what the parser would check for EFER:")
for i in (io64 or io32):
    eo = i + REL_EFER
    if 0 <= eo <= n - 8:
        print(f"  ioapic@0x{i:x}  parser EFER@0x{eo:x} = 0x{u64(eo):x}  "
              f"(need 0xD01/0x501)")
    else:
        print(f"  ioapic@0x{i:x}  parser EFER@0x{eo:x} = OUT OF BOUNDS (file too small)")

print("\nImplied global shift (REF_IOAPIC - found_offset) for each candidate:")
for i in (io64 or io32):
    shift = REF_IOAPIC - i
    print(f"  ioapic@0x{i:x} -> shift={shift} (0x{shift & 0xffffffff:x}); "
          f"real EFER would be at found_ioapic+actual_rel")

# Show the 48 bytes around the first ioapic candidate for manual inspection.
if io64 or io32:
    c = (io64 or io32)[0]
    lo = max(0, c - 8)
    hi = min(n, c + 40)
    print(f"\nhexdump around first candidate 0x{c:x}:")
    for off in range(lo, hi, 16):
        chunk = data[off:off+16]
        print(f"  0x{off:04x}: " + " ".join(f"{b:02x}" for b in chunk))
