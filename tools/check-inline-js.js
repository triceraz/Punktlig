// Parse the page's inline script without opening a browser.
//
// index.html carries a thousand lines of JavaScript that only ever run in a
// browser, so a syntax error is invisible until someone loads the page. This
// asks the same engine the browser uses whether the source parses at all.
//
//   node tools/check-inline-js.js web/index.html
//
// Exits non-zero on a syntax error, so it can gate a commit.

const fs = require("fs");
const vm = require("vm");

const target = process.argv[2];
if (!target) {
  console.error("usage: node tools/check-inline-js.js <file.html>");
  process.exit(2);
}

const html = fs.readFileSync(target, "utf8");
const bodies = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)]
  .map((m) => m[1]);

if (!bodies.length) {
  console.error("no inline script found");
  process.exit(1);
}

bodies.forEach((body, i) => {
  try {
    new vm.Script(body, { filename: `inline-${i}.js` });
    console.log(`script ${i}: parses, ${body.split("\n").length} lines`);
  } catch (err) {
    console.error(`script ${i}: SYNTAX ERROR ${err.message}`);
    process.exitCode = 1;
  }
});
