# Prior Art: labgrid

**Verdict: Study-only (with selective adapter reuse)**

labgrid is the closest existing open-source project to Autopilot. It handles serial/UART, SSH, power control, USB devices, and pytest integration with distributed resource management. Its board locking and reservation model is the most mature open-source solution to the "CI queue for shared hardware" problem. However, its concurrency model (exclusive lock, single consumer per resource) and execution model (imperative sequential strategies, no composable combinators) are architecturally incompatible with Autopilot's oracle combinator design.

---

## Architecture

### Core Abstractions (Local)

- **Environment**: loads YAML config and instantiates `Target` objects.
- **Target**: central orchestrator per device-under-test. Manages the lifecycle of Resources and Drivers.
- **Resource**: represents a physical interface (serial port path, USB device, power outlet). Pure data — holds connection parameters, does no I/O.
- **Driver**: binds to one or more Resources and implements one or more Protocols. Does the actual I/O. Declares dependencies via a `bindings` dict; Target validates at instantiation.
- **Protocol**: an abstract interface class (`ConsoleProtocol`, `PowerProtocol`, `CommandProtocol`). Drivers are discovered by protocol type, not name.
- **Strategy**: orchestrates multi-driver state sequences. Inherits from Driver and exposes a `transition(state)` method.

### Distributed Infrastructure

Three networked components:

- **Coordinator**: gRPC server. Central registry of all exported resources and Places. Handles place acquisition/release, reservations, and mutual exclusion. Does not proxy data — it only brokers control.
- **Exporter**: runs on a host connected to hardware. Registers resources with the coordinator via gRPC bidirectional streaming. For serial ports, typically spawns `ser2net` to expose UART over RFC2217.
- **Client**: queries the coordinator, acquires places, then connects directly to exporters for data-plane access.

Control plane through coordinator (gRPC). Data plane directly to exporter (ser2net RFC2217, SSH, etc.). This is a clean separation.

---

## Serial/UART, SSH, and Power Control

### UART/Serial

- `RawSerialPort`: local `/dev/ttyUSBx`
- `NetworkSerialPort`: remote serial via RFC2217 or raw TCP
- `USBSerialPort`: USB serial with udev property matching

`SerialDriver` implements `ConsoleProtocol` and `ConsoleExpectMixin`, which wraps pexpect:

```python
# expect returns (index, before, match, after)
console.expect(['login failed', r'root@\w+:'], timeout=10)
console.sendline('root')
console.settle(timeout=5)  # confirms silence on channel
```

`ShellDriver` stacked above provides `CommandProtocol` (login, shell prompt detection, command execution with exit-code parsing).

### SSH

`SSHDriver` uses an OpenSSH ControlMaster socket for connection multiplexing:
- Persistent master process with `ControlPersist=300`
- `run(cmd)` → `(stdout, stderr, returncode)`
- `interact()` spawns interactive TTY with `-t`
- `put()`/`get()` via SCP/SFTP; `rsync()`; `sshfs()` mount

### Power Control

Extensive: 25+ PDU models (Gude, APC, Raritan), GPIO/relay, MQTT (Tasmota), USB hub port switching, Modbus TCP, HID relay. All implement `PowerProtocol` with `on()`, `off()`, `cycle()`.

---

## Workflow Execution: GraphStrategy

labgrid's workflow primitive is GraphStrategy — the closest analog to Autopilot's oracle sequences:

```python
from labgrid.strategy import GraphStrategy, depends

class BoardStrategy(GraphStrategy):
    bindings = {
        'power': PowerProtocol,
        'console': ConsoleProtocol,
        'shell': ShellDriver,
    }

    def state_off(self):
        self.power.off()

    @depends('off')
    def state_uboot(self):
        self.power.on()
        self.console.expect('U-Boot', timeout=30)

    @depends('uboot')
    def state_shell(self):
        self.uboot.boot('')
        self.shell.login()
```

`@depends` forms a DAG. `transition(target_state)` computes the minimal path from the current state and executes only the delta. Supports graphviz visualization.

**This is imperative sequential state machines.** There are no composable `Sequence`, `Choice`, `Race`, `Parallel`, or `Repeat` primitives — Python code inside each `state_X` method, calling drivers directly.

---

## Resource Allocation and Board Locking

labgrid's strongest component, directly relevant to Autopilot's CI queue needs.

**Places** are named logical groupings of resources with wildcard match patterns.

**Acquisition flow:**
1. Client calls `AcquirePlace(place_name)` on coordinator
2. Coordinator checks: place not already acquired, no orphaned resource conflicts
3. Coordinator notifies exporters via gRPC streaming
4. Coordinator records `(user, host)` as holder

