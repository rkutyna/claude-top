#!/usr/bin/env python3
"""claude-top: per-session CPU / GPU / memory for Claude Code and everything it spawned.

Stdlib only, no root needed. Runs on macOS (ps, ioreg, libproc) and Linux (/proc;
GPU from DRM fdinfo for AMD/Intel and nvidia-smi for NVIDIA).

  claude_top.py            live dashboard
  claude_top.py --once     print one snapshot and exit
  claude_top.py --json     one snapshot as JSON
"""
import argparse
import ctypes
import curses
import glob
import json
import os
import plistlib
import re
import shutil
import struct
import subprocess
import sys
import time

MACOS = sys.platform == "darwin"
SESSIONS_DIR = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), "sessions")
RUSAGE_INFO_V2 = 2
FOOTPRINT_OFFSET = 72  # ri_phys_footprint in struct rusage_info_v2

_libproc = None
if MACOS:
    try:
        _libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        _rusage_buf = ctypes.create_string_buffer(1024)
    except OSError:
        pass


def run(cmd, binary=False, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=10, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return b"" if binary else ""
    return r.stdout if binary else r.stdout.decode("utf-8", "replace")


def footprint(pid):
    """Memory a process is really responsible for, in bytes, or None.

    macOS: physical footprint (what Activity Monitor shows). Linux: PSS, which
    splits shared pages between the processes sharing them so sums are honest.
    """
    if not MACOS:
        try:
            with open("/proc/%d/smaps_rollup" % pid) as fh:
                for line in fh:
                    if line.startswith("Pss:"):
                        return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return None
    if _libproc is None:
        return None
    if _libproc.proc_pid_rusage(pid, RUSAGE_INFO_V2, _rusage_buf) != 0:
        return None
    return struct.unpack_from("Q", _rusage_buf, FOOTPRINT_OFFSET)[0]


def parse_cputime(s):
    """ps 'time' column ([D-][H:]M:SS.cc) -> seconds."""
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    secs = 0.0
    for part in s.split(":"):
        secs = secs * 60 + float(part)
    return days * 86400 + secs


class Proc:
    __slots__ = ("pid", "ppid", "pgid", "rss", "cputime", "start", "cmd", "comm", "short",
                 "cpu", "gpu", "mem")

    @property
    def key(self):
        return (self.pid, self.start)


def is_claude(p):
    return os.path.basename(p.comm) == "claude" or p.short == "claude"


def read_procs():
    return read_procs_macos() if MACOS else read_procs_linux()


def read_procs_linux():
    tick = float(os.sysconf("SC_CLK_TCK"))
    page = os.sysconf("SC_PAGE_SIZE")
    procs = {}
    for name in os.listdir("/proc"):
        if not name.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % name) as fh:
                stat = fh.read()
            with open("/proc/%s/cmdline" % name, "rb") as fh:
                raw = fh.read()
            rp = stat.rindex(")")
            f = stat[rp + 2:].split()  # f[0] is field 3 (state) of proc(5) stat
            p = Proc()
            p.pid, p.ppid, p.pgid = int(name), int(f[1]), int(f[2])
            p.cputime = (int(f[11]) + int(f[12])) / tick
            p.start = f[19]
            p.rss = int(f[21]) * page
        except (OSError, ValueError, IndexError):
            continue
        p.short = stat[stat.index("(") + 1:rp]
        argv = raw.rstrip(b"\0").split(b"\0")
        p.cmd = b" ".join(argv).decode("utf-8", "replace") or "[%s]" % p.short
        p.comm = argv[0].decode("utf-8", "replace") or p.short
        p.cpu = p.gpu = 0.0
        p.mem = p.rss
        procs[p.pid] = p
    return procs


