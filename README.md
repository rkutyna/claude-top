# claude-top

`top` for [Claude Code](https://claude.com/claude-code): see how much CPU, GPU and memory each
session — and everything it spawned — is using, across all your running sessions at once.

Useful when your laptop fans spin up and you want to know *which* session started the test
suite, build or training run that is responsible.

```
claude-top   system: CPU 305% of 1100% (11 cores, 100% = 1 core)   GPU 18%   RAM 18.0G
every 2s   sort: cpu   [q]uit  [s]ort  [e]xpand/collapse  [+/-] interval

SESSION                            STATE     CPU%   GPU%      MEM PROCS  HEAVIEST / COMMAND
api-refactor                       busy     190.2    0.0     781M     5  python -m pytest -q
   3784                                       0.9    0.0     235M        claude (the session itself)
   9328                                      96.7    0.0     270M        python -m pytest -q
   9327                                      92.5    0.0     274M        python -m pytest -q
   +2 small                                   0.0    0.0       3M
docs-site                          idle       0.5    0.0     189M     1
   3048                                       0.5    0.0     189M        claude (the session itself)

ALL SESSIONS (2)                            190.7    0.0     970M     6
```

## Install

One file, Python 3.8+ standard library only, no root needed.

```bash
curl -fsSL https://raw.githubusercontent.com/rkutyna/claude-top/main/claude_top.py -o ~/.local/bin/claude-top
chmod +x ~/.local/bin/claude-top
```

(Any directory on your `PATH` works.)

## Use

```bash
claude-top            # live dashboard
claude-top --once     # print one snapshot and exit
claude-top --json     # one snapshot as JSON, for scripts
claude-top -i 5       # sample every 5 seconds
```

Keys in the dashboard: `s` cycles the sort (CPU, memory, GPU), `e` collapses to one line per
session, `+` / `-` change the refresh interval, `q` quits.

## Reading the numbers

- **CPU%** is `top`-style: 100% is one full core. A session row turns yellow above one core and
  red above half of all cores.
- **MEM** is physical footprint on macOS (the figure Activity Monitor shows) and PSS on Linux, so
  summing across processes does not double-count shared memory.
- **GPU%** is the share of GPU time used by the session's processes.
- Each session's total includes the `claude` process itself, which is listed separately so the
  things it launched stand out.
- A session whose `claude` process has exited but whose processes are still running stays
  listed with state `ended`.

## How it works

Claude Code records each running session in `~/.claude/sessions/<pid>.json` (name, working
directory, busy/idle). `claude-top` reads those, walks the process tree under each session's
PID, and sums usage. Processes that detach from their parent (`nohup`, daemons) are remembered
by PID and process group, so they stay attributed to the session that started them.

`CLAUDE_CONFIG_DIR` is honoured if you keep your Claude config somewhere other than `~/.claude`.

## Platform support

| | CPU / memory | GPU |
|---|---|---|
| macOS, Apple Silicon | yes | yes, per process (Metal clients, via `ioreg`) |
| macOS, Intel | yes | not expected to work |
| Linux | yes (`/proc`) | NVIDIA only, via `nvidia-smi pmon` |

Tested on macOS with real Claude Code sessions. The Linux backend has been tested against a
simulated session, not a real Claude Code install, and the NVIDIA path has not been run on real
hardware — reports welcome.

## Limitations

- A process that detaches within its first couple of seconds, before a sample sees it under its
  session, is not attributed.
- Processes started by a shared host (for example an MCP server launched by the desktop app
  rather than by a session) are not attributed to any one session.
- The session file format is internal to Claude Code and could change between versions.
- Not affiliated with Anthropic.

## License

MIT
