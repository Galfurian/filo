// Checks the OpenCode plugin against a fake `filo` and a fake client; run by test_filo.py.
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dir = mkdtempSync(join(tmpdir(), "filo-plugin-"));
const fake = join(dir, "filo");
writeFileSync(fake, `#!/usr/bin/env node
const [command, ...rest] = process.argv.slice(2);
if (command === "profile") {
  if (rest[0] === "manager") { console.log("Coordinates the others."); process.exit(0); }
  console.error("no such profile"); process.exit(1);
}
const name = rest[rest.indexOf("--as") + 1];
const line = (o) => console.log(JSON.stringify(o));
line({ kind: "joined", name, scope: "/x", pid: process.pid });
line({ kind: "participants", participants: [{ name: "boss", pid: 1 }] });
setTimeout(() => line({ kind: "message", from: "boss", to: name, message: "build it", reply: "filo send --to boss" }), 300);
setTimeout(() => line({ kind: "message", from: "spy", to: name, scope: "/x", origin: "/elsewhere", foreign: true, message: "hi", reply: "filo send --to spy --scope /elsewhere" }), 350);
setInterval(() => {}, 1000);
process.on("SIGTERM", () => process.exit(0));
`);
chmodSync(fake, 0o755);
process.env.FILO_BIN = fake;

const { default: plugin } = await import("../plugins/opencode/filo.js");
const sent = [];
const client = {
  app: { log: async () => {} },
  session: {
    promptAsync: async (request) => { sent.push(request); return {}; },
    status: async () => ({ data: {} }),
  },
};
const hooks = await plugin.server({ client, directory: dir });
const joinWith = async (sessionID, typed) => {
  await hooks["command.execute.before"]({ command: "filo", sessionID, arguments: typed });
  return hooks.tool.filo_join.execute({}, { sessionID });
};
const wait = (ms) => new Promise((done) => setTimeout(done, ms));

assert.match(await joinWith("bad", "a b c"), /must be "PROFILE" or "PROFILE NAME"/);
assert.match(await joinWith("none", "ghost worker"), /There is no profile "ghost"/);

const answer = await joinWith("s1", "manager chief");
assert.match(answer, /Joined the pool as chief\. Connected: boss\./);
assert.match(answer, /Your profile is manager[\s\S]*Coordinates the others\./);
assert.match(await joinWith("s1", ""), /already in the pool as chief/);

await wait(600);
assert.equal(sent.length, 0, "the message waits while the session is busy");
await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } });
await wait(50);
assert.equal(sent.length, 1);
assert.equal(sent[0].path.id, "s1");
assert.match(sent[0].body.parts[0].text, /^\[filo\] Message from boss: build it\n\nTo reply: filo send --to boss\n\n\[filo\] Message from spy, sent from \/elsewhere \(another scope\): hi\n\nTo reply: filo send --to spy --scope \/elsewhere$/);

assert.match(await joinWith("s2", "scout"), /Joined the pool as scout\..*You have no profile/s);
assert.equal(await hooks.tool.filo_leave.execute({}, { sessionID: "s1" }), "Left the pool.");
assert.equal(await hooks.tool.filo_leave.execute({}, { sessionID: "s1" }), "This session is not in the pool.");
await hooks.tool.filo_leave.execute({}, { sessionID: "s2" });

// OpenCode 2 loads the default export: an id and a setup that gets the context.
assert.equal(plugin.id, "filo");
const prompts = [];
let commands, tools = {}, onEvent;
const aborted = new Promise((done) => { onEvent = done; });
const ctx = {
  location: { directory: dir },
  session: { prompt: async (request) => { prompts.push(request); return {}; } },
  command: { transform: async (edit) => edit({ add: (definition) => { commands = definition; } }) },
  tool: { transform: async (edit) => edit({ add: (definition) => { tools[definition.name] = definition; } }) },
  event: {
    subscribe: async function* ({ signal }) {
      yield { type: "session.deleted", data: { sessionID: "t1" } };
      signal.addEventListener("abort", onEvent);
      await aborted;
    },
  },
};
const cleanup = await plugin.setup(ctx);
assert.equal(commands.name, "filo");
await commands.execute({ sessionID: "t1", prompt: { text: "manager chief" }, delivery: "queue" });
assert.equal(prompts.length, 1);
assert.equal(prompts[0].delivery, "queue");
assert.match(prompts[0].text, /Load the `filo` skill[\s\S]*Joined the pool as chief\. Connected: boss\.[\s\S]*Coordinates the others\./);
await wait(600);
assert.equal(prompts.length, 3, "each message is queued behind the busy session");
assert.match(prompts[1].text, /^\[filo\] Message from boss: build it\n\nTo reply: filo send --to boss$/);
assert.equal(prompts[1].sessionID, "t1");
assert.match(prompts[2].text, /Message from spy, sent from \/elsewhere \(another scope\): hi/);
assert.deepEqual(await tools.filo_leave.execute({}, { sessionID: "nobody" }), { content: "This session is not in the pool." });
assert.deepEqual(await tools.filo_leave.execute({}, { sessionID: "t1" }), { content: "Left the pool." });
const viaFile = await tools.filo_join.execute({ arguments: "scout" }, { sessionID: "t2" });
assert.match(viaFile.content, /Joined the pool as scout\./);
assert.match((await tools.filo_join.execute({}, { sessionID: "t2" })).content, /already in the pool as scout/);
await cleanup();
console.log("ok");
process.exit(0);
