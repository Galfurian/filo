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
setInterval(() => {}, 1000);
process.on("SIGTERM", () => process.exit(0));
`);
chmodSync(fake, 0o755);
process.env.FILO_BIN = fake;

const { Filo } = await import("../plugins/opencode/filo.js");
const sent = [];
const client = {
  app: { log: async () => {} },
  session: {
    promptAsync: async (request) => { sent.push(request); return {}; },
    status: async () => ({ data: {} }),
  },
};
const hooks = await Filo({ client, directory: dir });
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
assert.match(sent[0].body.parts[0].text, /^\[filo\] Message from boss: build it\n\nTo reply: filo send --to boss$/);

assert.match(await joinWith("s2", "scout"), /Joined the pool as scout\..*You have no profile/s);
assert.equal(await hooks.tool.filo_leave.execute({}, { sessionID: "s1" }), "Left the pool.");
assert.equal(await hooks.tool.filo_leave.execute({}, { sessionID: "s1" }), "This session is not in the pool.");
await hooks.tool.filo_leave.execute({}, { sessionID: "s2" });
console.log("ok");
process.exit(0);
