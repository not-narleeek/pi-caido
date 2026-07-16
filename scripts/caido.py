#!/usr/bin/env python3
"""
caido.py — Caido automation client for CTF / web pentesting workflows.

A dependency-free library + CLI that drives a running Caido instance through
its GraphQL API. Designed to be wrapped by the pi `caido` extension and invoked
seamlessly from the `solve-challenge` skill during web CTF challenges.

Capabilities
------------
- Discover a running Caido instance (GUI or headless) or launch a headless one.
- Authenticate via an API token (full access) or guest (existing project only).
- Ensure a project is active so the proxy / replay / history all work.
- Send raw HTTP requests through Caido's Repeater (logged, with response).
- Search HTTP history with Caido's HTTPQL query language.
- Fetch full raw request+response by id.
- List recent history, manage Scope, create Findings, export.

CLI (each supports --json for machine output)
----------------------------------------------
  caido.py status                       instance / auth / project bootstrap
  caido.py start [--port N]             launch a headless instance
  caido.py stop                         stop a headless instance started here
  caido.py send  -m METHOD -u URL [-H h:v]... [-d BODY] [--tls-sni S]
  caido.py search QUERY [--limit N]     HTTPQL, e.g. 'resp.raw.cont:"flag{"'
  caido.py get ID                       full raw request+response
  caido.py history [--limit N]
  caido.py scope-add NAME --allow p1 --allow p2 [--deny d1]
  caido.py export [--format json|csv] [--query Q]
  caido.py proxy                        print proxy URL + CA cert path
  caido.py ca                           write CA cert to stdout
  caido.py httpql-help                  print the HTTPQL cheatsheet

Auth precedence: $CAIDO_API_TOKEN or ~/.pi/caido/token  ->  guest login.

NOTE: All data operations require an active Caido project. On a fresh install
only a real (logged-in) user can create one, so log into the Caido GUI once
(free cloud account) and create/open a project, or set CAIDO_API_TOKEN.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# --------------------------------------------------------------------------
# Paths & constants
# --------------------------------------------------------------------------

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("CAIDO_STATE_DIR", os.path.join(HOME, ".pi", "caido"))
INSTANCE_FILE = os.path.join(STATE_DIR, "instance.json")
TOKEN_FILE = os.path.join(STATE_DIR, "token")
CA_FILE = os.path.join(STATE_DIR, "ca.crt")

CAIDO_CLI = (
    shutil.which("caido-cli")
    or "/usr/lib/caido/resources/bin/caido-cli"
    or shutil.which("caido")
    or "caido"
)

# Ports scanned when discovering an already-running instance.
DISCOVERY_PORTS = [8080, 8180, 8443, 8085, 8086, 8280, 8380]
DEFAULT_PORT = 8180

START_TIMEOUT = 25  # seconds to wait for a headless instance to come up
REPLAY_POLL = 6  # seconds to wait for a replay response


class CaidoError(Exception):
    """Raised for any automation failure (instance, auth, project, API)."""


# --------------------------------------------------------------------------
# Low-level GraphQL client
# --------------------------------------------------------------------------


class Caido:
    def __init__(self, base_url: str, token: str, auth_type: str = "guest"):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.auth_type = auth_type  # "api" | "guest"

    # -- HTTP helpers -------------------------------------------------------

    def _post(self, path: str, payload: bytes, headers: dict[str, str] | None = None,
              timeout: int = 20) -> bytes:
        h = {"content-type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.base_url + path, data=payload, headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            raise CaidoError(f"HTTP {e.code} from Caido {path}: {body[:500]}") from None
        except urllib.error.URLError as e:
            raise CaidoError(f"Cannot reach Caido at {self.base_url}: {e.reason}") from None

    def _get(self, path: str, timeout: int = 10) -> bytes:
        req = urllib.request.Request(self.base_url + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            raise CaidoError(f"HTTP {e.code} from Caido {path}") from None
        except urllib.error.URLError as e:
            raise CaidoError(f"Cannot reach Caido at {self.base_url}: {e.reason}") from None

    def gql(self, query: str, variables: dict[str, Any] | None = None) -> Any:
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        data = json.loads(self._post("/graphql", body, {"Authorization": f"Bearer {self.token}"}))
        if data.get("errors"):
            msgs = []
            for e in data["errors"]:
                msg = e.get("message", "GraphQL error")
                ext = e.get("extensions", {}).get("CAIDO", {})
                if ext.get("message"):
                    msg = ext["message"]
                msgs.append(msg)
            raise CaidoError("; ".join(msgs))
        return data.get("data")

    # -- auth ---------------------------------------------------------------

    @classmethod
    def guest_token(cls, base_url: str) -> tuple[str, str]:
        body = json.dumps(
            {"query": "mutation { loginAsGuest { token { accessToken } error { __typename } } }"}
        ).encode()
        data = json.loads(urllib.request.urlopen(
            urllib.request.Request(base_url + "/graphql", data=body,
                                   headers={"content-type": "application/json"}, method="POST"),
            timeout=10,
        ).read())
        if data.get("errors"):
            raise CaidoError(data["errors"][0].get("message", "guest login failed"))
        tok = data["data"]["loginAsGuest"]["token"]
        if not tok:
            raise CaidoError("guest login denied (guests disabled on this instance)")
        return tok["accessToken"], "guest"

    # -- project ------------------------------------------------------------

    def current_project(self) -> dict | None:
        d = self.gql("{ currentProject { project { id name status } readOnly } }")
        cp = d.get("currentProject")
        return cp if cp and cp.get("project") else None

    def list_projects(self) -> list[dict]:
        d = self.gql("{ projects { id name status } }")
        return d.get("projects", [])

    def select_project(self, project_id: str) -> dict:
        return self.gql(
            'mutation($id: ID!){ selectProject(id:$id){ currentProject{ project{ id name status } } } }',
            {"id": project_id},
        )

    def create_project(self, name: str) -> dict:
        d = self.gql(
            'mutation($n:String!){ createProject(input:{name:$n,temporary:false}){ project{ id name } error{ __typename } } }',
            {"n": name},
        )
        payload = d.get("createProject", {})
        proj = payload.get("project")
        if not proj:
            err = (payload.get("error") or {}).get("__typename", "unknown")
            raise CaidoError(f"could not create project ({err})")
        return proj

    def set_select_on_start(self, project_id: str) -> None:
        try:
            self.gql(
                'mutation($id:ID!){ setGlobalConfigProject(input:{selectOnStart:LAST_USED,selectProjectId:$id}){ __typename } }',
                {"id": project_id},
            )
        except CaidoError:
            pass  # non-fatal

    # -- requests / history -------------------------------------------------

    def _req_fields(self) -> str:
        return (
            "id host method path query port isTls sni length source createdAt "
            "response { statusCode length roundtripTime }"
        )

    def history(self, limit: int = 50, httpql: str | None = None,
                scope_id: str | None = None) -> dict:
        q = (
            "query($n:Int!,$f:HTTPQLInput,$s:ID){ requests(first:$n, filter:$f, scopeId:$s,"
            " order:{by:CREATED_AT,ordering:DESC}){ count{value} nodes { "
            + self._req_fields()
            + " } } }"
        )
        d = self.gql(q, {"n": limit, "f": {"code": httpql} if httpql else None, "s": scope_id})
        conn = d["requests"]
        return {"count": conn.get("count", {}).get("value", 0), "nodes": conn.get("nodes", [])}

    def search(self, httpql: str, limit: int = 50) -> dict:
        """HTTPQL search of captured history."""
        return self.history(limit=limit, httpql=httpql)

    def get_request(self, request_id: str) -> dict:
        q = "query($id:ID!){ request(id:$id){ " + self._req_fields() + " raw response { raw } } }"
        d = self.gql(q, {"id": request_id})
        req = d.get("request")
        if not req:
            raise CaidoError(f"no request with id {request_id}")
        req["raw_decoded"] = _b64dec(req.get("raw")).decode("utf-8", "replace")
        if req.get("response"):
            req["response"]["raw_decoded"] = _b64dec(req["response"].get("raw")).decode("utf-8", "replace")
        return req

    # -- send / replay ------------------------------------------------------

    def send(self, method: str, url: str, headers: list[tuple[str, str]] | None = None,
             body: bytes | str = b"", sni: str | None = None, timeout: int = REPLAY_POLL) -> dict:
        """Send a request via Caido Repeater and return the response."""
        conn, path = _parse_url(url)
        if sni:
            conn["SNI"] = sni
        raw = _build_raw(method, path, headers or [], conn["host"], conn["port"], body)
        raw_b64 = base64.b64encode(raw).decode()
        conn_b64 = _conn_b64(conn)

        # 1) create a replay session seeded with the raw request
        sess = self.gql(
            "mutation($ci:ConnectionInfoInput!,$r:Blob!){ createReplaySession("
            "input:{requestSource:{raw:{connectionInfo:$ci,raw:$r}}}){ session{ id } } }",
            {"ci": conn, "r": raw_b64},
        )
        session_id = sess["createReplaySession"]["session"]["id"]

        # 2) actually send it (async: returns immediately with a replay entry id)
        task = self.gql(
            "mutation($s:ID!,$ci:ConnectionInfoInput!,$r:Blob!){ startReplayTask(sessionId:$s,"
            " input:{connection:$ci,raw:$r,settings:{placeholders:[],updateContentLength:true,connectionClose:false}})"
            " { task { replayEntry { id } } error { __typename } } }",
            {"s": session_id, "ci": conn, "r": raw_b64},
        )
        entry = task["startReplayTask"]["task"]["replayEntry"]
        if not entry or not entry.get("id"):
            err = task["startReplayTask"].get("error")
            raise CaidoError(f"replay task rejected: {err}"
                             if err else "replay task returned no entry")
        entry_id = entry["id"]

        # 3) poll the replay entry until the request is ready (or error/timeout)
        req_id = None
        for _ in range(max(2, timeout * 2)):
            d = self.gql(
                "query($id:ID!){ replayEntry(id:$id){ error request{ id } } }",
                {"id": entry_id},
            )
            re = d.get("replayEntry") or {}
            if re.get("error"):
                raise CaidoError(f"replay failed: {re['error']}")
            if re.get("request"):
                req_id = re["request"]["id"]
                break
            time.sleep(0.5)
        if not req_id:
            raise CaidoError("replay did not complete within the timeout")

        info = self.get_request(req_id)
        info["replay_session_id"] = session_id
        info["replay_entry_id"] = entry_id
        info["connection"] = conn_b64
        return info

    # -- scope --------------------------------------------------------------

    def list_scopes(self) -> list[dict]:
        d = self.gql("{ scopes { id name } }")
        return d.get("scopes", [])

    def add_scope(self, name: str, allowlist: list[str], denylist: list[str] | None = None) -> dict:
        d = self.gql(
            "mutation($n:String!,$a:[String]!,$d:[String]!){ createScope(input:{name:$n,allowlist:$a,denylist:$d}){ scope{ id name } error{__typename} } }",
            {"n": name, "a": allowlist, "d": denylist or []},
        )
        return d.get("createScope", {})

    # -- findings / export --------------------------------------------------

    def create_finding(self, request_id: str, name: str, description: str = "",
                       severity: str = "MEDIUM") -> dict:
        # severity handled loosely; Caido FindingInput fields vary by version
        d = self.gql(
            "mutation($r:ID!,$n:String!,$desc:String!){ createFinding(requestId:$r,"
            " input:{name:$n,description:$desc}){ ...on FindingPayload { finding { id } } } }",
            {"r": request_id, "n": name, "desc": description or name},
        )
        return d.get("createFinding", {})

    def export(self, fmt: str = "JSON", httpql: str | None = None) -> dict:
        d = self.gql(
            "mutation($f:DataExportFormat!,$q:HTTPQLInput){ startExportRequestsTask("
            "input:{format:$f,includeRaw:true,filter:$q}){ ...on StartDataExportTaskPayload { dataExport { id } } } }",
            {"f": fmt, "q": {"code": httpql} if httpql else None},
        )
        return d.get("startExportRequestsTask", {})

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        cp = self.current_project()
        try:
            hist = self.history(limit=1)
            count = hist.get("count", 0)
        except CaidoError:
            count = None
        return {
            "instance": self.base_url,
            "auth": self.auth_type,
            "project": cp["project"]["name"] if cp else None,
            "project_id": cp["project"]["id"] if cp else None,
            "ready": cp is not None,
            "history_count": count,
        }


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _b64dec(s: str | None) -> bytes:
    if not s:
        return b""
    try:
        return base64.b64decode(s)
    except Exception:
        return b""


def _parse_url(url: str) -> tuple[dict, str]:
    if "://" not in url:
        url = "http://" + url
    u = urllib.parse.urlsplit(url)
    is_tls = u.scheme.lower() == "https"
    host = u.hostname or "127.0.0.1"
    port = u.port or (443 if is_tls else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    return {"host": host, "port": port, "isTLS": is_tls, "SNI": host if is_tls else None}, path


def _build_raw(method: str, path: str, headers: list[tuple[str, str]], host: str, port: int,
               body: bytes | str) -> bytes:
    if isinstance(body, str):
        body = body.encode()
    lines = [f"{method.upper()} {path} HTTP/1.1"]
    has_host = any(h.lower() == "host" for h, _ in headers)
    host_hdr = host if port in (80, 443) else f"{host}:{port}"
    if not has_host:
        lines.append(f"Host: {host_hdr}")
    seen = set()
    for h, v in headers:
        if h.lower() == "host":
            continue
        lines.append(f"{h}: {v}")
        seen.add(h.lower())
    if body and "content-length" not in seen:
        lines.append(f"Content-Length: {len(body)}")
    raw = ("\r\n".join(lines) + "\r\n\r\n").encode()
    return raw + body


def _conn_b64(conn: dict) -> dict:
    return {k: v for k, v in conn.items() if v is not None}


# --------------------------------------------------------------------------
# Instance discovery / lifecycle
# --------------------------------------------------------------------------


def _is_caido(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(base_url + "/graphql", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def discover_instance() -> str | None:
    for port in DISCOVERY_PORTS:
        url = f"http://127.0.0.1:{port}"
        if _is_caido(url):
            return url
    return None


def _free_port(preferred: int = DEFAULT_PORT) -> int:
    for p in [preferred, *DISCOVERY_PORTS, 8480, 8580, 8680, 8780, 8880]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    # last resort: let OS choose
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_headless(port: int | None = None, data_path: str | None = None,
                   extra_args: list[str] | None = None) -> dict:
    """Launch a detached headless Caido instance and wait for it to answer."""
    os.makedirs(STATE_DIR, exist_ok=True)
    port = port or _free_port()
    args = [
        CAIDO_CLI,
        "--invisible", "--no-open", "--allow-guests",
        "--listen", f"127.0.0.1:{port}",
    ]
    if data_path:
        args += ["--data-path", data_path]
    args += extra_args or []

    log_path = os.path.join(STATE_DIR, "instance.log")
    log = open(log_path, "ab")
    proc = subprocess.Popen(
        args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True,  # survive parent exit (setsid)
    )
    base_url = f"http://127.0.0.1:{port}"
    for _ in range(START_TIMEOUT * 2):
        if proc.poll() is not None:
            raise CaidoError(f"caido-cli exited early (code {proc.returncode}); see {log_path}")
        if _is_caido(base_url):
            info = {"base_url": base_url, "port": port, "pid": proc.pid, "log": log_path}
            _save_instance(info)
            return info
        time.sleep(0.5)
    proc.terminate()
    raise CaidoError(f"caido-cli did not come up within {START_TIMEOUT}s; see {log_path}")


def stop_instance() -> bool:
    info = _load_instance()
    if not info or not info.get("pid"):
        return False
    pid = info["pid"]
    killed = False
    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        pgid = pid
    for sig in (15, 9):  # SIGTERM then SIGKILL
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        except Exception:
            break
        for _ in range(20):
            if not instance_pid_alive(pid):
                killed = True
                break
            time.sleep(0.1)
        if killed:
            break
    try:
        os.remove(INSTANCE_FILE)
    except OSError:
        pass
    return killed or not instance_pid_alive(pid)


def _save_instance(info: dict) -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(INSTANCE_FILE, "w") as f:
        json.dump(info, f)


def _load_instance() -> dict | None:
    try:
        with open(INSTANCE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def instance_pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# Bootstrap: connect + auth + ensure project
# --------------------------------------------------------------------------


def _api_token() -> str | None:
    return os.environ.get("CAIDO_API_TOKEN") or _read_file(TOKEN_FILE)


def _read_file(path: str) -> str | None:
    try:
        with open(path) as f:
            t = f.read().strip()
            return t or None
    except Exception:
        return None


def _resolve_base_url(start: bool = True) -> tuple[str, dict | None]:
    """Find a running instance; optionally start a headless one."""
    url = discover_instance()
    if url:
        return url, None
    info = _load_instance()
    if info and instance_pid_alive(info.get("pid")) and _is_caido(info["base_url"]):
        return info["base_url"], info
    if start:
        info = start_headless()
        return info["base_url"], info
    raise CaidoError("no Caido instance running (run `caido.py start`)")


def connect(start: bool = True, project: str = "ctf") -> tuple[Caido, dict]:
    """Return (client, status). status['ready'] is False when a project is
    missing and cannot be created (e.g. guest on a fresh install)."""
    base_url, _ = _resolve_base_url(start=start)

    api = _api_token()
    if api:
        client = Caido(base_url, api, auth_type="api")
        # validate token
        try:
            client.gql("{ viewer { __typename } }")
        except CaidoError:
            client = Caido(base_url, *Caido.guest_token(base_url))
    else:
        client = Caido(base_url, *Caido.guest_token(base_url))

    status = client.status()
    # ensure project
    if not status["ready"]:
        projects = client.list_projects()
        if projects:
            client.select_project(projects[0]["id"])
            client.set_select_on_start(projects[0]["id"])
            status = client.status()
        else:
            # try to create (only works for a real user / api token)
            try:
                proj = client.create_project(project)
                client.select_project(proj["id"])
                client.set_select_on_start(proj["id"])
                status = client.status()
            except CaidoError:
                status["message"] = (
                    "No Caido project exists and this account cannot create one. "
                    "Open the Caido GUI once (log in with your free cloud account) and "
                    "create/open a project, or set CAIDO_API_TOKEN to an API token from "
                    "Caido Settings. Then re-run."
                )
    return client, status


def fetch_ca_cert(base_url: str) -> str:
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with urllib.request.urlopen(base_url + "/ca.crt", timeout=5) as r:
            data = r.read()
        with open(CA_FILE, "wb") as f:
            f.write(data)
        return CA_FILE
    except Exception as e:
        raise CaidoError(f"could not fetch CA cert: {e}")


# --------------------------------------------------------------------------
# HTTPQL cheatsheet
# --------------------------------------------------------------------------

HTTPQL_HELP = """\
Caido HTTPQL — filter language for HTTP history.

