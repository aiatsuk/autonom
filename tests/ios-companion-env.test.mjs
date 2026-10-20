import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { IdbCompanion, companionEnvironment } from "../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs";

test("companion environment keeps required system settings, drops secrets and loader overrides", () => {
  const system = {
    PATH: "/usr/bin:/bin", HOME: "/Users/test", TMPDIR: "/tmp/",
    USER: "test", LOGNAME: "test", LANG: "en_US.UTF-8", LC_ALL: "C",
    LC_CTYPE: "UTF-8", DEVELOPER_DIR: "/Applications/Xcode.app/Contents/Developer",
  };
  const input = { ...system, GITHUB_TOKEN: "fake", OPENAI_API_KEY: "fake",
    ANTHROPIC_API_KEY: "fake", AWS_SECRET_ACCESS_KEY: "fake", CUSTOM_SECRET: "fake",
    NODE_OPTIONS: "--require evil.js", DYLD_INSERT_LIBRARIES: "evil.dylib",
    LC_SECRET: "fake", AUTONOM_IDB_COMPANION: "remote:1234" };
  assert.deepEqual(companionEnvironment(input), system);
  assert.equal(input.CUSTOM_SECRET, "fake", "the caller's environment is not mutated");
  assert.deepEqual(companionEnvironment({}), {});
  assert.deepEqual(companionEnvironment({ PATH: undefined, HOME: null, LANG: 123 }), {});
});

test("every start filters explicit environments and never passes the caller's object to spawn", async (t) => {
  const directory = mkdtempSync(join(tmpdir(), "idbenv-"));
  t.after(() => rmSync(directory, { recursive: true, force: true }));
  const env = { PATH: "/usr/bin:/bin", CUSTOM_SECRET: "fake" };
  const launches = [];
  const companion = new IdbCompanion({
    udid: "TEST-UDID", env, socketDir: directory,
    spawn: (binary, args, options) => {
      launches.push(options.env);
      throw new Error("stop before launching");
    },
  });
  await assert.rejects(companion.start(), /stop before launching/);
  env.OTHER_SECRET = "another fake";
  await assert.rejects(companion.start(), /stop before launching/);
  assert.deepEqual(launches, [{ PATH: "/usr/bin:/bin" }, { PATH: "/usr/bin:/bin" }]);
  assert.notEqual(launches[0], env);
  assert.notEqual(launches[0], launches[1]);
});

test("the default process environment is filtered too", () => {
  const moduleUrl = new URL("../plugins/autonom/skills/android-emulator-browser/scripts/ios-idb-companion.mjs", import.meta.url);
  const script = `
    import { IdbCompanion } from ${JSON.stringify(moduleUrl.href)};
    const companion = new IdbCompanion({ udid: "TEST-UDID", transport: "tcp", port: 12345,
      spawn(binary, args, options) {
        console.log(JSON.stringify({ keys: Object.keys(options.env).sort(), path: options.env.PATH }));
        throw new Error("audit stop");
      }
    });
    try { await companion.start(); } catch (error) {
      if (error.message !== "audit stop") throw error;
    }
  `;
  const result = spawnSync(process.execPath, ["--input-type=module", "-e", script], {
    env: { PATH: "/usr/bin:/bin", CUSTOM_SECRET: "synthetic", OPENAI_API_KEY: "synthetic" },
    encoding: "utf8",
  });
  assert.equal(result.status, 0, result.stderr);
  assert.deepEqual(JSON.parse(result.stdout), { keys: ["PATH"], path: "/usr/bin:/bin" });
});
