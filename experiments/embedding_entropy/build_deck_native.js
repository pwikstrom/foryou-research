// Build the talk deck as NATIVE PowerPoint slides (editable text, shapes and
// charts) instead of baked PNG images. Reads the numbers exported by
// export_deck_data.py and mirrors the figures' layout/palette; speaker notes
// and slide order are shared with build_deck.js.
// Run: NODE_PATH=$(npm root -g) node experiments/embedding_entropy/build_deck_native.js

const pptxgen = require("pptxgenjs");
const fs = require("fs");
const path = require("path");
const { SLIDES, COLORS } = require("./build_deck.js");

const FIG_DIR = "/Users/<user>/GitHub_main/fyp_main_v02/tmp/figs_presentation";
const OUT = path.join(FIG_DIR, "six_minutes_of_beauty_reviews_native.pptx");
const D = JSON.parse(fs.readFileSync(path.join(FIG_DIR, "deck_data.json"), "utf8"));

const { BG, PANEL, FG, MUTED, ACCENT, ACCENT2, GOOD, BAD } = COLORS;
const VIOLET = "B39DDB";
const NICHE_PALETTE = ["64B5F6", "81C784", "E57373", "B39DDB", "4DB6AC", "F06292",
                       "A1887F", "90CAF9", "AED581", "FFD54F", "9575CD", "4DD0E1"];
const HEAD_FONT = "Trebuchet MS";
const BODY_FONT = "Calibri";

// Slide canvas: 10 x 5.625 in. Shared plot region for chart-like slides
// (height keeps category labels clear of the footer line at y=5.22).
const PLOT = { x: 0.7, y: 1.45, w: 8.7, h: 3.05 };
const GRID = "262B38";

let pres;

function header(slide, title, subtitle, footer) {
  slide.background = { color: BG };
  slide.addText(title, { x: 0.5, y: 0.22, w: 9.0, h: 0.55, fontSize: 24, bold: true,
    color: FG, fontFace: HEAD_FONT, margin: 0 });
  slide.addText(subtitle, { x: 0.5, y: 0.78, w: 9.0, h: 0.4, fontSize: 12.5,
    color: MUTED, fontFace: BODY_FONT, margin: 0 });
  slide.addText(footer, { x: 0.5, y: 5.22, w: 9.2, h: 0.34, fontSize: 9, italic: true,
    color: MUTED, fontFace: BODY_FONT, margin: 0 });
}

function vbar(slide, x, w, hFrac, color, valueLabel, catLabel, labelColor) {
  const top = PLOT.y + PLOT.h * (1 - hFrac);
  slide.addShape(pres.shapes.RECTANGLE, {
    x, y: top, w, h: PLOT.h * hFrac, fill: { color }, line: { type: "none" } });
  slide.addText(valueLabel, { x: x - 0.5, y: top - 0.42, w: w + 1.0, h: 0.38,
    fontSize: 19, bold: true, color: labelColor || FG, align: "center",
    fontFace: HEAD_FONT, margin: 0 });
  slide.addText(catLabel, { x: x - 0.65, y: PLOT.y + PLOT.h + 0.08, w: w + 1.3, h: 0.45,
    fontSize: 11.5, color: MUTED, align: "center", fontFace: BODY_FONT, margin: 0 });
}




// --- Slide builders, in talk order (aligned with SLIDES notes) ---

