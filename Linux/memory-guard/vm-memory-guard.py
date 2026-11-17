#!/usr/bin/env python3
"""VM memory guard: when the laptop is actually short on memory while lab VMs
run, pause one client VM and say so in a notification. If memory keeps
falling it pauses another, and as a last resort all of them, before the
desktop locks up.

The VMs have no memory balloon, so they cannot be shrunk live. A paused VM
stops touching its memory, so the kernel can move it to swap and leave it
there; resume is instant. Only available memory counts as "low": swap that is
in use while plenty of memory is free is not a problem.

Run with --status to print current numbers, --test to see the notification
without pausing anything.
"""
import json
import re
import subprocess
import sys
import threading
import time

URI = "qemu:///system"
THROTTLE_MATCH = re.compile(r"client", re.I)  # VMs that may be paused
POLL_SECONDS = 5

GIB = 1024 * 1024  # meminfo is in KiB
LOW_AVAILABLE = 6 * GIB        # pause one client when less than this is available
STEP = 1 * GIB                 # ... and one more for each further drop of this much
RECOVERED_AVAILABLE = 8 * GIB  # above this the episode is over; the next dip starts fresh
CRIT_AVAILABLE = 2 * GIB       # pause every client when less than this is available
CRIT_SWAP_FRACTION = 0.5       # ... and at least half the swap is used (or there is none)
NOTICE_SECONDS = 60 * 60       # how long a notification's Resume button is listened to

paused_by_guard = set()
state_lock = threading.Lock()


def log(msg):
    print(msg, flush=True)


def meminfo():
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])
    return info


def decide(avail, swap_total, swap_used, floor):
    """What to do now: 'none', 'one' (pause one client) or 'all' (pause every client).

    floor is the available memory at the last pause of this low-memory episode,
    or None if nothing has been paused since memory last recovered."""
    swap_spent = swap_total == 0 or swap_used >= swap_total * CRIT_SWAP_FRACTION
    if avail < CRIT_AVAILABLE and swap_spent:
        return "all"
    if avail >= LOW_AVAILABLE:
        return "none"
    if floor is None or avail <= floor - STEP:
        return "one"
    return "none"


def virsh(*args, timeout=120):
    return subprocess.run(["virsh", "-c", URI, *args], capture_output=True,
                          text=True, timeout=timeout)


def vms(state_flag):
    out = virsh("list", "--name", state_flag, timeout=15).stdout
    return [name for name in out.split() if THROTTLE_MATCH.search(name)]


def resident_kib(name):
    """Host memory the VM holds right now."""
    for line in virsh("dommemstat", name, timeout=15).stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == "rss":
            return int(parts[1])
    return 0


def biggest(names):
    """The client holding the most memory: pausing it helps the most."""
    return max(names, key=resident_kib) if names else None


def pause(names):
    done = [name for name in names if virsh("suspend", name).returncode == 0]
    with state_lock:
        paused_by_guard.update(done)
    log(f"paused: {done}")
    return done


def resume(names):
    done = [name for name in names if virsh("resume", name).returncode == 0]
    with state_lock:
        paused_by_guard.difference_update(done)
    log(f"resumed: {done}")
    return done


SOUND_DIR = "/usr/share/sounds/freedesktop/stereo"


def alert_sinks():
    """The default output plus the laptop speakers, so the alert is heard
    even when the default is a headset nobody is wearing."""
    sinks = [None]  # None = default output
    try:
        out = subprocess.run(["pactl", "--format=json", "list", "sinks"],
                             capture_output=True, text=True, timeout=5).stdout
        default = subprocess.run(["pactl", "get-default-sink"], capture_output=True,
                                 text=True, timeout=5).stdout.strip()
        for sink in json.loads(out):
            text = f"{sink.get('description', '')} {sink.get('active_port', '')}"
            if "speaker" in text.lower() and sink["name"] != default:
                sinks.append(sink["name"])
    except Exception:
        pass
    return sinks


def play(sound):
    """Play an alert sound without waiting for it."""
    for sink in alert_sinks():
        cmd = ["paplay"] + ([f"--device={sink}"] if sink else [])
        try:
            subprocess.Popen(cmd + [f"{SOUND_DIR}/{sound}.oga"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass


def short(name):
    return name.replace("HYD-", "")


def describe(info):
    avail = info["MemAvailable"] / GIB
    swap = (info["SwapTotal"] - info["SwapFree"]) / GIB
    return f"{avail:.1f} GB memory left, {swap:.1f} GB of swap in use"


def notice(names, info, everything=False, test=False):
    """Tell the user what was paused. The notification carries a Resume button;
    this waits for it, so call it in a background thread."""
    who = ", ".join(short(n) for n in names)
    if everything:
        title = f"Memory almost gone: paused {who}"
        play("alarm-clock-elapsed")
    else:
        title = f"Memory is low: paused {who}"
    body = (f"{describe(info)}. Paused VMs hold their place and resume instantly, "
            "here or in Lab Manager.")
    if test:
        body = "Test only, nothing was paused. " + body
    button = "Resume" if len(names) > 1 else f"Resume {who}"
    try:
        out = subprocess.run(["notify-send", "-a", "VM memory guard", "-i", "dialog-warning",
                              "-u", "critical", "-A", f"resume={button}", title, body],
                             capture_output=True, text=True, timeout=NOTICE_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return
    if out.stdout.strip() == "resume" and not test:
        resume(names)


def in_background(fn, *args, **kwargs):
    threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True).start()


def main():
    if "--status" in sys.argv:
        print(describe(meminfo()))
        print("running clients:", vms("--state-running"))
        print("paused clients:", vms("--state-paused"))
        return
    if "--test" in sys.argv:
        name = biggest(vms("--state-running"))
        if name is None:
            print("no client VM is running")
            return
        print(f"would pause {name}")
        notice([name], meminfo(), test=True)
        return

    log("VM memory guard started")
    floor = None
    while True:
        time.sleep(POLL_SECONDS)
        try:
            info = meminfo()
            avail = info["MemAvailable"]
            if avail >= RECOVERED_AVAILABLE:
                floor = None
            action = decide(avail, info["SwapTotal"], info["SwapTotal"] - info["SwapFree"], floor)
            if action == "none":
                continue
            running = vms("--state-running")
            floor = avail  # also when nothing could be paused, so this is not retried every poll
            if not running:
                continue
            log(f"{'critical' if action == 'all' else 'low'}: {describe(info)}")
            done = pause(running if action == "all" else [biggest(running)])
            if done:
                in_background(notice, done, info, everything=action == "all")
        except Exception as exc:  # keep the guard alive whatever happens
            log(f"error: {exc!r}")


if __name__ == "__main__":
    main()
