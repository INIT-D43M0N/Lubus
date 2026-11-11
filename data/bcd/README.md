# Windows BCD templates for direct installs

Creating a bootable Windows installation requires a valid BCD store. Yaku
looks for one of these, in order:

1. `bcdboot` helper via `YAKU_BCDBOOT`
2. a template file in this directory:
   - `uefi.bcd` for GPT/UEFI installs
   - `bios.bcd` for MBR/BIOS installs

## bcdboot helper contract

A pure Linux system cannot execute `bcdboot.exe` directly. Provide a wrapper
that translates the mounted paths to a Windows/Wine/PE environment and runs
`bcdboot` there:

```sh
#!/bin/sh
# YAKU_BCDBOOT=/path/to/this-script
# $1 = mounted Windows volume
# $2 = mounted ESP (empty for BIOS)
# $3 = uefi or bios
exec wine /path/to/bcdboot.exe ... 
```

Or a native helper/VM that accepts the same three arguments. If the helper
returns 0, Yaku assumes the BCD store and boot files were created.

## Template contract

A template must be a known-good BCD store for the corresponding firmware
mode and target layout. Templates are copied to:

- UEFI: `EFI/Microsoft/Boot/BCD` on the ESP
- BIOS: `Boot/BCD` on the Windows volume

Templates are a controlled-environment fallback; use `bcdboot` when possible.