function slideHeadline(slide) {
  slide.background = { color: BG };
  slide.addText("Do TikTok 'rabbit holes' exist?", { x: 0.5, y: 0.3, w: 9, h: 0.65,
    fontSize: 30, bold: true, color: FG, fontFace: HEAD_FONT, margin: 0 });
  slide.addText("73 donated watch histories · 4.3M videos watched · 260k videos mapped in semantic space",
    { x: 0.5, y: 0.98, w: 9, h: 0.35, fontSize: 13, color: MUTED, fontFace: BODY_FONT, margin: 0 });
  const cards = [
    ["82%", "of people have at least\none genuine binge", ACCENT],
    ["0.2%", "of watch time is all\nbinges add up to", ACCENT2],
    ["6 min", "the typical binge:\n5 videos, then it's over", GOOD],
    ["0", "binges that 'led users\ndeeper' down a path", BAD],
  ];
  cards.forEach(([big, small, colour], i) => {
    const x = 0.5 + i * 2.34;
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.6, w: 2.14, h: 2.4,
      fill: { color: PANEL }, line: { color: colour, width: 1.75 }, rectRadius: 0.07 });
    slide.addText(big, { x, y: 1.92, w: 2.14, h: 0.8, fontSize: 36, bold: true,
      color: colour, align: "center", fontFace: HEAD_FONT, margin: 0 });
    slide.addText(small, { x: x + 0.12, y: 2.82, w: 1.9, h: 1.0, fontSize: 12,
      color: FG, align: "center", fontFace: BODY_FONT, margin: 0 });
  });
  slide.addText("Binges are real, habitual — and brief, benign and commercial.",
    { x: 0.5, y: 4.5, w: 9, h: 0.35, fontSize: 15, color: FG, fontFace: BODY_FONT, margin: 0 });
  slide.addText("The dramatic rabbit-hole narrative does not appear in these data.",
    { x: 0.5, y: 4.88, w: 9, h: 0.35, fontSize: 15, color: FG, fontFace: BODY_FONT, margin: 0 });
}




function slideRarity(slide) {
  header(slide, "Nearly everyone binges — almost no one binges much",
    "Share of each person's total watch time spent in binge episodes (73 donors, sorted)",
    "Episode = ≥4 distinct, semantically similar videos in a row within one session " +
    "(cosine distance < 0.5 on video embeddings).");
  slide.addChart(pres.charts.BAR, [{
    name: "% of watch time in binges",
    labels: D.exposure_pct.map(() => ""),
    values: D.exposure_pct.map((v) => Math.max(v, 0.004)),
  }], {
    x: PLOT.x, y: PLOT.y, w: PLOT.w, h: PLOT.h, barDir: "col",
    chartColors: [ACCENT], chartArea: { fill: { color: BG } },
    plotArea: { fill: { color: BG } },
    catAxisHidden: true, valAxisLabelColor: MUTED, valAxisLabelFontSize: 10,
    valAxisLabelFormatCode: '0"%"',
    valGridLine: { color: GRID, size: 0.5 }, catGridLine: { style: "none" },
    showLegend: false, showTitle: false, barGapWidthPct: 25,
    valAxisMaxVal: 5, valAxisMinVal: 0,
  });
  slide.addText("half the donors sit at or below 0.2% — the tall bars are the exceptions",
    { x: 1.4, y: 1.7, w: 7.2, h: 0.3, fontSize: 13, color: ACCENT2,
      fontFace: BODY_FONT, margin: 0 });
  slide.addText("each bar = one person  ·  the axis runs only to 5% — every bar is near zero",
    { x: PLOT.x, y: PLOT.y + PLOT.h + 0.12, w: 8.5, h: 0.3, fontSize: 11.5,
      color: MUTED, fontFace: BODY_FONT, margin: 0 });
}




