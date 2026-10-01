---
title: WSL disk reclaim
parent: Reference
nav_order: 5
---

# WSL disk reclaim

WSL2 keeps each distro in an ext4 VHDX on the Windows drive. A non-sparse VHDX
only grows: space freed inside Linux stays allocated on Windows until a manual
compaction. `agentihooks gc` frees space inside WSL; making the VHDX sparse is
what returns that space to Windows.

## Check

From WSL:

```bash
findmnt -no OPTIONS /                     # 'discard' must be listed
powershell.exe -NoProfile -Command 'Get-ChildItem HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss | ForEach-Object { $p = Get-ItemProperty $_.PSPath; "{0}|{1}" -f $p.DistributionName, $p.BasePath }'
```

From Windows (PowerShell), with the distro's `BasePath`:

```powershell
fsutil sparse queryflag <BasePath>\ext4.vhdx
```

`This file is NOT set as sparse` means freed space never returns to Windows.

## Make the VHDX sparse (one time)

Run from Windows PowerShell. Every WSL session stops in step 1.

```powershell
wsl --shutdown
wsl --export <Distro> D:\backup\<Distro>.tar
wsl --manage <Distro> --set-sparse true
```

Some WSL builds ask for `--allow-unsafe` on the last command; the export in
step 2 is the rollback (`wsl --import`). Then start the distro and release the
space that is already free:

```bash
sudo fstrim -v /
```

Verify: `fsutil sparse queryflag <BasePath>\ext4.vhdx` reports the file as
sparse, and deleting a few GB inside WSL shrinks the VHDX size shown in Explorer.

## Why `/tmp` loses files on WSL

`/usr/lib/tmpfiles.d/tmp.conf` has `D /tmp`, so systemd empties `/tmp` on every
VM boot: a crash, `wsl --shutdown`, a Windows reboot, an idle VM shutdown, or a
WSL update. Keep working files under `~/scratchpad/<repo>/<task>/`
(`agentihooks scratch new`), never in `/tmp`.
