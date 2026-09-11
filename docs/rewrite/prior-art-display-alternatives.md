# Prior Art: Display Alternatives (pyte, Textual, Zellij, Ratatui)

The display architecture challenge: Python owns raw bytes from hardware (UART fd, SSH channel, PTY) in BiStream objects. These bytes must be:
1. Matched by pattern-matching oracles — requires raw bytes, not rendered text
2. Displayed to a human in real-time — requires terminal rendering
3. Accessible to Claude via MCP — requires raw bytes

The PTY-tee model (Python tees raw bytes; display layer gets a rendered copy) is architecturally correct. These tools were evaluated as potential display consumers on the right side of that tee.

---

## pyte

**Verdict: Study / Use with caution — correct architecture fit, concerning maintenance.**

### What It Is

pyte is a pure-Python, in-memory VT100/VT220/VTXXX terminal emulator. It simulates what a real terminal does — consuming a byte stream and maintaining a virtual screen — entirely in Python heap, with no actual display surface.

### Input/Output Model

Two classes:

- **`ByteStream`**: accepts raw bytes, decodes to UTF-8, passes decoded text to the parent `Stream` which parses escape sequences and emits events (`DRAW "a"`, `LINEFEED`, `CURSOR_POSITION`, etc.).
- **`Screen`**: receives those events and maintains a sparse `rows × cols` matrix of `Char` objects — each storing a character plus style attributes (fg/bg color, bold, italic, blink).

The rendered output is `screen.display` — a list of Unicode strings, one per row. Styling metadata is accessible per character.

### Data Fidelity

**Confirmed: pyte loses raw bytes**, equivalent in kind to tmux's `capture-pane` (though different in mechanism):
- `ByteStream.feed(raw_bytes)` immediately decodes bytes. Raw bytes are not stored.
- Escape sequences are consumed and discarded after being applied to the character grid. `\x1b[31m` (set red foreground) is gone after the `Char` objects are colored.
- Non-printable bytes that don't correspond to recognized VT sequences are dropped.

**This is intentional and irrelevant for Autopilot.** pyte sits on the display side of the tee. The oracle and MCP paths receive raw bytes directly from BiStream — pyte never touches them.

### Correct Integration Pattern

```python
# BiStream tees raw bytes:
raw_bytes → oracle (raw bytes, unchanged)
          → pyte.ByteStream.feed()  →  pyte.Screen  →  display widget
```

pyte never participates in oracle matching or MCP delivery.

### Performance

14 KB/s (115200 bps UART) is modest for any Python parser. `feed()` is a synchronous, CPU-bound call that completes quickly at this rate — callable from a coroutine without blocking the event loop meaningfully. For safety with multiple simultaneous streams, wrap in `asyncio.run_in_executor()`.

### asyncio Compatibility

Not natively asyncio but fully compatible. `feed()` is synchronous and non-blocking at UART rates; call directly from coroutines or delegate to thread executor at high throughput.

### Maintenance Status

