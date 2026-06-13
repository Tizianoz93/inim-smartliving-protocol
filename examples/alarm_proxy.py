#!/usr/bin/env python3
"""
Transparent TCP proxy for INIM SmartLiving protocol capture.

Listens on a local port and forwards all traffic to the alarm panel while
logging every byte (text log + optional binary dump).

Usage:
    python alarm_proxy.py --target 192.168.1.50
    python alarm_proxy.py --target panel.local --listen-port 5004

    # or set environment variables:
    export INIM_HOST=192.168.1.50
    python alarm_proxy.py

Point SmartLeague (or any client) at this machine's IP and listen port instead
of the panel directly.

During capture, press SPACE to insert log markers:
  1st press → START marker (before an action)
  2nd press → END marker   (after an action)
  alternating pairs for each operation.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime

BUFFER_SIZE = 4096
MARKER_LINE = "=" * 80


@dataclass(frozen=True)
class ProxyConfig:
    listen_host: str
    listen_port: int
    target_host: str
    target_port: int
    log_dir: str


def parse_args(argv: list[str] | None = None) -> ProxyConfig:
    parser = argparse.ArgumentParser(
        description="TCP proxy for INIM SmartLiving traffic capture (port 5004)",
    )
    parser.add_argument(
        "--target", "-t",
        default=os.environ.get("INIM_HOST"),
        help="Panel IP or hostname to forward to (or set INIM_HOST)",
    )
    parser.add_argument(
        "--target-port",
        type=int,
        default=int(os.environ.get("INIM_PORT", "5004")),
        help="Panel TCP port (default: 5004, or INIM_PORT)",
    )
    parser.add_argument(
        "--listen", "-l",
        default=os.environ.get("INIM_PROXY_LISTEN", "0.0.0.0"),
        help="Local bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--listen-port", "-p",
        type=int,
        default=int(os.environ.get("INIM_PROXY_PORT", "5004")),
        help="Local listen port (default: 5004, or INIM_PROXY_PORT)",
    )
    parser.add_argument(
        "--log-dir",
        default=os.environ.get("INIM_PROXY_LOG_DIR", "capture_logs"),
        help="Directory for session logs (default: capture_logs)",
    )
    args = parser.parse_args(argv)

    if not args.target:
        parser.error(
            "panel address required: use --target HOST or set INIM_HOST"
        )

    return ProxyConfig(
        listen_host=args.listen,
        listen_port=args.listen_port,
        target_host=args.target,
        target_port=args.target_port,
        log_dir=args.log_dir,
    )


class MarkerState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pair = 1
        self._expect_start = True

    def toggle(self, log: logging.Logger) -> None:
        with self._lock:
            if self._expect_start:
                label = f"START #{self._pair}"
                self._expect_start = False
            else:
                label = f"END #{self._pair}"
                self._pair += 1
                self._expect_start = True

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        log.info("")
        log.info(MARKER_LINE)
        log.info(f"[MARKER {label}] {ts}")
        log.info(MARKER_LINE)
        log.info("")


def keyboard_listener(stop: threading.Event, markers: MarkerState, log: logging.Logger) -> None:
    """Listen for space bar in the terminal (non-blocking for the proxy)."""
    if sys.platform == "win32":
        import msvcrt

        while not stop.is_set():
            if msvcrt.kbhit() and msvcrt.getch() == b" ":
                markers.toggle(log)
            stop.wait(0.05)
        return

    if not sys.stdin.isatty():
        return

    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop.is_set():
            ready, _, _ = select.select([sys.stdin], [], [], 0.05)
            if ready and sys.stdin.read(1) == " ":
                markers.toggle(log)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def hexdump(data: bytes, prefix: str = "") -> str:
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i : i + 16]
        hex_part = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{prefix}{i:04X}  {hex_part:<47}  {asc}")
    return "\n".join(lines)


def try_decode(data: bytes) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            decoded = data.decode(enc)
            if all(32 <= ord(c) < 127 or c in "\r\n\t" for c in decoded):
                return f"[{enc}] {decoded.strip()!r}"
        except Exception:
            pass
    return f"[binary] {data.hex()}"


class Session:
    _counter = 0
    _counter_lock = threading.Lock()

    def __init__(
        self,
        client_sock: socket.socket,
        client_addr: tuple,
        config: ProxyConfig,
        session_ts: str,
        log: logging.Logger,
    ) -> None:
        with Session._counter_lock:
            Session._counter += 1
            self.sid = Session._counter

        self.client_sock = client_sock
        self.client_addr = client_addr
        self.config = config
        self.session_ts = session_ts
        self.log = log
        self.central_sock: socket.socket | None = None
        self._closed = False

        self.log.info(f"[S{self.sid}] New connection from {client_addr}")

    def connect_to_panel(self) -> bool:
        try:
            self.central_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.central_sock.settimeout(10)
            self.central_sock.connect((self.config.target_host, self.config.target_port))
            self.central_sock.settimeout(None)
            self.log.info(
                f"[S{self.sid}] Connected to panel "
                f"{self.config.target_host}:{self.config.target_port}"
            )
            return True
        except OSError as exc:
            self.log.error(f"[S{self.sid}] Cannot reach panel: {exc}")
            return False

    def _forward(
        self,
        src: socket.socket,
        dst: socket.socket,
        direction: str,
        raw_fh,
    ) -> None:
        arrow = "→ PANEL" if direction == "TX" else "← PANEL"
        try:
            while not self._closed:
                try:
                    data = src.recv(BUFFER_SIZE)
                except OSError:
                    break

                if not data:
                    self.log.info(
                        f"[S{self.sid}] {direction}: peer closed connection"
                    )
                    break

                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self.log.info(f"[S{self.sid}] {arrow}  {len(data)} bytes  @ {ts}")
                self.log.debug(f"\n{hexdump(data, prefix='  ')}")
                self.log.debug(f"  text: {try_decode(data)}")

                ts_ms = int(time.time() * 1000) & 0xFFFFFFFF
                dir_byte = 0 if direction == "TX" else 1
                raw_fh.write(ts_ms.to_bytes(4, "big"))
                raw_fh.write(dir_byte.to_bytes(1, "big"))
                raw_fh.write(len(data).to_bytes(4, "big"))
                raw_fh.write(data)
                raw_fh.flush()

                try:
                    dst.sendall(data)
                except OSError as exc:
                    self.log.error(f"[S{self.sid}] Forward error ({direction}): {exc}")
                    break
        finally:
            self._closed = True

    def run(self) -> None:
        os.makedirs(self.config.log_dir, exist_ok=True)
        session_raw = os.path.join(
            self.config.log_dir,
            f"session_{self.session_ts}_s{self.sid}.bin",
        )
        with open(session_raw, "wb") as raw_fh:
            if not self.connect_to_panel():
                self.client_sock.close()
                return

            t_tx = threading.Thread(
                target=self._forward,
                args=(self.client_sock, self.central_sock, "TX", raw_fh),
                daemon=True,
                name=f"S{self.sid}-TX",
            )
            t_rx = threading.Thread(
                target=self._forward,
                args=(self.central_sock, self.client_sock, "RX", raw_fh),
                daemon=True,
                name=f"S{self.sid}-RX",
            )
            t_tx.start()
            t_rx.start()
            t_tx.join()
            t_rx.join()

        self.log.info(f"[S{self.sid}] Session ended")
        for sock in (self.client_sock, self.central_sock):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass


def setup_logging(log_dir: str) -> tuple[logging.Logger, str, str]:
    os.makedirs(log_dir, exist_ok=True)
    session_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = os.path.join(log_dir, f"session_{session_ts}.log")

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
        force=True,
    )
    return logging.getLogger("proxy"), session_ts, log_file


def main(argv: list[str] | None = None) -> int:
    config = parse_args(argv)
    log, session_ts, log_file = setup_logging(config.log_dir)
    markers = MarkerState()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((config.listen_host, config.listen_port))
    server.listen(5)

    log.info(f"Listening on {config.listen_host}:{config.listen_port}")
    log.info(f"Forwarding → {config.target_host}:{config.target_port}")
    log.info(f"Session log: {log_file}")
    log.info(
        "Log markers: press SPACE for START/END pairs around manual actions."
    )
    log.info("Ready. Point your client software at this host.\n")

    kb_stop = threading.Event()
    kb_thread = threading.Thread(
        target=keyboard_listener,
        args=(kb_stop, markers, log),
        daemon=True,
        name="keyboard-markers",
    )
    kb_thread.start()

    shutdown = False

    def _shutdown_handler(_sig, _frame) -> None:
        nonlocal shutdown
        shutdown = True
        kb_stop.set()
        try:
            server.close()
        except OSError:
            pass

    signal.signal(signal.SIGINT, _shutdown_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _shutdown_handler)

    server.settimeout(1.0)

    try:
        while not shutdown:
            try:
                client_sock, client_addr = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            session = Session(client_sock, client_addr, config, session_ts, log)
            threading.Thread(
                target=session.run,
                daemon=True,
                name=f"session-{session.sid}",
            ).start()
    except KeyboardInterrupt:
        shutdown = True
    finally:
        kb_stop.set()
        try:
            server.close()
        except OSError:
            pass

    if shutdown:
        log.info("Proxy stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
