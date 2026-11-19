// Loads a widget in Chromium the way a host would (sandboxed iframe, opaque origin, the MCP Apps
// default CSP) and plays the host side of the protocol. Prints one JSON report on stdout.
// Usage: node run.cjs <widget.html>
const fs = require("fs");
let playwright;
try { playwright = require("playwright"); } catch (e) { playwright = require("/opt/node-tools/node_modules/playwright"); }

const CSP = "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; " +
  "img-src 'self' data:; media-src 'self' data:; connect-src 'none'";

const HOST = `
<iframe id="app" sandbox="allow-scripts" style="width:520px;height:400px;border:0"></iframe>
<script>
  const log = (window.__log = []);
  const frame = document.getElementById("app");
  const JPEG = "data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wAALCAABAAEBAREA/8QAFAABAAAAAAAAAAAAAAAAAAAAA//EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AN//Z";
  const wrap = (body) => ({ content: [{ type: "text", text: JSON.stringify(body) }], structuredContent: body });
  const send = (message) => frame.contentWindow.postMessage(message, "*");
  let polls = 0;
  window.addEventListener("message", (event) => {
    if (event.source !== frame.contentWindow) return;
    const msg = event.data;
    if (msg === "pwned") { log.push({ pwned: true }); return; }
    log.push(msg);
    if (!msg || msg.jsonrpc !== "2.0" || msg.id === undefined) {
      if (msg && msg.method === "ui/notifications/initialized") {
        send({ jsonrpc: "2.0", method: "ui/notifications/tool-input", params: { arguments: {} } });
        send({ jsonrpc: "2.0", method: "bogus/notification", params: {} });
        send({ not: "json-rpc" });
        send({ jsonrpc: "2.0", id: 9001, method: "ping" });
        send({ jsonrpc: "2.0", id: 9002, method: "something/unknown" });
        send({ jsonrpc: "2.0", method: "ui/notifications/tool-result", params: wrap({
          kind: "generation", status: "submitted",
          jobs: [
            { index: 0, job_id: "job_a", status: "queued" },
            { index: 1, job_id: "job_b", status: "failed",
              error: { code: "x", message: "<img src=x onerror=\\"parent.postMessage('pwned','*')\\">" } },
          ],
        }) });
      }
      return;
    }
    if (msg.method === "ui/initialize") {
      if (window.__dropFirst && !window.__dropped) { window.__dropped = true; return; }  // a late listener
      send({ jsonrpc: "2.0", id: msg.id, result: {
        protocolVersion: msg.params.protocolVersion, hostInfo: { name: "harness", version: "1" },
        hostCapabilities: {}, hostContext: { theme: "dark" } } });
    } else if (msg.method === "tools/call" && msg.params.name === "jobs_wait") {
      polls += 1;
      const done = polls >= 2;
      send({ jsonrpc: "2.0", id: msg.id, result: wrap({ kind: "jobs", all_terminal: done, jobs: msg.params.arguments.job_ids.map((id) => (
        done ? { job_id: id, status: "completed", results: [{ media_id: "med_1", kind: "image", width: 8, height: 8, filename: "<b>x</b>.png" }] }
             : { job_id: id, status: "running", progress: 0.5 })) }) });
    } else if (msg.method === "tools/call" && msg.params.name === "media_previews") {
      send({ jsonrpc: "2.0", id: msg.id, result: wrap({ kind: "media_previews", items: [
        { media_id: "med_1", kind: "image", data_url: JPEG },
        { media_id: "med_2", kind: "image", data_url: "https://evil.test/x.png" },
        { media_id: "med_3", kind: "image", data_url: "javascript:alert(1)" } ] }) });
    } else {
      send({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "Method not found" } });
    }
  });
  window.__dropFirst = location.hash === "#late";
  frame.src = "http://widget.test/app.html";
</script>`;

(async () => {
  const html = fs.readFileSync(process.argv[2], "utf8");
  const browser = await playwright.chromium.launch();
  const report = {};
  for (const mode of ["normal", "late"]) {
    const page = await browser.newPage();
    const problems = [];
    const network = [];
    page.on("console", (m) => { if (m.type() === "error") problems.push(m.text()); });
    page.on("pageerror", (e) => problems.push(String(e)));
    page.on("request", (r) => { if (!/^(http:\/\/(host|widget)\.test\/|data:)/.test(r.url())) network.push(r.url()); });
    await page.route("http://host.test/", (r) => r.fulfill({ contentType: "text/html", body: HOST }));
    await page.route("http://widget.test/app.html", (r) =>
      r.fulfill({ contentType: "text/html", headers: { "content-security-policy": CSP }, body: html }));
    await page.goto("http://host.test/" + (mode === "late" ? "#late" : ""));
    await page.waitForTimeout(mode === "late" ? 4500 : 6500);
    const log = await page.evaluate(() => window.__log);
    const frame = page.frames().find((f) => f.url().includes("widget.test"));
    const dom = await frame.evaluate(() => ({
      tiles: document.querySelectorAll(".tile").length,
      images: [...document.querySelectorAll("img")].map((i) => i.src.slice(0, 23) + "|" + i.naturalWidth),
      injected: document.querySelectorAll("b, img[src='x'], script:not(:first-of-type)").length,
      text: document.body.innerText.replace(/\s+/g, " "),
      theme: document.documentElement.getAttribute("data-theme"),
    }));
    report[mode] = { log, dom, problems, network };
    await page.close();
  }
  await browser.close();
  process.stdout.write(JSON.stringify(report));
})().catch((e) => { console.error(e); process.exit(1); });
