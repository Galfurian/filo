// filo for OpenCode.
//
// OpenCode has no command that pushes a message into a running session, so this
// plugin is the bridge. `/filo [profile] [name]` runs the filo_join tool; the tool
// starts `filo join` for that session and turns every message the listener
// receives into a prompt in the same session. The session id comes from OpenCode
// itself, so nothing is guessed, and OpenCode needs no port or special flag.
//
// It speaks both plugin APIs. OpenCode 2 reads the default export's `id` and `setup`; OpenCode 1
// reads its `server`. Neither needs an import, which matters because the file is linked from the
// checkout, where nothing can be imported.
import { spawn, spawnSync } from "node:child_process";
import { createInterface } from "node:readline";

const FILO = process.env.FILO_BIN || "filo";
const WORD = /^[A-Za-z0-9_.-]+$/;
const POLL_MS = 1500;

// What a listener's message looks like to the session that receives it.
const render = (event) => {
  const reply = event.reply ? `\n\nTo reply: ${event.reply}` : "";
  const outside = event.foreign ? `, sent from ${event.origin} (another scope)` : "";
  return `[filo] Message from ${event.from}${outside}: ${event.message}${reply}`;
};

// The part both versions share: one `filo join` listener per session. `onMessage` is how a
// version puts what the listener hears into the session.
const pool = ({ directory, onMessage }) => {
  const sessions = new Map(); // sessionID -> { name, child, busy, queue }
  const typed = new Map(); // sessionID -> what the user typed after /filo

  const stop = (sessionID) => {
    const state = sessions.get(sessionID);
    if (!state) return false;
    sessions.delete(sessionID);
    state.child.kill("SIGTERM");
    return true;
  };

  const start = (sessionID, name) =>
    new Promise((resolve, reject) => {
      const child = spawn(FILO, ["join", "--as", name, "--json", "--parent", String(process.pid)], {
        cwd: directory,
        stdio: ["ignore", "pipe", "pipe"],
      });
      // The turn that runs filo_join is itself busy, so early messages wait for it to end.
      const state = { name, child, busy: true, queue: [] };
      let joinedName = null;
      let settled = false;
      let errors = "";
      const finish = (action) => {
        if (!settled) {
          settled = true;
          clearTimeout(timer);
          action();
        }
      };
      const timer = setTimeout(() => finish(() => { child.kill("SIGTERM"); reject(new Error("filo did not answer in 10 seconds")); }), 10000);
      child.stderr.on("data", (chunk) => { errors += chunk; });
      child.on("error", (error) => finish(() => reject(new Error(`cannot run ${FILO}: ${error.message}`))));
      child.on("exit", () => {
        if (sessions.get(sessionID) === state) sessions.delete(sessionID);
        finish(() => reject(new Error(errors.trim() || "filo join ended")));
      });
      createInterface({ input: child.stdout }).on("line", (line) => {
        let event;
        try { event = JSON.parse(line); } catch { return; }
        if (event.kind === "joined") {
          joinedName = event.name;
          state.name = event.name;
        } else if (event.kind === "participants" && joinedName) {
          sessions.set(sessionID, state);
          finish(() => resolve({ name: joinedName, others: event.participants.map((p) => p.name) }));
        } else if (event.kind === "message") {
          onMessage(sessionID, state, event);
        }
      });
    });

  process.on("exit", () => { for (const state of sessions.values()) state.child.kill("SIGTERM"); });

  // What /filo does, as text for the session to act on. `words` is what the user typed after it.
  const join = async (sessionID, words) => {
    const existing = sessions.get(sessionID);
    if (existing) return `This session is already in the pool as ${existing.name}. Tell the user, and stop.`;
    if (words.length > 2 || !words.every((word) => WORD.test(word))) {
      return `The arguments after /filo must be "PROFILE" or "PROFILE NAME". Tell the user, and stop.`;
    }
    let profile = null;
    if (words[0]) {
      const found = spawnSync(FILO, ["profile", words[0]], { cwd: directory, encoding: "utf8" });
      if (found.status === 0) profile = { name: words[0], text: found.stdout.trim() };
      else if (words.length === 2) return `There is no profile "${words[0]}". Tell the user, and stop.`;
    }
    const wanted = words[1] ?? words[0] ?? "opencode";
    let joined;
    try {
      joined = await start(sessionID, wanted);
    } catch (error) {
      return `Could not join the pool: ${error.message}. Tell the user, and stop.`;
    }
    const others = joined.others.length ? joined.others.join(", ") : "nobody else yet";
    return [
      `Joined the pool as ${joined.name}. Connected: ${others}.`,
      profile ? `Your profile is ${profile.name}. Follow it:\n\n${profile.text}` : "You have no profile.",
      `Messages arrive as user messages that start with "[filo] Message from <name>:" and end with the command to reply.`,
      "Tell the user your name, your profile and who is connected, and stop. Connecting is the whole task.",
    ].join("\n\n");
  };

  const leave = (sessionID) => (stop(sessionID) ? "Left the pool." : "This session is not in the pool.");

  return { sessions, typed, join, leave, stop };
};

