import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

test("project contains required runnable entrypoints", () => {
  assert.ok(fs.existsSync(new URL("../src/index.js", import.meta.url)));
  assert.ok(fs.existsSync(new URL("../src/db.js", import.meta.url)));
  const pkg = JSON.parse(fs.readFileSync(new URL("../package.json", import.meta.url), "utf8"));
  assert.equal(pkg.type, "module");
  assert.ok(pkg.scripts.start);
});
