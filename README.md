# Hermes Delta Chat Platform Adapter

Connect [Hermes Agent](https://hermes-agent.nousresearch.com) to [Delta Chat](https://delta.chat) — E2EE encrypted messaging over email.

Delta Chat uses Autocrypt for end-to-end encryption and SMTP/IMAP as transport, making it decentralized, privacy-first, and independent of any US-based cloud provider.

## Prerequisites

- Hermes Agent installed and running
- A Delta Chat account (chatmail recommended — e.g. `nine.testrun.org`)
- Python 3.11+

## Installation

### 1. Install the plugin

```bash
# Clone into Hermes plugins directory
git clone https://github.com/hellquist/hermes-deltachat.git ~/.hermes/plugins/deltachat

# Install Python dependencies
pip install deltabot-cli deltachat2
```

### 2. Configure environment

Add to `~/.hermes/.env`:

```bash
# Required
DELTACHAT_ADDR=yourname@nine.testrun.org
DELTACHAT_PASSWORD=your-chatmail-password

# Security (recommended)
DELTACHAT_ALLOWED_USERS=yourname@nine.testrun.org

# Optional
DELTACHAT_HOME_CHANNEL=yourname@nine.testrun.org
```

### 3. Enable in config.yaml

```yaml
gateway:
  platforms:
    deltachat:
      enabled: true
```

### 4. Restart the gateway

```bash
hermes gateway restart
```

## How it works

The adapter starts a `deltachat-core` process that handles all IMAP/SMTP connections and Autocrypt E2EE. It communicates with the core via JSON-RPC, listening for incoming messages and sending replies.

```
User (Delta Chat app) ↔ Chatmail server ↔ deltachat-core ↔ Hermes adapter ↔ AIAgent
```

## Security

- **End-to-end encrypted** — Autocrypt ensures messages are encrypted between you and the bot
- **No US cloud dependency** — chatmail servers are run by the Delta Chat community
- **Decentralized** — works with any email server, not just chatmail
- **Access control** — use `DELTACHAT_ALLOWED_USERS` to restrict who can message the bot

## Development

```bash
# Clone for development
git clone git@github.com:hellquist/hermes-deltachat.git ~/git/hermes-deltachat

# Symlink to plugins
ln -s ~/git/hermes-deltachat ~/.hermes/plugins/deltachat
```

## License

MIT
