// Browser smoke test against the scratch instance (127.0.0.1:8421, scripts/scratch-instance.sh up):
//   node tests/ui/ui_check.js [check ...]
// Connects to an already-running Chromium over CDP (SV_CDP, default http://localhost:9222) in its own browser
// context, and closes only that context. Playwright is loaded from SV_PLAYWRIGHT (a module path) or, by default,
// from a normal `playwright` install.
const { chromium } = require(process.env.SV_PLAYWRIGHT || "playwright");

const BASE = process.env.SV_BASE || "http://127.0.0.1:8421";
const PASSWORD = process.env.SV_PASSWORD || "scratch";
const SHOTS = process.env.SV_SHOTS || "/tmp/sv-shots";
require("fs").mkdirSync(SHOTS, { recursive: true });

const results = [];
function check(name, ok, detail = "") {
  results.push({ name, ok, detail });
  console.log(`${ok ? "PASS" : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
}

async function login(page) {
  await page.goto(`${BASE}/login`);
  await page.fill("#password", PASSWORD);
  await Promise.all([page.waitForNavigation(), page.click(".login button[type=submit]")]);
}

const checks = {
  async editor(ctx, label) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    await login(page);
    await page.goto(`${BASE}/courses/D413/notes/notebook?mode=edit`);
    await page.waitForSelector(".CodeMirror", { timeout: 10000 });
    check(`${label}: EasyMDE loads`, true);
    const iconFont = await page.$eval(".editor-toolbar button.bold, .editor-toolbar .fa-bold",
      (el) => getComputedStyle(el.querySelector("i") || el, "::before").fontFamily).catch(() => "");
    check(`${label}: toolbar icons use vendored FontAwesome`, /FontAwesome/i.test(iconFont), iconFont);
    await page.click(".CodeMirror");
    await page.keyboard.press("Control+End");
    const marker = `autosave-${label}-${Date.now()}`;
    await page.keyboard.type(`\n\n${marker}\n\n\`\`\`python\nprint("hi")\n\`\`\`\n`);
    await page.waitForFunction(() => /^Saved /.test(document.getElementById("save-state").textContent), null, { timeout: 10000 });
    check(`${label}: autosave after 2 s idle`, true);
    await page.screenshot({ path: `${SHOTS}/${label}-editor.png`, fullPage: false });
    await page.goto(`${BASE}/courses/D413/notes/notebook`);
    const body = await page.textContent(".rendered");
    check(`${label}: saved text renders in view mode`, body.includes(marker));
    const hl = await page.$$eval(".rendered pre code.hljs", (els) => els.length);
    check(`${label}: highlight.js applied to code blocks`, hl > 0, `${hl} block(s)`);
    await page.screenshot({ path: `${SHOTS}/${label}-view.png`, fullPage: true });
    check(`${label}: no JS errors`, errors.length === 0, errors.join(" | "));
    await page.close();
  },

  async competencies(ctx, label) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await login(page);
    await page.goto(`${BASE}/courses/D282/competencies`);
    if (await page.$("button:has-text('Yes, replace')") === null && await page.$(".comp-row") === null) {
      await page.fill("#comp-text", "• Describes the AWS shared responsibility model\n2. Selects S3 storage classes\n3) Explains IAM policies");
      await Promise.all([page.waitForNavigation(), page.click("button:has-text('Import')")]);
    }
    const rows = await page.$$(".comp-row");
    check(`${label}: competency import`, rows.length === 3, `${rows.length} rows`);
    const first = await page.$(".comp-row");
    const id = await first.getAttribute("id");
    await page.click(`#${id} button[value='4']`);
    await page.waitForFunction((id) => document.querySelector(`#${id} button[value='4']`).getAttribute("aria-pressed") === "true", id, { timeout: 5000 });
    const score = (await page.textContent("#readiness-box .stat")).trim();
    check(`${label}: HTMX confidence updates row + readiness`, score.startsWith("80"), `readiness ${score}`);
    const href = await page.getAttribute(`#${id} .comp-text a`, "href");
    await page.goto(`${BASE}${href}`);
    const anchor = href.split("#")[1];
    check(`${label}: competency links to its notes section`, (await page.$(`[id='${anchor}']`)) !== null, anchor);
    await page.goto(`${BASE}/courses/D282/competencies`);
    await page.screenshot({ path: `${SHOTS}/${label}-competencies.png`, fullPage: true });
    check(`${label}: no JS errors (competencies)`, errors.length === 0, errors.join(" | "));
    await page.close();
  },

  async review(ctx, label) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await login(page);
    await page.goto(`${BASE}/courses/D281/cards`);
    await page.fill("textarea[name=text]", `Q: ${label} ls flag for hidden files?\nA: \`-a\`\n\n${label} chmod 755 :: rwxr-xr-x\n{{c1::/etc/passwd}} lists local users (${label})`);
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/cards/import'] button[type=submit]")]);
    const banner = await page.textContent(".banner");
    check(`${label}: bulk import`, /Imported 3 cards/.test(banner), banner.trim().split("\n")[0]);
    await page.goto(`${BASE}/review?course=D281`);
    const before = parseInt((await page.textContent(".review .muted")).match(/(\d+) left/)[1], 10);
    check(`${label}: answer + grades hidden until flipped`, !(await page.isVisible("#grades")) && !(await page.isVisible("#back")));
    if (label === "phone") {
      const box = await page.locator("#flip").boundingBox();
      await page.tap("#flip");
      const grades = await page.$$eval(".grades button", (bs) => bs.map((b) => Math.round(b.getBoundingClientRect().height)));
      check(`${label}: grade buttons ≥ 44 px tall`, grades.every((h) => h >= 44) && box.height >= 44, `flip ${Math.round(box.height)} px, grades ${grades.join("/")} px`);
      await page.screenshot({ path: `${SHOTS}/${label}-review.png`, fullPage: false });
      await Promise.all([page.waitForNavigation(), page.tap(".grades button[value='3']")]);
    } else {
      await page.keyboard.press("Space");
      check(`${label}: space shows the answer`, await page.isVisible("#back"));
      await Promise.all([page.waitForNavigation(), page.keyboard.press("3")]);
    }
    const after = await page.textContent(".review .muted");
    const left = parseInt(after.match(/(\d+) left/)[1], 10);
    check(`${label}: grading advances the queue`, left === before - 1, `${before} → ${left} left`);
    check(`${label}: no JS errors (review)`, errors.length === 0, errors.join(" | "));
    await page.close();
  },

  async quiz(ctx, label) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("dialog", (d) => d.accept());
    await login(page);
    const code = label === "phone" ? "D316" : "D317";
    for (let i = 1; i <= 10; i++) {
      await page.goto(`${BASE}/courses/${code}/quizzes/questions/new`);
      await page.fill("#prompt", `Q${i}: Which is correct for item ${i}?`);
      await page.fill("#choices", `* right ${i}\nwrong ${i}\nalso wrong ${i}`);
      await page.fill("#explanation", `Because ${i}.`);
      await Promise.all([page.waitForNavigation(), page.click("button.primary[type=submit]")]);
    }
    await page.goto(`${BASE}/courses/${code}/quizzes`);
    await page.fill("#count", "10");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/quizzes/start'] button[type=submit]")]);
    const fieldsets = await page.$$("fieldset.q");
    check(`${label}: 10-question quiz runs`, fieldsets.length === 10, `${fieldsets.length} questions`);
    // answer 7 right, 3 wrong (choice text tells us which is right)
    let n = 0;
    for (const fs of fieldsets) {
      const labels = await fs.$$(".choices label");
      for (const l of labels) {
        const t = await l.textContent();
        if ((n < 7 && t.includes("right")) || (n >= 7 && t.includes("also wrong"))) { await l.click(); break; }
      }
      n++;
    }
    await Promise.all([page.waitForNavigation(), page.click("#submit-btn")]);
    const stat = (await page.textContent(".stat")).replace(/\s+/g, " ").trim();
    check(`${label}: quiz scores`, stat.startsWith("7/10"), stat);
    await page.screenshot({ path: `${SHOTS}/${label}-quiz-result.png`, fullPage: false });
    await page.goto(`${BASE}/courses/${code}/notes/mistakes`);
    const misses = await page.$$eval(".rendered h3", (hs) => hs.filter((h) => /Quiz #\d+ · miss/.test(h.textContent)).length);
    check(`${label}: misses appended to mistakes.md`, misses === 3, `${misses} entries`);
    if (label === "desktop") {
      await page.goto(`${BASE}/courses/${code}/quizzes`);
      await page.fill("#count", "2");
      await page.check("#timed");
      await page.fill("input[name=minutes]", "1");
      await Promise.all([page.waitForNavigation(), page.click("form[action$='/quizzes/start'] button[type=submit]")]);
      const t0 = await page.textContent("#timer");
      await page.waitForURL(/\/quizzes\/\d+$/, { timeout: 5000 });
      await page.waitForSelector(".stat", { timeout: 75000 });
      check(`${label}: timed quiz submits itself at 0:00`, true, `timer started at ${t0}`);
    }
    check(`${label}: no JS errors (quiz)`, errors.length === 0, errors.join(" | "));
    await page.close();
  },

  async assessment(ctx, label) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await login(page);
    const setType = async (code, t) => {
      await page.goto(`${BASE}/courses/${code}/assessment`);
      await page.selectOption("#atype", t);
      await Promise.all([page.waitForNavigation(), page.click("form[action$='/assessment/type'] button")]);
    };
    // OA: D413
    await setType("D413", "OA");
    await page.goto(`${BASE}/courses/D413/competencies`);
    if (!(await page.$(".comp-row"))) {
      await page.fill("#comp-text", "Explains 802.11 standards\nCalculates link budgets");
      await Promise.all([page.waitForNavigation(), page.click("button:has-text('Import')")]);
    }
    await page.goto(`${BASE}/courses/D413/assessment`);
    const sel = await page.$$("select[name^=level_]");
    await sel[0].selectOption("competent");
    await sel[1].selectOption("approaching");
    await page.fill("#score", "74");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/preassessment'] button[type=submit]")]);
    const score = (await page.textContent("aside .stat")).replace(/\s+/g, " ").trim();
    check(`${label}: OA pre-assessment changes readiness`, score.startsWith("60"), score);
    await page.fill("#exam_date", "2026-10-16");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/assessment/exam'] button")]);
    check(`${label}: OA exam scheduled`, (await page.textContent(".page")).includes("Exam Oct 16, 2026"));
    await page.screenshot({ path: `${SHOTS}/${label}-oa.png`, fullPage: true });
    // PA: D339
    await setType("D339", "PA");
    await page.fill("#rubric", "A. Audience analysis\nB. Organization\nC. Sources cited");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/rubric'] button")]);
    await page.fill("#draft-text", "Memo to the network team.\nFirst pass.");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/drafts'] button")]);
    await page.fill("#draft-text", "Memo to the network team.\nSecond pass, tighter.\nSources: RFC 2328.");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/drafts'] button")]);
    await page.selectOption("#tasks select[name=status] >> nth=0", "done").catch(() => {});
    await page.waitForLoadState("load");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/assessment/diff'] button")]);
    check(`${label}: PA diff between versions`, (await page.$$("table.diff")).length === 1);
    await page.goto(`${BASE}/courses/D339/assessment`);
    await page.selectOption("form[action$='/assessment/submissions'] select[name=result]", "revision");
    await Promise.all([page.waitForNavigation(), page.click("form[action$='/assessment/submissions'] button")]);
    const txt = await page.textContent(".page");
    check(`${label}: PA integrity line + revision counted`, txt.includes("must be your own work") && /[1-9]\d* revisions?\b/.test(txt));
    await page.screenshot({ path: `${SHOTS}/${label}-pa.png`, fullPage: true });
    // cert: D282
    await setType("D282", "cert");
    check(`${label}: cert prep shows cert record`, (await page.textContent(".page")).includes("AWS Certified Cloud Practitioner"));
    await page.screenshot({ path: `${SHOTS}/${label}-cert.png`, fullPage: false });
    check(`${label}: no JS errors (assessment)`, errors.length === 0, errors.join(" | "));
    await page.close();
  },

  async dashboard(ctx, label) {
    const page = await ctx.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await login(page);
    await page.goto(`${BASE}/courses/D413`);
    if (await page.$("form[action='/sessions/start'] button")) {
      await Promise.all([page.waitForNavigation(), page.click("form[action='/sessions/start'] button")]);
    }
    await page.waitForTimeout(3000);
    await page.reload();
    const secs = (t) => t.split(":").map(Number).reduce((a, b) => a * 60 + b, 0);
    const shown = (await page.textContent(".timer-pill .js-elapsed")).trim();
    check(`${label}: study timer survives reload`, secs(shown) >= 3, `pill shows ${shown}`);
    await page.goto(`${BASE}/`);
    const text = await page.textContent(".page");
    check(`${label}: dashboard shows term, next up, SAP, program`, ["Term 1", "Next up", "SAP", "Program", "CU passed"].every((s) => text.includes(s)));
    await page.screenshot({ path: `${SHOTS}/${label}-dashboard.png`, fullPage: true });
    await page.goto(`${BASE}/sessions`);
    await Promise.all([page.waitForNavigation(), page.click("form[action='/sessions/stop'] button")]);
    check(`${label}: stop timer`, (await page.$(".timer-pill")) === null);
    check(`${label}: no JS errors (dashboard)`, errors.length === 0, errors.join(" | "));
    await page.close();
  },
};

(async () => {
  const browser = await chromium.connectOverCDP(process.env.SV_CDP || "http://localhost:9222");
  const wanted = process.argv.slice(2).length ? process.argv.slice(2) : Object.keys(checks);
  const viewports = { phone: { width: 390, height: 844 }, desktop: { width: 1280, height: 860 } };
  try {
    for (const [label, viewport] of Object.entries(viewports)) {
      const ctx = await browser.newContext({ viewport, deviceScaleFactor: 1, isMobile: label === "phone", hasTouch: label === "phone" });
      try {
        for (const name of wanted) {
          try { await checks[name](ctx, label); } catch (e) { check(`${label}: ${name}`, false, e.message.split("\n")[0]); }
        }
      } finally {
        await ctx.close();
      }
    }
  } finally {
    // Deliberately no browser.close(): on a CDP connection it can shut down the shared Chromium.
  }
  const failed = results.filter((r) => !r.ok).length;
  console.log(`\n${results.length - failed} passed, ${failed} failed. Screenshots in ${SHOTS}`);
  process.exit(failed ? 1 : 0);
})();
