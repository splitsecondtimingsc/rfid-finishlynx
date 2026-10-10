#!/usr/bin/env python3
"""Main entry point for RFID-to-FinishLynx middleware."""

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Optional

from .config import Config, load_config

logger = logging.getLogger(__name__)


def setup_logging(config: Config):
    """Configure logging based on configuration.

    Args:
        config: Application configuration.
    """
    console_level = getattr(logging, config.logging.level.upper(), logging.INFO)

    # Root logger at DEBUG so the web log handler captures everything
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Suppress noisy third-party loggers
    logging.getLogger('sllurp').setLevel(logging.WARNING)
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('engineio').setLevel(logging.WARNING)
    logging.getLogger('socketio').setLevel(logging.WARNING)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler at user-configured level
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(console_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    # File handler at DEBUG level; a new file each day, two weeks kept
    # (rfid_timing.log.2026-10-01 ...). It was one ever-growing file.
    if config.logging.file:
        fh = DailyLogFile(config.logging.file)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root.addHandler(fh)


class DailyLogFile(TimedRotatingFileHandler):
    """Rotates at midnight. On Windows the rename fails while another
    program has the file open; then today's file simply continues (and
    nothing is lost) instead of every later line failing to rotate."""

    def __init__(self, filename: str, keep_days: int = 14):
        super().__init__(filename, when="midnight", backupCount=keep_days, encoding="utf-8", delay=False)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                self.stream = self._open()
            self.rolloverAt = self.computeRollover(int(time.time()))


def find_config_path(base: Optional[Path] = None) -> Optional[str]:
    """Find config.yaml in standard locations.

    config.yaml is per-machine (the console saves settings into it) and is
    not in git. A fresh clone has only config.example.yaml; on first run it
    is copied to config.yaml next to it."""
    base = base or Path(__file__).parent.parent
    search_paths = [
        Path.cwd() / "config.yaml",
        base / "config.yaml",
    ]
    for path in search_paths:
        if path.exists():
            return str(path)
    example = base / "config.example.yaml"
    if example.exists():
        target = base / "config.yaml"
        shutil.copyfile(example, target)
        logger.info(f"No config.yaml found - created {target} from config.example.yaml")
        return str(target)
    return None


def run_cli_mode(config: Config):
    """Run in CLI mode without web interface."""
    from .finishlynx import FinishLynxOutput, FinishLynxListener # NEW: Import FinishLynxListener
    from .lora_receiver import LoRaReceiver
    from .rfid_reader import RFIDReader, TagRead, create_reader
    from .transport import LoRaTransport

    running = True

    def signal_handler(signum, frame):
        nonlocal running
        logger.info("Shutdown signal received")
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if config.mode == "finish_line":
        # Finish line mode
        finishlynx = FinishLynxOutput(config.finishlynx)
        if not finishlynx.connect():
            logger.warning("Failed to connect to FinishLynx (Output) - continuing anyway")
            # If output connection fails, we might still want to listen, but for sending, it's an issue.
            # Decide if this should stop the app or just warn. For now, warn.

        finishlynx_listener = FinishLynxListener(config.finishlynx_listener.host, config.finishlynx_listener.port)
        finishlynx_listener.start() # NEW: Start the listener

        def on_tag_read(tag: TagRead):
            if finishlynx.is_connected:
                finishlynx.send_split(tag.bib, tag.timestamp)

        reader = create_reader(
            config.reader,
            config.timing,
            on_tag_read=on_tag_read,
            reader_id="FINISH",
        )
        if not reader.connect():
            logger.error("Failed to connect to RFID reader")
            running = False # Set running to False if RFID reader connection fails
            # sys.exit(1) # Removed sys.exit(1) to allow graceful shutdown

        lora_receiver = None
        if config.lora.enabled:
            def on_lora_read(message):
                if finishlynx.is_connected:
                    finishlynx.send_split(message.bib, message.timestamp)

            lora_receiver = LoRaReceiver(config.lora, on_message=on_lora_read)
            lora_receiver.start()

        logger.info("Finish line mode started (CLI)")

        while running:
            if not finishlynx.is_connected:
                finishlynx.reconnect()
            time.sleep(1.0)

        if lora_receiver:
            lora_receiver.stop()
        reader.disconnect()
        finishlynx.disconnect()
        finishlynx_listener.stop() # NEW: Stop the listener during shutdown

    elif config.mode == "remote_lap":
        # Remote lap mode
        lora = LoRaTransport(config.lora)
        if not lora.connect():
            logger.error("Failed to connect to LoRa")
            sys.exit(1)

        def on_tag_read(tag: TagRead):
            lora.send_tag_read(tag.bib, tag.timestamp, config.lora.reader_id)

        reader = create_reader(
            config.reader,
            config.timing,
            on_tag_read=on_tag_read,
            reader_id=config.lora.reader_id,
        )
        if not reader.connect():
            logger.error("Failed to connect to RFID reader")
            sys.exit(1)

        logger.info(f"Remote lap mode started (CLI, reader ID: {config.lora.reader_id})")

        while running:
            if not lora.is_connected:
                lora.connect()
            time.sleep(1.0)

        reader.disconnect()
        lora.disconnect()

    logger.info("Shutdown complete")


def run_web_mode(config: Config, config_path: str, host: str, port: int):
    """Run with web interface."""
    from .app import Application
    from .web import (
        emit_remote_unit_update, emit_tag_read, install_log_handler,
        process_read_for_finishlynx, run_web_server, set_application,
    )

    install_log_handler()

    app = Application(config, config_path)
    set_application(app)

    # Set up callbacks for web UI
    app.set_tag_read_callback(emit_tag_read)
    app.set_remote_unit_change_callback(emit_remote_unit_update)
    app.set_process_for_finishlynx_callback(process_read_for_finishlynx)

    # Handle shutdown
    def signal_handler(signum, frame):
        logger.info("Shutdown signal received")
        app.shutdown()
        os._exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Auto-start at boot on both ends: a station (or the finish laptop)
    # should reach a running state with no clicks, retrying until the
    # pieces it needs come up. Once running, per-device reconnection is
    # the hardware supervisor's job; if the operator presses Stop later,
    # this loop is already done and won't fight them.
    import threading

    def _auto_start():
        while True:
            try:
                if app.start():
                    return
            except Exception:
                logger.exception("Auto-start attempt crashed")
            logger.warning("Auto-start: not up yet, retrying in 10 s")
            time.sleep(10)

    threading.Thread(target=_auto_start, daemon=True).start()

    logger.info(f"Starting web interface at http://{host}:{port}")
    print(f"\n  Open your browser to: http://{host}:{port}\n")

    # Run web server (blocks)
    run_web_server(host=host, port=port)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="RFID-to-FinishLynx middleware",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                      # Run with web interface
  %(prog)s --no-web             # Run in CLI mode only
  %(prog)s --web-port 8080      # Web interface on port 8080
  %(prog)s --reader-host 10.0.15.104  # Override reader IP
        """,
    )
    parser.add_argument(
        "-c", "--config",
        help="Path to configuration file (default: config.yaml)",
        default=None,
    )
    parser.add_argument(
        "-m", "--mode",
        choices=["finish_line", "remote_lap"],
        help="Operating mode (overrides config file)",
        default=None,
    )
    parser.add_argument(
        "--reader-host",
        help="RFID reader IP address (overrides config file)",
        default=None,
    )
    parser.add_argument(
        "--finishlynx-host",
        help="FinishLynx IP address (overrides config file)",
        default=None,
    )
    parser.add_argument(
        "--lora-port",
        help="LoRa serial port (overrides config file)",
        default=None,
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose logging (DEBUG level)",
    )
    parser.add_argument(
        "--no-web",
        action="store_true",
        help="Run without web interface (CLI only)",
    )
    parser.add_argument(
        "--web-host",
        default=None,
        help="Web interface address (default: 127.0.0.1 on the finish line - "
             "nothing else on the network uses its console; 0.0.0.0 on a station, "
             "whose dashboard is opened from the laptop)",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8080,
        help="Web interface port (default: 8080)",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Web UI only: never open the reader, clocks, radio, camera or FinishLynx "
             "(for trying a change next to the live console; see hw_guard.py)",
    )

    args = parser.parse_args()
    if args.preview:
        os.environ["RFID_PREVIEW"] = "1"

    # Find config path
    config_path = args.config or find_config_path()

    # Load configuration
    config = load_config(config_path)

    # Apply command-line overrides
    if args.mode:
        config.mode = args.mode
    if args.reader_host:
        config.reader.host = args.reader_host
    if args.finishlynx_host:
        config.finishlynx.host = args.finishlynx_host
    if args.lora_port:
        config.lora.port = args.lora_port
        config.lora.enabled = True
    if args.verbose:
        config.logging.level = "DEBUG"

    # Setup logging
    setup_logging(config)

    logger.info(f"RFID-FinishLynx middleware starting in {config.mode} mode")

    if args.no_web:
        run_cli_mode(config)
    else:
        # 2026-10-04 review: the console has no login, so the finish line
        # listens only on this PC unless told otherwise
        host = args.web_host or ("0.0.0.0" if config.mode == "remote_lap" else "127.0.0.1")
        run_web_mode(config, config_path, host, args.web_port)


if __name__ == "__main__":
    main()