**Reservation system (for CI queuing):**
```bash
labgrid-client reserve board=imx6-foo   # returns token
labgrid-client wait                       # blocks until allocated
labgrid-client -p + lock                  # acquire the reserved place
```
Reservation states: `waiting → allocated → acquired → expired`. Places are tagged with key=value pairs; reservation filter matches against tags, supporting priority scheduling.

**Orphan recovery**: if an exporter disconnects while a place is acquired, resources become "orphaned." Coordinator periodically reacquires when exporters reconnect — resilient to network transients.

---

## Fundamental Limitations vs. Autopilot's Oracle Model

### No combinator algebra

Strategies are imperative. There are no composable combinators. Every composition is hand-coded Python. Autopilot's `Sequence(A, B)`, `Race([A, B])`, `Timeout(30, oracle)` have no equivalent.

### No BiStream / multi-consumer model

labgrid assumes a single active consumer of any console at a time. No `BiStream` abstraction, no tee/fan-out of byte streams, no concurrent oracle evaluation on the same stream. A "simultaneous PTY + MCP" architecture is architecturally impossible without significant extension.

### No named stream registry

`StreamContext` is a named registry any combinator can look up (`"uart0"`, `"ssh_session"`). labgrid drivers are bound at Target construction time; runtime dynamic stream lookup is not supported.

### No Parallel with forked cursors

Autopilot's `Parallel` snapshots the StreamContext per branch. labgrid's Target object is global mutable state — not something you snapshot, fork, or thread through combinators.

### No Timeout combinator

labgrid timeouts are per-call parameters to `expect()`. There is no way to apply a budget to a composed sequence without manual deadline arithmetic.

### No Zenoh/pub-sub integration

labgrid has no middleware integration. Autopilot's VCMux/Zenoh pub-sub VM console multiplexing has no analog.

### No MCP server concept

labgrid has no mechanism to expose console sessions to AI agents. `interact()` is purely for human terminal use.

### No concurrent dual-consumer

labgrid's place-locking model means: one client holds the lock at a time. While a test runs, no other client can access the resource. "Simultaneous tmux pane + MCP + oracle" is impossible — labgrid assumes one consumer per console.

### Single resource per driver type

GitHub issue #109 (open): multiple driver/resource instances of the same type on a single target (e.g., two serial ports) is not well supported. Autopilot routinely needs multiple named streams per target.

### Shared filesystem assumption

Distributed labgrid assumes NFS or SMB for file sharing between coordinator host and exporter hosts. Not a constraint Autopilot has.

---

## What Is Worth Adopting

### Study-only (architecture reference)

- **Board locking model**: the coordinator/place/reservation system is the definitive open-source implementation of "CI queue for shared hardware." Study its reservation state machine, orphan recovery, and distributed synchronization before designing Autopilot's resource allocation.
- **GraphStrategy DAG**: the `@depends` + incremental path execution pattern is clean for "bring to known state" sequencing. Autopilot's chain oracle sequences could be informed by this.
- **Udev-based resource discovery**: dynamic USB device tracking via udev events is directly applicable to Autopilot's USB relay and USB serial device management.
- **ser2net integration pattern**: the exporter's use of `ser2net` to expose UART as RFC2217 TCP is a proven pattern for remote serial access.

### Potentially reusable as adapter backends

- **Power driver implementations**: the `labgrid/driver/power/` submodule contains standalone HTTP/SNMP clients for 25+ PDU models. Autopilot could import specific power backend modules (e.g., `labgrid.driver.power.gude`) directly as adapters.
- **USBSerialPort udev matching**: the udev property matching (`ID_SERIAL_SHORT`, `ID_PATH`) for stable USB-path-based device identification is directly reusable.
- **SSHDriver ControlMaster pattern**: the persistent SSH master + keepalive subprocess is a practical pattern Autopilot's SSH BiStream backend should adopt.

### Not reusable

- `SerialDriver`/`ConsoleExpectMixin`: tightly coupled to pexpect's synchronous blocking model.
- `Strategy`: assumes single target, single consumer, synchronous transitions.
- `Environment`/`Target`: object model does not map to Autopilot's named-stream registry.

---

## Summary

labgrid confirms the problem space is non-trivial and provides a mature reference implementation for resource management. Its execution model and concurrency model are wrong for Autopilot's oracle combinator design. The most actionable takeaway: study labgrid's coordinator/exporter/place model as the reference architecture for Autopilot's resource allocation subsystem, and import specific power driver backends as thin adapters. Do not build Autopilot's oracle execution engine on top of labgrid.

---

*Sources: [labgrid docs](https://labgrid.readthedocs.io), [GitHub](https://github.com/labgrid-project/labgrid), [design decisions](https://labgrid.readthedocs.io/en/stable/design_decisions.html), GitHub issue #109*