function slideAnatomy(slide) {
  const a = D.anatomy;
  header(slide, "What a binge actually looks like",
    "One real session: each dot is a video, coloured by topic; the highlighted band is the binge",
    `One donor, one evening. Binge: ${a.n_distinct} distinct ${a.niche} videos in ` +
    `${Math.round(a.duration_min)} minutes; ordinary varied scrolling before and after.`);
  const x0 = a.x_min - 1.5, x1 = a.x_max + 1.5;
  const sx = (v) => PLOT.x + ((v - x0) / (x1 - x0)) * PLOT.w;
  // Dots live below a clear top strip inside the band so the band label never
  // collides with data.
  const dotTop = PLOT.y + 0.5, dotH = PLOT.h - 0.85;
  const sy = (v) => dotTop + (1 - v) * dotH;
  const bandX = sx(0), bandW = sx(a.ep_len_min) - sx(0);
  const axisY = dotTop + dotH + 0.18;
  // Amber binge band behind the dots, ending at the axis line.
  slide.addShape(pres.shapes.RECTANGLE, {
    x: bandX, y: PLOT.y, w: bandW, h: axisY - PLOT.y,
    fill: { color: ACCENT2, transparency: 80 }, line: { type: "none" } });
  const colourOf = (g) => g === "binge" ? ACCENT2
    : g === "unmapped" ? "4A5160"
    : NICHE_PALETTE[parseInt(g.slice(5), 10) % NICHE_PALETTE.length];
  a.x.forEach((xv, i) => {
    const g = a.group[i];
    const r = g === "binge" ? 0.085 : g === "unmapped" ? 0.055 : 0.07;
    slide.addShape(pres.shapes.OVAL, {
      x: sx(xv) - r, y: sy(a.y[i]) - r, w: 2 * r, h: 2 * r,
      fill: { color: colourOf(g), transparency: g === "unmapped" ? 30 : 0 },
      line: { type: "none" } });
  });
  // Band label centred INSIDE the band's clear top strip (two lines so it
  // never overhangs the band edges).
  slide.addText(`${a.n_distinct} similar videos\n${Math.round(a.duration_min)} minutes`,
    { x: bandX, y: PLOT.y + 0.05, w: bandW, h: 0.5,
      fontSize: 11.5, bold: true, color: ACCENT2, align: "center", fontFace: HEAD_FONT, margin: 0 });
  // Time axis with a zero tick at the binge start.
  slide.addShape(pres.shapes.LINE, { x: PLOT.x, y: axisY, w: PLOT.w, h: 0,
    line: { color: MUTED, width: 1 } });
  slide.addShape(pres.shapes.LINE, { x: bandX, y: axisY, w: 0, h: 0.07,
    line: { color: MUTED, width: 1 } });
  slide.addText("0", { x: bandX - 0.1, y: axisY + 0.06, w: 0.2, h: 0.22, fontSize: 10,
    color: MUTED, align: "center", fontFace: BODY_FONT, margin: 0 });
  slide.addText("every other colour = a different topic · grey = unmapped videos",
    { x: PLOT.x, y: axisY + 0.28, w: 6.6, h: 0.3, fontSize: 11,
      color: MUTED, fontFace: BODY_FONT, margin: 0 });
  slide.addText("minutes (0 = binge starts) →", { x: 7.0, y: axisY + 0.28,
    w: 2.4, h: 0.3, fontSize: 11, color: MUTED, align: "right", fontFace: BODY_FONT, margin: 0 });
}




