// Checks the OpenCode plugin against a fake `filo` and a fake client; run by test_filo.py.
import assert from "node:assert/strict";
import { chmodSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// A hang would be a failure too: what keeps node alive after the last check is reported, not waited for.
setTimeout(() => { console.error("the test did not end: something is still open"); process.exit(1); }, 60000).unref();

const dir = mkdtempSync(join(tmpdir(), "filo-plugin-"));
const fake = join(dir, "filo");
const pids = join(dir, "pids");
// What the fake listener does depends on its name: "slow" is late to answer, "eager" has a message ready
// at once, "mortal" ends by itself, and the others say who they are and send two messages.
writeFileSync(fake, `#!/usr/bin/env node
const { appendFileSync } = require("node:fs");
const [command, ...rest] = process.argv.slice(2);
if (command === "profile") {
  if (rest[0] === "manager") { console.log("Coordinates the others."); process.exit(0); }
  console.error("no such profile"); process.exit(1);
}
const name = rest[rest.indexOf("--as") + 1];
appendFileSync(${JSON.stringify(pids)}, name + " " + process.pid + "\\n");
const line = (o) => console.log(JSON.stringify(o));
const message = (o) => line({ kind: "message", to: name, ...o });
const start = () => {
  line({ kind: "joined", name, scope: "/x", pid: process.pid });
  line({ kind: "participants", participants: [{ name: "boss", pid: 1 }] });
};
if (name === "slow") setTimeout(start, 400);
else start();
if (name === "eager") setImmediate(() => message({ from: "boss", message: "first thing", reply: "filo send --to boss" }));
else if (name === "mortal") setTimeout(() => { console.error("the registry went away"); process.exit(3); }, 200);
else if (name !== "slow") {
  setTimeout(() => message({ from: "boss", message: "build it", reply: "filo send --to boss" }), 300);
  setTimeout(() => message({ from: "spy", scope: "/x", origin: "/elsewhere", foreign: true, message: "hi", reply: "filo send --to spy --scope /elsewhere" }), 350);
}
setInterval(() => {}, 1000);
process.on("SIGTERM", () => process.exit(0));
`);
chmodSync(fake, 0o755);
process.env.FILO_BIN = fake;

const wait = (ms) => new Promise((done) => setTimeout(done, ms));
const until = async (condition, what, ms = 5000) => {
  const end = Date.now() + ms;
  while (!condition()) {
    assert.ok(Date.now() < end, `gave up waiting for ${what}`);
    await wait(20);
  }
};
const started = (name) => readFileSync(pids, "utf8").split("\n").filter((row) => row.startsWith(`${name} `)).map((row) => Number(row.split(" ")[1]));
const alive = (pid) => { try { process.kill(pid, 0); return true; } catch { return false; } };
writeFileSync(pids, "");

const { default: plugin } = await import("../plugins/opencode/filo.js");

// ---- OpenCode 1: `server` gets the client and returns hooks.
const sent = [];
let failNext = false;
const client = {
  app: { log: async () => {} },
  session: {
    promptAsync: async (request) => {
      if (failNext) { failNext = false; return { error: "not now" }; }
      sent.push(request);
      return {};
    },
    status: async () => ({ data: {} }),
  },
};
const hooks = await plugin.server({ client, directory: dir });
const joinWith = async (sessionID, typed) => {
  await hooks["command.execute.before"]({ command: "filo", sessionID, arguments: typed });
  return hooks.tool.filo_join.execute({}, { sessionID });
};
const idle = (sessionID) => hooks.event({ event: { type: "session.idle", properties: { sessionID } } });
const leaveWith = (sessionID) => hooks.tool.filo_leave.execute({}, { sessionID });

assert.match(await joinWith("bad", "a b c"), /must be "PROFILE" or "PROFILE NAME"/);
assert.match(await joinWith("bad", "-h"), /must be "PROFILE" or "PROFILE NAME"/, "a word that filo would take for an option");
assert.match(await joinWith("none", "ghost worker"), /There is no profile "ghost"/);

const answer = await joinWith("s1", "manager chief");
assert.match(answer, /Joined the pool as chief\. Connected: boss\./);
assert.match(answer, /Your profile is manager[\s\S]*Coordinates the others\./);
assert.match(await joinWith("s1", ""), /already in the pool as chief/);

await wait(600);
assert.equal(sent.length, 0, "the message waits while the session is busy");
await idle("s1");
await until(() => sent.length === 1, "the messages that waited");
assert.equal(sent[0].path.id, "s1");
assert.match(sent[0].body.parts[0].text, /^\[filo\] Message from boss: build it\n\nTo reply: filo send --to boss\n\n\[filo\] Message from spy, sent from \/elsewhere \(another scope\): hi\n\nTo reply: filo send --to spy --scope \/elsewhere$/);

assert.match(await joinWith("s2", "scout"), /Joined the pool as scout\..*You have no profile/s);
assert.equal(await leaveWith("s1"), "Left the pool.");
assert.equal(await leaveWith("s1"), "This session is not in the pool.");
await leaveWith("s2");

// a join that is asked for twice at once starts one listener
const twice = await Promise.all([joinWith("d1", "slow"), joinWith("d1", "slow")]);
assert.ok(twice.some((text) => /Joined the pool as slow\./.test(text)), twice.join("\n"));
assert.ok(twice.some((text) => /already joining the pool/.test(text)), twice.join("\n"));
assert.equal(started("slow").length, 1);
await leaveWith("d1");

// leaving while the join is still being answered ends the listener it started
const joining = joinWith("p1", "slow");
await until(() => started("slow").length === 2, "the second slow listener");
assert.equal(await leaveWith("p1"), "Left the pool.");
assert.match(await joining, /Could not join the pool: the join was cancelled/);
await until(() => started("slow").every((pid) => !alive(pid)), "the slow listeners to end");

// a listener that ends by itself is told to its session, which can then tell the user
const sentBefore = sent.length;
assert.match(await joinWith("m1", "mortal"), /Joined the pool as mortal\./);
await wait(400);
await idle("m1");
await until(() => sent.length === sentBefore + 1, "the farewell");
assert.match(sent.at(-1).body.parts[0].text, /^\[filo\] The listener of mortal ended: the registry went away\./);
assert.match(await joinWith("m1", "scout"), /Joined the pool as scout\./, "the session can join again");
await leaveWith("m1");

// a message that the client refuses is not lost: it is tried again
assert.match(await joinWith("e1", "eager"), /Joined the pool as eager\./);
await wait(100);
failNext = true;
const attempts = sent.length;
await idle("e1");
await wait(100);
assert.equal(sent.length, attempts, "the first try was refused");
await idle("e1");
await until(() => sent.length === attempts + 1, "the second try");
assert.match(sent.at(-1).body.parts[0].text, /Message from boss: first thing/);
await leaveWith("e1");

// ---- OpenCode 2: the default export has an id and a setup that gets the context.
assert.equal(plugin.id, "filo");
const prompts = [];
let commands;
const tools = {};
// A stream of events that hands out what is emitted into it, for the plugin that subscribes to it.
const feed = () => {
  const fed = [];
  let wake;
  return {
    emit: (event) => { fed.push(event); wake?.(); },
    stream: async function* ({ signal }) {
      while (!signal.aborted) {
        if (fed.length) { yield fed.shift(); continue; }
        await new Promise((done) => { wake = done; signal.addEventListener("abort", done, { once: true }); });
      }
    },
  };
};
const first = feed();
const context = (subscribe) => ({
  location: { directory: dir },
  session: {
    prompt: async (request) => {
      // the answer to /filo is slow to be given, as when the model provider is
      if (/^The user ran \/filo/.test(request.text)) await wait(150);
      prompts.push(request);
      return {};
    },
  },
  command: { transform: async (edit) => edit({ add: (definition) => { commands = definition; } }) },
  tool: { transform: async (edit) => edit({ add: (definition) => { tools[definition.name] = definition; } }) },
  event: { subscribe },
});
const cleanup = await plugin.setup(context(first.stream));
assert.equal(commands.name, "filo");
const run = (sessionID, text) => commands.execute({ sessionID, prompt: { text }, delivery: "queue" });

await run("t1", "manager chief");
assert.equal(prompts.length, 1);
assert.equal(prompts[0].delivery, "queue");
assert.match(prompts[0].text, /Load the `filo` skill[\s\S]*Joined the pool as chief\. Connected: boss\.[\s\S]*Coordinates the others\./);
await until(() => prompts.length === 3, "the two messages");
assert.match(prompts[1].text, /^\[filo\] Message from boss: build it\n\nTo reply: filo send --to boss$/);
assert.equal(prompts[1].sessionID, "t1");
assert.match(prompts[2].text, /Message from spy, sent from \/elsewhere \(another scope\): hi/);
assert.deepEqual(await tools.filo_leave.execute({}, { sessionID: "nobody" }), { content: "This session is not in the pool." });
assert.deepEqual(await tools.filo_leave.execute({}, { sessionID: "t1" }), { content: "Left the pool." });
const viaFile = await tools.filo_join.execute({ arguments: "scout" }, { sessionID: "t2" });
assert.match(viaFile.content, /Joined the pool as scout\./);
assert.match((await tools.filo_join.execute({}, { sessionID: "t2" })).content, /already in the pool as scout/);
await tools.filo_leave.execute({}, { sessionID: "t2" });

// a message that is ready at once does not reach the session before the answer to /filo
prompts.length = 0;
await run("o1", "eager");
await until(() => prompts.length === 2, "the answer and the message");
assert.match(prompts[0].text, /^The user ran \/filo[\s\S]*Joined the pool as eager\./);
assert.match(prompts[1].text, /Message from boss: first thing/);
await tools.filo_leave.execute({}, { sessionID: "o1" });

// a session that is deleted takes its listener with it
await run("x1", "victim");
const [victim] = started("victim");
assert.ok(alive(victim));
first.emit({ type: "session.deleted", data: { sessionID: "x1" } });
await until(() => !alive(victim), "the listener of a deleted session to end");

// a listener that ends by itself is told to its session
prompts.length = 0;
await run("m2", "mortal");
await until(() => prompts.some((request) => /The listener of mortal ended: the registry went away/.test(request.text)), "the farewell");

// a stream of events that fails is subscribed again
let subscriptions = 0;
const second = feed();
const flaky = async function* (options) {
  subscriptions += 1;
  if (subscriptions === 1) throw new Error("the service restarted");
  yield* second.stream(options);
};
const flakyCleanup = await plugin.setup(context(flaky));
await until(() => subscriptions >= 2, "a second subscription", 3000);
await run("x2", "victim");
const victims = started("victim");
second.emit({ type: "session.deleted", data: { sessionID: "x2" } });
await until(() => !alive(victims.at(-1)), "the listener of a session deleted after the stream came back");

await flakyCleanup();
await cleanup();
await until(() => readFileSync(pids, "utf8").split("\n").filter(Boolean).every((row) => !alive(Number(row.split(" ")[1]))), "every listener to be gone");
console.log("ok");
