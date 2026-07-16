# HTTPQL reference

[HTTPQL](https://docs.caido.io/docs/concepts/httpql) is Caido's filter language
for HTTP history. `caido_search` passes your expression straight to Caido's
`requests(filter:)` GraphQL field.

Run `/caido httpql` inside pi for the inline cheatsheet.

## Syntax

```
namespace.field.operator:value
```

- Dots separate `namespace.field`; a colon separates `operator:value`.
- **No spaces** inside a clause. Join clauses with `and`, `or`, `not` and
  parentheses.

## Namespaces & fields

| Namespace | Fields |
|-----------|--------|
| `req` | `id`, `ext`, `host`, `port`, `method`, `path`, `query`, `raw`, `created_at`, `tls`, `len` |
| `resp` | `code`, `raw`, `roundtrip`, `len` |

## Operators

### String operators (quote the value)

| Op | Meaning |
|----|---------|
| `eq:"x"` | equals |
| `ne:"x"` | not equals |
| `cont:"x"` | contains |
| `ncont:"x"` | does not contain |
| `like:"%x%"` | SQL-like wildcard match |
| `nlike:"%x%"` | negated like |
| `regex:/pat/` | regex match |
| `nregex:/pat/` | negated regex |

### Integer operators (bare value)

| Op | Meaning |
|----|---------|
| `eq:200` / `ne:200` | equal / not equal |
| `gt:200` / `gte:200` | greater than / greater-or-equal |
| `lt:200` / `lte:200` | less than / less-or-equal |

### Boolean

```
req.tls.eq:true
req.tls.eq:false
```

## Examples

```text
# Method / status
req.method.eq:"GET"
req.method.eq:"POST" and resp.code.eq:500

# Body / header content
resp.raw.cont:"flag{"
resp.raw.cont:"stack trace"
req.raw.cont:"Authorization: Bearer"

# Host / path
req.host.eq:"challenge.ctf.io"
req.path.cont:"/admin"
req.path.regex:/^\/api\/v[0-9]+\//
req.host.like:"%.htb"

# Size / timing
resp.code.gt:400
resp.len.gt:10000
resp.roundtrip.gt:2000

# TLS
req.tls.eq:true and req.port.eq:8443

# Combinations
(resp.code.eq:200 or resp.code.eq:301) and resp.raw.cont:"token"
```

## Tips

- `resp.raw.cont:"flag{"` is the classic CTF finisher — search every captured
  response for the flag format after a fuzzing/replay session.
- Search results return `id · status · method host path · size`. Pull the full
  body with `caido_get_request <id>` (or `/caido get <id>`).
- Combine `req.path.cont:"/api"` with `resp.code.eq:200` to triage which API
  endpoints actually returned data.