def read_procs_macos():
    # TZ=UTC so lstart is comparable with procStart in the session files
    env = dict(os.environ, TZ="UTC")
    comms = {}
    for line in run(["ps", "-axo", "pid=,comm="]).splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2:
            comms[int(parts[0])] = parts[1]
    procs = {}
    out = run(["ps", "-axo", "pid=,ppid=,pgid=,rss=,time=,lstart=,command="], env=env)
    for line in out.splitlines():
        f = line.split(None, 10)
        if len(f) < 11:
            continue
        try:
            p = Proc()
            p.pid, p.ppid, p.pgid = int(f[0]), int(f[1]), int(f[2])
            p.rss = int(f[3]) * 1024
            p.cputime = parse_cputime(f[4])
        except ValueError:
            continue
        p.start = " ".join(f[5:10])
        p.cmd = f[10]
        p.comm = comms.get(p.pid, p.cmd.split(" ", 1)[0])
        p.short = os.path.basename(p.comm)
        p.cpu = p.gpu = 0.0
        p.mem = p.rss
        procs[p.pid] = p
    return procs


class MacGpu:
    """Per-process GPU time from the IOKit registry (Apple Silicon)."""

    available = True

    def __init__(self):
        self.prev = {}  # registry id -> accumulated GPU ns

    def _clients(self):
        raw = run(["ioreg", "-r", "-c", "IOGPUDeviceUserClient", "-a"], binary=True)
        clients = {}
        try:
            entries = plistlib.loads(raw) if raw else []
        except Exception:
            return clients
        for e in entries:
            m = re.match(r"pid (\d+)", e.get("IOUserClientCreator", ""))
            if not m:
                continue
            total = sum(u.get("accumulatedGPUTime", 0) for u in e.get("AppUsage") or [])
            clients[e.get("IORegistryEntryID")] = (int(m.group(1)), total)
        return clients

    def _system(self):
        raw = run(["ioreg", "-r", "-d", "1", "-c", "IOAccelerator", "-a"], binary=True)
        try:
            for e in plistlib.loads(raw):
                util = (e.get("PerformanceStatistics") or {}).get("Device Utilization %")
                if util is not None:
                    return float(util)
        except Exception:
            pass
        return None

    def sample(self, dt):
        """Returns ({pid: percent of GPU time}, system percent or None)."""
        clients = self._clients()
        by_pid = {}
        if dt and dt > 0:
            for rid, (pid, ns) in clients.items():
                delta = ns - self.prev.get(rid, ns)
                if delta > 0:
                    by_pid[pid] = by_pid.get(pid, 0.0) + delta / 1e9 / dt * 100
        self.prev = {rid: ns for rid, (pid, ns) in clients.items()}
        return by_pid, self._system()


class NvidiaGpu:
    """Per-process GPU utilisation from nvidia-smi, when it is installed."""

    def __init__(self):
        self.smi = shutil.which("nvidia-smi")
        self.available = bool(self.smi)

    def sample(self, dt):
        if not self.smi:
            return {}, None
        by_pid = {}
        for line in run([self.smi, "pmon", "-c", "1", "-s", "u"]).splitlines():
            f = line.split()
            if len(f) < 4 or line.startswith("#"):
                continue
            try:
                by_pid[int(f[1])] = by_pid.get(int(f[1]), 0.0) + float(f[3])
            except ValueError:
                continue  # "-" for an idle slot
        utils = []
        for line in run([self.smi, "--query-gpu=utilization.gpu",
                         "--format=csv,noheader,nounits"]).splitlines():
            try:
                utils.append(float(line.strip()))
            except ValueError:
                pass
        return by_pid, (sum(utils) / len(utils) if utils else None)


