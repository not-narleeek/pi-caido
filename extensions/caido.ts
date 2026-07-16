/**
 * Caido extension for pi.
 *
 * Wraps the bundled `scripts/caido.py` GraphQL automation client so the agent
 * can drive a running Caido instance seamlessly during CTF web challenges:
 *
 *   - send requests through Caido's Repeater (logged, with response)
 *   - search captured HTTP history with Caido's HTTPQL
 *   - fetch full raw request/response pairs
 *   - list recent history, manage Scope, export
 *
 * Everything the agent sends is captured in Caido's HTTP history, so the
 * researcher sees the same traffic in the Caido GUI in parallel.
 *
 * Tools (for the LLM): caido_request, caido_search, caido_get_request,
 *                       caido_history, caido_proxy, caido_scope
 * Command (for the user): /caido [status|start|stop|send ...|search ...|
 *                          get <id>|history|proxy|httpql]
 *
 * Prerequisites: open the Caido GUI once (free cloud login) to create a user +
 * project, OR set CAIDO_API_TOKEN. A headless instance is auto-started here.
 */

import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Text } from "@earendil-works/pi-tui";
import { Type } from "typebox";

const DEFAULT_MAX_BYTES = 50_000;
const DEFAULT_MAX_LINES = 2000;

function formatSize(bytes: number): string {
	if (bytes < 1024) return `${bytes}B`;
	if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
	return `${(bytes / (1024 * 1024)).toFixed(1)}MB`;
}

interface Truncation {
	content: string;
	truncated: boolean;
	outputLines: number;
	totalLines: number;
	outputBytes: number;
	totalBytes: number;
}

function truncateHead(content: string, opts: { maxLines?: number; maxBytes?: number } = {}): Truncation {
	const maxLines = opts.maxLines ?? DEFAULT_MAX_LINES;
	const maxBytes = opts.maxBytes ?? DEFAULT_MAX_BYTES;
	const lines = content.split("\n");
	const totalLines = lines.length;
	const totalBytes = Buffer.byteLength(content, "utf8");
	let out = "";
	let outputLines = 0;
	let truncated = false;
	for (const line of lines) {
		const candidate = out.length === 0 ? line : out + "\n" + line;
		if (outputLines + 1 > maxLines || Buffer.byteLength(candidate, "utf8") > maxBytes) {
			truncated = true;
			break;
		}
		out = candidate;
		outputLines++;
	}
	return {
		content: out,
		truncated,
		outputLines,
		totalLines,
		outputBytes: Buffer.byteLength(out, "utf8"),
		totalBytes,
	};
}

// --------------------------------------------------------------------------
// Path resolution
//
// The Python helper is shipped in this package at scripts/caido.py. It is
// resolved in this order so the package is self-contained while staying
// backwards-compatible with a legacy global install:
//   1. $CAIDO_PY            — explicit override (handy for dev / debugging)
//   2. <package>/scripts/caido.py — bundled script (default for pi packages)
//   3. ~/.pi/scripts/caido.py    — legacy global install
// --------------------------------------------------------------------------

const PKG_ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const BUNDLED_PY = join(PKG_ROOT, "scripts", "caido.py");
const LEGACY_PY = join(homedir(), ".pi", "scripts", "caido.py");

const STATE_DIR = process.env.CAIDO_STATE_DIR || join(homedir(), ".pi", "caido");
const TOKEN_FILE = join(STATE_DIR, "token");

function resolveScript(): string {
	if (process.env.CAIDO_PY) return process.env.CAIDO_PY;
	if (existsSync(BUNDLED_PY)) return BUNDLED_PY;
	if (existsSync(LEGACY_PY)) return LEGACY_PY;
	// Fall through to the bundled path; the spawn error will be informative.
	return BUNDLED_PY;
}

interface CaidoStatus {
	instance?: string;
	auth?: string;
	project?: string | null;
	ready?: boolean;
	history_count?: number | null;
	proxy?: string;
	ca_cert?: string;
	error?: string;
	message?: string;
	[key: string]: unknown;
}