Syntax:  namespace.field.operator:value   (dots + colon, NO spaces inside a clause)
Join clauses with  and / or / not  and parentheses.

Namespaces & fields:
  req.id  req.ext  req.host  req.port  req.method  req.path  req.query
  req.raw  req.created_at  req.tls  req.len
  resp.code  resp.raw  resp.roundtrip  resp.len

String operators (value quoted):
  eq:"x"   ne:"x"   cont:"x"   ncont:"x"
  like:"%x%"   nlike:"%x%"   regex:/pat/   nregex:/pat/
Int operators (value bare):
  eq:200   ne:200   gt:200   gte:200   lt:200   lte:200
Bool:    req.tls.eq:true   req.tls.eq:false

Examples:
  req.method.eq:"GET"
  resp.code.eq:200
  resp.raw.cont:"flag{"
  req.host.eq:"challenge.ctf.io"
  req.method.eq:"POST" and resp.code.eq:500
  req.path.cont:"/admin" or req.path.regex:/^/api/v[0-9]+/
  resp.code.gt:400 and resp.raw.cont:"stack"
"""


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _print(obj: Any, json_out: bool) -> None:
    if json_out:
        print(json.dumps(obj, indent=2, default=str))
    else:
        if isinstance(obj, (dict, list)):
            print(json.dumps(obj, indent=2, default=str))
        else:
            print(obj)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="caido.py", description="Caido automation client")
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="instance/auth/project bootstrap status")
    sp = sub.add_parser("start", help="launch a headless instance")
    sp.add_argument("--port", type=int, default=None)
    sub.add_parser("stop", help="stop a headless instance started here")
    sub.add_parser("proxy", help="print proxy URL + CA cert path")
    sub.add_parser("ca", help="write CA cert to stdout")
    sub.add_parser("httpql-help", help="print HTTPQL cheatsheet")

    sp = sub.add_parser("send", help="send a request via Caido Repeater")
    sp.add_argument("-m", "--method", default="GET")
    sp.add_argument("-u", "--url", required=True)
    sp.add_argument("-H", "--header", action="append", default=[], metavar="NAME:VALUE")
    sp.add_argument("-d", "--data", default="")
    sp.add_argument("--tls-sni", default=None)

    sp = sub.add_parser("search", help="search history with HTTPQL")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=50)

    sp = sub.add_parser("get", help="full raw request+response by id")
    sp.add_argument("id")

    sp = sub.add_parser("history", help="recent captured requests")
    sp.add_argument("--limit", type=int, default=50)

    sp = sub.add_parser("scope-add", help="add a scope")
    sp.add_argument("name")
    sp.add_argument("--allow", action="append", required=True, metavar="HOST/GLOB")
    sp.add_argument("--deny", action="append", default=[], metavar="HOST/GLOB")

    sp = sub.add_parser("export", help="export captured requests")
    sp.add_argument("--format", choices=["JSON", "CSV"], default="JSON")
    sp.add_argument("--query", default=None)

    args = p.parse_args(argv)
    jo = args.json

    try:
        if args.cmd == "httpql-help":
            print(HTTPQL_HELP)
            return 0
        if args.cmd == "start":
            info = start_headless(args.port)
            _print(info, jo)
            return 0
        if args.cmd == "stop":
            _print({"stopped": stop_instance()}, jo)
            return 0

        # remaining commands need a connected client
        if args.cmd == "status":
            client, status = connect(start=False)
            _print(status, jo)
            return 0 if status.get("ready") else 2

        client, status = connect(start=True)

        if args.cmd in ("ca", "proxy"):
            # only need a running instance + auth, not an active project
            try:
                cert = fetch_ca_cert(client.base_url)
            except CaidoError as e:
                _print({"error": str(e)}, jo)
                return 1
            if args.cmd == "ca":
                sys.stdout.buffer.write(open(cert, "rb").read())
            else:
                _print({"proxy": client.base_url, "ca_cert": cert,
                        "curl_env": f"http_proxy={client.base_url} https_proxy={client.base_url}",
                        "REQUESTS_CA_BUNDLE": cert, "SSL_CERT_FILE": cert}, jo)
            return 0

        if not status.get("ready"):
            _print(status, jo)
            return 2

        if args.cmd == "send":
            headers = [tuple(h.split(":", 1)) for h in args.header if ":" in h]
            res = client.send(args.method, args.url, headers, args.data, args.tls_sni)
            if jo:
                _print(res, True)
            else:
                _print_send(res)
            return 0
        if args.cmd == "search":
            _print(client.search(args.query, args.limit), jo)
            return 0
        if args.cmd == "get":
            r = client.get_request(args.id)
            if jo:
                _print(r, True)
            else:
                sys.stdout.write(_format_raw(r) + "\n")
            return 0
        if args.cmd == "history":
            _print(client.history(args.limit), jo)
            return 0
        if args.cmd == "scope-add":
            _print(client.add_scope(args.name, args.allow, args.deny), jo)
            return 0
        if args.cmd == "export":
            _print(client.export(args.format, args.query), jo)
            return 0
    except CaidoError as e:
        _print({"error": str(e)}, jo)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


def _print_send(res: dict) -> None:
    resp = res.get("response") or {}
    head = (
        f"{res.get('method','')} {res.get('host','')}{res.get('path','')}\n"
        f"-> {resp.get('statusCode','?')}  {resp.get('length','?')}B  {resp.get('roundtripTime','?')}ms\n"
        f"request_id={res.get('id')}  replay_session={res.get('replay_session_id')}\n"
    )
    raw = resp.get("raw_decoded") or ""
    parts = raw.split("\r\n\r\n", 1)
    out = head
    if len(parts) == 2:
        out += "\n--- response body ---\n" + parts[1][:4000]
    elif raw:
        out += "\n--- response (raw) ---\n" + raw[:4000]
    else:
        out += "\n(no response body)\n"
    sys.stdout.write(out + "\n")


def _format_raw(r: dict) -> str:
    out = ["===== REQUEST =====", r.get("raw_decoded") or "",
           "\n===== RESPONSE =====", (r.get("response") or {}).get("raw_decoded") or ""]
    return "\n".join(out)


if __name__ == "__main__":
    sys.exit(main())