**Concerning.** Last PyPI release: 0.8.0 in 2019. Repo version: `0.8.3.dev` with sporadic commits since. Classified as inactive by PyPI activity metrics. Known rendering quirks (issue #84: edge cases in ANSI sequence handling). Works for common cases but may misrender unusual sequences from embedded hardware.

The rendering failure mode is acceptable for Autopilot: rendering errors only affect the display pane, not the oracle path. The human sees garbled text; the oracle is unaffected.

---

## Textual

**Verdict: Study — the right display framework if Autopilot moves the entire TUI into Python and eliminates tmux as a dependency.**

### Architecture

Textual is a full-screen TUI application framework: asyncio-native throughout, reactive widget system (CSS layout, DOM-like component tree, event propagation), delta-update rendering achieving ~120 FPS. It owns the terminal (raw mode, full-screen alternate buffer) but renders to whatever terminal it is given — including a tmux pane.

It is an *application framework*, not a terminal emulator. It has no built-in widget for displaying raw PTY byte streams.

### VT Byte Stream Display

No built-in primitive. The integration path uses **textual-terminal** (github.com/mitosch/textual-terminal) — a community widget combining pyte (VT emulation) with Textual's `Strip`/`Segment` rendering model:

```python
from textual_terminal import Terminal

# In a Textual App:
class AutopilotUI(App):
    def compose(self):
        yield Terminal(command="cat /dev/null")  # empty; bytes fed programmatically

    async def on_mount(self):
        terminal = self.query_one(Terminal)
        async for chunk in uart_bistream:
            terminal.feed(chunk)  # pyte.ByteStream.feed() internally
```

A custom widget from scratch using pyte + Textual's Line API (implementing `render_line(y)` returning `Strip` objects) is ~500–1000 lines of Python.

### asyncio Native

Yes, natively asyncio. Feeding bytes from an asyncio BiStream to a widget is natural:

```python
async def watch_stream(self, bistream: UartBiStream) -> None:
    while True:
        chunk = await bistream.read()
        self.query_one(TermWidget).feed(chunk)
```

### Running Inside tmux

Textual takes full-screen control but renders to whatever terminal it is given. Running Textual as a tmux pane works — Textual renders within the pane's terminal dimensions.

### Tradeoffs

**Upside:** eliminates tmux as a required dependency; multi-stream dashboard with proper Python widget composition; actively maintained (Textualize, 250k+ PyPI downloads/quarter in 2025).

**Downside:** textual-terminal has 139 stars and 16 commits — not battle-tested. Building a robust VT widget from scratch is significant engineering. pyte's maintenance risk applies to any pyte-backed display widget.

---

## Zellij

**Verdict: Avoid.**

### What It Is

Zellij is a Rust-based terminal multiplexer (tmux alternative) with a plugin system, better defaults, and cleaner UX.

### Python Access

The plugin system compiles to WebAssembly. Rust is the only officially supported language; Python cannot compile to stable WASM for this purpose. No Python plugin SDK.

CLI subprocess interface exists:
```bash
zellij action write-chars "text"
zellij action dump-screen       # → rendered viewport text
zellij subscribe                # → rendered viewport changes as JSON
```

**Critical limitation: `zellij subscribe` and `dump-screen` deliver rendered text, not raw bytes.** Zellij's pane model is: Zellij spawns the process → Zellij owns the PTY → Zellij renders output → you get rendered text back. Python does not own the PTY.

There is no API to hand Zellij an existing file descriptor or SSH channel and say "display this." This is a fundamental architectural mismatch with Autopilot's requirement that Python owns the raw byte streams.

### Verdict

Switching from tmux to Zellij would be a lateral move at best — same structural limitation (multiplexer owns PTY, Python cannot retain raw bytes), added cost (no stable Python plugin API, no mature Python SDK). The CLI control surface is cleaner than tmux's but irrelevant if the architecture remains wrong.

---

## Ratatui

**Verdict: Avoid (for Autopilot).**

### What It Is

Ratatui is a Rust crate for building TUI applications using an immediate-mode rendering model. 20.6k GitHub stars, actively maintained. Rich widget set (paragraphs, tables, gauges, charts).

### Python Bindings

Two projects exist:
- **pyratatui** (github.com/pyratatui/pyratatui): PyO3-based, 35+ widgets, asyncio-native via `AsyncTerminal`, v0.2.8 (April 2026). 119 stars.
- **ratatui-py** (github.com/holo-q/ratatui-py): ctypes/FFI via C ABI shim. Fewer stars.

### VT Byte Stream Display

Neither pyratatui nor Ratatui has a terminal emulator widget for displaying raw PTY/UART byte streams. Ratatui is an *output* framework — it renders your data into the terminal using widgets. There is no `PtyWidget` accepting a file descriptor and emulating VT100.

To use Ratatui for Autopilot's display needs:
1. Implement VT100 byte-stream parsing (or use pyte, though Ratatui is Rust)
2. Convert parsed state to Ratatui `Text`/`Span` objects
3. Render via `Paragraph` widget

This is reimplementing pyte + Textual with a Rust FFI layer. Higher engineering cost, lower Python ecosystem support, no gain over Textual for a Python-first project.

---

## Architectural Recommendation

The current PTY-tee-into-tmux approach is architecturally sound. The question is tmux as the display consumer — not the tee model itself.

The most direct upgrade path is **pyte + Textual**:

- Keep BiStream owning raw bytes
- Keep oracle reading raw bytes directly from BiStream
- Replace the tmux `pipe-pane` with: asyncio BiStream → `pyte.ByteStream.feed()` in a Textual widget (via textual-terminal or a custom widget)
- Textual runs as a tmux pane (or standalone) and displays multiple streams in a dashboard

This eliminates tmux as a *required* display dependency. MCP still gets raw bytes from BiStream. Oracles still get raw bytes. Humans see a Python-rendered TUI. The tradeoffs:

- **Risk**: pyte maintenance (stale, potential rendering quirks from unusual embedded sequences)
- **Cost**: VT widget engineering (~500–1000 lines)
- **Gain**: full Python control of layout, multi-stream dashboards, no tmux hard dependency

If the team decides the engineering cost is not justified, keeping tmux with `pipe-pane -I` is a valid choice — the oracle and MCP paths are unaffected in either case.

---

## Summary Table

| Tool | Owns Raw Bytes | Python-Native | VT Emulator | Asyncio | Maintenance | Verdict |
|---|---|---|---|---|---|---|
| **pyte** | No (display side of tee) | Yes | Yes (output only) | Compatible | Inactive | Study |
| **Textual** | No (display framework) | Yes | Via pyte plugin | Native | Active | Study |
| **Zellij** | No (owns PTY itself) | No (Rust/WASM) | Built-in | N/A | Active | Avoid |
| **Ratatui** | No | Via FFI | No primitive | Via pyratatui | Active | Avoid |

*Sources: [pyte GitHub](https://github.com/selectel/pyte), [pyte docs](https://pyte.readthedocs.io), [Textual GitHub](https://github.com/Textualize/textual), [textual-terminal](https://github.com/mitosch/textual-terminal), [Zellij programmatic control docs](https://zellij.dev/documentation/programmatic-control.html), [Ratatui GitHub](https://github.com/ratatui/ratatui), [pyratatui](https://github.com/pyratatui/pyratatui)*
