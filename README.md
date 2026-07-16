# pi-caido

> **Drive [Caido](https://caido.io/) from [pi](https://pi.dev).** Send requests through the Repeater, search captured HTTP history with HTTPQL, route scanners through the Caido proxy, and keep every byte of your CTF / pentest traffic in one place — all from inside your AI coding agent.

`pi-caido` is a [pi package](https://pi.dev/packages) that exposes Caido as six first-class agent tools plus a `/caido` command and a live status footer. It wraps a small, **dependency-free** Python automation client that speaks Caido's GraphQL API directly. No Burp extensions, no browser automation, no extra daemons — just one Python file and one TypeScript extension.

---

## Table of contents

- [What it does](#what-it-does)
- [Requirements](#requirements)
- [Install](#install)
- [First-run setup](#first-run-setup)
- [Quick start](#quick-start)
- [Architecture](#architecture)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [HTTPQL quick reference](#httpql-quick-reference)
- [Security notes](#security-notes)
- [Troubleshooting](#troubleshooting)
- [License](#license)

---

## What it does

Every HTTP request the agent makes can be routed through Caido and is then:

- **captured** in Caido's HTTP history (visible in the GUI in real time),
- **searchable** with Caido's HTTPQL filter language,
- **replayable** and exportable alongside the rest of your traffic.

Concretely, the package gives the agent these tools:

| Tool | What the agent uses it for |
|------|----------------------------|
| `caido_request` | Send an HTTP request through Caido's Repeater and get back status, headers, and body. Prefer this over `curl` for manual request crafting. |
| `caido_search` | Filter captured history with HTTPQL, e.g. `resp.raw.cont:"flag{"`. |
| `caido_get_request` | Dump a full raw request/response pair by id. |
| `caido_history` | List the most recent captured requests. |
| `caido_proxy` | Print the proxy URL + CA cert path and the `http_proxy` / `SSL_CERT_FILE` env lines to route `curl`, `httpx`, `ffuf`, `feroxbuster`, etc. through Caido. |
| `caido_scope` | List scopes or add a new one with allow/deny host globs. |

And a `/caido` command for you:

```
/caido                       status (instance · auth · project · request count)
/caido start|stop            launch / kill a headless Caido instance
/caido proxy                 proxy URL + CA cert + export lines
/caido httpql                HTTPQL cheatsheet
/caido send -u URL -m POST … fire a request through the Repeater
/caido search 'resp.code.eq:500'
/caido get <id>              full raw request+response
/caido history
```

A live footer (🌐 `caido · <project> · 42 reqs`) shows readiness at a glance.

---

## Requirements

| Component | Version | Notes |
|-----------|---------|-------|
| **pi** | any recent | the agent that loads this package — `npm i -g @earendil-works/pi-coding-agent` |
| **Caido** | 2024.x+ | the desktop app (GUI) or `caido-cli`. Install from <https://caido.io/download> |
| **Python** | 3.8+ | **stdlib only** — no `pip install` needed. `python3` must be on `PATH` |
| **Node** | 18+ | for the TypeScript extension |

The Python client uses only the standard library (`urllib`, `json`, `base64`, `socket`, `subprocess`, `argparse`). There is nothing to install on the Python side.

---

## Install

Pick **one** of the three sources. `pi install` writes to user settings (`~/.pi/agent/settings.json`) by default; add `-l` for project-local settings.

### 1. From git (recommended — always latest)

```bash
pi install git:github.com/not-narleeek/pi-caido
```

Pin a tag/commit if you want reproducibility:

```bash
pi install git:github.com/not-narleeek/pi-caido@v0.1.0
```

### 2. From npm (once published)

```bash
pi install npm:pi-caido
```

### 3. From a local clone (for development)

```bash
git clone https://github.com/not-narleeek/pi-caido
cd pi-caido
npm install        # installs TypeScript peer deps for local type-checking
pi install .       # or: pi install ./pi-caido  (absolute or relative path)
```

Verify it loaded:

```bash
pi list                 # pi-caido should appear under packages
/caido status           # inside a pi session
```

Try it without committing it to settings:

```bash
pi -e git:github.com/not-narleeek/pi-caido     # ephemeral, current run only
```

> **Updating:** `pi update --extensions` reconciles git packages to their pinned ref; `pi update npm:pi-caido` updates a single package. `pi remove npm:pi-caido` (or the git: spec) uninstalls.

---

## First-run setup

Caido data operations (history, search, send) require an **active project**, and only a real logged-in user can create one. Do this once:

1. Open the **Caido GUI**, log in with your **free Caido cloud account**, and create/open a project. (This seeds a user + project on disk.)
2. *Optional but recommended:* in Caido → **Settings → API**, create an **API token** and export it:
   ```bash
   export CAIDO_API_TOKEN=cai_xxx
   ```
   With a token set, a headless instance is auto-started on first use and you never need the GUI running.

From now on, the agent can auto-discover your running Caido instance (or spin up a headless one) and everything "just works".

---

## Quick start

Inside a pi session (your repo, a CTF directory, anywhere):

```text
# Let the agent send a request and see it land in Caido
> send a GET to http://challenge.ctf.io:8080/ and look for anything juicy

# Agent uses caido_request (or caido_proxy + the bash tool) instead of plain curl.
```

Route a scanner through Caido so its traffic is captured:

```text
> point ffuf at https://target.htb/ and brute /usr/share/wordlists/dirb/common.txt

# Agent calls caido_proxy, then runs:
#   export http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080
#   export REQUESTS_CA_BUNDLE=/home/.../.pi/caido/ca.crt SSL_CERT_FILE=...
#   ffuf -x http://127.0.0.1:8080 -u https://target.htb/FUZZ -w common.txt
# ...and every hit becomes searchable with caido_search.
```

Find the flag in what was captured:

```text
> search captured traffic for the flag format

# Agent runs caido_search 'resp.raw.cont:"flag{"' and caido_get_request <id>.
```

The footer updates live: 🌐 `caido · ctf · 318 reqs`.

---

## Architecture

Three layers, no hidden moving parts:

```
┌──────────────────────────────────────────────────────────────────┐
│                            pi (the agent)                          │
│                                                                    │
│   ┌────────────────────────────────────────────────────────────┐ │
│   │  extensions/caido.ts   ←── this package (TypeScript)        │ │
│   │   • 6 tools: caido_request · caido_search · caido_get_*    │ │
│   │              caido_history · caido_proxy · caido_scope     │ │
│   │   • /caido command   • live status footer                  │ │
│   └───────────────────┬────────────────────────────────────────┘ │
│                       │ spawnSync("python3", [scripts/caido.py …])│
│   ┌───────────────────▼────────────────────────────────────────┐ │
│   │  scripts/caido.py     ←── this package (stdlib Python)      │ │
│   │   • discover / launch headless instance                     │ │
│   │   • guest-or-API-token auth                                 │ │
│   │   • ensure an active project                                │ │
│   │   • send via Repeater, search HTTPQL, export, scope, …      │ │
│   └───────────────────┬────────────────────────────────────────┘ │
└───────────────────────┼────────────────────────────────────────────┘
                        │  HTTP + GraphQL  (/graphql, /ca.crt)
┌───────────────────────▼────────────────────────────────────────────┐
│                 Caido instance  (GUI or headless)                   │
│    Repeater · HTTP history · Scope · Findings · Proxy (e.g. :8080)  │
└────────────────────────────────┬───────────────────────────────────┘
                                 │  proxy (http_proxy / https_proxy)
                       ┌─────────▼─────────┐
                       │   target web app   │
                       └────────────────────┘
```

**Why this split?**

- **`caido.ts`** is the *agent surface*: it defines the tools (names, schemas, prompt guidance, rendering), the `/caido` command, and the footer. It owns **no** network logic — it just shells out to the Python client and formats JSON. This keeps the TypeScript thin and lets pi's own tool pipeline handle rendering, truncation, and streaming.
- **`caido.py`** is the *Caido surface*: it owns instance lifecycle, auth, project bootstrap, and every GraphQL call. Being pure stdlib Python means it runs anywhere `python3` exists with **zero install step**, and you can also use it standalone as a CLI (see below).
- **The Caido instance** is the *source of truth*: all captured traffic lives there, so the agent and a human staring at the GUI always see the same thing.

### File layout

```
pi-caido/
├── extensions/
│   └── caido.ts          # pi extension: tools, /caido command, footer
├── scripts/
│   └── caido.py          # dependency-free GraphQL automation client (+ CLI)
├── docs/
│   ├── ARCHITECTURE.md   # deeper dive into lifecycle & data flow
│   └── HTTPQL.md         # HTTPQL reference
├── package.json          # pi manifest (pi.extensions) + npm metadata
├── tsconfig.json         # local type-checking only (pi compiles the extension)
├── README.md
├── CHANGELOG.md
└── LICENSE
```

---

## How it works

### 1. Instance discovery & auto-start

When a tool runs, the extension first ensures a Caido instance is reachable. `caido.py` tries, in order:

1. **Discover** a running instance by probing local ports (`8080, 8180, 8443, 8085, …`). If your GUI/CLI is already up, it's reused.
2. **Reattach** to a headless instance it previously started (tracked in `~/.pi/caido/instance.json`).
3. **Launch** a headless instance with `caido-cli --invisible --no-open --allow-guests --listen 127.0.0.1:<port>` and wait for `/graphql` to answer. The process is detached (`setsid`) so it survives the agent exiting.

### 2. Authentication

Auth precedence (first non-empty wins):

1. **`$CAIDO_API_TOKEN`** (or `~/.pi/caido/token`) → full access, can create projects.
2. **Guest login** (`loginAsGuest`) → can use an *existing* project only, cannot create one. This is why the one-time GUI login exists.

A bad/expired API token falls back to guest automatically.

### 3. Project bootstrap

Data calls need a *current project*. The client:

1. Reads `currentProject`. If set, done.
2. Otherwise lists projects; if any exist, selects the first and pins it as `selectOnStart`.
3. If none exist, tries to `createProject` (only succeeds for a real user/API token). On failure, it returns a clear "open the GUI once" message instead of crashing.

### 4. Sending a request (`caido_request`)

A request is sent through Caido's **Repeater**, so it lands in history like any other traffic:

1. Parse the URL → `{host, port, isTLS, SNI}` + path.
2. Build a raw HTTP/1.1 byte blob (`_build_raw`), adding `Host`/`Content-Length` as needed.
3. `createReplaySession` seeded with the raw request.
4. `startReplayTask` to actually fire it (async).
5. **Poll** the replay entry until the request id is ready (or it errors / times out at ~6s).
6. Fetch the full request+response by id and decode the base64 payloads.

The extension then renders status/headers/body, truncating to keep output small while leaving the full data in Caido for `caido_get_request` to retrieve.

### 5. Searching history (`caido_search`)

`caido_search` passes your [HTTPQL](docs/HTTPQL.md) expression straight to Caido's `requests(filter:)` GraphQL field and returns `id · status · method host path · size` rows. Full bodies come back via `caido_get_request <id>`.

### 6. Routing scanners (`caido_proxy`)

`caido_proxy` fetches Caido's CA cert (`/ca.crt`) to disk and prints:

```bash
export http_proxy=http://127.0.0.1:8080 https_proxy=http://127.0.0.1:8080
export REQUESTS_CA_BUNDLE=/home/.../.pi/caido/ca.crt SSL_CERT_FILE=/home/.../.pi/caido/ca.crt
```

The agent applies these in the `bash` tool before invoking `curl`/`httpx`/`ffuf`/`feroxbuster`, so every request those tools make flows through Caido and becomes searchable.

### Standalone CLI

`scripts/caido.py` is also a usable CLI on its own — handy in scripts, CI, or outside pi:

```bash
python3 scripts/caido.py status
python3 scripts/caido.py send -m POST -u http://localhost:8000/api -H 'Content-Type:application/json' -d '{"k":1}'
python3 scripts/caido.py search 'resp.raw.cont:"flag{"' --limit 5
python3 scripts/caido.py get <id>
python3 scripts/caido.py proxy
python3 scripts/caido.py httpql-help
```

Every subcommand supports `--json` for machine output.

---

## Configuration

All optional. Sensible defaults mean zero config for most setups.

| Env var | Default | Purpose |
|---------|---------|---------|
| `CAIDO_API_TOKEN` | *(unset → guest)* | Caido API token (full access; enables auto project creation + headless-only runs). |
| `CAIDO_PY` | *(bundled)* | Override the path to `caido.py` (dev/debugging). Falls back to the bundled script, then `~/.pi/scripts/caido.py`. |
| `CAIDO_STATE_DIR` | `~/.pi/caido` | Where instance metadata, the CA cert, and `instance.log` are written. |

State files (under `$CAIDO_STATE_DIR`):

```
instance.json   # pid + port + base_url of a headless instance we started
token           # API token if you'd rather store it than export the env var
ca.crt          # Caido CA cert, fetched on demand
instance.log    # headless instance stdout/stderr
```

> Add `export CAIDO_API_TOKEN=cai_xxx` to your shell rc for a fire-and-forget setup.

---

## HTTPQL quick reference

HTTPQL is Caido's filter language for HTTP history. Form: `namespace.field.operator:value`.

```text
req.method.eq:"POST"
resp.code.eq:200
resp.raw.cont:"flag{"
req.host.eq:"challenge.ctf.io"
req.path.cont:"/admin" or req.path.regex:/^\/api\/v[0-9]+/
resp.code.gt:400 and resp.raw.cont:"stack"
```

Full reference: [docs/HTTPQL.md](docs/HTTPQL.md), or run `/caido httpql` inside pi.

---

## Security notes

- **Runs with your permissions.** Like all pi packages, this extension executes code (`python3`, and indirectly `caido-cli`). Review the source before installing — there are only two files (`extensions/caido.ts`, `scripts/caido.py`).
- **Guest auth is read-mostly.** Without an API token, the client uses guest login, which can use an existing project but cannot create one. It also cannot read other users' data.
- **Headless instance is loopback only.** It listens on `127.0.0.1:<port>` and is detached, not exposed to the network.
- **The CA cert** is Caido's intercepting root; treat it like any proxy CA. It's stored under your state dir with normal file perms.
- **Tokens** never leave your machine; they're sent only to your local Caido instance as a `Bearer` header.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `⚠ Caido not ready: … no active project` | Open the Caido GUI once, log in, create/open a project. Or set `CAIDO_API_TOKEN`. |
| `caido-cli exited early` / instance won't start | Check `$CAIDO_STATE_DIR/instance.log`. Ensure `caido`/`caido-cli` is installed and on `PATH`, or set the path in `scripts/caido.py` (`CAIDO_CLI`). |
| Guest login denied | The instance was started without `--allow-guests`, or guests are disabled. Start via `/caido start` (this package) or set an API token. |
| `python3: not found` | Install Python ≥3.8 and ensure it's on `PATH`. |
| Scanners hit TLS errors | You forgot the CA env vars — `caido_proxy` prints them; the agent sets them automatically when it drives the scanner. |
| Want a fresh instance | `/caido stop` then `/caido start`, or delete `$CAIDO_STATE_DIR/instance.json`. |

---

## License

[MIT](LICENSE) — free to use, modify, and distribute. Attribution appreciated but not required.
