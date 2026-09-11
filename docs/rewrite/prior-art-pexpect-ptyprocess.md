# Prior Art: pexpect and ptyprocess

**pexpect verdict: Inspiration only — the match-and-consume intuition is correct, but the architecture is the exact antipattern Autopilot's oracle model addresses.**

**ptyprocess verdict: Use — viable PTY-backed BiStream transport substrate.**

---

## pexpect

### Internal Architecture

pexpect has four layers:

**Layer 1 — Process/FD management** (`pty_spawn.py`, `fdpexpect.py`): `spawn` delegates process creation to `ptyprocess.PtyProcess.spawn()`, which does a `pty.fork()` — atomically creates a PTY master/slave pair and forks. The child's stdio is connected to the PTY slave; the parent holds the master fd in `self.child_fd`. `fdpexpect.fdspawn` accepts any pre-opened fd (socket, serial port, named pipe) and skips the fork — the escape hatch for non-PTY streams.

**Layer 2 — Buffered reading**: uses `select.select()` with a timeout, reads up to `maxread` bytes, appends to internal `_buffer`. `searchwindowsize` controls how many trailing bytes are searched per iteration — a performance knob, not a read cursor. Earlier bytes are preserved but skipped.

**Layer 3 — Pattern matching** (`expect.py`): two searcher classes implementing `search(buffer, freshlen, searchwindowsize)`:
- `searcher_string`: `buffer.find()` across all candidate strings, returns earliest match.
- `searcher_re`: `re.search()` across compiled patterns (all with `re.DOTALL`), returns leftmost-starting match.

**Layer 4 — Expect loop**: loops, calling the searcher on the accumulated buffer. On match, slices the buffer. On no-match, reads more data until timeout.

### Exact Semantics of `expect()`: Match and Consume

```python
# After match found at [start, end):
self.buffer = incoming[searcher.end:]       # remainder after match — preserved
self.before = incoming[:searcher.start]     # bytes before match — discarded
self.after  = incoming[searcher.start:searcher.end]  # the matched text itself
self.match  = searcher.match               # re.MatchObject or string
```

Key points:
- `before` is consumed and **permanently discarded** from the live buffer — no re-scan possible.
- `after` is the matched text itself (common misconception: it is not "text after the match").
- Remainder `incoming[searcher.end:]` is kept for the next `expect()` call.
- Read cursor advances past the match end — this is Autopilot's cursor-advance mechanic, implemented as buffer slice.
- All regexes use `re.DOTALL` — `.` matches newlines, relevant for multi-line boot sequences.
- `$` anchors are unreliable (no line-boundary awareness in a streaming context). Use `\r\n` explicitly — PTY translates `\n` → `\r\n`.

### Mapping to Autopilot Oracle Patterns

**Boot prompt / command / response:**
```python
child = pexpect.spawn('minicom -D /dev/ttyUSB0')
child.expect(r'login:\s*')
child.sendline('root')
child.expect(r'\$\s*')

child.sendline('uname -r')
index = child.expect([r'\d+\.\d+\.\d+', pexpect.TIMEOUT, pexpect.EOF])
if index == 0:
    print('kernel:', child.match.group(0))
```

**Branching on multiple patterns (Choice equivalent):**
```python
index = child.expect(['Press ENTER to boot', 'Error:', 'login:'])
# patterns tried simultaneously against same buffer; leftmost-starting match wins
```

This is the closest pexpect analog to the Choice combinator. **There is no composable type** — just an integer return value that you branch on imperatively.

### What pexpect Lacks (vs. Autopilot Oracle Model)

| Autopilot concept | pexpect equivalent | Gap |
|---|---|---|
| `Oracle: (StreamContext, Timeout) → (Verdict, StreamContext)` | `expect(patterns, timeout)` → int | Returns int; no typed Verdict, no StreamContext |
| `StreamContext` (named BiStream registry) | Single `child_fd` per spawn | No multi-stream, no naming, no registry |
| `Choice` (multiple patterns, one stream) | `expect([p1, p2, p3])` | Equivalent but imperative, not a composable type |
| `Race` (multiple streams) | Not supported | Must thread manually; no native select-across-spawns |
| `Parallel` (forked cursors) | Not supported | Buffer is mutable global state; no snapshot/fork |
| `Sequence` | Sequential `expect()` calls | Equivalent but not composable |
| `Repeat` | Manual loop | Not a combinator |
| Interactive oracle | `interact()` | Hardcoded to stdin/stdout; not extensible to MCP |

### Async Support

pexpect 4.x added `async_=True`:

```python
await child.expect(r'login:\s*', async_=True)
```

The async support is bolted-on: the underlying `Expecter.expect_loop()` is still synchronous; async only changes how it yields between read attempts using `loop.add_reader()`. Known problems:
- Issue #347: `KeyError` on fd registration when multiple SSH sessions share an event loop — reader not properly deregistered on certain EOF paths.
- Python 3.11 broke `async_=True` usage entirely (`asyncio.coroutine` removed); required patching.
- No native `asyncio.StreamReader` integration.

### Production Failure Modes

**Buffer stale data**: buffer is not cleared between `sendline()` and `expect()`. If the previous `expect()` left bytes in the buffer (e.g., echoed shell prompt), the next `expect()` may match stale data — the "matched old prompt instead of new response" failure.

