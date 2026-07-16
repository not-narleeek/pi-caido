# Architecture

A deeper look at how `pi-caido` is structured and the data flows between its
three layers. For a high-level overview, see the [README](../README.md#architecture).

## Layers

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

### Why split it into two files?

- **`caido.ts` is the agent surface.** It only knows about pi: registering
  tools (names, schemas, prompt guidance, custom rendering), the `/caido`
  command, and the footer. It contains **zero** network code. Every tool call
  boils down to `spawnSync("python3", [script, ...args])` + JSON parse + format.
  This keeps the TypeScript thin and lets pi's own tool pipeline handle
  rendering, truncation, streaming, and error display.

- **`caido.py` is the Caido surface.** It owns everything Caido-specific:
  instance lifecycle, auth, project bootstrap, and every GraphQL mutation/query.
  Being pure-stdlib Python means it runs anywhere `python3` exists with no
  install step, and it doubles as a standalone CLI for scripts/CI outside pi.

- **Caido is the source of truth.** All captured traffic lives in the Caido
  instance, so the agent and a human watching the GUI always see identical data.
  The extension intentionally does **not** cache responses — it always goes back
  to Caido, so there's a single store to search, replay, and export from.

## Lifecycle of a tool call

```
LLM calls caido_request { url, method, headers, body }
        │
        ▼
requireReady(ctx)
   ├─ ensureInstance()
   │     ├─ runJson(["status"])   # probe: is one already up?
   │     └─ (if not) runJson(["start"])  # launch headless caido-cli
   │     knownInstance = true
   ├─ refreshStatus(ctx)          # GET currentProject + history count
   │     ├─ ready?  → paint footer 🌐 caido · <project> · N reqs
   │     └─ !ready? → return notReady() with the bootstrap message
        │
        ▼ (ready)
runJson(["send","-u",url,"-m",method,"-H","k:v",...])   # spawn caido.py
        │
        ▼  (inside caido.py)
   1. _resolve_base_url()   discover | reattach | start headless
   2. auth                  CAIDO_API_TOKEN → validate, else guest login
   3. ensure project        currentProject | select first | create
   4. send()                _build_raw → createReplaySession → startReplayTask
                            → poll replayEntry until request id ready
   5. get_request(id)       decode base64 raw req + resp
   → prints JSON to stdout
        │
        ▼  (back in caido.ts)
   parse JSON → render summary + headers + body (truncated to ~50KB/2000 lines)
   → return tool result; footer refreshes request count
```

## Instance discovery & headless launch

`caido.py` finds a Caido instance in this order:

1. **Probe** local ports `8080, 8180, 8443, 8085, 8086, 8280, 8380` for an
   already-running instance (your GUI or a previous `caido-cli`). First port
   whose `/graphql` answers `200` wins.
2. **Reattach**: read `~/.pi/caido/instance.json`; if that PID is alive and its
   `base_url` answers, reuse it.
3. **Launch headless**: pick a free port, run
   `caido-cli --invisible --no-open --allow-guests --listen 127.0.0.1:<port>`
   detached via `setsid` (survives the agent exiting), poll `/graphql` for up
   to ~25s, then record `{base_url, port, pid, log}` to `instance.json`.

`/caido stop` SIGTERMs (then SIGKILLs) the process group and removes the file.

## Authentication

```
$CAIDO_API_TOKEN or ~/.pi/caido/token   ──►  Bearer auth (full access)
        │ absent or rejected by viewer{} query
        ▼
loginAsGuest  ──►  guest token (existing project only, no create)
```

A bad/expired token automatically falls back to guest so a stale token never
hard-blocks work.

## Project bootstrap

Data operations require a *current project*. Flow:

```
currentProject?
  ├─ set  ─► ready
  └─ null ─► list projects
              ├─ ≥1 ─► selectProject(first) + setGlobalConfigProject(selectOnStart:LAST_USED)
              └─  0 ─► createProject()   # works only for real user / API token
                        ├─ ok   ─► select + pin
                        └─ fail ─► return message: "open the Caido GUI once…"
```

## Sending via the Repeater

`send()` does not open a raw socket — it uses Caido's Repeater so the request
is captured identically to proxied traffic:

1. `_parse_url` → `ConnectionInfo {host, port, isTLS, SNI}` + path.
2. `_build_raw` assembles an HTTP/1.1 byte blob, injecting `Host` and
   `Content-Length` when missing.
3. `createReplaySession(input:{requestSource:{raw:{connectionInfo, raw}}})`.
4. `startReplayTask(sessionId, input:{connection, raw, settings})` — async,
   returns a `replayEntry.id`.
5. Poll `replayEntry(id){ error request{id} }` until `request` is non-null
   (success), `error` is set (failure), or ~6s elapses (timeout).
6. `get_request(id)` decodes the base64 `raw` of request and response.

## Script resolution (packaging)

The extension resolves the bundled Python script at load time:

| Priority | Source | When it applies |
|----------|--------|-----------------|
| 1 | `$CAIDO_PY` | Explicit override (dev/debug) |
| 2 | `<package>/scripts/caido.py` | Default — works for `pi install git:` / `npm:` / local |
| 3 | `~/.pi/scripts/caido.py` | Legacy global install (backwards compatible) |

This makes the package fully self-contained while still honoring an existing
global `~/.pi/scripts/caido.py` if present.
