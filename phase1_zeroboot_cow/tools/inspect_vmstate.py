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


def cr3_plausible(cr3, cr4):
    """Mirror of Rust cr3_plausible: page-table base (bits 12..51) nonzero,
    reserved bits 52..63 clear; with CR4.PCIDE (bit 17) the low 12 bits are the
    PCID (Linux uses nonzero PCIDs), else only PWT|PCD (0x18) may be set."""
    BASE = 0x000FFFFFFFFFF000
    if (cr3 & BASE) == 0 or (cr3 & ~(BASE | 0xFFF) & 0xFFFFFFFFFFFFFFFF):
        return False
    return bool(cr4 & (1 << 17)) or (cr3 & 0xFFF & ~0x18) == 0


def efer_candidates():
    """Same predicate as the Rust find_efer(). NOTE: Python's `&` binds looser
    than `==`/`!=` (the opposite of Rust), so every mixed expression is
    parenthesised explicitly -- do not remove these parens."""
    cr_delta = DELTA["cr"]  # -0x28
    out = []
    for e in range(n - 16):
        efer = u64(e)
        if efer not in (0xD01, 0x501):
            continue
        if (u64(e + 8) & 0xFFFFF000) != 0xFEE00000:
            continue
        cr = e + cr_delta
        if cr < 0 or cr + 32 > n:
            continue
        cr0, cr3, cr4 = u64(cr), u64(cr + 16), u64(cr + 24)
        if (cr0 & 1) == 0 or (cr0 & (1 << 31)) == 0:
            continue
        if not cr3_plausible(cr3, cr4):
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
    print("  Raw EFER-valued u64s and what the derived fields would be for each:")
    efer_any = [i for i in range(n - 8) if u64(i) in (0xD01, 0x501)]
    for e in efer_any:
        apic = u64(e + 8) if e + 16 <= n else 0
        apic_ok = (apic & 0xFFFFF000) == 0xFEE00000
        print(f"\n  @0x{e:x}: EFER=0x{u64(e):x}  apic_base(+8)=0x{apic:x} "
              f"[{'ok' if apic_ok else 'NO'}]")
        cr = e + DELTA["cr"]
        if 0 <= cr and cr + 32 <= n:
            cr0, cr2, cr3, cr4 = u64(cr), u64(cr + 8), u64(cr + 16), u64(cr + 24)
            print(f"      derived CR @0x{cr:x}: CR0=0x{cr0:x} CR2=0x{cr2:x} "
                  f"CR3=0x{cr3:x} CR4=0x{cr4:x}")
            print(f"      CR0.PE|PG={'ok' if (cr0 & 1) and (cr0 & (1 << 31)) else 'NO'}"
                  f"  CR3 plausible(PCID-aware)="
                  f"{'ok' if cr3_plausible(cr3, cr4) else 'NO'}")
        r = e + DELTA["regs"]
        if 0 <= r and r + 144 <= n:
            print(f"      derived RIP @0x{r + 128:x} = 0x{u64(r + 128):x}  "
                  f"RSP = 0x{u64(r + 48):x}")
    print("\n  If the EFER+apic pair is right (ok) but CR looks wrong, the CR delta")
    print("  for this build differs from the reference -- paste this output back.")
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
ok &= check("APIC base masks to 0xFEE00000", (apic_base_v & 0xFFFFF000) == 0xFEE00000,
            f"0x{apic_base_v:x}")
ok &= check("CR0.PE and CR0.PG set (paging on)", (cr0 & 1) and (cr0 & (1 << 31)),
            f"0x{cr0:x}")
ok &= check("CR3 page-table base nonzero (PCID-aware)", cr3_plausible(cr3, cr4),
            f"0x{cr3:x} base=0x{cr3 & 0x000FFFFFFFFFF000:x} "
            f"pcid=0x{cr3 & 0xFFF:x} PCIDE={'on' if cr4 & (1 << 17) else 'off'}")
ok &= check("CR4.PAE set", bool(cr4 & (1 << 5)), f"0x{cr4:x}")
ok &= check("RIP nonzero", rip != 0, f"0x{rip:x}")
ok &= check("GDT limit sane (<0x10000)", gdt_lim < 0x10000, f"0x{gdt_lim:x}")
ok &= check("IDT limit sane (<0x10000)", idt_lim < 0x10000, f"0x{idt_lim:x}")
# Soft check: KVM LAPIC version is 0x14. Reported, not fatal.
check("LAPIC version low byte == 0x14 (soft)", (lapic_ver & 0xFF) == 0x14,
      f"0x{lapic_ver & 0xff:x}")
ok &= off["xsave"] + 4 <= n


# --- MSR chunks (mirror of Rust find_msr_chunks / parse_msrs, ksm patch 0004) ---
def msr_chunks():
    """u64 8, u64 n, u64 n, then n x {u64 16, u32 index, u32 0, u64 data}."""
    rec = lambda o: o + 24 <= n and u64(o) == 16 and u32(o + 12) == 0
    out, o = [], 0
    while o + 24 <= n:
        cnt = u64(o + 8)
        if u64(o) == 8 and 1 <= cnt <= 1024 and u64(o + 16) == cnt and \
                all(rec(o + 24 + 24 * k) for k in range(cnt)):
            out.append((o + 24, cnt))
            o += 24 + 24 * cnt
        else:
            o += 1
    return out


msrs = {}
chunks = msr_chunks()
for start, cnt in chunks:
    for k in range(cnt):
        e = start + 24 * k + 8
        msrs.setdefault(u32(e), u64(e + 8))
tsc, deadline, st = msrs.get(0x10), msrs.get(0x6e0), msrs.get(0x4B564D01)
print(f"\nMSR chunks: {[(hex(a), c) for a, c in chunks]}  ({len(msrs)} MSRs)")
print(f"  IA32_TSC = {tsc}   TSC_DEADLINE = {deadline}   KVM_SYSTEM_TIME_NEW = "
      f"{hex(st) if st is not None else None}")
ok &= check("MSR array found (>= 8 entries)", sum(c for _, c in chunks) >= 8)
ok &= check("IA32_TSC present and nonzero (fork clock continuity)", bool(tsc))
# Soft: an armed deadline should be shortly after the snapshot TSC (< ~10 s).
check("TSC_DEADLINE just after TSC (soft)",
      deadline is not None and tsc and (deadline == 0 or 0 <= deadline - tsc < 10 * 3_000_000_000),
      f"delta={deadline - tsc if deadline and tsc else None} cycles")
ok &= check("KVM_SYSTEM_TIME_NEW enabled (bit 0)", st is not None and st & 1 == 1)

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
