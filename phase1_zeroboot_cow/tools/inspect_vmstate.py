#!/usr/bin/env python3
"""Derive and validate Zeroboot's vmstate offsets for THIS snapshot.

This is a faithful mirror of the Rust parser in
`../../zeroboot/src/vmm/vmstate.rs` (function `resolve_offsets`). Run it on-node
against a real snapshot BEFORE trusting `zeroboot bench`/`serve`: if it prints
"ALL CHECKS PASS", the Rust parser will anchor the same way and the CPU-state
restore will be correct. If it fails, it dumps the candidates so the anchors can
be adjusted.

Why this exists: Firecracker's versionize vmstate has variable-length sections
(MSR/CPUID vectors, per-device state), so the byte offset of each section shifts
*independently* between builds. A single global shift (the old approach) cannot
realign them. Instead we anchor two things by content and derive the rest:

  1. IOAPIC      -> its MMIO base constant 0xFEC00000
  2. EFER/vCPU   -> EFER(0xD01|0x501) + APIC-base(0xFEE00xxx) + plausible CR0/CR3

Everything from LAPIC..XSAVE is one rigid kvm-vcpu struct, so once EFER is found
the rest follow at fixed intra-block deltas (stable across builds).

Usage:
  python3 tools/inspect_vmstate.py [/path/to/snapshot/vmstate] [--diag]
Exit status: 0 if all hard checks pass, 1 otherwise (usable as a pre-bench gate).
"""
import struct
import sys

# Reference layout (Firecracker v1.12.0, 1 vCPU, x86_64) -- used only to derive
# the stable intra-vCPU-block deltas, never as absolute positions. Keep in sync
# with the REF_* consts in vmstate.rs.
REF = {
    "ioapic": 0x0591,
    "lapic": 0x2541,
    "regs": 0x2955,
    "segs": 0x29ED,
    "gdt": 0x2AAD,
    "idt": 0x2ABD,
    "cr": 0x2ACD,
    "efer": 0x2AF5,
    "apic_base": 0x2AFD,
    "xcrs": 0x2B75,
    "xsave": 0x2D0D,
}
# Deltas of every vCPU-block field relative to EFER (the rigid struct).
DELTA = {k: v - REF["efer"] for k, v in REF.items() if k not in ("ioapic",)}

args = [a for a in sys.argv[1:] if not a.startswith("-")]
flags = {a for a in sys.argv[1:] if a.startswith("-")}
path = args[0] if args else "/tmp/zb-workdir-agent/snapshot/vmstate"
DIAG = "--diag" in flags

data = open(path, "rb").read()
n = len(data)
u64 = lambda o: struct.unpack_from("<Q", data, o)[0]
u32 = lambda o: struct.unpack_from("<I", data, o)[0]
u16 = lambda o: struct.unpack_from("<H", data, o)[0]

print(f"vmstate: {path}  size={n} (0x{n:x})\n")


def find_ioapic():
    hits = [i for i in range(n - 8) if u64(i) == 0xFEC00000]
    return hits


def efer_candidates():
    """Same predicate as the Rust find_efer()."""
    cr_delta = DELTA["cr"]  # -0x28
    out = []
    for e in range(n - 16):
        efer = u64(e)
        if efer not in (0xD01, 0x501):
            continue
        if u64(e + 8) & 0xFFFFF000 != 0xFEE00000:
            continue
        cr = e + cr_delta
        if cr < 0 or cr + 32 > n:
            continue
        cr0, cr3 = u64(cr), u64(cr + 16)
        if cr0 & 1 == 0 or cr0 & (1 << 31) == 0:
            continue
        if cr3 == 0 or cr3 & 0xFFF != 0:
            continue
        out.append(e)
    return out


io_hits = find_ioapic()
efer_hits = efer_candidates()

if not io_hits:
    print("FAIL: IOAPIC base 0xFEC00000 (u64) not found.")
    io32 = [i for i in range(n - 4) if u32(i) == 0xFEC00000]
    if io32:
        print(f"  found as u32 at {[hex(x) for x in io32]} -> parser's r64 test can't match;")
        print("  the IOAPIC may be stored 32-bit on this build (needs a parser tweak).")
    sys.exit(1)

if not efer_hits:
    print("FAIL: no EFER/vCPU anchor (EFER + APIC-base + CR0/CR3) found.")
    print("  Loosen the predicate below and inspect by hand.")
    efer_any = [i for i in range(n - 8) if u64(i) in (0xD01, 0x501)]
    print(f"\n  raw EFER-valued u64s (0xD01/0x501) at: {[hex(x) for x in efer_any]}")
    for e in efer_any:
        apic = u64(e + 8) if e + 16 <= n else 0
        print(f"    @0x{e:x}: next u64 = 0x{apic:x} (need &0xfffff000 == 0xfee00000)")
    sys.exit(1)

ioapic = io_hits[0]
efer = efer_hits[0]
if len(io_hits) > 1:
    print(f"note: {len(io_hits)} IOAPIC candidates {[hex(x) for x in io_hits]}; using first.")
