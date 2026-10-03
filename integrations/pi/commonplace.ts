/**
 * commonplace extension for pi and omp (oh-my-pi)
 *
 * Gives pi and omp the same shared memory Claude Code and Codex use:
 *   - session_start: loads the server's rules for agents and the memory index
 *     (global, this host, this repo's project scope); before_agent_start adds
 *     them to the system prompt.
 *   - tools: memory_recall, memory_get, memory_remember, memory_update,
 *     memory_forget — thin wrappers over `commonplace call <tool> <json>`.
 *     Writes record the author as pi@<host> or omp@<host>.
 *
 * Requires the `commonplace` CLI on PATH (`uv tool install ...`), or set
 * COMMONPLACE_BIN. The CLI finds the shared server via
 * ~/.config/commonplace/config.toml (or COMMONPLACE_URL); otherwise it uses
 * the local database. If the CLI or server is unavailable the extension stays
 * quiet and the agent starts normally.
 *
 * Install: copy or symlink into ~/.pi/agent/extensions/ (pi) or
 * ~/.omp/agent/extensions/ (omp).
 */

import { execFile } from "node:child_process";
import { Type } from "@earendil-works/pi-ai";
import { type BeforeAgentStartEventResult, defineTool, type ExtensionAPI } from "@earendil-works/pi-coding-agent";

const BIN = process.env.COMMONPLACE_BIN || "commonplace";
let app = "pi"; // "omp" once before_agent_start shows omp's event shape
let host = ""; // this machine's host scope, from `commonplace scope`
const agent = () => (host ? `${app}@${host}` : app);
const MARKER = "<!-- commonplace -->";

function run(args: string[], cwd?: string): Promise<string> {
	return new Promise((resolve, reject) => {
		execFile(BIN, args, { cwd, timeout: 20_000, maxBuffer: 4 * 1024 * 1024 }, (err, stdout, stderr) => {
			if (err) reject(new Error((stderr || err.message).trim()));
			else resolve(stdout);
		});
	});
}

const call = (tool: string, args: Record<string, unknown>) => run(["call", tool, JSON.stringify(args)]);
const text = (t: string) => ({ content: [{ type: "text" as const, text: t }], details: undefined });

const SCOPE = Type.String({
	description: "'global', 'host:<hostname>' or 'project:<host/owner/repo>' (the prompt section lists this session's scopes)",
});
const NAME = Type.String({ description: "Short kebab-case slug, unique within the scope" });
const TYPE = Type.Union(
	[Type.Literal("user"), Type.Literal("feedback"), Type.Literal("project"), Type.Literal("reference")],
	{ description: "user | feedback | project | reference" },
);
const EXPIRES = Type.String({
	description: "YYYY-MM-DD after which the memory leaves the index and recall (update: empty string clears it)",
});

const recallTool = defineTool({
	name: "memory_recall",
	label: "Recall memory",
	description: "Search shared agent memory (commonplace) by relevance. Use before remembering to avoid duplicates.",
	parameters: Type.Object({
		query: Type.String({ description: "Free-text query" }),
		scopes: Type.Optional(Type.Array(Type.String(), { description: "Limit to these scopes" })),
		limit: Type.Optional(Type.Number({ description: "Max results (default 10)" })),
	}),
	async execute(_id, params) {
		return text(await call("recall", params));
	},
});

const getTool = defineTool({
	name: "memory_get",
	label: "Get memory",
	description: "Fetch the full body of one shared memory listed in the commonplace index.",
	parameters: Type.Object({ scope: SCOPE, name: NAME }),
	async execute(_id, params) {
		return text(await call("get", params));
	},
});

const rememberTool = defineTool({
	name: "memory_remember",
	label: "Remember",
	description:
		"Save a durable memory shared with other agents and machines. The commonplace rules in the system prompt apply.",
	parameters: Type.Object({
		scope: SCOPE,
		name: NAME,
		type: TYPE,
		description: Type.String({ description: "One line, used to judge relevance later" }),
		body: Type.String({ description: "The fact. For feedback/project, add **Why:** and **How to apply:** lines." }),
		expires: Type.Optional(EXPIRES),
	}),
	async execute(_id, params) {
		return text(await call("remember", { ...params, agent: agent() }));
	},
});

const updateTool = defineTool({
	name: "memory_update",
	label: "Update memory",
	description: "Replace a shared memory with a new version (the old one stays in history). Omitted fields carry over.",
	parameters: Type.Object({
		scope: SCOPE,
		name: NAME,
		description: Type.Optional(Type.String()),
		body: Type.Optional(Type.String()),
		type: Type.Optional(TYPE),
		expires: Type.Optional(EXPIRES),
	}),
	async execute(_id, params) {
		return text(await call("update", { ...params, agent: agent() }));
	},
});

const forgetTool = defineTool({
	name: "memory_forget",
	label: "Forget memory",
	description: "Retire a shared memory that is wrong or obsolete (kept in history).",
	parameters: Type.Object({ scope: SCOPE, name: NAME }),
	async execute(_id, params) {
		return text(await call("forget", { ...params, agent: agent() }));
	},
});

export default function commonplace(pi: ExtensionAPI) {
	let index = "";

	for (const tool of [recallTool, getTool, rememberTool, updateTool, forgetTool]) pi.registerTool(tool);

	pi.on("session_start", async (_event, ctx) => {
		try {
			const line = (await run(["scope", ctx.cwd], ctx.cwd)).split("\n").find((l) => l.startsWith("host:"));
			if (line) host = line.slice("host:".length).trim();
			index = (await run(["index", "--instructions", "--cwd", ctx.cwd], ctx.cwd)).trim();
		} catch {
			index = ""; // CLI missing or server down: start without shared memory
		}
	});

	pi.on("before_agent_start", (event) => {
		const block = `${MARKER}\n${index}\n\nUse the memory_* tools to read and write this shared memory.`;
		if (event.systemPromptOptions) {
			// pi. appendSystemPrompt rather than a custom section: providers that
			// swap in their own base prompt (e.g. pi-claude-bridge) forward only
			// the portable parts — context files, skills and the append text —
			// and drop sections.
			if (!index) return;
			const opts = event.systemPromptOptions;
			if (opts.appendSystemPrompt?.includes(MARKER)) return;
			opts.appendSystemPrompt = opts.appendSystemPrompt ? `${opts.appendSystemPrompt}\n\n${block}` : block;
			return;
		}
		// omp loads pi extensions but sends the prompt as a list of blocks, with
		// no systemPromptOptions; pi's types declare a string.
		const blocks: unknown = event.systemPrompt;
		if (!Array.isArray(blocks)) return;
		app = "omp";
		if (!index || blocks.some((b) => typeof b === "string" && b.includes(MARKER))) return;
		// omp keeps a returned override only until the next prompt, so return it
		// on every call. Its result type takes string[] where pi's takes string.
		return { systemPrompt: [...blocks, block] } as unknown as BeforeAgentStartEventResult;
	});
}
