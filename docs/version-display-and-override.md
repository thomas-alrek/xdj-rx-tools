# UTILITY → VERSION No. shows `Ver --.--` — where it comes from, and how to override it

## Symptom

In the on-device menu **UTILITY → VERSION INFORMATION** (bottom row), the
emulator shows:

```
Ver --.--
```

A real XDJ-RX shows something like `Ver 1.xx` (main) / a sub version. This is a
**cosmetic emulation gap**, not a fault — nothing depends on it. This note
records exactly what produces the string so that a future *custom firmware*
(a patched `rbp`, or a boot-time seed) can override the displayed version
**without patching any MCU/panel code**.

## Where the string is built

Handler: **`FUN_00223140`** in `rbp` (the "VERSION INFORMATION / Welcome
message" state machine; the retry string it uses is at `0x420dd4`, and this is
reached as **case 5** of that handler).

Case 5 formats a single display line:

```
snprintf(buf, "M:%s S:%x.%x", <main_ver_string>, <sub_ver>>>8, <sub_ver>&0xff)
```

so the full field is `M:<main> S:<sub>`. The LCD's **`Ver --.--`** is the
**M:** (main) half — i.e. the main-version *string* is literally `--.--`.

### MAIN version (`M:`) — read from a file

The main version string is read at display time from the guest files:

| Guest path       | Role                                             |
|------------------|--------------------------------------------------|
| `/tmp/smdj2.rev` | main revision record → the `M:` string           |
| `/tmp/smdj.rev`  | companion rev buffer; empty → `"Can't get version."` fallback |

On the running emulator `/tmp/smdj2.rev` literally contains:

```
--.-- [4206:4195:]
```

The `4206:4195` are the panel-MCU **model / ID** numbers (these *did* come
through), but the **version fields after the `:` are blank**, so the parsed
version string is `--.--`. That blank-but-present record is exactly why the
screen reads `Ver --.--` instead of the "retry"/"Can't get version" fallbacks.

`rbp` only ever **reads** these paths in the version handler. (The one other
reference to the `/tmp/smdj2.rev` string literal, in `FUN_001aefac`, is a
config-schema/path-table *registration*, not a writer.) Whatever seeds the file
at boot with the `--.-- [4206:4195:]` content comes from the panel-MCU
StaticInfomation handshake path, which the emulator does not fully service.

### SUB version (`S:`) — from the StaticInfomation handshake

The sub version is the 16-bit word **`DAT_0123e054`** (init `0xffff`). It is
filled by the **sub-ucom `StaticInfomation` handshake** (RTTI
`N2ui16StaticInfomationE`) — a query/response the panel MCU answers on the SPI
link. While it is still `0xffff`, case 5 prints the retry form
(`"...StaticInfomation is not completed. retry..."` → `S:Read Again`).

**The QEMU sub-ucom harness is a control-input stub only.** It injects the
100-byte button/analog frame (see [subucom-frame-map.md](subucom-frame-map.md))
but does **not** answer the StaticInfomation/version query, so `DAT_0123e054`
stays `0xffff` and the file keeps its blank version. That is the root of the
`--.--`.

## Overriding the version WITHOUT patching MCU code (future custom firmware)

Goal: when we ship a patched-`rbp` "custom firmware", be able to set the
displayed version freely, without touching any MCU/panel-MCU microcode and
without reverse-patching the StaticInfomation protocol. Three routes, cheapest
first:

1. **Seed the rev files (no code at all).** Write a proper record to
   `/tmp/smdj.rev` and `/tmp/smdj2.rev` on the guest before the VERSION screen
   is opened (a boot hook / init snippet / the QEMU harness dropping the file).
   Case 5 reads `/tmp/smdj2.rev` at display time, so a line of the form
   `<ver> [<model>:<id>:]` (e.g. `2.21 [4206:4195:]`) makes `M:` show `2.21`.
   - This is the **recommended** override: pure userspace, no `rbp` patch, no
     MCU code. It controls the **MAIN** (`M:`) half.
   - Caveat to verify when implemented: confirm nothing rewrites the file back
     to `--.--` after the seed (the original seeder lives on the
     StaticInfomation path, which the harness doesn't run — so in the emulator
     the seed should stick; on real hardware the MCU would re-seed it).

2. **Answer StaticInfomation in the QEMU harness.** Implement the
   StaticInfomation query/response in the sub-ucom harness so `DAT_0123e054`
   becomes a real value. This lights up the **SUB** (`S:`) half (and is what a
   real panel does). More work; only needed if we want `S:` correct too. Still
   **no `rbp`/MCU patch** — it's harness code.

3. **Patch `rbp` directly (custom-firmware route).** In a patched `rbp`, seed
   `DAT_0123e054` to a constant and/or short-circuit the `M:`/`S:` formatting
   in `FUN_00223140` case 5. Fully under our control, but this *is* patching the
   main binary — use only if we want the version baked into the firmware image
   itself rather than supplied at runtime.

### Summary

| Want to control | Source                      | No-MCU-patch override            |
|-----------------|-----------------------------|----------------------------------|
| `M:` (main)     | `/tmp/smdj2.rev` file        | **Seed the file** (route 1)      |
| `S:` (sub)      | `DAT_0123e054` (StaticInfo)  | Harness StaticInfomation (route 2) |
| both, baked in  | `FUN_00223140` case 5        | patched `rbp` (route 3)          |

## References

- `rbp` `FUN_00223140` — version/welcome state machine, case 5 = VERSION INFORMATION.
- `rbp` `FUN_001aefac` @ `0x1aefcc` — the only *other* ref to the
  `/tmp/smdj2.rev` literal; a config-table registration, not a writer.
- String literals: `/tmp/smdj.rev` @ file off `0x416388`,
  `/tmp/smdj2.rev` @ `0x418dbc`.
- StaticInfomation RTTI: `N2ui16StaticInfomationE`; sub-version word
  `DAT_0123e054` (init `0xffff`).
- Panel frame / harness: [subucom-frame-map.md](subucom-frame-map.md),
  [rbp-and-panel-protocol.md](rbp-and-panel-protocol.md).