**Kernel log injection**: on UART/serial, kernel debug messages arrive interleaved with application output. A pattern for `login:` may never match if a kernel message bisects it. Requires retry-on-timeout with re-send.

**UART overrun**: `sendline()` transmits at OS speed, which can overflow hardware UART FIFOs on devices without flow control. No built-in rate limiting.

**PTY echo**: `sendline('cmd')` causes the echo `cmd\r\n` to appear in the stream before the response. Patterns must account for this or use `child.setecho(False)`.

### Reusability of pexpect's Internals

**What can be reused:** The `searcher_re` / `searcher_string` classes are independent of the spawn object. They take a buffer, `freshlen`, and optional `searchwindowsize`, and return a match index + `.start`/`.end`/`.match`. This is exactly what Autopilot's Pattern oracle needs internally: given a byte buffer, return which pattern matched earliest and where.

**What cannot be reused:** The expect loop, the buffer object, the fd read machinery — all assume a single spawn-bound fd. No concept of a named stream registry, no cursor-per-stream, no forked cursors for Parallel.

**Verdict:** pexpect's `searcher_re` is viable inspiration. The buffer-slice-and-advance mechanic (`buffer = buffer[match.end():]`) is the correct intuition and should be replicated in Autopilot's BiStream cursor advance. The rest of pexpect's architecture — single-fd, mutable global buffer, blocking loop, bolted-on async — is the exact antipattern the oracle combinator model addresses.

---

## ptyprocess

ptyprocess was split out of pexpect as a standalone library (v0.7.0, 2020). It is pexpect's only subprocess backend; `spawn` creates a `PtyProcess` internally.

### What It Provides Over `os.openpty()` + `subprocess.Popen()`

Raw `os.openpty()` + `subprocess.Popen` with a PTY has race conditions documented in Python's bug tracker: Popen does not atomically fork + close fds. ptyprocess uses `pty.fork()` (atomic fork + PTY pair creation) and adds:

- **Error forwarding pipe**: a pipe from child to parent that carries exec errors. If `exec()` fails, the child writes the exception through the pipe before dying. Parent raises immediately — no silent dead child.
- **Controlled fd cleanup**: all fds except 0/1/2 are closed in the child (with `pass_fds` whitelist). Avoids the fd-inheritance race that plagues threaded `Popen` usage.
- **Terminal configuration before exec**: `setwinsize()` and `setecho()` called in child before `exec()`, with graceful handling of `EINVAL`/`ENOTTY`/`ENXIO` across Linux, BSD, and Solaris.
- **Platform normalization**: Linux reports PTY closure via `errno.EIO`; BSD returns empty bytes. ptyprocess normalizes both to `EOFError`.

### API

```python
from ptyprocess import PtyProcess

p = PtyProcess.spawn(['bash'], dimensions=(24, 80))
# p.fd: integer master PTY fd

data = p.read(1024)          # bytes; raises EOFError on close
p.write(b'ls\n')
p.setwinsize(40, 120)
p.setecho(False)
p.sendcontrol('c')           # sends Ctrl-C (SIGINT via pty line discipline)
p.kill(signal.SIGTERM)
p.terminate(force=True)      # escalates to SIGKILL
p.isalive()                  # polls waitpid(WNOHANG)
```

### Asyncio Compatibility

None built in. The fd is a standard integer, pollable with `select`/`poll`/`epoll`. Integration with asyncio:

```python
loop = asyncio.get_event_loop()
reader = asyncio.StreamReader()
protocol = asyncio.StreamReaderProtocol(reader)
transport, _ = await loop.connect_read_pipe(lambda: protocol, os.fdopen(p.fd, 'rb', 0))
```

Or simpler: `loop.add_reader(p.fd, callback)` for non-blocking reads. This is exactly what pexpect's `_async.py` does internally.

### As a BiStream Foundation

ptyprocess is an excellent PTY-backed BiStream transport substrate:

- Single integer fd usable with `asyncio.add_reader` / `os.read` / `os.write`
- Proper lifecycle: fork, exec error detection, signal, wait
- Terminal dimension control (relevant for interactive oracle — window resize signaling)
- `PtyProcessUnicode` for text-mode streams if needed

What ptyprocess does not provide: buffering, pattern matching, cursor state, read position. That is intentional — it is a pure transport. Autopilot owns the buffer and cursor above it.

**Platform support**: Linux, BSD (macOS), Solaris, AIX. **No Windows** (requires POSIX PTYs).

### Maintenance Status

Actively maintained; part of the pexpect organization. v0.7.0 is current and stable.

---

## Summary

| Concept | pexpect | ptyprocess | Autopilot use |
|---|---|---|---|
| Pattern matching primitive | `searcher_re.search()` — extractable | — | Inspiration for Pattern oracle internals |
| BiStream transport (PTY) | `spawn` (too coupled) | `PtyProcess` — clean fd + lifecycle | Use ptyprocess directly |
| Multi-stream / StreamContext | Not supported | Not applicable | Implement in engine/oracle.py |
| Forked cursors (Parallel) | Not supported | Not applicable | Implement in engine/combinators.py |
| Asyncio integration | Bolted-on, buggy | `add_reader(p.fd, ...)` — clean | Use loop.add_reader |

*Sources: [pexpect docs](https://pexpect.readthedocs.io), [ptyprocess docs](https://ptyprocess.readthedocs.io), [pexpect GitHub issues #50, #347](https://github.com/pexpect/pexpect/issues)*
