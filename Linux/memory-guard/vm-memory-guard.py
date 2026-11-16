#!/usr/bin/env python3
"""VM memory guard: warn when the laptop is short on memory while lab VMs run,
offer to pause or save the client VMs, and pause them automatically as a last
resort before the desktop locks up.

The VMs have no memory balloon, so they cannot be shrunk live. The two levers:
  pause  - instant; a paused VM stops touching memory, so the kernel can move
           it to swap and leave it there. Resume is instant.
  save   - writes the VM's memory to disk and frees it (managed save). The VM
           picks up where it left off the next time it is started.

Run with --test to show the warning now, --status to print current numbers.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time

URI = "qemu:///system"
THROTTLE_MATCH = re.compile(r"client", re.I)  # VMs that may be paused/saved
POLL_SECONDS = 5

GIB = 1024 * 1024  # meminfo is in KiB
WARN_AVAILABLE = 6 * GIB       # warn when less than this is available ...
WARN_SWAP_USED = 4 * GIB       # ... or when this much swap is in use
CRIT_AVAILABLE = 2 * GIB       # auto-pause when less than this is available
CRIT_SWAP_FRACTION = 0.5       # ... and at least half the swap is used
SNOOZE_SECONDS = 30 * 60       # "Ignore" keeps quiet this long
REWARN_SECONDS = 10 * 60       # unanswered warning repeats after this long

state_lock = threading.Lock()
snooze_until = 0.0
last_warn = 0.0
paused_by_guard = set()


def log(msg):
    print(msg, flush=True)


def meminfo():
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0])
    return info


def virsh(*args, timeout=120):
    return subprocess.run(["virsh", "-c", URI, *args], capture_output=True,
                          text=True, timeout=timeout)


def vms(state_flag):
    out = virsh("list", "--name", state_flag, timeout=15).stdout
    return [name for name in out.split() if THROTTLE_MATCH.search(name)]


def pause_clients():
    done = []
    for name in vms("--state-running"):
        if virsh("suspend", name).returncode == 0:
            done.append(name)
    with state_lock:
        paused_by_guard.update(done)
    log(f"paused: {done}")
    return done


def resume_clients():
    done = []
    for name in vms("--state-paused"):
        if virsh("resume", name).returncode == 0:
            done.append(name)
    with state_lock:
        paused_by_guard.clear()
    log(f"resumed: {done}")
    return done


def save_clients():
    done = []
    for name in vms("--state-running") + vms("--state-paused"):
        if virsh("managedsave", name, timeout=600).returncode == 0:
            done.append(name)
    with state_lock:
        paused_by_guard.difference_update(done)
    log(f"saved to disk: {done}")
    return done


BLANK_ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "blank.svg")


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


WINDOW = os.path.join(os.path.dirname(os.path.abspath(__file__)), "warning-window.py")


def notify(title, body, actions=None, urgency="critical", sound="dialog-warning",
           recommended=None, level="warning"):
    """Tell the user something.

    With actions - a list of (key, button label, what it does) - this opens
    the always-on-top warning window, blocks until a button is chosen and
    returns its key ('' if the window is closed). Without actions it sends a
    plain desktop notification."""
    first, _, rest = body.partition("\n")
    if urgency == "critical" and sound:
        play(sound)
    if not actions:
        subprocess.run(["notify-send", "-a", title, "-i", BLANK_ICON, "-u", urgency,
                        first, rest], timeout=15)
        return ""
    spec = {"title": title.lstrip("\u26a0\ufe0f\U0001f6d1\u23f8 "), "headline": first,
            "detail": rest, "level": level,
            "actions": [{"key": key, "label": label, "desc": desc,
                         "recommended": key == recommended}
                        for key, label, desc in actions]}
    try:
        out = subprocess.run([sys.executable, WINDOW, json.dumps(spec)],
                             capture_output=True, text=True, timeout=REWARN_SECONDS)
        return out.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""


def describe(info):
    avail = info["MemAvailable"] / GIB
    swap = (info["SwapTotal"] - info["SwapFree"]) / GIB
    return f"{avail:.1f} GB memory left, {swap:.1f} GB of swap in use"


def handle(choice):
    global snooze_until
    if choice == "pause":
        done = pause_clients()
        offer_resume("\u23f8\ufe0f CLIENT VMS PAUSED", f"Paused {len(done)} client VMs.\nThey hold their place and resume instantly.")
    elif choice == "save":
        notify("\u2744\ufe0f CLIENT VMS", "Freezing client VMs…\nSaving them to disk to free their memory. It can take a minute.",
               urgency="normal")
        done = save_clients()
        notify("\u2744\ufe0f CLIENT VMS", f"Froze {len(done)} client VMs.\nTheir memory is free. Use Resume lab in Lab Manager "
               "to bring them back.", urgency="normal")
    elif choice == "ignore":
        with state_lock:
            snooze_until = time.time() + SNOOZE_SECONDS


def offer_resume(title, body, sound=None, level="warning"):
    choice = notify(title, body, [
        ("resume", "Resume now", "Starts the paused VMs again right away."),
        ("save", "Freeze instead", "Saves them to disk and frees their memory. "
                                   "Resume lab brings them back."),
        ("keep", "Keep paused", "Leaves them paused. Resume them later in Lab Manager."),
    ], sound=sound, level=level)
    if choice == "resume":
        resume_clients()
    elif choice == "save":
        handle("save")


def vm_memory_gb(names):
    """Memory the given VMs would give back if frozen, in GB."""
    total = 0
    for name in names:
        for line in virsh("dominfo", name, timeout=15).stdout.splitlines():
            if line.startswith("Used memory:"):
                total += int(line.split()[2])
    return total / GIB


def recommend(info):
    """Pause is enough while swap has room for the paused VMs; once swap is
    filling or memory is nearly gone, only Freeze actually gives memory back."""
    swap_total = info["SwapTotal"]
    swap_used = swap_total - info["SwapFree"]
    if swap_total == 0 or swap_used > swap_total * 0.25 or info["MemAvailable"] < 4 * GIB:
        return "save"
    return "pause"


def warn(info):
    names = vms("--state-running")
    if not names:
        return
    choice = notify(
        "\u26a0\ufe0f WARNING: MEMORY IS RUNNING LOW",
        f"{describe(info)}.\n{len(names)} client VMs are running. "
        "The laptop may lock up if this continues.",
        [("pause", "Pause client VMs",
          "Stops them instantly and resumes instantly. Frees no memory right away."),
         ("save", "Freeze client VMs",
          f"Saves them to disk and frees about {vm_memory_gb(names):.0f} GB. "
          "Resume lab brings them back."),
         ("ignore", "Ignore 30 min", "Does nothing and stays quiet for 30 minutes.")],
        recommended=recommend(info))
    handle(choice)


def emergency(info):
    done = pause_clients()
    if done:
        offer_resume("\U0001f6d1 CLIENT VMS PAUSED TO PREVENT A FREEZE",
                     f"{describe(info)}.\nPaused: {', '.join(done)}.",
                     sound="alarm-clock-elapsed", level="critical")


def in_background(fn, *args):
    threading.Thread(target=fn, args=args, daemon=True).start()


def main():
    global last_warn
    if "--status" in sys.argv:
        print(describe(meminfo()))
        print("running clients:", vms("--state-running"))
        print("paused clients:", vms("--state-paused"))
        return
    if "--test" in sys.argv:
        warn(meminfo())
        return

    log("VM memory guard started")
    busy = threading.Event()

    def guarded(fn, info):
        try:
            fn(info)
        finally:
            busy.clear()

    while True:
        time.sleep(POLL_SECONDS)
        try:
            info = meminfo()
            avail = info["MemAvailable"]
            swap_total = info["SwapTotal"]
            swap_used = swap_total - info["SwapFree"]
            swap_spent = swap_total == 0 or swap_used >= swap_total * CRIT_SWAP_FRACTION

            if avail < CRIT_AVAILABLE and swap_spent:
                # Last resort: act first, tell the user after. Runs even while
                # a warning is waiting for an answer.
                if vms("--state-running"):
                    log(f"critical: {describe(info)}")
                    in_background(emergency, info)
                    time.sleep(30)
                continue

            low = avail < WARN_AVAILABLE or swap_used > WARN_SWAP_USED
            now = time.time()
            with state_lock:
                quiet = now < snooze_until or now - last_warn < REWARN_SECONDS
            if low and not quiet and not busy.is_set():
                log(f"warning: {describe(info)}")
                last_warn = now
                busy.set()
                in_background(guarded, warn, info)
        except Exception as exc:  # keep the guard alive whatever happens
            log(f"error: {exc!r}")


if __name__ == "__main__":
    main()
