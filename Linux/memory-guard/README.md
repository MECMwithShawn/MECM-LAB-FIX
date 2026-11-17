# VM memory guard

A small background service for the Linux lab host. When the laptop is actually short on memory
while client VMs are running, it pauses one of them and tells you in a notification. If memory
keeps falling it pauses another, and as a last resort all of them, so the desktop does not lock up.

## Why

The full lab (DC1, GW1, CM1 and four clients) is allowed up to 34 GB. On a 62 GB laptop with no
swap, a large build on top of that used all the memory and the machine locked up hard enough to
need the power button. The kernel never killed anything to recover.

## What it does

| When | Action |
| :--- | :--- |
| Under 6 GB available | Pauses the client VM holding the most memory |
| Each further 1 GB drop after that | Pauses the next one |
| Under 2 GB available and half the swap used (or no swap) | Pauses every running client VM, with an alarm sound |
| Back above 8 GB available | The episode is over; the next dip starts again with one VM |

- Only available memory counts as low. Swap that is in use while plenty of memory is free does
  nothing (the first version warned on 4 GB of swap in use, which fired with 19 GB free).
- Each pause shows one notification that says which VM was paused and why. Its **Resume** button
  resumes that VM; Lab Manager can resume it too. Nothing asks for a decision first.
- **Pause** stops a VM instantly and resumes it instantly. It keeps its memory, but a paused VM no
  longer touches it, so the kernel can move it to swap and leave it there.
- Only VMs with `client` in the name are touched. DC1, GW1 and CM1 keep running.
- The guard never resumes a VM by itself.
- To free a paused VM's memory outright, use *Freeze lab* in Lab Manager.

The lab VMs have no memory balloon device, so they cannot be shrunk while running.

## Add swap first

The guard works best with swap; without it, a pause frees nothing and the last-resort level is
reached sooner. A 32 GB swap file on an ext4 root:

```bash
sudo fallocate -l 32G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## Install

```bash
./install.sh            # copies to ~/.local/share/vm-memory-guard, enables the user service
./install.sh --remove   # stops and removes it
```

No sudo. The user must be in the `libvirt` group, as for Lab Manager.

## Files

| File | What it is |
| :--- | :--- |
| `vm-memory-guard.py` | The service. Thresholds are the constants at the top; `decide()` is the rule. |
| `tests/test_guard.py` | Checks for the rule. |

## Try it

```bash
python3 ~/.local/share/vm-memory-guard/vm-memory-guard.py --status   # current numbers and client VM states
python3 ~/.local/share/vm-memory-guard/vm-memory-guard.py --test     # shows the notification; pauses nothing
python3 -B -m unittest discover -s tests                             # the rule
journalctl --user -u vm-memory-guard                                 # what it has done
```

## Notes

- Tested: the rule (unit tests) and the notification. Not yet exercised: a pause under real
  memory pressure.