class DrmGpu:
    """Per-process GPU time from DRM fdinfo (amdgpu, i915; Linux 5.19+).

    Every open handle on a GPU shows up as /proc/<pid>/fdinfo/<fd> with
    cumulative "drm-engine-<name>: <n> ns" counters, readable for your own
    processes without root.
    """

    DRIVERS = ("amdgpu", "i915")

    def __init__(self):
        self.prev = {}  # (device, client id) -> accumulated engine ns
        self.cards = [c for c in glob.glob("/sys/class/drm/card[0-9]*")
                      if re.search(r"card\d+$", c)]
        drivers = [os.path.basename(os.path.realpath(c + "/device/driver")) for c in self.cards]
        self.available = any(d in self.DRIVERS for d in drivers)

    def _clients(self):
        clients = {}  # (device, client id) -> (pid, ns)
        for name in sorted((n for n in os.listdir("/proc") if n.isdigit()), key=int):
            fd_dir = "/proc/%s/fd" % name
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue  # not ours, or gone
            for fd in fds:
                try:
                    if not os.readlink("%s/%s" % (fd_dir, fd)).startswith("/dev/dri/"):
                        continue
                    with open("/proc/%s/fdinfo/%s" % (name, fd)) as fh:
                        text = fh.read()
                except OSError:
                    continue
                cid = dev = None
                ns = 0
                for line in text.splitlines():
                    key, _, val = line.partition(":")
                    val = val.split()
                    if not val:
                        continue
                    if key == "drm-client-id":
                        cid = val[0]
                    elif key == "drm-pdev":
                        dev = val[0]
                    elif key.startswith("drm-engine-") and val[-1] == "ns" and val[0].isdigit():
                        ns += int(val[0])
                # one client is often open on several fds, and inherited across
                # fork; count it once, for the newest process holding it
                if cid is not None:
                    clients[(dev, cid)] = (int(name), ns)
        return clients

    def _system(self):
        utils = []
        for card in self.cards:
            try:
                with open(card + "/device/gpu_busy_percent") as fh:  # amdgpu only
                    utils.append(float(fh.read().strip()))
            except (OSError, ValueError):
                pass
        return max(utils) if utils else None

    def sample(self, dt):
        if not self.available:
            return {}, None
        clients = self._clients()
        by_pid = {}
        if dt and dt > 0:
            for key, (pid, ns) in clients.items():
                delta = ns - self.prev.get(key, ns)
                if delta > 0:
                    by_pid[pid] = by_pid.get(pid, 0.0) + delta / 1e9 / dt * 100
        self.prev = {key: ns for key, (pid, ns) in clients.items()}
        return by_pid, self._system()


class LinuxGpu:
    """NVIDIA and DRM backends together, for machines with both."""

    def __init__(self):
        self.backends = [b for b in (NvidiaGpu(), DrmGpu()) if b.available]
        self.available = bool(self.backends)

    def sample(self, dt):
        by_pid, system = {}, []
        for b in self.backends:
            pids, util = b.sample(dt)
            for pid, pct in pids.items():
                by_pid[pid] = by_pid.get(pid, 0.0) + pct
            if util is not None:
                system.append(util)
        return by_pid, (max(system) if system else None)


def pretty_cmd(p):
    """Readable command line: unwrap Claude Code's zsh wrapper, drop long paths."""
    m = re.search(r"&& eval '(.*)' (?:\\< /dev/null )?&& pwd -P", p.cmd, re.S)
    if m:
        inner = m.group(1).replace("'\"'\"'", "'").replace("\\012", " ")
        return "sh: " + " ".join(inner.split())
    args = p.cmd[len(p.comm):] if p.cmd.startswith(p.comm) else ""
    return os.path.basename(p.comm) + args


class Session:
    def __init__(self, sid, pid):
        self.sid = sid
        self.pid = pid
        self.name = "claude %d" % pid
        self.cwd = ""
        self.status = ""
        self.alive = True
        self.procs = []

    @property
    def cpu(self):
        return sum(p.cpu for p in self.procs)

    @property
    def gpu(self):
        return sum(p.gpu for p in self.procs)

    @property
    def mem(self):
        return sum(p.mem for p in self.procs)

    @property
    def spawned(self):
        return [p for p in self.procs if p.pid != self.pid]


