"""
Hermes Delta Chat Platform Adapter

Connects Hermes to Delta Chat via the deltachat2 JSON-RPC API.
Delta Chat core handles IMAP/SMTP and E2EE (Autocrypt) automatically.

Usage:
  1. Install: pip install deltabot-cli deltachat2
  2. Configure: DELTACHAT_ADDR and DELTACHAT_PASSWORD in .env
  3. Enable in config.yaml: gateway.platforms.deltachat.enabled: true
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform, PlatformConfig

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────

def _find_deltachat_core() -> str | None:
    """Locate the deltachat-core binary."""
    # Common locations
    candidates = [
        "deltachat-core",
        "deltachat_rpc_server",
        # pip-installed entry points
        str(Path(sys.prefix) / "bin" / "deltachat-core"),
        str(Path.home() / ".local" / "bin" / "deltachat-core"),
    ]
    for c in candidates:
        try:
            result = subprocess.run(
                ["which", c], capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
    return None


def _ensure_account(addr: str, password: str) -> str | None:
    """Ensure a Delta Chat account exists, return config dir path."""
    config_dir = Path.home() / ".config" / "deltachat" / addr
    config_dir.mkdir(parents=True, exist_ok=True)

    # Write account config
    ctx_path = config_dir / "context.json"
    if not ctx_path.exists():
        ctx = {
            "addr": addr,
            "mail_pwd": password,
            "configured": False,
            "bot": True,
        }
        with open(ctx_path, "w") as f:
            json.dump(ctx, f, indent=2)
        logger.info("Created Delta Chat account config: %s", ctx_path)

    return str(config_dir)


# ── Adapter ──────────────────────────────────────────────────────────

class DeltaChatAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Delta Chat."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("deltachat"))
        extra = config.extra or {}

        self.addr = (
            os.getenv("DELTACHAT_ADDR") or extra.get("addr", "")
        )
        self.password = (
            os.getenv("DELTACHAT_PASSWORD") or extra.get("password", "")
        )
        self.home_channel = (
            os.getenv("DELTACHAT_HOME_CHANNEL") or extra.get("home_channel", "")
        )

        self._core_proc: subprocess.Popen | None = None
        self._rpc: Any = None
        self._poll_task: asyncio.Task | None = None
        self._config_dir: str | None = None
        self._known_messages: set = set()

    # ── Lifecycle ─────────────────────────────────────────────────

    async def connect(self) -> bool:
        """Start Delta Chat core and connect to its JSON-RPC API."""
        if not self.addr or not self.password:
            logger.error("DELTACHAT_ADDR and DELTACHAT_PASSWORD must be set")
            return False

        # Find core binary
        core_bin = _find_deltachat_core()
        if not core_bin:
            logger.error(
                "deltachat-core not found. Install: pip install deltabot-cli"
            )
            return False

        # Ensure account config exists
        self._config_dir = _ensure_account(self.addr, self.password)

        # Start core process
        try:
            self._core_proc = subprocess.Popen(
                [core_bin],
                env={
                    **os.environ,
                    "DC_ACCOUNT_CONFIG_DIR": self._config_dir,
                },
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            logger.error("Failed to start deltachat-core binary")
            return False

        # Give core a moment to start
        await asyncio.sleep(2)

        # Connect via JSON-RPC (core listens on a Unix socket or TCP)
        # Delta Chat core exposes JSON-RPC on stdout by default
        # We use the deltachat2 library's RPC client
        try:
            from deltachat2.rpc import DeltaChatRpc
        except ImportError:
            logger.error(
                "deltachat2 not installed. Run: pip install deltachat2"
            )
            self._stop_core()
            return False

        try:
            self._rpc = DeltaChatRpc()
            # Connect via the core's JSON-RPC socket
            # The core creates a socket in the config dir
            sock_path = Path(self._config_dir) / "rpc.sock"
            if sock_path.exists():
                await self._rpc.connect(sock_path)
            else:
                # Fall back to TCP on localhost
                await self._rpc.connect("localhost", 21000)

            # Get or create account
            accounts = await self._rpc.get_accounts()
            if not accounts:
                acc_id = await self._rpc.add_account()
            else:
                acc_id = accounts[0]

            # Configure and start
            await self._rpc.set_config(acc_id, "addr", self.addr)
            await self._rpc.set_config(acc_id, "mail_pwd", self.password)
            await self._rpc.set_config(acc_id, "bot", "1")
            await self._rpc.configure(acc_id)

            # Start the event loop
            self._poll_task = asyncio.create_task(self._poll_loop(acc_id))
            self._mark_connected()
            logger.info(
                "Delta Chat connected: %s (account %s)", self.addr, acc_id
            )
            return True

        except Exception as e:
            logger.error("Failed to connect to Delta Chat core: %s", e)
            self._stop_core()
            return False

    async def disconnect(self) -> None:
        """Shut down Delta Chat core."""
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        self._stop_core()
        self._mark_disconnected()

    def _stop_core(self) -> None:
        if self._core_proc:
            try:
                self._core_proc.terminate()
                self._core_proc.wait(timeout=5)
            except Exception:
                self._core_proc.kill()
            self._core_proc = None
        self._rpc = None

    # ── Event polling ──────────────────────────────────────────────

    async def _poll_loop(self, acc_id: int) -> None:
        """Poll for incoming messages from Delta Chat core."""
        if not self._rpc:
            return

        while self._running:
            try:
                # Wait for the next event
                event = await self._rpc.wait_event(timeout=30)
                if event is None:
                    continue

                # We only care about incoming messages
                if event.kind != "DC_EVENT_INCOMING_MSG":
                    continue

                # Fetch the message
                msg = await self._rpc.get_message(acc_id, event.msg_id)
                if msg is None:
                    continue

                # Deduplicate
                if msg.id in self._known_messages:
                    continue
                self._known_messages.add(msg.id)

                # Get chat info
                chat = await self._rpc.get_chat(acc_id, msg.chat_id)
                if chat is None:
                    continue

                # Build Hermes message event
                hermes_event = MessageEvent(
                    message_id=str(msg.id),
                    chat_id=str(msg.chat_id),
                    sender_id=msg.from_id or str(msg.chat_id),
                    text=msg.text or "",
                    type=MessageType.TEXT,
                    raw=msg,
                )

                await self.handle_message(hermes_event)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Delta Chat poll error: %s", e)
                await asyncio.sleep(2)

    # ── Sending ───────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        """Send a text message to a Delta Chat chat."""
        if not self._rpc:
            return SendResult(success=False, error="Not connected")

        try:
            from deltachat2 import MsgData

            accounts = await self._rpc.get_accounts()
            if not accounts:
                return SendResult(success=False, error="No account")

            acc_id = accounts[0]
            msg_data = MsgData(text=content)

            if reply_to:
                msg_data.quote_id = int(reply_to)

            msg_id = await self._rpc.send_msg(
                acc_id, int(chat_id), msg_data
            )
            return SendResult(
                success=True,
                message_id=str(msg_id),
            )

        except Exception as e:
            logger.error("Delta Chat send error: %s", e)
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str) -> None:
        """Send typing indicator (not supported by Delta Chat core)."""
        pass  # Delta Chat doesn't support typing indicators via JSON-RPC

    async def get_chat_info(self, chat_id: str) -> dict:
        """Get chat metadata."""
        if not self._rpc:
            return {"name": chat_id, "type": "dm"}

        try:
            accounts = await self._rpc.get_accounts()
            if accounts:
                chat = await self._rpc.get_chat(accounts[0], int(chat_id))
                if chat:
                    return {
                        "name": chat.name or chat_id,
                        "type": "group" if chat.is_group else "dm",
                    }
        except Exception:
            pass

        return {"name": chat_id, "type": "dm"}


# ── Plugin registration ──────────────────────────────────────────────

def check_requirements() -> bool:
    """Check if Delta Chat dependencies are available."""
    addr = os.getenv("DELTACHAT_ADDR", "").strip()
    password = os.getenv("DELTACHAT_PASSWORD", "").strip()
    if not (addr and password):
        return False

    try:
        import deltachat2  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config) -> bool:
    """Validate platform configuration."""
    extra = getattr(config, "extra", None) or {}
    addr = os.getenv("DELTACHAT_ADDR") or extra.get("addr", "")
    password = os.getenv("DELTACHAT_PASSWORD") or extra.get("password", "")
    return bool(addr and password)


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars."""
    addr = os.getenv("DELTACHAT_ADDR", "").strip()
    password = os.getenv("DELTACHAT_PASSWORD", "").strip()
    if not (addr and password):
        return None

    seed = {"addr": addr, "password": password}
    home = os.getenv("DELTACHAT_HOME_CHANNEL", "").strip()
    if home:
        seed["home_channel"] = {"chat_id": home, "name": "Home"}
    return seed


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="deltachat",
        label="Delta Chat",
        adapter_factory=lambda cfg: DeltaChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=["DELTACHAT_ADDR", "DELTACHAT_PASSWORD"],
        install_hint="pip install deltabot-cli deltachat2",
        env_enablement_fn=_env_enablement,
        cron_deliver_env_var="DELTACHAT_HOME_CHANNEL",
        allowed_users_env="DELTACHAT_ALLOWED_USERS",
        allow_all_env="DELTACHAT_ALLOW_ALL_USERS",
        max_message_length=50000,
        platform_hint=(
            "You are chatting via Delta Chat — E2EE encrypted messaging over email. "
            "It supports markdown formatting."
        ),
        emoji="💬",
    )