function slideNotRandom(slide) {
  const ns = D.null_scatter;
  header(slide, "Binges are real patterns, not coincidence",
    "Episodes found in each person's real history vs the same videos in shuffled order",
    "Each person's viewing re-ordered at random 200 times and re-scanned. " +
    `${ns.n_fdr} of ${ns.n_donors} real histories beat their own chance baseline (FDR-corrected).`);
  // log10 scale from 0.04 to 120, with gridlines + tick labels for reference.
  const lo = Math.log10(0.04), hi = Math.log10(120);
  const sy = (v) => PLOT.y + (1 - (Math.log10(Math.max(v, 0.04) + 0.05) - lo) / (hi - lo)) * PLOT.h;
  [["0", 0.0], ["1", 1], ["10", 10], ["75", 75]].forEach(([lbl, v]) => {
    const y = sy(v);
    slide.addShape(pres.shapes.LINE, { x: PLOT.x + 0.45, y, w: PLOT.w - 0.45, h: 0,
      line: { color: GRID, width: 0.75 } });
    slide.addText(lbl, { x: PLOT.x - 0.1, y: y - 0.12, w: 0.45, h: 0.24, fontSize: 10,
      color: MUTED, align: "right", fontFace: BODY_FONT, margin: 0 });
  });
  // Axis title sits in the left gutter between the 75 and 10 tick rows.
  slide.addText("episodes\nper person", { x: PLOT.x - 0.2, y: 1.85, w: 1.0,
    h: 0.45, fontSize: 9.5, color: MUTED, fontFace: BODY_FONT, margin: 0 });
  let seed = 7;
  const rand = () => { seed = (seed * 9301 + 49297) % 233280; return seed / 233280; };
  const nullX = 3.3, obsX = 6.6;
  ns.null_mean.forEach((v) => {
    const cx = nullX + (rand() - 0.5) * 0.9;
    slide.addShape(pres.shapes.OVAL, { x: cx, y: sy(v) - 0.05, w: 0.1, h: 0.1,
      fill: { color: MUTED, transparency: 25 }, line: { type: "none" } });
  });
  ns.obs.forEach((v) => {
    const cx = obsX + (rand() - 0.5) * 0.9;
    slide.addShape(pres.shapes.OVAL, { x: cx, y: sy(v) - 0.05, w: 0.1, h: 0.1,
      fill: { color: ACCENT, transparency: 15 }, line: { type: "none" } });
  });
  slide.addText("same videos, shuffled order", { x: nullX - 0.95, y: PLOT.y + PLOT.h + 0.12,
    w: 2.6, h: 0.32, fontSize: 13, color: MUTED, align: "center", fontFace: BODY_FONT, margin: 0 });
  slide.addText("real viewing order", { x: obsX - 0.95, y: PLOT.y + PLOT.h + 0.12, w: 2.6,
    h: 0.32, fontSize: 13, color: ACCENT, align: "center", fontFace: BODY_FONT, margin: 0 });
  // Cluster labels in clear space: left of each cluster, vertically centred on it.
  slide.addText("essentially\nzero →", { x: 1.15, y: sy(0.07) - 0.4, w: 1.5, h: 0.55,
    fontSize: 12, color: FG, align: "right", fontFace: BODY_FONT, margin: 0 });
  // Label sits in the clear band between the 10 and 1 gridlines.
  slide.addText("← 1 to 75\nepisodes each", { x: obsX + 0.7, y: (sy(10) + sy(1)) / 2 - 0.28,
    w: 1.9, h: 0.55, fontSize: 12, color: ACCENT, fontFace: BODY_FONT, margin: 0 });
}




function slideNiches(slide) {
  header(slide, "Reviews, recipes & how-tos get binged — politics doesn't",
    "How much more often a topic appears in binges than in the same people's overall viewing",
    "Bars = observed ÷ expected from 500 matched random draws of each donor's own diet. " +
    "All blue bars significant (FDR); politics is the one tested topic that is not.");
  const rows = [...D.niches].sort((a, b) => b.ratio - a.ratio);
  const maxR = Math.max(...rows.map((r) => r.ratio));
  const x0 = 3.1, maxW = 4.6, rowH = 0.225, gap = 0.01, top = 1.3;
  const rowsEnd = top + rows.length * (rowH + gap);
  // Dashed 1x reference line FIRST, so the bars are drawn over it and the line
  // shows only in the gaps (not as a stripe through the fills).
  const oneX = x0 + (1 / maxR) * maxW;
  slide.addShape(pres.shapes.LINE, { x: oneX, y: top - 0.04, w: 0, h: rowsEnd - top + 0.06,
    line: { color: MUTED, width: 1, dashType: "dash" } });
  rows.forEach((r, i) => {
    const y = top + i * (rowH + gap);
    const colour = r.fdr_significant ? ACCENT : BAD;
    slide.addText(r.niche, { x: 0.3, y, w: 2.68, h: rowH, fontSize: 10, color: FG,
      align: "right", valign: "middle", fontFace: BODY_FONT, margin: 0 });
    slide.addShape(pres.shapes.RECTANGLE, { x: x0, y: y + 0.02, w: (r.ratio / maxR) * maxW,
      h: rowH - 0.04, fill: { color: colour }, line: { type: "none" } });
    const lbl = `${r.ratio.toFixed(1)}×` + (r.fdr_significant ? "" : "  — not significant");
    slide.addText(lbl, { x: x0 + (r.ratio / maxR) * maxW + 0.08, y, w: 2.2, h: rowH,
      fontSize: 10.5, color: r.fdr_significant ? FG : BAD, valign: "middle",
      fontFace: BODY_FONT, margin: 0, bold: !r.fdr_significant });
  });
  slide.addText("dashed line: 1× = no more than expected", { x: oneX + 0.06, y: rowsEnd + 0.05,
    w: 3.2, h: 0.24, fontSize: 9.5, color: MUTED, fontFace: BODY_FONT, margin: 0 });
}




