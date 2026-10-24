/**
 * Renders the mock-mode app with Playwright and saves screenshots to apps/web/screenshots/.
 * Also asserts there is no horizontal scroll at 375 px on every page.
 *
 * Usage (from apps/web, with the app running in mock mode, e.g. `pnpm build && pnpm start`):
 *   NODE_PATH=/opt/node22/lib/node_modules BASE_URL=http://localhost:3000 node scripts/screenshots.cjs
 *
 * Uses the preinstalled Playwright and browsers; never runs "playwright install".
 */
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright");

const BASE = process.env.BASE_URL || "http://localhost:3000";
const OUT = path.resolve(__dirname, "..", "screenshots");
const fixtures = JSON.parse(fs.readFileSync(path.resolve(__dirname, "..", "lib", "mock", "fixtures", "demo-batch.json"), "utf8"));
const byFile = (prefix) => fixtures.documents.find((d) => d.filename.startsWith(prefix));

const PAGES = ["/", "/review", "/approvals", "/followups", "/dashboard", "/compare", "/scorecard", "/try"];

async function settle(page) {
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(400);
}

async function main() {
  fs.mkdirSync(OUT, { recursive: true });
  const browser = await chromium.launch();
  const failures = [];
  try {
    // ---------------------------------------------------------------- desktop
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    page.on("pageerror", (e) => failures.push(`page error: ${e.message}`));

    await page.goto(`${BASE}/`);
    await page.waitForSelector('[data-testid="doc-card"]');
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "board.png"), fullPage: true });

    // Live run: click the demo button, capture mid-run, then the settled board.
    await page.click('[data-testid="run-demo"]');
    await page.waitForTimeout(2600);
    await page.screenshot({ path: path.join(OUT, "board-live.png") });
    await page.waitForFunction(
      () => document.querySelector('[data-testid="count-ready_to_pay"]')?.textContent?.trim() === "18",
      null,
      { timeout: 30000 },
    );
    await page.waitForTimeout(800);
    const counts = await page.evaluate(() =>
      Object.fromEntries(
        ["ready_to_pay", "needs_review", "flagged"].map((c) => [c, document.querySelector(`[data-testid="count-${c}"]`)?.textContent?.trim()]),
      ),
    );
    if (counts.ready_to_pay !== "18" || counts.needs_review !== "4" || counts.flagged !== "3")
      failures.push(`settled board counts wrong: ${JSON.stringify(counts)}`);
    const cost = await page.textContent('[data-testid="cost-meter"]');
    if (!/\$0\.27/.test(cost || "")) failures.push(`cost meter did not reach $0.27: ${cost}`);
    await page.screenshot({ path: path.join(OUT, "board-settled.png"), fullPage: true });

    // Viewer: hidden-text PDF, faint-text photo, handwritten review.
    const pdfAttack = byFile("18-");
    const photoAttack = byFile("13-");
    const handwritten = byFile("03-");
    for (const [name, doc] of [
      ["viewer-hidden-text.png", pdfAttack],
      ["viewer-faint-text.png", photoAttack],
      ["viewer-review.png", handwritten],
    ]) {
      await page.goto(`${BASE}/documents/${doc.id}`);
      await page.waitForSelector('[data-testid="document-viewer"]');
      await page.waitForFunction(() => Number(document.querySelector('[data-testid="overlay-canvas"]')?.getAttribute("data-rects")) > 0);
      await settle(page);
      await page.screenshot({ path: path.join(OUT, name), fullPage: false });
    }
    const alert = await page.goto(`${BASE}/documents/${pdfAttack.id}`).then(() => page.waitForSelector('[data-testid="hidden-text-alert"]'));
    const alertText = await alert.textContent();
    if (!alertText.includes("Hidden instruction detected: attempted bank-account change, blocked")) failures.push("hidden-text label missing");
    if (/\d{6,}/.test(alertText)) failures.push("unmasked number in hidden-text alert");

    await page.goto(`${BASE}/compare`);
    await page.waitForSelector('[data-testid="comparison-rows"] li');
    await settle(page);
    await page.screenshot({ path: path.join(OUT, "compare.png"), fullPage: true });
    if ((await page.locator('[data-testid="wrong-naive"]').count()) === 0) failures.push("no WRONG stamps on comparison");

    // Upload a PDF in mock mode: the viewer renders it with pdf.js (no page image exists offline).
    await page.goto(`${BASE}/`);
    await page.waitForSelector('[data-testid="doc-card"]');
    await page.setInputFiles('input[type="file"]', path.resolve(__dirname, "..", "public", "mock", "report-card.pdf"));
    await page.waitForSelector('button[aria-label="Open report-card.pdf"]');
    await page.waitForTimeout(2500);
    await page.click('button[aria-label="Open report-card.pdf"]');
    await page.waitForSelector('canvas[aria-label="Invoice PDF"]');
    await page.waitForFunction(() => {
      const c = document.querySelector('canvas[aria-label="Invoice PDF"]');
      return c instanceof HTMLCanvasElement && c.width > 0 && c.height > 0;
    }, null, { timeout: 15000 });
    await page.waitForTimeout(800);
    const painted = await page.evaluate(() => {
      const c = document.querySelector('canvas[aria-label="Invoice PDF"]');
      const ctx = c.getContext("2d");
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      let dark = 0;
      for (let i = 0; i < d.length; i += 4) if (d[i] < 128 && d[i + 3] > 0) dark++;
      return dark;
    });
    if (painted < 50) failures.push(`pdf.js did not render the uploaded PDF (dark px ${painted})`);
    await page.screenshot({ path: path.join(OUT, "viewer-pdf-upload.png") });

    for (const [route, file] of [
      ["/dashboard", "dashboard.png"],
      ["/scorecard", "scorecard.png"],
      ["/approvals", "approvals.png"],
      ["/review", "review.png"],
      ["/followups", "followups.png"],
      ["/try", "try.png"],
    ]) {
      await page.goto(`${BASE}${route}`);
      await settle(page);
      await page.waitForTimeout(300);
      await page.screenshot({ path: path.join(OUT, file), fullPage: true });
    }
    await ctx.close();

    // ---------------------------------------------------------------- hero (1000x750)
    const hero = await browser.newContext({ viewport: { width: 1000, height: 750 }, deviceScaleFactor: 2 });
    const hp = await hero.newPage();
    await hp.goto(`${BASE}/?hero=1&doc=${photoAttack.id}`);
    await hp.waitForSelector('[data-testid="hidden-text-alert"]');
    await hp.waitForFunction(() => Number(document.querySelector('[data-testid="overlay-canvas"]')?.getAttribute("data-rects")) > 0);
    await settle(hp);
    await hp.screenshot({ path: path.join(OUT, "hero.png") });
    await hero.close();

    // ---------------------------------------------------------------- mobile 375 px
    const mobile = await browser.newContext({ viewport: { width: 375, height: 812 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true });
    const mp = await mobile.newPage();
    const routes = [...PAGES, `/documents/${pdfAttack.id}`, `/documents/${handwritten.id}`];
    for (const route of routes) {
      await mp.goto(`${BASE}${route}`);
      await settle(mp);
      const { sw, cw } = await mp.evaluate(() => ({ sw: document.documentElement.scrollWidth, cw: document.documentElement.clientWidth }));
      if (sw > cw) failures.push(`horizontal scroll at 375px on ${route}: scrollWidth ${sw} > ${cw}`);
    }
    await mp.goto(`${BASE}/`);
    await mp.waitForSelector('[data-testid="doc-card"]');
    await settle(mp);
    await mp.screenshot({ path: path.join(OUT, "mobile-board.png") });
    await mp.goto(`${BASE}/documents/${pdfAttack.id}`);
    await settle(mp);
    await mp.screenshot({ path: path.join(OUT, "mobile-viewer.png") });
    await mobile.close();
  } finally {
    await browser.close();
  }
  if (failures.length) {
    console.error(`FAILED:\n- ${failures.join("\n- ")}`);
    process.exit(1);
  }
  console.log(`screenshots written to ${OUT}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
