// mBot2 blocks for TurboWarp (https://turbowarp.org-style editors).
//
// Talks to this repo's server.py over its browser WebSocket (/ws), the same
// JSON messages static/index.html sends, so it works with either transport
// (USB serial or Wi-Fi). Load it as an UNSANDBOXED custom extension:
//   Extensions -> Custom Extension -> URL -> http://lookingglass.local/scratch/ext/mbot2.js
//
// The editor page must be plain http (or localhost): an https page can't open a
// ws:// socket to the LAN (mixed content). By default it connects to
// ws://<page host>/mbot/ws, which nginx on lookingGlass proxies to :8097/ws;
// the "connect to" block overrides that.
//
// Safety: the server stops the robot if drive commands stop for 0.5 s, so this
// resends the current target every 100 ms while moving. The stop sign, closing
// the tab, or losing the socket all stop the robot.
(function (Scratch) {
  "use strict";
  if (!Scratch.extensions.unsandboxed) {
    throw new Error("mBot2 extension must run unsandboxed (tick 'Run extension without sandbox').");
  }

  const MAX_RPM = 200;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const defaultUrl = () =>
    location.protocol === "http:" ? `ws://${location.host}/mbot/ws` : "ws://lookingglass.local/mbot/ws";

  let ws = null;
  let url = defaultUrl();
  let link = "disconnected"; // websocket + robot link state, for the reporter
  let robotUp = false;
  let tel = {};
  let target = [0, 0];
  let wantOpen = true;
  let retry = null;

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  function connect(newUrl) {
    if (newUrl) url = newUrl;
    wantOpen = true;
    clearTimeout(retry);
    if (ws) {
      ws.onclose = null;
      ws.close();
    }
    link = "connecting";
    try {
      ws = new WebSocket(url);
    } catch (e) {
      link = "error: " + e.message;
      return;
    }
    ws.onopen = () => (link = "server up, waiting for robot");
    ws.onmessage = (ev) => {
      let m;
      try {
        m = JSON.parse(ev.data);
      } catch (e) {
        return;
      }
      if (m.type === "link") {
        robotUp = m.state === "up";
        link = robotUp ? "connected" : m.error || m.state;
      } else if (m.type === "telemetry") {
        tel = m;
      }
    };
    ws.onclose = () => {
      robotUp = false;
      link = "disconnected";
      if (wantOpen) retry = setTimeout(() => connect(), 3000);
    };
  }

  function setTarget(l, r) {
    target = [clamp(Math.round(l), -MAX_RPM, MAX_RPM), clamp(Math.round(r), -MAX_RPM, MAX_RPM)];
    if (target[0] === 0 && target[1] === 0) send({ type: "stop" });
    else send({ type: "drive", l: target[0], r: target[1] });
  }
  const stop = () => setTarget(0, 0);

  // Keepalive for the server's 0.5 s drive watchdog.
  setInterval(() => {
    if (target[0] || target[1]) send({ type: "drive", l: target[0], r: target[1] });
  }, 100);

  const vm = Scratch.vm;
  vm.runtime.on("PROJECT_STOP_ALL", stop);
  window.addEventListener("beforeunload", stop);

  const sleep = (s) => new Promise((res) => setTimeout(res, Math.max(0, s) * 1000));
  const num = (v) => Scratch.Cast.toNumber(v);
  const rnd = (v) => Math.round(num(v) * 10) / 10;

  class MBot2 {
    getInfo() {
      return {
        id: "jcaccmbot2",
        name: "mBot2",
        color1: "#2f8bd8",
        color2: "#2370b3",
        blocks: [
          {
            opcode: "connectTo",
            blockType: Scratch.BlockType.COMMAND,
            text: "connect to [URL]",
            arguments: { URL: { type: Scratch.ArgumentType.STRING, defaultValue: url } },
          },
          { opcode: "isConnected", blockType: Scratch.BlockType.BOOLEAN, text: "robot connected?" },
          { opcode: "linkState", blockType: Scratch.BlockType.REPORTER, text: "connection status" },
          "---",
          {
            opcode: "drive",
            blockType: Scratch.BlockType.COMMAND,
            text: "drive [DIR] at [SPEED] RPM",
            arguments: {
              DIR: { type: Scratch.ArgumentType.STRING, menu: "dir", defaultValue: "forward" },
              SPEED: { type: Scratch.ArgumentType.NUMBER, defaultValue: 80 },
            },
          },
          {
            opcode: "driveFor",
            blockType: Scratch.BlockType.COMMAND,
            text: "drive [DIR] at [SPEED] RPM for [SECS] seconds",
            arguments: {
              DIR: { type: Scratch.ArgumentType.STRING, menu: "dir", defaultValue: "forward" },
              SPEED: { type: Scratch.ArgumentType.NUMBER, defaultValue: 80 },
              SECS: { type: Scratch.ArgumentType.NUMBER, defaultValue: 1 },
            },
          },
          {
            opcode: "wheels",
            blockType: Scratch.BlockType.COMMAND,
            text: "set left wheel [L] right wheel [R] RPM",
            arguments: {
              L: { type: Scratch.ArgumentType.NUMBER, defaultValue: 50 },
              R: { type: Scratch.ArgumentType.NUMBER, defaultValue: 50 },
            },
          },
          { opcode: "stopMotors", blockType: Scratch.BlockType.COMMAND, text: "stop motors" },
          "---",
          {
            opcode: "ledColor",
            blockType: Scratch.BlockType.COMMAND,
            text: "set LEDs to [COLOR]",
            arguments: { COLOR: { type: Scratch.ArgumentType.COLOR, defaultValue: "#00a0ff" } },
          },
          {
            opcode: "ledRGB",
            blockType: Scratch.BlockType.COMMAND,
            text: "set LEDs to red [R] green [G] blue [B]",
            arguments: {
              R: { type: Scratch.ArgumentType.NUMBER, defaultValue: 255 },
              G: { type: Scratch.ArgumentType.NUMBER, defaultValue: 0 },
              B: { type: Scratch.ArgumentType.NUMBER, defaultValue: 0 },
            },
          },
          { opcode: "ledOff", blockType: Scratch.BlockType.COMMAND, text: "turn LEDs off" },
          {
            opcode: "beep",
            blockType: Scratch.BlockType.COMMAND,
            text: "beep at [FREQ] Hz",
            arguments: { FREQ: { type: Scratch.ArgumentType.NUMBER, defaultValue: 880 } },
          },
          {
            opcode: "say",
            blockType: Scratch.BlockType.COMMAND,
            text: "show [TEXT] on screen",
            arguments: { TEXT: { type: Scratch.ArgumentType.STRING, defaultValue: "Hello!" } },
          },
          "---",
          { opcode: "distance", blockType: Scratch.BlockType.REPORTER, text: "distance (cm)" },
          { opcode: "line", blockType: Scratch.BlockType.REPORTER, text: "line sensor" },
          { opcode: "battery", blockType: Scratch.BlockType.REPORTER, text: "battery %" },
          {
            opcode: "tilt",
            blockType: Scratch.BlockType.REPORTER,
            text: "[AXIS] angle",
            arguments: { AXIS: { type: Scratch.ArgumentType.STRING, menu: "axis", defaultValue: "roll" } },
          },
          { opcode: "light", blockType: Scratch.BlockType.REPORTER, text: "light level" },
          { opcode: "loudness", blockType: Scratch.BlockType.REPORTER, text: "loudness" },
          {
            opcode: "wheelSpeed",
            blockType: Scratch.BlockType.REPORTER,
            text: "[WHEEL] wheel speed",
            arguments: { WHEEL: { type: Scratch.ArgumentType.STRING, menu: "wheel", defaultValue: "left" } },
          },
        ],
        menus: {
          dir: { acceptReporters: true, items: ["forward", "backward", "left", "right"] },
          axis: { acceptReporters: false, items: ["roll", "pitch"] },
          wheel: { acceptReporters: false, items: ["left", "right"] },
        },
      };
    }

    connectTo({ URL }) {
      connect(String(URL));
    }
    isConnected() {
      return robotUp;
    }
    linkState() {
      return link;
    }

    drive({ DIR, SPEED }) {
      const v = clamp(num(SPEED), -MAX_RPM, MAX_RPM);
      const turn = v * 0.6;
      const map = { forward: [v, v], backward: [-v, -v], left: [-turn, turn], right: [turn, -turn] };
      const lr = map[String(DIR).toLowerCase()] || [0, 0];
      setTarget(lr[0], lr[1]);
    }
    async driveFor(args) {
      this.drive(args);
      await sleep(num(args.SECS));
      stop();
    }
    wheels({ L, R }) {
      setTarget(num(L), num(R));
    }
    stopMotors() {
      stop();
    }

    ledColor({ COLOR }) {
      const [r, g, b] = Scratch.Cast.toRgbColorList(COLOR);
      send({ type: "led", r, g, b });
    }
    ledRGB({ R, G, B }) {
      const c = (v) => clamp(Math.round(num(v)), 0, 255);
      send({ type: "led", r: c(R), g: c(G), b: c(B) });
    }
    ledOff() {
      send({ type: "led_off" });
    }
    async beep({ FREQ }) {
      send({ type: "beep", freq: clamp(Math.round(num(FREQ)), 100, 4000) });
      await sleep(0.15); // the tone length on the robot
    }
    say({ TEXT }) {
      send({ type: "say", text: String(TEXT).slice(0, 40) });
    }

    distance() {
      return tel.dist == null ? "" : rnd(tel.dist);
    }
    line() {
      return tel.line == null ? "" : tel.line;
    }
    battery() {
      return tel.batt == null ? "" : tel.batt;
    }
    tilt({ AXIS }) {
      const v = AXIS === "pitch" ? tel.pitch : tel.roll;
      return v == null ? "" : rnd(v);
    }
    light() {
      return tel.light == null ? "" : tel.light;
    }
    loudness() {
      return tel.loud == null ? "" : tel.loud;
    }
    wheelSpeed({ WHEEL }) {
      // EM2 is mounted mirrored (server.py RIGHT_SIGN = -1), so flip it to make
      // forward positive on both wheels.
      const v = WHEEL === "right" ? (tel.em2 == null ? null : -tel.em2) : tel.em1;
      return v == null ? "" : rnd(v);
    }
  }

  connect();
  Scratch.extensions.register(new MBot2());
})(Scratch);