function slideCreator(slide) {
  const a = D.authors;
  header(slide, "Half of all binges are mostly one creator",
    "Share of binges where a single creator accounts for at least half the videos",
    "Chance = matched random draws from each donor's own viewing. Suggests creator " +
    "loyalty (following someone you like), not algorithmic topic spirals.");
  const exp = a.null_pct_author_dominated * 100, obs = a.obs_pct_author_dominated * 100;
  // Shared baseline rule anchors both bars; the chance bar is visually ~zero,
  // so it gets an explicit annotation instead of relying on a 2 px sliver.
  slide.addShape(pres.shapes.LINE, { x: 1.6, y: PLOT.y + PLOT.h, w: 6.8, h: 0,
    line: { color: MUTED, width: 1 } });
  vbar(slide, 2.6, 1.6, Math.max(exp / 55, 0.012), MUTED, `${exp.toFixed(1)}%`,
    "expected by chance", FG);
  slide.addText("(essentially never)", { x: 2.0, y: PLOT.y + PLOT.h - 0.75, w: 2.8, h: 0.26,
    fontSize: 11, italic: true, color: MUTED, align: "center", fontFace: BODY_FONT, margin: 0 });
  vbar(slide, 5.8, 1.6, obs / 55, ACCENT2, `${obs.toFixed(1)}%`,
    "observed in real binges", ACCENT2);
  slide.addText("≈ 480× more often\nthan chance", { x: 7.7, y: PLOT.y + PLOT.h - 1.7,
    w: 1.7, h: 0.55, fontSize: 11.5, color: ACCENT2, fontFace: BODY_FONT, margin: 0 });
}




function slideHabitExit(slide) {
  header(slide, "Binges are habits — and finales, not traps",
    "People return to their binge topic for weeks; after a binge, the session winds down sooner",
    `Left: same-topic binge pairs vs chance (median return ~${Math.round(D.rq9.median_return_gap_days)} days). ` +
    "Right: session time left after a binge vs matched binge-free sessions, paired within person.");
  const panel = (cx, title, colour, pair, unit, maxV) => {
    slide.addText(title, { x: cx - 2.0, y: 1.28, w: 4.0, h: 0.55, fontSize: 13, bold: true,
      color: colour, align: "center", fontFace: BODY_FONT, margin: 0 });
    pair.forEach(([label, v, c], i) => {
      const x = cx - 1.7 + i * 1.9;
      const hF = Math.max(v / maxV, 0.012);
      const top = 4.55 - 2.45 * hF;
      slide.addShape(pres.shapes.RECTANGLE, { x, y: top, w: 1.4, h: 2.45 * hF,
        fill: { color: c }, line: { type: "none" } });
      slide.addText(`${v}${unit}`, { x: x - 0.3, y: top - 0.36, w: 2.0, h: 0.32,
        fontSize: 16, bold: true, color: FG, align: "center", fontFace: HEAD_FONT, margin: 0 });
      slide.addText(label, { x: x - 0.35, y: 4.62, w: 2.1, h: 0.5, fontSize: 10.5,
        color: MUTED, align: "center", fontFace: BODY_FONT, margin: 0 });
    });
  };
  panel(2.6, "the same person re-binges\nthe same topic (6× chance)", VIOLET,
    [["chance", +(D.rq9.null_same_niche_pair_share * 100).toFixed(1), MUTED],
     ["observed", +(D.rq9.obs_same_niche_pair_share * 100).toFixed(1), VIOLET]], "%", 16);
  panel(7.4, "sessions end sooner after a binge\n(absorbing, then done)", GOOD,
    [["no binge (same point)", Math.round(D.rq10.median_remaining_control_min), MUTED],
     ["right after a binge", Math.round(D.rq10.median_remaining_after_episode_min), GOOD]],
    " min", 20);
}




