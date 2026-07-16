# Hermes Delta Chat Platform Adapter

Connect [Hermes Agent](https://hermes-agent.nousresearch.com) to [Delta Chat](https://delta.chat) — E2EE encrypted messaging over email.

Delta Chat uses Autocrypt for end-to-end encryption and SMTP/IMAP as transport, making it decentralized, privacy-first, and independent of any US-based cloud provider.

## Prerequisites

- Hermes Agent installed and running
- `deltachat-rpc-server` installed (Arch: `pacman -S deltachat-rpc-server`, macOS: `brew install signal-cli`)
- Python 3.11+

## Installation

### 1. Install Python dependencies

```bash
pip install deltabot-cli deltachat2
```

### 2. Create a bot account

The bot gets its own Delta Chat account on a chatmail server (no password needed):

```bash
python -c "from deltabot_cli import BotCli; BotCli('hermes-bot').start()" \
    init DCACCOUNT:https://nine.testrun.org/new
```

### 3. Get the invite link

```bash
python -c "from deltabot_cli import BotCli; BotCli('hermes-bot').start()" link
```

Open the link in your Delta Chat app (or scan the QR code) to add the bot as a contact.

### 4. Install the plugin

```bash
git clone https://github.com/hellquist/hermes-deltachat.git ~/.hermes/plugins/deltachat
```

### 5. Configure environment

Add to `~/.hermes/.env`:

```bash
# Path to bot config (default: ~/.config/hermes-bot)
DELTACHAT_CONFIG_DIR=$HOME/.config/hermes-bot

# Security (recommended) — your email address
DELTACHAT_ALLOWED_USERS=you@nine.testrun.org

# Optional — for cron delivery
DELTACHAT_HOME_CHANNEL=you@nine.testrun.org
```

### 6. Enable in config.yaml

```yaml
gateway:
  platforms:
    deltachat:
      enabled: true
```

### 7. Restart the gateway

```bash
hermes gateway restart
```

## How it works

The adapter uses `IOTransport` from the `deltachat2` library to start `deltachat-rpc-server` as a subprocess. Communication happens via JSON-RPC over stdio — no network ports, no passwords.

```
User (Delta Chat app) ↔ Chatmail server ↔ deltachat-rpc-server ↔ Hermes adapter ↔ AIAgent
```

The bot account is created with `deltabot-cli init` and configured on a chatmail server. You add the bot as a contact via an invite link.

## Security

- **End-to-end encrypted** — Autocrypt ensures messages are encrypted between you and the bot
- **No US cloud dependency** — chatmail servers are run by the Delta Chat community
- **Decentralized** — works with any email server, not just chatmail
- **Access control** — use `DELTACHAT_ALLOWED_USERS` to restrict who can message the bot
- **No passwords in config** — the bot account uses chatmail's QR-code/invite system

## Development

```bash
# Clone for development
git clone git@github.com:hellquist/hermes-deltachat.git ~/git/hermes-deltachat

# Symlink to plugins
ln -s ~/git/hermes-deltachat ~/.hermes/plugins/deltachat
```

## License

MIT
