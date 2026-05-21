"""
UART adapter: UARTBiStream backed by pyserial-asyncio-fast.

pyserial-asyncio-fast is used instead of the original pyserial-asyncio
(original is being deprecated by Home Assistant 2026-07 and blocks the
event loop on some platforms).

The old system used synchronous serial.Serial in a dedicated thread.
The new adapter is fully asyncio-native: serial reads are driven by the
event loop, no thread needed.

Buffer reset on open: the old system calls reset_input_buffer() +
reset_output_buffer() immediately after opening. This purges any bytes
that arrived on the UART before Autopilot connected — essential for
deterministic pattern matching. The new adapter does the same via
writer.transport.serial (the underlying pyserial instance).

Usage:
    stream = await open_uart("/dev/ttyACM0", baudrate=115200)
    ctx.streams["tty0"] = stream
    ctx.register_cleanup("tty0", stream.close)
"""

from __future__ import annotations

import asyncio

import serial_asyncio_fast
import structlog

from engine.oracle import Error, Matched, StreamContext, Verdict

log = structlog.get_logger()


class UARTBiStream:
    """
    Bidirectional serial UART stream.

    Read: asyncio.StreamReader driven by pyserial-asyncio-fast transport.
    Write: pyserial-asyncio-fast transport (non-blocking, writes to kernel buffer).
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        device: str,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._device = device
        self._closed = False

    async def read(self, n: int = 4096) -> bytes:
        return await self._reader.read(n)

    async def write(self, data: bytes) -> None:
        self._writer.write(data)
        # drain() flushes the kernel write buffer — important for UART where
        # writes larger than the buffer would otherwise be silently truncated.
        await self._writer.drain()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._writer.close()
            except Exception:
                pass
            log.debug("uart.closed", device=self._device)

    @property
    def device(self) -> str:
        return self._device


class UARTSourceOracle:
    """
    Open a UART device and register it as a named stream in ctx.

    Maps to the old chain 'map_source' step type.
    Registers a cleanup hook that closes the UART on chain teardown.
    """

    def __init__(
        self,
        stream_name: str,
        device: str,
        baudrate: int = 115200,
        preprocess: bool = True,
    ) -> None:
        self._stream_name = stream_name
        self._device = device
        self._baudrate = baudrate
        self._preprocess = preprocess

    async def __call__(
        self, ctx: StreamContext, timeout: float
    ) -> tuple[Verdict, StreamContext]:
        try:
            stream = await open_uart(self._device, self._baudrate)
        except Exception as exc:
            log.warning(
                "uart_source.open_failed",
                stream=self._stream_name,
                device=self._device,
                error=repr(exc),
            )
            return Error(f"uart_open_failed: {exc}"), ctx

        ctx.register_cleanup(self._stream_name, stream.close)
        if self._preprocess:
            from engine.primitives import FilterBiStream
            stream = FilterBiStream(stream)
        ctx.streams[self._stream_name] = stream
        log.info(
            "uart_source.ready",
            stream=self._stream_name,
            device=self._device,
        )
        return Matched("ok"), ctx


async def open_uart(device: str, baudrate: int = 115200) -> UARTBiStream:
    """
    Open a UART device and return a UARTBiStream.

    Resets input and output buffers immediately after opening to discard
    any bytes that arrived before Autopilot connected. This is required for
    deterministic pattern matching — stale boot output from a previous session
    must not be seen by oracles in the new chain.

    Register the returned stream's close() as a cleanup_hook:
        stream = await open_uart("/dev/ttyACM0")
        ctx.streams["tty0"] = stream
        ctx.register_cleanup("tty0", stream.close)
    """
    log.info("uart.opening", device=device, baudrate=baudrate)
    reader, writer = await serial_asyncio_fast.open_serial_connection(
        url=device, baudrate=baudrate
    )

    # Reset buffers to discard pre-connect UART traffic.
    # writer.transport is a SerialTransport; .serial is the underlying pyserial instance.
    try:
        ser = writer.transport.serial
        ser.reset_input_buffer()
        ser.reset_output_buffer()
    except AttributeError:
        log.warning("uart.buffer_reset_unavailable", device=device)

    log.info("uart.opened", device=device, baudrate=baudrate)
    return UARTBiStream(reader, writer, device)