function slidePrediction(slide) {
  header(slide, "A binge announces itself — as a choice being made",
    "Flagging 'a binge starts now' beats guessing 17× — and the strongest signals are " +
    "the viewer's own likes, follows and searches",
    `Logistic models, per-person time-ordered split; onsets are rare ` +
    `(${(D.p3.full.base_rate * 100).toFixed(2)}% of plays), so this is evidence about ` +
    "mechanism, not a practical alarm. Lift 1× = guessing.");
  const bars = [
    ["the stream is already\nnarrowing (momentum)", D.p3.momentum_only.lift, ACCENT],
    ["behaviour: dwell, time,\nlikes & searches", D.p3.precursor_only.lift, ACCENT2],
    ["everything\ncombined", D.p3.full.lift, GOOD],
  ];
  const maxV = 18;
  // Reference line first (bars drawn over it), label in the clear left gutter.
  const oneY = PLOT.y + PLOT.h * (1 - 1 / maxV);
  slide.addShape(pres.shapes.LINE, { x: PLOT.x, y: oneY, w: PLOT.w, h: 0,
    line: { color: MUTED, width: 1.25, dashType: "dash" } });
  slide.addText("1× =\nguessing", { x: 0.62, y: oneY - 0.62, w: 0.85, h: 0.55, fontSize: 10.5,
    color: MUTED, fontFace: BODY_FONT, margin: 0 });
  bars.forEach(([label, v, c], i) => {
    vbar(slide, 1.9 + i * 2.8, 1.5, v / maxV, c, `${v.toFixed(1)}×`, label, FG);
  });
}




function slideNoRabbitHole(slide) {
  header(slide, "No evidence of the 'rabbit-hole descent'",
    "Every binge, plotted by how far it travelled and how directed its path was",
    "A 'descent' would travel far in a consistent direction (upper-right region). " +
    "Across all 389 binges — and all 10 alternative definitions tested — that region is empty.");
  const sx = (v) => PLOT.x + (v / 1.1) * PLOT.w;
  const sy = (v) => PLOT.y + (1 - v / 1.0) * PLOT.h;
  // The empty "descent" zone.
  slide.addShape(pres.shapes.RECTANGLE, { x: sx(0.5), y: sy(1.0), w: sx(1.1) - sx(0.5),
    h: sy(0.5) - sy(1.0), fill: { color: BAD, transparency: 86 },
    line: { color: BAD, width: 1.25, dashType: "dash" } });
  D.geometry.diameter.forEach((dv, i) => {
    slide.addShape(pres.shapes.OVAL, {
      x: sx(dv) - 0.035, y: sy(D.geometry.straightness[i]) - 0.035, w: 0.07, h: 0.07,
      fill: { color: ACCENT, transparency: 40 }, line: { type: "none" } });
  });
  slide.addText('the "led ever deeper" zone:\n0 binges land here', {
    x: sx(0.62), y: sy(0.88), w: sx(1.08) - sx(0.62), h: 0.7, fontSize: 14, bold: true,
    color: BAD, align: "center", fontFace: HEAD_FONT, margin: 0 });
  // Caption sits in the empty upper-left, clear of the dot cloud.
  slide.addText("binges sit in one topic\nand stay there", { x: sx(0.03), y: sy(0.78),
    w: 2.6, h: 0.55, fontSize: 11.5, color: ACCENT, fontFace: BODY_FONT, margin: 0 });
  // Light L-shaped axes frame the geometry claim.
  slide.addShape(pres.shapes.LINE, { x: PLOT.x, y: PLOT.y, w: 0, h: PLOT.h,
    line: { color: "3A4150", width: 1 } });
  slide.addShape(pres.shapes.LINE, { x: PLOT.x, y: PLOT.y + PLOT.h, w: PLOT.w, h: 0,
    line: { color: "3A4150", width: 1 } });
  slide.addText("how far the binge travelled in topic space →", { x: PLOT.x,
    y: PLOT.y + PLOT.h + 0.1, w: 5.5, h: 0.3, fontSize: 11, color: MUTED,
    fontFace: BODY_FONT, margin: 0 });
  slide.addText("↑ how directed the path was", { x: PLOT.x, y: PLOT.y - 0.32, w: 3.2, h: 0.28,
    fontSize: 11, color: MUTED, fontFace: BODY_FONT, margin: 0 });
}