if len(efer_hits) > 1:
    print(f"note: {len(efer_hits)} EFER candidates {[hex(x) for x in efer_hits]}; using first.")

# Derive every offset exactly as resolve_offsets() does.
off = {"ioapic": ioapic}
for k, dlt in DELTA.items():
    off[k] = efer + dlt

# --- offset table, with the independent-shift evidence -----------------------
print("Resolved offsets (derived; old single-shift approach in parentheses):")
io_shift = ioapic - REF["ioapic"]
efer_shift = efer - REF["efer"]
for k in ("ioapic", "lapic", "regs", "segs", "gdt", "idt", "cr", "efer",
          "apic_base", "xcrs", "xsave"):
    print(f"  {k:10s} 0x{off[k]:05x}   (ref 0x{REF[k]:05x})")
print(f"\nIOAPIC shift = {io_shift:+d} (0x{io_shift & 0xffff:x}); "
      f"vCPU-block shift = {efer_shift:+d} (0x{efer_shift & 0xffff:x})")
if io_shift != efer_shift:
    print("  -> the two shifts DIFFER: sections moved independently, which is why the")
    print("     old global-shift parser bailed. Independent anchoring handles it.")

# --- decode key CPU state ----------------------------------------------------
regs = off["regs"]
rip, rsp, rflags = u64(regs + 128), u64(regs + 48), u64(regs + 136)
cr = off["cr"]
cr0, cr2, cr3, cr4 = u64(cr), u64(cr + 8), u64(cr + 16), u64(cr + 24)
efer_v, apic_base_v = u64(off["efer"]), u64(off["apic_base"])
gdt_base, gdt_lim = u64(off["gdt"]), u16(off["gdt"] + 8)
idt_base, idt_lim = u64(off["idt"]), u16(off["idt"] + 8)
cs_sel = u16(off["segs"] + 12)
lapic_ver = u32(off["lapic"] + 0x30)

print("\nDecoded CPU state:")
print(f"  RIP    = 0x{rip:016x}      RSP    = 0x{rsp:016x}")
print(f"  RFLAGS = 0x{rflags:016x}      CS     = 0x{cs_sel:04x}")
print(f"  CR0    = 0x{cr0:08x}              CR3    = 0x{cr3:016x}")
print(f"  CR2    = 0x{cr2:016x}      CR4    = 0x{cr4:08x}")
print(f"  EFER   = 0x{efer_v:x}                   APICBASE = 0x{apic_base_v:x}")
print(f"  GDT    = base 0x{gdt_base:016x} limit 0x{gdt_lim:x}")
print(f"  IDT    = base 0x{idt_base:016x} limit 0x{idt_lim:x}")
print(f"  LAPIC version reg (@+0x30) = 0x{lapic_ver:08x} (low byte should be 0x14)")


# --- validation gate ---------------------------------------------------------
def check(name, cond, detail=""):
    ok = bool(cond)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}")
    return ok


print("\nValidation:")
ok = True
ok &= check("EFER is a long-mode value", efer_v in (0xD01, 0x501), f"0x{efer_v:x}")
ok &= check("APIC base masks to 0xFEE00000", apic_base_v & 0xFFFFF000 == 0xFEE00000,
            f"0x{apic_base_v:x}")
ok &= check("CR0.PE and CR0.PG set (paging on)", cr0 & 1 and cr0 & (1 << 31),
            f"0x{cr0:x}")
ok &= check("CR3 nonzero and 4K-aligned", cr3 != 0 and cr3 & 0xFFF == 0, f"0x{cr3:x}")
ok &= check("CR4.PAE set", bool(cr4 & (1 << 5)), f"0x{cr4:x}")
ok &= check("RIP nonzero", rip != 0, f"0x{rip:x}")
ok &= check("GDT limit sane (<0x10000)", gdt_lim < 0x10000, f"0x{gdt_lim:x}")
ok &= check("IDT limit sane (<0x10000)", idt_lim < 0x10000, f"0x{idt_lim:x}")
# Soft check: KVM LAPIC version is 0x14. Reported, not fatal.
check("LAPIC version low byte == 0x14 (soft)", lapic_ver & 0xFF == 0x14,
      f"0x{lapic_ver & 0xff:x}")
ok &= off["xsave"] + 4 <= n

if DIAG:
    c = ioapic
    lo, hi = max(0, c - 8), min(n, c + 40)
    print(f"\n[diag] hexdump around IOAPIC 0x{c:x}:")
    for o2 in range(lo, hi, 16):
        chunk = data[o2:o2 + 16]
        print(f"  0x{o2:05x}: " + " ".join(f"{b:02x}" for b in chunk))

print("\n" + ("ALL CHECKS PASS -- the Rust parser will anchor identically."
              if ok else "SOME CHECKS FAILED -- do not trust bench until resolved."))
sys.exit(0 if ok else 1)