const wordsOf = (text) => (text ?? "").trim().split(/\s+/).filter(Boolean);

const JOIN_TOOL = "Join the filo pool of this directory, so that other agents can message this session. Use it for /filo.";
const LEAVE_TOOL = "Leave the filo pool of this directory. Use it when the user tells you to leave.";

// OpenCode 1: a function that gets the client and returns hooks.
const server = async ({ client, directory }) => {
  const note = (level, message) => {
    try {
      Promise.resolve(client.app.log({ body: { service: "filo", level, message } })).catch(() => {});
    } catch {
      // logging must never break a session
    }
  };

  // A message waits while the session works, then all that waited go in one prompt.
  const flush = async (sessionID, state) => {
    if (state.busy || state.queue.length === 0) return;
    const text = state.queue.splice(0).join("\n\n");
    state.busy = true;
    try {
      await client.session.promptAsync({ path: { id: sessionID }, body: { parts: [{ type: "text", text }] } });
    } catch (error) {
      state.busy = false;
      state.queue.unshift(text);
      note("error", `could not deliver to ${state.name}: ${error}`);
    }
  };

  const { sessions, typed, join, leave, stop } = pool({
    directory,
    onMessage: (sessionID, state, event) => {
      state.queue.push(render(event));
      void flush(sessionID, state);
    },
  });

  // Events can be missed, so a slow poll also notices a session that went idle.
  const timer = setInterval(async () => {
    const waiting = [...sessions].filter(([, state]) => state.queue.length > 0);
    if (waiting.length === 0) return;
    let working = {};
    try { working = (await client.session.status()).data ?? {}; } catch { return; }
    for (const [sessionID, state] of waiting) {
      if (!working[sessionID]) state.busy = false;
      void flush(sessionID, state);
    }
  }, POLL_MS);
  timer.unref();

  return {
    "command.execute.before": async (input) => {
      if (input.command === "filo") typed.set(input.sessionID, input.arguments ?? "");
    },

    event: async ({ event }) => {
      const sessionID = event.properties?.sessionID;
      const state = sessions.get(sessionID);
      if (event.type === "session.deleted") {
        stop(event.properties?.info?.id ?? sessionID);
      } else if (state && event.type === "session.status") {
        state.busy = event.properties.status.type !== "idle";
        if (!state.busy) void flush(sessionID, state);
      } else if (state && event.type === "session.idle") {
        state.busy = false;
        void flush(sessionID, state);
      }
    },

    tool: {
      filo_join: {
        description: JOIN_TOOL,
        args: {},
        async execute(_args, ctx) {
          const words = wordsOf(typed.get(ctx.sessionID));
          typed.delete(ctx.sessionID);
          return join(ctx.sessionID, words);
        },
      },

      filo_leave: {
        description: LEAVE_TOOL,
        args: {},
        async execute(_args, ctx) {
          return leave(ctx.sessionID);
        },
      },
    },
  };
};

// OpenCode 2: a definition with an id and a setup that gets the context. `/filo` is a command of
// the plugin itself, so the session receives the answer as a prompt. The inbox queues a prompt
// while the session works.
const setup = async (ctx) => {
  const { stop, join, leave, sessions } = pool({
    directory: ctx.location.directory,
    onMessage: (sessionID, _state, event) => {
      ctx.session.prompt({ sessionID, text: render(event), delivery: "queue" }).catch(() => {});
    },
  });

  await ctx.command.transform((editor) => {
    editor.add({
      name: "filo",
      description: "Join the filo pool, optionally with a profile: /filo [profile] [name]",
      execute: async ({ sessionID, prompt, delivery }) => {
        const answer = await join(sessionID, wordsOf(prompt.text));
        const text = [
          "The user ran /filo. Load the `filo` skill if you have not yet, because it says how to reply to messages.",
          answer,
        ].join("\n\n");
        await ctx.session.prompt({ sessionID, text, delivery });
      },
    });
  });

  // The command file linked for OpenCode 1 may still be the one that answers /filo; it asks
  // the model to call this tool, and the model passes what the user typed.
  await ctx.tool.transform((editor) => {
    editor.add({
      name: "filo_join",
      description: JOIN_TOOL,
      input: {
        type: "object",
        properties: { arguments: { type: "string", description: "Exactly what the user typed after /filo, if anything." } },
        additionalProperties: false,
      },
      execute: async (input, context) => ({ content: await join(context.sessionID, wordsOf(input.arguments)) }),
    });
    editor.add({
      name: "filo_leave",
      description: LEAVE_TOOL,
      input: { type: "object", properties: {}, additionalProperties: false },
      execute: async (_input, context) => ({ content: leave(context.sessionID) }),
    });
  });

  const events = new AbortController();
  void (async () => {
    for await (const event of ctx.event.subscribe({ signal: events.signal })) {
      if (event.type === "session.deleted") stop(event.data.sessionID);
    }
  })().catch(() => {});

  return () => {
    events.abort();
    for (const sessionID of [...sessions.keys()]) stop(sessionID);
  };
};

export default { id: "filo", setup, server };
