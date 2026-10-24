// Runs an n8n Code node's jsCode ("Run Once for All Items" mode) outside n8n, with a stub of the
// parts of the n8n runtime the InvoiceDesk workflows use: $input.all(), $env and
// this.helpers.getBinaryDataBuffer(itemIndex, propertyName). Reads a JSON job on stdin:
//   { "code": "...", "env": {...}, "items": [{ "json": {...}, "binary": { key: { mimeType, fileName, data_b64 } } }] }
// and prints { "ok": true, "items": [...] } or { "ok": false, "error": "..." }.
'use strict';
let raw = '';
process.stdin.setEncoding('utf8');
process.stdin.on('data', (c) => { raw += c; });
process.stdin.on('end', async () => {
  try {
    const job = JSON.parse(raw);
    const items = (job.items || []).map((it) => {
      const binary = {};
      for (const [k, v] of Object.entries(it.binary || {})) {
        binary[k] = { mimeType: v.mimeType, fileName: v.fileName, fileSize: String(Buffer.from(v.data_b64, 'base64').length) };
      }
      return { json: it.json || {}, binary };
    });
    const helpers = {
      async getBinaryDataBuffer(i, key) {
        const b = job.items[i].binary[key];
        return Buffer.from(b.data_b64, 'base64');
      },
    };
    const $input = { all: () => items };
    const $env = job.env || {};
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const fn = new AsyncFunction('$input', '$env', 'require', job.code);
    const out = await fn.call({ helpers }, $input, $env, require);
    process.stdout.write(JSON.stringify({ ok: true, items: out }));
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, error: String(err && err.message ? err.message : err) }));
  }
});
