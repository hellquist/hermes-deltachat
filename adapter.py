"""
Hermes Delta Chat Platform Adapter

Connects Hermes to Delta Chat via the deltachat2 JSON-RPC API.
Delta Chat core handles IMAP/SMTP and E2EE (Autocrypt) automatically.

Uses the same approach as deltabot-cli: IOTransport starts deltachat-rpc-server
as a subprocess and communicates via stdio. No password needed — the bot account
is created with `deltabot-cli init DCACCOUNT:https://nine.testrun.org/new`.

Usage:
  1. Create a bot account:
     python -c "from deltabot_cli import BotCli; BotCli('hermes-bot').start()" \
         init DCACCOUNT:https://nine.testrun.org/new

  2. Get the invite link:
     python -c "from deltabot_cli import BotCli; BotCli('hermes-bot').start()" link

  3. Enable in config.yaml:
       gateway:
         platforms:
           deltachat:
             enabled: true
             config_dir: /home/mathias/.config/hermes-bot  # optional, default: ~/.config/hermes-bot
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.config import Platform, PlatformConfig

from deltachat2.types import EventTypeIncomingMsg, EventTypeChatModified
from deltachat2 import MessageData

logger = logging.getLogger(__name__)

# Default config directory — no env vars needed
DEFAULT_CONFIG_DIR = str(Path.home() / ".config" / "hermes-bot")

# ── Adapter ──────────────────────────────────────────────────────────

class DeltaChatAdapter(BasePlatformAdapter):
    """Hermes platform adapter for Delta Chat.

    Uses IOTransport from deltachat2 to start deltachat-rpc-server as a
    subprocess and communicate via JSON-RPC over stdio. The bot account
    must be pre-configured with `deltabot-cli init`.
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform("deltachat"))
        extra = config.extra or {}

        # Config directory — where the bot account lives
        # Can be set in config.yaml under gateway.platforms.deltachat.extra.config_dir
        self.config_dir = extra.get("config_dir", "") or DEFAULT_CONFIG_DIR

        # Bot address (for display/identification)
        self.addr = extra.get("addr", "")

        self.home_channel = extra.get("home_channel", "")

        self._rpc: Any = None
        self._transport: Any = None
        self._poll_task: asyncio.Task | None = None
        self._known_messages: set = set()
        self._acc_id: int | None = None

    # ── Lifecycle ─────────────────────────────────────────────────

    async def connect(self, **kwargs) -> bool:
        """Start Delta Chat core and connect via IOTransport."""
        accounts_dir = os.path.join(self.config_dir, "accounts")
        if not os.path.isdir(accounts_dir):
            logger.error(
                "Delta Chat accounts dir not found: %s. "
                "Run: python -c \"from deltabot_cli import BotCli; "
                "BotCli('hermes-bot').start()\" init DCACCOUNT:https://nine.testrun.org/new",
                accounts_dir,
            )
            return False

        try:
            from deltachat2 import Rpc
            from deltachat2.transport import IOTransport
        except ImportError:
            logger.error(
                "deltachat2 not installed. Run: pip install deltachat2"
            )
            return False

        try:
            # IOTransport starts deltachat-rpc-server as a subprocess
            # and communicates via stdio JSON-RPC
            self._transport = IOTransport(
                accounts_dir=accounts_dir,
                rpc_executable="deltachat-rpc-server",
            )
            self._transport.start()
            self._rpc = Rpc(self._transport)

            # Get the first (and only) account
            accounts = self._rpc.get_all_account_ids()
            if not accounts:
                logger.error("No Delta Chat accounts found in %s", accounts_dir)
                self._stop()
                return False

            self._acc_id = accounts[0]

            # Start IO (connect to IMAP/SMTP)
            self._rpc.start_io(self._acc_id)

            # Configure as bot (auto-accept contact requests)
            try:
                self._rpc.set_config(self._acc_id, "bot", "1")
            except Exception:
                pass  # Non-critical, may already be set

            # Get bot address if not already set
            if not self.addr:
                self.addr = self._rpc.get_config(self._acc_id, "addr") or ""

            # Start the event polling loop
            self._poll_task = asyncio.create_task(self._poll_loop())
            self._mark_connected()
            logger.info(
                "Delta Chat connected: %s (account %s)",
                self.addr or "unknown",
                self._acc_id,
            )
            return True

        except Exception as e:
            logger.error("Failed to connect to Delta Chat: %s", e)
            self._stop()
            return False

    async def disconnect(self) -> None:
        """Shut down Delta Chat core."""
        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        self._stop()
        self._mark_disconnected()

    def _stop(self) -> None:
        if self._rpc:
            try:
                if self._acc_id is not None:
                    self._rpc.stop_io(self._acc_id)
            except Exception:
                pass
            self._rpc = None
        if self._transport:
            try:
                self._transport.close()
            except Exception:
                pass
            self._transport = None

    # ── Event polling ──────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Poll for incoming messages from Delta Chat core."""
        if not self._rpc or self._acc_id is None:
            return

        while self._running:
            try:
                # Wait for the next event (blocking call in thread)
                event = await asyncio.get_event_loop().run_in_executor(
                    None, self._rpc.get_next_event
                )
                if event is None:
                    continue

                # Log all non-info events for debugging
                etype = type(event.event).__name__
                if etype not in ("EventTypeInfo", "str"):
                    logger.info("Delta Chat event: %s", etype)

                # Handle new chats / contact requests — accept and send welcome
                if isinstance(event.event, EventTypeChatModified):
                    await self._handle_new_chat(event.event.chat_id)
                    continue

                # Handle incoming messages
                if isinstance(event.event, EventTypeIncomingMsg):
                    # Fetch the message
                    msg = self._rpc.get_message(self._acc_id, event.event.msg_id)
                    if msg is None:
                        continue

                    # Deduplicate
                    if msg.id in self._known_messages:
                        continue
                    self._known_messages.add(msg.id)

                    # Get chat info
                    chat = self._rpc.get_basic_chat_info(self._acc_id, msg.chat_id)
                    if chat is None:
                        continue

                    # Build Hermes message event
                    from gateway.session import SessionSource
                    from gateway.config import Platform

                    source = SessionSource(
                        platform=Platform("deltachat"),
                        chat_id=str(msg.chat_id),
                        user_id=str(msg.from_id),
                        user_name=str(getattr(msg.sender, "display_name", "") or ""),
                        chat_type="dm",
                    )

                    hermes_event = MessageEvent(
                        text=msg.text or "",
                        message_type=MessageType.TEXT,
                        source=source,
                        raw_message=msg,
                        message_id=str(msg.id),
                    )

                    await self.handle_message(hermes_event)

                    # Mark message as seen — sends MDN read receipt
                    # so the sender sees two green checkmarks in DeltaChat
                    try:
                        self._rpc.markseen_msgs(self._acc_id, [msg.id])
                    except Exception:
                        pass  # Non-critical

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Delta Chat poll error: %s", e)
                await asyncio.sleep(2)

    async def _handle_new_chat(self, chat_id: int) -> None:
        """Handle a new chat — accept if contact request and send welcome message."""
        if not self._rpc or self._acc_id is None:
            return

        try:
            chat = self._rpc.get_basic_chat_info(self._acc_id, chat_id)
            if chat is None:
                return

            if chat.is_contact_request:
                logger.info("Accepting contact request for chat %s", chat_id)
                self._rpc.accept_chat(self._acc_id, chat_id)
                # Send welcome message to open the chat
                msg_data = MessageData(text="Hej! Jag är Argus, Mathias personliga AI-assistent. Välkommen! 👋")
                self._rpc.send_msg(self._acc_id, chat_id, msg_data)
                logger.info("Sent welcome message to chat %s", chat_id)
        except Exception as e:
            logger.warning("Error handling new chat %s: %s", chat_id, e)

    # ── Sending ───────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: str | None = None,
        metadata: dict | None = None,
    ) -> SendResult:
        """Send a text message to a Delta Chat chat."""
        if not self._rpc or self._acc_id is None:
            return SendResult(success=False, error="Not connected")

        try:
            msg_data = MessageData(text=content)

            if reply_to:
                msg_data.quoted_message_id = int(reply_to)

            msg_id = self._rpc.send_msg(
                self._acc_id, int(chat_id), msg_data
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
        if not self._rpc or self._acc_id is None:
            return {"name": chat_id, "type": "dm"}

        try:
            chat = self._rpc.get_basic_chat_info(self._acc_id, int(chat_id))
            if chat:
                return {
                    "name": chat.name or chat_id,
                    "type": "group" if getattr(chat, "is_group", False) else "dm",
                }
        except Exception:
            pass

        return {"name": chat_id, "type": "dm"}


# ── Plugin registration ──────────────────────────────────────────────

def check_requirements() -> bool:
    """Check if Delta Chat dependencies are available."""
    accounts_dir = os.path.join(DEFAULT_CONFIG_DIR, "accounts")
    if not os.path.isdir(accounts_dir):
        return False

    try:
        import deltachat2  # noqa: F401
        return True
    except ImportError:
        return False


def validate_config(config) -> bool:
    """Validate platform configuration."""
    extra = getattr(config, "extra", None) or {}
    config_dir = extra.get("config_dir", "") or DEFAULT_CONFIG_DIR
    accounts_dir = os.path.join(config_dir, "accounts")
    return os.path.isdir(accounts_dir)


def _env_enablement() -> dict | None:
    """Seed PlatformConfig.extra from env vars."""
    accounts_dir = os.path.join(DEFAULT_CONFIG_DIR, "accounts")
    if not os.path.isdir(accounts_dir):
        return None

    seed = {"config_dir": DEFAULT_CONFIG_DIR}
    return seed


def register(ctx):
    """Plugin entry point — called by the Hermes plugin system."""
    ctx.register_platform(
        name="deltachat",
        label="Delta Chat",
        adapter_factory=lambda cfg: DeltaChatAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        required_env=[],
        install_hint=(
            "1. pip install deltabot-cli deltachat2\n"
            "2. Create bot account:\n"
            "   python -c \"from deltabot_cli import BotCli; "
            "BotCli('hermes-bot').start()\" init DCACCOUNT:https://nine.testrun.org/new\n"
            "3. Get invite link:\n"
            "   python -c \"from deltabot_cli import BotCli; "
            "BotCli('hermes-bot').start()\" link"
        ),
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