class Sampler:
    def __init__(self):
        self.ncpu = os.cpu_count() or 1
        try:
            if MACOS:
                self.ram = int(run(["sysctl", "-n", "hw.memsize"]).strip())
            else:
                self.ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError):
            self.ram = 0
        self.gpu = MacGpu() if MACOS else LinuxGpu()
        self.prev_cpu = {}      # proc key -> cputime
        self.prev_t = None
        self.sessions = {}      # sid -> Session (kept after exit while orphans live)
        self.sticky = {}        # proc key -> sid, survives reparenting to launchd
        self.sticky_pgid = {}   # pgid -> sid
        self.sys_cpu = 0.0
        self.sys_gpu = None

    def _discover_sessions(self, procs):
        roots = {}  # pid -> sid
        for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
            try:
                with open(path) as fh:
                    d = json.load(fh)
                pid = int(d["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            p = procs.get(pid)
            if p is None:
                continue
            same_start = " ".join(str(d.get("procStart", "")).split()) == p.start
            if not (same_start or is_claude(p) or "claude" in p.cmd.lower()):
                continue  # stale file, pid reused by something else
            sid = d.get("sessionId") or "pid:%d" % pid
            s = self.sessions.get(sid)
            if s is None:
                s = self.sessions[sid] = Session(sid, pid)
            s.pid = pid
            s.cwd = d.get("cwd") or ""
            s.name = d.get("name") or os.path.basename(s.cwd) or s.name
            s.status = d.get("status") or ""
            roots[pid] = sid
        # claude processes with no session file
        for p in procs.values():
            if p.pid not in roots and is_claude(p):
                sid = "pid:%d:%s" % p.key
                if sid not in self.sessions:
                    self.sessions[sid] = Session(sid, p.pid)
                roots[p.pid] = sid
        return roots

    def _attribute(self, procs, roots):
        owner = dict(roots)
        for p in procs.values():
            sid = self.sticky.get(p.key)
            if sid and p.pid not in owner:
                owner[p.pid] = sid

        def resolve(pid):
            chain = []
            while pid not in owner and pid > 1 and pid in procs and pid not in chain:
                chain.append(pid)
                pid = procs[pid].ppid
            sid = owner.get(pid)
            for c in chain:
                owner[c] = sid
            return sid

        for pid in procs:
            resolve(pid)
        # detached process groups that started under a session
        for p in procs.values():
            if owner.get(p.pid) is None and p.pgid in self.sticky_pgid:
                owner[p.pid] = self.sticky_pgid[p.pgid]
        owner = {pid: sid for pid, sid in owner.items() if sid}
        for pid in procs:
            resolve(pid)

        self.sticky = {p.key: owner[p.pid] for p in procs.values() if owner.get(p.pid)}
        live_pgids = set(p.pgid for p in procs.values())
        self.sticky_pgid = {g: s for g, s in self.sticky_pgid.items() if g in live_pgids}
        for p in procs.values():
            sid = owner.get(p.pid)
            if sid and p.pid == p.pgid and p.pid not in roots:
                self.sticky_pgid[p.pgid] = sid
        return owner

    def sample(self):
        now = time.time()
        procs = read_procs()
        dt = (now - self.prev_t) if self.prev_t else None
        gpu_by_pid, self.sys_gpu = self.gpu.sample(dt)
        for pid, pct in gpu_by_pid.items():
            if pid in procs:
                procs[pid].gpu = pct

        if dt and dt > 0:
            total = 0.0
            for p in procs.values():
                # a pid we have not seen before started inside this interval
                before = self.prev_cpu.get(p.key, 0.0)
                p.cpu = max(0.0, p.cputime - before) / dt * 100
                total += p.cpu
            self.sys_cpu = total
        self.prev_cpu = {p.key: p.cputime for p in procs.values()}
        self.prev_t = now

        roots = self._discover_sessions(procs)
        owner = self._attribute(procs, roots)
        live_sids = set(roots.values())
        for s in self.sessions.values():
            s.procs = []
            s.alive = s.sid in live_sids
        for p in procs.values():
            sid = owner.get(p.pid)
            if sid in self.sessions:
                fp = footprint(p.pid)
                if fp:
                    p.mem = fp
                self.sessions[sid].procs.append(p)
        for sid in [sid for sid, s in self.sessions.items() if not s.procs]:
            del self.sessions[sid]
        return list(self.sessions.values())


def fmt_mem(b):
    mb = b / 1048576.0
    if mb >= 1024:
        return "%.1fG" % (mb / 1024)
    return "%.0fM" % mb


SORTS = ("cpu", "mem", "gpu")
ROW = "%-34s %-6s %7s %6s %8s %5s  %s"
A_PLAIN, A_BOLD, A_DIM, A_WARN, A_HOT, A_HEAD = range(6)


def render(sampler, sessions, sort="cpu", expanded=True, width=120, interval=2.0):
    """Returns [(text, style)] lines for either curses or plain output."""
    ncpu = sampler.ncpu
    keyf = lambda o: getattr(o, sort)
    sessions = sorted(sessions, key=keyf, reverse=True)
    all_cpu = sum(s.cpu for s in sessions)
    all_gpu = sum(s.gpu for s in sessions)
    all_mem = sum(s.mem for s in sessions)

    def heat(cpu):
        if cpu >= ncpu * 50:
            return A_HOT
        if cpu >= 100:
            return A_WARN
        return A_BOLD

    gpu_sys = "n/a" if sampler.sys_gpu is None else "%.0f%%" % sampler.sys_gpu
    fmt_gpu = (lambda v: "%.1f" % v) if sampler.gpu.available else (lambda v: "-")
    lines = [
        ("claude-top   system: CPU %.0f%% of %d%% (%d cores, 100%% = 1 core)   GPU %s   RAM %s"
         % (sampler.sys_cpu, ncpu * 100, ncpu, gpu_sys, fmt_mem(sampler.ram)), A_BOLD),
        ("every %.0fs   sort: %s   [q]uit  [s]ort  [e]xpand/collapse  [+/-] interval"
         % (interval, sort), A_DIM),
        ("", A_PLAIN),
        (ROW % ("SESSION", "STATE", "CPU%", "GPU%", "MEM", "PROCS", "HEAVIEST / COMMAND"), A_HEAD),
    ]
    for s in sessions:
        spawned = sorted(s.spawned, key=keyf, reverse=True)
        heaviest = pretty_cmd(spawned[0]) if spawned and keyf(spawned[0]) > 0 else ""
        state = s.status if s.alive else "ended"
        lines.append((ROW % (s.name[:34], state[:6], "%.1f" % s.cpu, fmt_gpu(s.gpu),
                             fmt_mem(s.mem), len(s.procs), heaviest), heat(s.cpu)))
        if not expanded:
            continue
        shown = [p for p in spawned if p.cpu >= 1 or p.gpu >= 1 or p.mem >= 100 * 1048576][:6]
        own = [p for p in s.procs if p.pid == s.pid]
        for p in own + shown:
            label = "claude (the session itself)" if p.pid == s.pid else pretty_cmd(p)
            lines.append((ROW % ("   %-7d" % p.pid, "", "%.1f" % p.cpu, fmt_gpu(p.gpu),
                                 fmt_mem(p.mem), "", label),
                          A_DIM if p.cpu < 100 else A_WARN))
        hidden = len(spawned) - len(shown)
        if hidden > 0:
            rest = [p for p in spawned if p not in shown]
            lines.append((ROW % ("   +%d small" % hidden, "", "%.1f" % sum(p.cpu for p in rest),
                                 fmt_gpu(sum(p.gpu for p in rest)),
                                 fmt_mem(sum(p.mem for p in rest)), "", ""), A_DIM))
    if not sessions:
        lines.append(("no running Claude Code sessions found", A_DIM))
    lines.append(("", A_PLAIN))
    lines.append((ROW % ("ALL SESSIONS (%d)" % len(sessions), "", "%.1f" % all_cpu,
                         fmt_gpu(all_gpu), fmt_mem(all_mem),
                         sum(len(s.procs) for s in sessions), ""), A_BOLD))
    return [(t[:max(1, width - 1)], a) for t, a in lines]


def to_json(sampler, sessions):
    return {
        "system": {"cpu_percent": round(sampler.sys_cpu, 1), "cores": sampler.ncpu,
                   "gpu_percent": sampler.sys_gpu, "ram_bytes": sampler.ram},
        "sessions": [{
            "session_id": s.sid, "name": s.name, "cwd": s.cwd, "pid": s.pid,
            "status": s.status if s.alive else "ended",
            "cpu_percent": round(s.cpu, 1), "gpu_percent": round(s.gpu, 1),
            "mem_bytes": s.mem,
            "processes": [{"pid": p.pid, "cpu_percent": round(p.cpu, 1),
                           "gpu_percent": round(p.gpu, 1), "mem_bytes": p.mem,
                           "command": pretty_cmd(p)[:300]}
                          for p in sorted(s.procs, key=lambda p: p.cpu, reverse=True)],
        } for s in sorted(sessions, key=lambda s: s.cpu, reverse=True)],
    }


def ui(stdscr, sampler, interval):
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_YELLOW, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    styles = {
        A_PLAIN: 0, A_BOLD: curses.A_BOLD, A_DIM: curses.A_DIM,
        A_WARN: curses.color_pair(1) | curses.A_BOLD,
        A_HOT: curses.color_pair(2) | curses.A_BOLD,
        A_HEAD: curses.A_REVERSE,
    }
    stdscr.timeout(100)
    sort, expanded = "cpu", True
    sampler.sample()
    time.sleep(0.5)
    sessions = sampler.sample()
    next_t = time.time() + interval
    dirty = True
    while True:
        if time.time() >= next_t:
            sessions = sampler.sample()
            next_t = time.time() + interval
            dirty = True
        if dirty:
            h, w = stdscr.getmaxyx()
            stdscr.erase()
            for y, (text, style) in enumerate(render(sampler, sessions, sort, expanded, w, interval)[:h]):
                try:
                    stdscr.addstr(y, 0, text, styles[style])
                except curses.error:
                    pass
            stdscr.refresh()
            dirty = False
        ch = stdscr.getch()
        if ch == -1:
            continue
        dirty = True
        if ch in (ord("q"), 27):
            return
        if ch == ord("s"):
            sort = SORTS[(SORTS.index(sort) + 1) % len(SORTS)]
        elif ch == ord("e"):
            expanded = not expanded
        elif ch in (ord("+"), ord("=")):
            interval = min(30.0, interval + 1)
        elif ch == ord("-"):
            interval = max(1.0, interval - 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-i", "--interval", type=float, default=2.0, help="seconds between samples")
    ap.add_argument("--once", action="store_true", help="print one snapshot and exit")
    ap.add_argument("--json", action="store_true", help="print one snapshot as JSON and exit")
    args = ap.parse_args()
    sampler = Sampler()
    if args.once or args.json or not sys.stdout.isatty():
        sampler.sample()
        time.sleep(max(0.5, min(args.interval, 2.0)))
        sessions = sampler.sample()
        if args.json:
            json.dump(to_json(sampler, sessions), sys.stdout, indent=2)
            print()
        else:
            for text, _ in render(sampler, sessions, width=200, interval=args.interval)[2:]:
                print(text)
        return
    try:
        curses.wrapper(ui, sampler, max(1.0, args.interval))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
