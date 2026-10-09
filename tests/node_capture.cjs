const crypto = require("node:crypto");
const fs = require("node:fs");
const inspector = require("node:inspector");
const path = require("node:path");

const folder = process.env.JS_COVERAGE_DIR;
const script = process._eval;
if (folder && typeof script === "string") {
  const session = new inspector.Session();
  session.connect();
  session.post("Profiler.enable");
  session.post("Profiler.startPreciseCoverage", { callCount: true, detailed: true });
  process.on("exit", () => {
    session.post("Profiler.takePreciseCoverage", (error, coverage) => {
      if (error) throw error;
      const source = crypto.createHash("sha256").update(script).digest("hex");
      const unique = `${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}`;
      fs.mkdirSync(path.join(folder, "sources"), { recursive: true });
      const stored = path.join(folder, "sources", `${source}.js`);
      if (!fs.existsSync(stored)) {
        fs.writeFileSync(`${stored}.${unique}`, script);
        fs.renameSync(`${stored}.${unique}`, stored);
      }
      const result = coverage.result
        .filter((entry) => /^\[eval\d*\]$/.test(entry.url))
        .map((entry) => ({ source, functions: entry.functions }));
      fs.writeFileSync(path.join(folder, `capture-node-${unique}.json`), JSON.stringify({ result }));
    });
  });
}