function slideClosing(slide) {
  slide.background = { color: BG };
  slide.addText("What this study cannot say", { x: 0.55, y: 0.45, w: 9, h: 0.7,
    fontSize: 30, bold: true, color: FG, fontFace: HEAD_FONT, margin: 0 });
  const cards = [
    ["Minutes, not months", "We detect narrowing within sittings. A slow drift of someone's diet over months is a different study — and our next one.", ACCENT],
    ["Volunteers, not the world", "73 donors, mostly Australian, ~6 months of history each. Patterns, not population rates — and not the groups of most concern.", ACCENT2],
    ["Watched, not offered", "A viewing history cannot separate “the algorithm offered it” from “the user sought it out”. No causal claim about the algorithm is made.", BAD],
  ];
  cards.forEach(([head, body, colour], i) => {
    const x = 0.55 + i * 3.05;
    slide.addShape(pres.shapes.ROUNDED_RECTANGLE, { x, y: 1.45, w: 2.85, h: 2.5,
      fill: { color: PANEL }, line: { color: colour, width: 1.5 }, rectRadius: 0.08 });
    slide.addText(head, { x: x + 0.2, y: 1.63, w: 2.45, h: 0.32, fontSize: 13.5, bold: true,
      color: colour, fontFace: HEAD_FONT, margin: 0 });
    slide.addText(body, { x: x + 0.2, y: 2.05, w: 2.45, h: 1.75, fontSize: 11.5, color: FG,
      fontFace: BODY_FONT, margin: 0, valign: "top" });
  });
  slide.addText([
    { text: "Where next: ", options: { bold: true, color: GOOD } },
    { text: "map the unmapped 75% of viewing · test the slow cross-month drift · same anatomy on other platforms",
      options: { color: FG } },
  ], { x: 0.55, y: 4.35, w: 9.1, h: 0.4, fontSize: 12.5, fontFace: BODY_FONT, margin: 0 });
  slide.addText("Full methods, data definitions and technical results: FYP research platform",
    { x: 0.55, y: 4.95, w: 9, h: 0.3, fontSize: 10, italic: true, color: MUTED,
      fontFace: BODY_FONT, margin: 0 });
}




async function main() {
  pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = "FYP Research Platform";
  pres.title = "Six minutes of beauty reviews - what TikTok rabbit holes actually look like (native)";

  // Builders aligned with the SLIDES notes order: fig0,1,2,5,3,4,7,8,6.
  const builders = [slideHeadline, slideRarity, slideAnatomy, slideNotRandom,
                    slideNiches, slideCreator, slideHabitExit, slidePrediction,
                    slideNoRabbitHole];
  builders.forEach((build, i) => {
    const slide = pres.addSlide();
    build(slide);
    slide.addNotes(SLIDES[i].notes);
  });
  const close = pres.addSlide();
  slideClosing(close);
  close.addNotes(
`CLOSING SLIDE - the three honest limits, said plainly (see TALKING_POINTS.md for the full script).
END LINE: "We went looking for the rabbit hole where it is said to live - inside real people's feeds, at the timescale of a scroll - and what we found instead was six minutes of beauty reviews."`);

  await pres.writeFile({ fileName: OUT });
  console.log("wrote", OUT);
}

main().catch((e) => { console.error(e); process.exit(1); });
