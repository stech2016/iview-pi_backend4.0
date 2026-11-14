"""STECH iVIEW gateway launcher. Built into one ready-made program per system (Windows .exe, Linux, macOS) by the build
pipeline, with Python and every library the plant script needs INSIDE it - the customer installs nothing.

It keeps the plant script running, and restarts it after a crash or after a signed self-update.
The script is INSIDE the program: the customer's folder holds the program and link.json, no Python file. The first start copies the
script out of the program into the folder "program" (as gateway.bin) so that the signed self-update can replace it; a newer script
inside a newer program replaces an older one there.

   stech-gateway              run and keep running (what the customer double-clicks / the autostart starts)
   stech-gateway <file> ..    run that one script file once (used by the updater to test a new version)
   stech-gateway --bundled    print the version of the script inside the program (build check)
"""
import json
import os
import re
import runpy
import subprocess
import sys
import time


def _folder() -> str:
    return os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def _bundle_libraries() -> None:  # never called: makes the packager include every library the plant script imports
    import plant_gateway  # noqa: F401


def _version_of(path: str) -> tuple:
    try:
        with open(path, "rb") as f:
            m = re.search(rb'^VERSION = "([0-9][0-9.]*)"', f.read(), re.M)
        return tuple(int(x) for x in m.group(1).decode().split(".")) if m else (0,)
    except Exception:
        return (0,)


def bundled_script() -> str:
    """The script that travels inside the program (next to launcher.py when run as a plain script)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "plant_gateway.py")


def prepare_script(here: str) -> str:
    """Returns the script file the program runs: <folder>/program/gateway.bin. Created from the script inside the program on the
    first start; replaced when the program carries a newer one; a script from an older download (plant_gateway.py next to the
    program) is taken over once, and moved out of sight, so the customer's folder holds no .py file."""
    folder = os.path.join(here, "program")
    os.makedirs(folder, exist_ok=True)
    script = os.path.join(folder, "gateway.bin")
    legacy = os.path.join(here, "plant_gateway.py")
    best, best_v = (script, _version_of(script)) if os.path.isfile(script) else (None, (-1,))
    for cand in (legacy, bundled_script()):
        if os.path.isfile(cand) and _version_of(cand) > best_v:
            best, best_v = cand, _version_of(cand)
    if best is not None and best != script:
        tmp = script + ".tmp"
        with open(best, "rb") as f, open(tmp, "wb") as g:
            g.write(f.read())
        os.replace(tmp, script)
    if os.path.isfile(legacy):
        try:
            os.replace(legacy, os.path.join(folder, "from_older_download.bak"))
        except OSError:
            pass
    return script if os.path.isfile(script) else ""


def run_script(path: str, args: list[str]) -> int:
    sys.argv = [path] + args
    sys.path.insert(0, os.path.dirname(os.path.abspath(path)))
    try:
        runpy.run_path(path, run_name="__main__")
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