export default function caidoExtension(pi: ExtensionAPI) {
	// session-scoped state
	let knownReady = false; // do we have a ready (instance + project) client?
	let knownInstance = false; // is any instance running?
	let lastError = "";
	let timer: ReturnType<typeof setInterval> | null = null;

	// --------------------------------------------------------------------------
	// run the python helper
	// --------------------------------------------------------------------------

	function runPy(args: string[], opts: { json?: boolean; timeout?: number; input?: string } = {}): {
		stdout: string;
		stderr: string;
		code: number;
	} {
		const r = spawnSync("python3", [resolveScript(), ...(opts.json ? ["--json"] : []), ...args], {
			encoding: "utf8",
			timeout: opts.timeout ?? 60_000,
			input: opts.input,
			maxBuffer: 32 * 1024 * 1024,
		});
		return { stdout: r.stdout ?? "", stderr: r.stderr ?? "", code: r.status ?? -1 };
	}

	function runJson<T = CaidoStatus>(args: string[], timeout?: number): T {
		const r = runPy(args, { json: true, timeout });
		let parsed: any;
		try {
			parsed = JSON.parse(r.stdout || "{}");
		} catch {
			throw new Error(
				`caido: could not parse output. stderr: ${r.stderr.slice(0, 500) || "(none)"}`,
			);
		}
		if (parsed && parsed.error) throw new Error(String(parsed.error));
		if (r.code !== 0 && parsed && !parsed.ready && parsed.message) {
			// not-ready exit (code 2): surface the bootstrap message
			const e = new Error(parsed.message);
			(e as any).notReady = true;
			throw e;
		}
		return parsed as T;
	}

	/** Ensure a Caido instance is running; start headless if not. */
	function ensureInstance(): void {
		if (knownInstance) return;
		try {
			// `status` won't auto-start; if no instance, it errors fast
			runJson(["status"]);
			knownInstance = true;
		} catch {
			try {
				runJson(["start"]);
				knownInstance = true;
			} catch (e) {
				lastError = e instanceof Error ? e.message : String(e);
			}
		}
	}

	/** Refresh readiness cache + footer. Returns current status. */
	function refreshStatus(ctx?: ExtensionContext): CaidoStatus {
		try {
			const s = runJson<CaidoStatus>(["status"]);
			knownReady = !!s.ready;
			knownInstance = !!s.instance;
			lastError = "";
			if (ctx) renderFooter(ctx, s);
			return s;
		} catch (e) {
			knownReady = false;
			lastError = e instanceof Error ? e.message : String(e);
			const s: CaidoStatus = { ready: false, error: lastError };
			if (ctx) renderFooter(ctx, s);
			return s;
		}
	}

	function requireReady(ctx: ExtensionContext): CaidoStatus | null {
		ensureInstance();
		const s = refreshStatus(ctx);
		if (!s.ready) return null; // caller surfaces message
		return s;
	}

	// --------------------------------------------------------------------------
	// footer
	// --------------------------------------------------------------------------

	function renderFooter(ctx: ExtensionContext, s?: CaidoStatus): void {
		if (!ctx.hasUI) return;
		const th = ctx.ui.theme;
		let text: string;
		if (!s) s = { ready: false };
		if (s.ready) {
			const n = s.history_count ?? 0;
			text =
				th.fg("success", "🌐 ") +
				th.fg("text", "caido") +
				th.fg("dim", " · ") +
				th.fg("accent", s.project || "project") +
				th.fg("dim", " · ") +
				th.fg("muted", `${n} reqs`);
		} else if (s.instance) {
			text =
				th.fg("warning", "🌐 ") +
				th.fg("muted", "caido · no project (open Caido GUI once)");
		} else if (lastError) {
			text = th.fg("error", "🌐 ") + th.fg("muted", "caido · " + lastError.slice(0, 30));
		} else {
			text = th.fg("dim", "🌐 caido off");
		}
		ctx.ui.setStatus("caido", text);
	}

	function startTimer(ctx: ExtensionContext): void {
		if (timer) return;
		timer = setInterval(() => refreshStatus(ctx), 15_000);
	}

	function stopTimer(): void {
		if (timer) {
			clearInterval(timer);
			timer = null;
		}
	}

	// --------------------------------------------------------------------------
	// helpers for formatting tool output
	// --------------------------------------------------------------------------

	function previewBody(raw: string | undefined, bytes = 4000): string {
		if (!raw) return "(empty)";
		const bin = Buffer.from(raw, "base64");
		const text = bin.toString("utf8");
		const parts = text.split("\r\n\r\n");
		const body = parts.length > 1 ? parts.slice(1).join("\r\n\r\n") : text;
		if (body.length <= bytes) return body;
		return body.slice(0, bytes) + `\n... [truncated, ${formatSize(bin.length)} total]`;
	}

	function headersFromRaw(raw: string | undefined): string {
		if (!raw) return "";
		const text = Buffer.from(raw, "base64").toString("utf8");
		const head = text.split("\r\n\r\n")[0] ?? "";
		return head;
	}

	// --------------------------------------------------------------------------
	// tools
	// --------------------------------------------------------------------------

	pi.registerTool({
		name: "caido_request",
		label: "Caido Request",
		description:
			"Send an HTTP request through Caido's Repeater and return the response (status, headers, body). The request is logged in Caido alongside the rest of the captured traffic. Use this for web CTF challenges instead of plain curl when you want traffic visible in Caido. Requires a Caido instance with an active project (the extension auto-starts a headless instance).",
		promptSnippet: "Send an HTTP request through Caido (logged, with response)",
		promptGuidelines: [
			"Use caido_request to send HTTP requests during web challenges so they are captured in Caido; prefer it over bash curl for manual request crafting and iteration.",
			"Use caido_search with Caido HTTPQL (e.g. resp.raw.cont:\"flag{\") to find captured traffic, and caido_get_request to dump a full request/response pair.",
			"Use caido_proxy to get the proxy URL + CA cert, then route crawling tools (curl/httpx/ffuf) through it with http_proxy=<url> so all challenge traffic is captured automatically.",
		],
		parameters: Type.Object({
			method: Type.Optional(Type.String({ description: "HTTP method (default GET)" })),
			url: Type.String({ description: "Full URL, e.g. http://challenge.ctf.io:8080/admin" }),
			headers: Type.Optional(
				Type.Array(Type.Object({ name: Type.String(), value: Type.String() }), {
					description: "Request headers",
				}),
			),
			body: Type.Optional(Type.String({ description: "Request body (raw)" })),
		}),
		async execute(_id, params, _signal, _onUpdate, ctx) {
			const ready = requireReady(ctx);
			if (!ready) return notReady(ctx);
			const args = ["send", "-u", params.url];
			if (params.method) args.push("-m", params.method);
			if (params.body) args.push("-d", params.body);
			for (const h of params.headers ?? []) {
				if (h.name) args.push("-H", `${h.name}:${h.value}`);
			}
			try {
				const res = runJson<any>(args, 45_000);
				refreshStatus(ctx);
				const resp = res.response || {};
				const status = resp.statusCode ?? "?";
				const summary = `${res.method || params.method || "GET"} ${res.host || ""}${res.path || params.url}\n-> ${status}  ${resp.length ?? "?"}B  ${resp.roundtripTime ?? "?"}ms  (request_id=${res.id})`;
				const head = headersFromRaw(resp.raw);
				const body = previewBody(resp.raw);
				const out = `${summary}\n\n--- response headers ---\n${head}\n\n--- response body ---\n${body}`;
				const trunc = truncateHead(out, { maxBytes: DEFAULT_MAX_BYTES, maxLines: 2000 });
				let text = trunc.content;
				if (trunc.truncated) text += `\n\n[Output truncated. Full request/response in Caido, request_id=${res.id}]`;
				return {
					content: [{ type: "text" as const, text }],
					details: {
						request_id: res.id,
						replay_session_id: res.replay_session_id,
						method: res.method,
						host: res.host,
						path: res.path,
						status_code: status,
						length: resp.length,
						time_ms: resp.roundtripTime,
						response_headers: head,
						response_body: body,
					},
				};
			} catch (e) {
				return toolError(ctx, e);
			}
		},
		renderCall(args, theme) {
			let t = theme.fg("toolTitle", theme.bold("caido_request "));
			t += theme.fg("accent", (args.method || "GET") + " ") + theme.fg("dim", String(args.url ?? ""));
			return new Text(t, 0, 0);
		},
		renderResult(result, _opts, theme) {
			const d = (result.details || {}) as any;
			if (d.error) return new Text(theme.fg("error", "✖ " + d.error), 0, 0);
			if (!d.request_id) {
				const c = result.content[0];
				return new Text(c?.type === "text" ? c.text : "", 0, 0);
			}
			const t =
				theme.fg("success", "✓ ") +
				theme.fg("accent", d.status_code ?? "?") +
				theme.fg("dim", " · ") +
				theme.fg("muted", d.method + " " + (d.host || "") + (d.path || "")) +
				theme.fg("dim", " · " + (d.length ?? "?") + "B");
			return new Text(t, 0, 0);
		},
	});

	pi.registerTool({
		name: "caido_search",
		label: "Caido Search",
		description:
			'Search captured Caido HTTP history using HTTPQL. Syntax: namespace.field.operator:value, e.g. resp.raw.cont:"flag{", req.method.eq:"POST" and resp.code.eq:500, req.path.cont:"/admin". Returns matching requests (id, method, host, path, status, size).',
		promptSnippet: "Search Caido HTTP history with HTTPQL",
		parameters: Type.Object({
			query: Type.String({ description: 'HTTPQL expression, e.g. resp.raw.cont:"flag{" (syntax: req.host.eq:"x", resp.code.eq:200)' }),
			limit: Type.Optional(Type.Number({ description: "Max results (default 50)" })),
		}),
		async execute(_id, params, _s, _u, ctx) {
			const ready = requireReady(ctx);
			if (!ready) return notReady(ctx);
			try {
				const args = ["search", params.query];
				if (params.limit) args.push("--limit", String(params.limit));
				const res = runJson<any>(args, 30_000);
				const nodes = res.nodes || [];
				const lines =
					nodes.length === 0
						? `No matches for: ${params.query}`
						: nodes
								.map(
									(n: any) =>
										`${n.id}  [${n.response?.statusCode ?? "?"}] ${n.method} ${n.host}${n.path}  ${n.response?.length ?? "?"}B`,
								)
								.join("\n");
				const text = `${res.count ?? nodes.length} match(es):\n${lines}\n\nUse caido_get_request <id> for the full raw pair.`;
				return {
					content: [{ type: "text" as const, text }],
					details: { count: res.count ?? nodes.length, nodes },
				};
			} catch (e) {
				return toolError(ctx, e);
			}
		},
		renderCall(args, theme) {
			return new Text(theme.fg("toolTitle", theme.bold("caido_search ")) + theme.fg("dim", String(args.query ?? "")), 0, 0);
		},
	});

	pi.registerTool({
		name: "caido_get_request",
		label: "Caido Get Request",
		description:
			"Fetch the full raw request and response for a captured Caido request by id (from caido_search or caido_history).",
		promptSnippet: "Dump a full captured request/response pair from Caido by id",
		parameters: Type.Object({ id: Type.String({ description: "Caido request id" }) }),
		async execute(_id, params, _s, _u, ctx) {
			const ready = requireReady(ctx);
			if (!ready) return notReady(ctx);
			try {
				const r = runPy(["get", params.id]);
				const text = r.stdout || "(empty)";
				const trunc = truncateHead(text, { maxBytes: DEFAULT_MAX_BYTES, maxLines: 2000 });
				let out = trunc.content;
				if (trunc.truncated) out += `\n\n[Output truncated. Full data in Caido, request_id=${params.id}]`;
				return { content: [{ type: "text" as const, text: out }], details: { id: params.id } };
			} catch (e) {
				return toolError(ctx, e);
			}
		},
		renderCall(args, theme) {
			return new Text(theme.fg("toolTitle", theme.bold("caido_get_request ")) + theme.fg("dim", String(args.id ?? "")), 0, 0);
		},
	});

	pi.registerTool({
		name: "caido_history",
		label: "Caido History",
		description: "List the most recently captured requests in Caido's HTTP history.",
		promptSnippet: "List recent Caido HTTP history",
		parameters: Type.Object({ limit: Type.Optional(Type.Number()) }),
		async execute(_id, params, _s, _u, ctx) {
			const ready = requireReady(ctx);
			if (!ready) return notReady(ctx);
			try {
				const args = ["history"];
				if (params.limit) args.push("--limit", String(params.limit));
				const res = runJson<any>(args, 30_000);
				const nodes = res.nodes || [];
				const lines =
					nodes.length === 0
						? "No captured traffic yet."
						: nodes
								.map(
									(n: any) =>
										`${n.id}  [${n.response?.statusCode ?? "?"}] ${n.method} ${n.host}${n.path}`,
								)
								.join("\n");
				return {
					content: [{ type: "text" as const, text: `${res.count ?? nodes.length} total:\n${lines}` }],
					details: { count: res.count, nodes },
				};
			} catch (e) {
				return toolError(ctx, e);
			}
		},
		renderCall(_a, theme) {
			return new Text(theme.fg("toolTitle", theme.bold("caido_history")), 0, 0);
		},
	});

	pi.registerTool({
		name: "caido_proxy",
		label: "Caido Proxy",
		description:
			"Return the Caido proxy URL and CA certificate path so crawling/scanning tools (curl, httpx, ffuf, feroxbuster) route through Caido and their traffic is captured. Also prints ready-to-use shell env vars.",
		promptSnippet: "Get Caido proxy URL + CA cert for routing tools through it",
		parameters: Type.Object({}),
		async execute(_id, _p, _s, _u, ctx) {
			ensureInstance();
			try {
				const p = runJson<any>(["proxy"], 15_000);
				const text =
					`Caido proxy: ${p.proxy}\n` +
					`CA cert:    ${p.ca_cert}\n\n` +
					`Route tools through Caido:\n` +
					`  export http_proxy=${p.proxy} https_proxy=${p.proxy}\n` +
					`  export REQUESTS_CA_BUNDLE=${p.ca_cert} SSL_CERT_FILE=${p.ca_cert}\n` +
					`  curl --cacert ${p.ca_cert} -x ${p.proxy} http://target/ ...\n` +
					`  ffuf -x ${p.proxy} ...   (HTTP); for HTTPS point -x at the proxy too.\n\n` +
					`All captured traffic is queryable via caido_search.`;
				return { content: [{ type: "text" as const, text }], details: p };
			} catch (e) {
				return toolError(ctx, e);
			}
		},
		renderCall(_a, theme) {
			return new Text(theme.fg("toolTitle", theme.bold("caido_proxy")), 0, 0);
		},
	});

	pi.registerTool({
		name: "caido_scope",
		label: "Caido Scope",
		description:
			"Manage Caido scope. action 'list' shows scopes; 'add' creates one with allow/deny host globs so Caido only captures in-scope targets.",
		promptSnippet: "List or add Caido scopes",
		parameters: Type.Object({
			action: StringEnum(["list", "add"] as const),
			name: Type.Optional(Type.String()),
			allow: Type.Optional(Type.Array(Type.String())),
			deny: Type.Optional(Type.Array(Type.String())),
		}),
		async execute(_id, params, _s, _u, ctx) {
			const ready = requireReady(ctx);
			if (!ready) return notReady(ctx);
			try {
				if (params.action === "list") {
					// list_scopes isn't a CLI subcommand; use status-derived approach via gql? Use python one-liner.
					return {
						content: [{ type: "text" as const, text: "Use /caido to manage scopes, or the GUI. (list via Caido GUI for now)" }],
						details: {},
					};
				}
				const args = ["scope-add", params.name || "ctf"];
				for (const a of params.allow ?? []) args.push("--allow", a);
				for (const d of params.deny ?? []) args.push("--deny", d);
				const res = runJson<any>(args);
				return { content: [{ type: "text" as const, text: `Scope added: ${JSON.stringify(res)}` }], details: res };
			} catch (e) {
				return toolError(ctx, e);
			}
		},
		renderCall(args, theme) {
			return new Text(theme.fg("toolTitle", theme.bold("caido_scope ")) + theme.fg("dim", String(args.action ?? "")), 0, 0);
		},
	});

	function notReady(ctx: ExtensionContext) {
		startTimer(ctx);
		const msg =
			lastError ||
			"Caido is running but has no active project. Open the Caido GUI once (free cloud login) to create a project, or set CAIDO_API_TOKEN to an API token from Caido Settings, then retry.";
		return {
			content: [{ type: "text" as const, text: `⚠ Caido not ready: ${msg}` }],
			details: { error: msg, not_ready: true },
			isError: true,
		};
	}

	function toolError(ctx: ExtensionContext, e: unknown) {
		startTimer(ctx);
		const msg = e instanceof Error ? e.message : String(e);
		lastError = msg;
		return {
			content: [{ type: "text" as const, text: `Caido error: ${msg}` }],
			details: { error: msg },
			isError: true,
		};
	}

	// --------------------------------------------------------------------------
	// /caido command
	// --------------------------------------------------------------------------

	pi.registerCommand("caido", {
		description: "Drive Caido: /caido [status|start|stop|proxy|httpql|send ...|search ...|get <id>|history]",
		handler: async (args, ctx) => {
			const [sub, ..._rest] = args.trim().split(/\s+/);
			const subArgs = args.trim().slice(sub.length).trim();

			if (sub === "start") {
				try {
					const info = runJson(["start"]);
					knownInstance = true;
					ctx.ui.notify(`Caido headless started: ${info.base_url}`, "info");
				} catch (e) {
					ctx.ui.notify(`start failed: ${emsg(e)}`, "error");
				}
				refreshStatus(ctx);
				return;
			}
			if (sub === "stop") {
				runJson(["stop"]);
				knownInstance = false;
				knownReady = false;
				stopTimer();
				ctx.ui.notify("Caido headless stopped", "info");
				renderFooter(ctx, { ready: false });
				return;
			}
			if (sub === "httpql") {
				const r = runPy(["httpql-help"]);
				ctx.ui.notify(r.stdout, "info");
				return;
			}
			if (sub === "status" || !sub) {
				const s = refreshStatus(ctx);
				const lines = [
					`instance:  ${s.instance || "(none)"}`,
					`auth:      ${s.auth || "?"}`,
					`project:   ${s.project || "(none)"}`,
					`ready:     ${s.ready ? "yes" : "no"}`,
					`history:   ${s.history_count ?? "?"} requests`,
				];
				if (s.message) lines.push(`note: ${s.message}`);
				ctx.ui.notify(lines.join("\n"), "info");
				return;
			}
			if (sub === "proxy") {
				try {
					const p = runJson<any>(["proxy"]);
					ctx.ui.notify(
						`Proxy: ${p.proxy}\nCA: ${p.ca_cert}\n\nexport http_proxy=${p.proxy} https_proxy=${p.proxy}\nexport REQUESTS_CA_BUNDLE=${p.ca_cert} SSL_CERT_FILE=${p.ca_cert}`,
						"info",
					);
				} catch (e) {
					ctx.ui.notify(`proxy failed: ${emsg(e)}`, "error");
				}
				return;
			}
			if (sub === "send") {
				ensureInstance();
				try {
					const out = runPy(["send", ...subArgs]);
					ctx.ui.notify(out.stdout || out.stderr || "(no output)", "info");
					refreshStatus(ctx);
				} catch (e) {
					ctx.ui.notify(`send failed: ${emsg(e)}`, "error");
				}
				return;
			}
			if (sub === "search" || sub === "history" || sub === "get") {
				ensureInstance();
				const out = runPy([sub, ...subArgs]);
				ctx.ui.notify(out.stdout || out.stderr || "(no output)", "info");
				return;
			}
			ctx.ui.notify(
				"Usage: /caido [status|start|stop|proxy|httpql|send -u URL|search QUERY|get ID|history]",
				"info",
			);
		},
	});

	function emsg(e: unknown): string {
		return e instanceof Error ? e.message : String(e);
	}

	// --------------------------------------------------------------------------
	// lifecycle
	// --------------------------------------------------------------------------

	pi.on("session_start", async (_event, ctx) => {
		// Light-touch: paint an idle footer; do NOT auto-start a headless
		// instance from the factory. Tools/commands start it on first use.
		if (existsSync(TOKEN_FILE)) {
			// a configured API token likely means the user wants it ready
			ensureInstance();
			refreshStatus(ctx);
			startTimer(ctx);
		} else {
			renderFooter(ctx, { ready: false });
		}
	});

	pi.on("session_shutdown", async () => {
		stopTimer();
	});
}