def _jread(name: str):
    try:
        with open(os.path.join(_folder(), name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _jwrite(name: str, data) -> None:
    with open(os.path.join(_folder(), name), "w", encoding="utf-8") as f:
        json.dump(data, f)


def _jdel(name: str) -> None:
    try:
        os.remove(os.path.join(_folder(), name))
    except OSError:
        pass


def side(exe: str, tag: str) -> str:
    """Neighbour file of the program: STECH-Gateway.exe -> STECH-Gateway.new.exe (Windows runs only files that end in .exe)."""
    return exe[:-4] + f".{tag}.exe" if exe.lower().endswith(".exe") else f"{exe}.{tag}"


def swap_in(exe: str) -> bool:
    """A verified new program waits as <exe>.new: keep the old one as <exe>.old and put the new one in its place.
    Renaming a running program is allowed on every system (deleting or overwriting it is not on Windows)."""
    new, old = side(exe, "new"), side(exe, "old")
    if not os.path.isfile(new):
        return False
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        os.replace(old, side(exe, "old2"))
    os.replace(exe, old)
    os.replace(new, exe)
    return True


def swap_back(exe: str) -> bool:
    """The new program did not work: the old one (.old) takes its place again and the new one is kept as .bad."""
    old, bad = side(exe, "old"), side(exe, "bad")
    if not os.path.isfile(old):
        return False
    try:
        if os.path.exists(bad):
            os.remove(bad)
    except OSError:
        pass
    os.replace(exe, bad)
    os.replace(old, exe)
    return True


def roll_back_program(exe: str, reason: str) -> None:
    pending = _jread("program_pending.json") or {}
    ok = swap_back(exe)
    if pending.get("to"):
        _jwrite("program_blocked.json", {"version": pending["to"]})
    _jwrite("update_report.json", {"event": "program_rolled_back", "from": pending.get("from", ""), "to": pending.get("to", ""), "reason": reason[:80]})
    _jdel("program_pending.json")
    print(f"The new gateway program did not work ({reason}); " + ("the previous one was put back." if ok else "no previous program was found."))


AUTOSTART_NAME = "STECH iVIEW Gateway"


def _is_admin() -> bool:
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def ensure_autostart(exe: str, run=subprocess.run, platform: str | None = None, admin: bool | None = None, home: str | None = None) -> str:
    """Plug and play: the first time the program runs it registers itself to start with the computer. Nothing for the customer to do.
    Windows: started as administrator -> a start-up task for the whole PC (runs even with nobody signed in, hidden);
             otherwise -> the signed-in user's start-up entry (no administrator rights needed), started minimised.
    macOS: a LaunchAgent. Linux: the install-autostart.sh file in the folder (a service needs root).
    Remembered in autostart.json, so it runs once and again only if the program folder was moved. Returns what was done."""
    platform = platform or sys.platform
    state = _jread("autostart.json") or {}
    if state.get("exe") == exe:
        return "already"
    mode = ""
    try:
        if platform.startswith("win"):
            if admin if admin is not None else _is_admin():
                r = run(["schtasks", "/create", "/tn", AUTOSTART_NAME, "/tr", f'"{exe}"', "/sc", "onstart", "/ru", "SYSTEM", "/rl", "HIGHEST", "/f"], capture_output=True)
                mode = "task" if r.returncode == 0 else ""
            if not mode:
                r = run(["reg", "add", r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run", "/v", AUTOSTART_NAME, "/t", "REG_SZ",
                         "/d", f'cmd /c start "" /min "{exe}"', "/f"], capture_output=True)
                mode = "user" if r.returncode == 0 else ""
        elif platform == "darwin":
            agents = os.path.join(home or os.path.expanduser("~"), "Library", "LaunchAgents")
            os.makedirs(agents, exist_ok=True)
            plist = os.path.join(agents, "com.stech.iview.gateway.plist")
            with open(plist, "w", encoding="utf-8") as f:
                f.write('<?xml version="1.0" encoding="UTF-8"?>\n<plist version="1.0"><dict><key>Label</key><string>com.stech.iview.gateway</string>'
                        f'<key>ProgramArguments</key><array><string>{exe}</string></array><key>WorkingDirectory</key><string>{os.path.dirname(exe)}</string>'
                        '<key>RunAtLoad</key><true/></dict></plist>\n')
            mode = "launchagent"          # starts at the next sign-in; this copy is already running
    except Exception:
        mode = ""
    if mode:
        _jwrite("autostart.json", {"exe": exe, "mode": mode})
        print(f"Set up: the gateway now starts by itself with this computer ({mode}).")
    return mode


def supervise() -> int:
    here = _folder()
    script = prepare_script(here)
    if not script:
        print("The gateway program is damaged (its script is missing). Download the gateway again from the app.")
        time.sleep(10)
        return 1
    os.environ["STECH_SCRIPT"] = script
    frozen = getattr(sys, "frozen", False)
    exe = sys.executable
    if frozen:
        ensure_autostart(exe)
    me = [exe] if frozen else [exe, os.path.abspath(__file__)]
    while True:
        pending = _jread("program_pending.json")
        if pending and frozen:               # a new program is on trial: 3 starts without a confirmed connection = back to the old one
            pending["starts"] = int(pending.get("starts", 0)) + 1
            if pending["starts"] > 3:
                roll_back_program(exe, "it kept stopping")
            else:
                _jwrite("program_pending.json", pending)
        code = subprocess.call(me + [script], cwd=here)
        if code == 3:   # another copy is already running on this computer
            print("Another copy of this gateway is already running, so this window can be closed.")
            return 0
        if frozen and code == 11:            # the plant script asks for the verified new program to be switched in
            try:
                print("Switching to the new gateway program..." if swap_in(exe) else "No new program found.")
            except OSError as e:
                print(f"Could not switch the program ({e}); continuing with the current one.")
                _jdel("program_pending.json")
            continue
        if frozen and code == 12:            # the new program could not reach the cloud
            roll_back_program(exe, "it could not connect to the cloud")
            continue
        print("The gateway stopped. Restarting in 5 seconds...")
        time.sleep(5)


LAUNCHER_VERSION = "1"


def main() -> int:
    # a file named on the command line is resolved BEFORE the program changes into its own folder (a relative path would be lost)
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-") and os.path.isfile(sys.argv[1]):
        sys.argv[1] = os.path.abspath(sys.argv[1])
    os.environ["STECH_LAUNCHER"] = LAUNCHER_VERSION  # the plant script reads this to know what the program around it can run
    os.environ["STECH_HOME"] = _folder()            # the plant script keeps link.json, its log and its store in the program's folder
    os.chdir(_folder())
    if len(sys.argv) > 1 and sys.argv[1] == "--bundled":
        v = _version_of(bundled_script())
        print(".".join(map(str, v)))
        return 0 if v != (0,) else 1
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-") and os.path.isfile(sys.argv[1]):
        return run_script(sys.argv[1], sys.argv[2:])
    return supervise()


if __name__ == "__main__":
    sys.exit(main())
