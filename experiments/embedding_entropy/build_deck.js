// Build the "Six minutes of beauty reviews" talk deck from the figure PNGs.
// Each figure (1920x1080, dark) is placed full-bleed on its own 16:9 slide in
// the recommended talk order, with the per-slide talking points embedded as
// speaker notes. A matching dark closing slide carries the limits + next steps.
// Run: NODE_PATH=$(npm root -g) node experiments/embedding_entropy/build_deck.js

const pptxgen = require("pptxgenjs");
const path = require("path");

const FIG_DIR = "/Users/<user>/GitHub_main/fyp_main_v02/tmp/figs_presentation";
const OUT = path.join(FIG_DIR, "six_minutes_of_beauty_reviews.pptx");

const BG = "10131A";
const PANEL = "181C26";
const FG = "E8EAF0";
const MUTED = "8B93A7";
const ACCENT = "4FC3F7";
const ACCENT2 = "FFB74D";
const GOOD = "81C784";
const BAD = "E57373";

// Talk order with speaker notes (condensed from TALKING_POINTS.md: open-with,
// talk track, numbers, anticipated Q&A).
const SLIDES = [
  {
    img: "fig0_headline.png",
    notes:
`OPEN: "Everyone has heard the rabbit-hole story: the algorithm pulls you in, video by video, somewhere darker. We went looking for it in real people's actual viewing - and this is what we found."

TALK: 73 donated TikTok watch histories, 4.3M watched videos, to the second, with permission. We built a machine map of what 260k of those videos are ABOUT and searched every history for binges: runs of videos about essentially the same thing. The four numbers are the study in miniature: binges are real and most people have them (82%); they are a vanishing fraction of viewing (0.2% of watch time); they are short (6 minutes, 5 videos); and the number that "led someone deeper" - the actual rabbit-hole shape - is zero.

IF ASKED "only 73 people?": deep look at a modest volunteer group, not a survey; every finding tested within-person against their own behaviour; patterns, not population rates.
IF ASKED "what's a genuine binge?": >=4 DIFFERENT videos in a row, one sitting, same topic - occurring beyond what chance produces in that person's own shuffled viewing.`
  },
  {
    img: "fig1_rare_but_universal.png",
    notes:
`OPEN: "Here is every donor in the study, as a bar. The height is how much of their watch time was spent binging."

TALK: Four out of five donors have at least one genuine binge - normal behaviour, not a vulnerable few. But look at the scale: the typical binger spends about one fifth of one percent of watch time in binges - roughly one minute in every eight hours. Even the most binge-prone person is under five percent. People binge the way people occasionally eat a whole packet of biscuits: really, but rarely. It is not how they watch.

NUMBERS: 60/73 with >=1 episode; 82% at calibrated definition (59-92% across stricter/looser); median 0.2% of watch time; max ~4.6%.
IF ASKED "missing binges?": we only see binges among mapped videos (~25% of viewing). Where coverage is best we find ~2.5x more - so true amounts are higher, but still well under 1%. The shape of the story doesn't change.`
  },
  {
    img: "fig2_anatomy.png",
    notes:
`OPEN: "This is one real binge, from one real session. Every dot is a video."

TALK: Time runs left to right; zero is where the binge starts. Colours are topics - every colour a different subject; ordinary scrolling is a confetti of topics, a dozen in twenty minutes. Faint grey dots are videos our map doesn't cover. Then the amber band: nine videos in ten minutes, all beauty product reviews. Afterwards - back to confetti. That's the phenomenon: not a descent, not a spiral - a PAUSE ON ONE TOPIC embedded in ordinary varied viewing.

NUMBERS: this example 9 distinct beauty-review videos / 10 min; typical episode 5 videos / ~6 min.
IF ASKED "cherry-picked?": chosen to be TYPICAL - mid-size, one of 389. Any of them looks like this; the longest ran to a few dozen videos and was exceptional.`
  },
  {
    img: "fig5_not_random.png",
    notes:
`OPEN: "Before the interesting findings, the boring-but-crucial one: how do we know these aren't just luck?"

TALK: Anyone watching twenty thousand videos sometimes gets similar ones in a row by chance - like four heads somewhere in ten thousand coin flips. So: take each person's actual videos, shuffle the watching order 200 times, run the same detector on every shuffled history. Left: shuffled - essentially zero binges, ever. Right: real - one to seventy-five per person. Whatever produces binges is about WHEN videos were watched, not just what. 57 of 73 donors beat their own chance baseline (corrected for multiple testing).

IF ASKED "what does the shuffle preserve?": everything except order - same person, videos, sessions, volume. Only the timing breaks. That's what makes it the right comparison.`
  },
  {
    img: "fig3_what_gets_binged.png",
    notes:
`OPEN: "So what do people actually binge? If you believe the public narrative, you'd guess outrage. Here's what it really is."

TALK: Each bar: how much more often a topic shows up in binges than in the same people's ordinary viewing - already accounts for taste. The top reads like a newsagent's lifestyle shelf: beauty reviews 6x their ordinary share, sermons 6.5x, product reviews, recipes, personal care, crafts, travel, financial advice. What they share is FORMAT: instructional, serial, review-like - one video naturally invites the next. The red bar is the punchline: POLITICS is the one tested topic NOT binged beyond its ordinary share. Our donors watch politics; they don't binge it. A second independent test agrees: binges are slightly LESS political than the same person's everyday diet.

NUMBERS: beauty 6.2x; sermons 6.5x; recipes 5.2x; politics 1.3x n.s. (p=0.07); 14 of 15 topics significant.
IF ASKED "political binges hidden in unmapped videos?": in principle possible - honest limit. But every signal we CAN see points the same way; nothing hints otherwise.`
  },
  {
    img: "fig4_creator_loyalty.png",
    notes:
`OPEN: "Now the question behind the whole debate: who's driving - the algorithm or the person?"

TALK: For every binge: how many different creators made its videos? In 48% of binges, at least half the videos come from a SINGLE creator. Random same-sized sets from the same people's viewing: 0.1%. A one-creator run is what it looks like when a person finds a creator they like and watches through their work - the digital equivalent of discovering a novelist and reading three chapters. Creator loyalty: a deliberate human pattern, not an algorithm spiralling through a topic.

NUMBERS: 48.3% vs 0.1% expected; the similarity map was built WITHOUT creator information, so this wasn't baked in.
IF ASKED "doesn't the algorithm feed you creators you like?": plausibly helps - viewing data can't separate "offered and accepted" from "sought out", and we say so. But the pattern is anchored on a person the viewer chose, is habitual (next slide), and is preceded by the viewer's own actions. Every arrow points to the viewer's hand on the wheel.`
  },
  {
    img: "fig7_habit_and_exit.png",
    notes:
`OPEN: "Two findings on one slide - together they reframe what a binge IS."

TALK: Left - does the same person binge the same thing again? Yes: 14% of a person's binge pairs share their topic vs 2% at random - six times over. Median gap between same-topic binges: ten days. Nearly half of repeat bingers have one personal topic covering most of their binges. The binge is a STANDING APPOINTMENT: every week or two, back to the beauty reviews, back to the sermons. Right - the time-trap question. During a binge people genuinely lock in: ~6 seconds longer per video. But AFTER a binge the session winds down sooner: 10 minutes of viewing left vs 18 at the same point in the same person's binge-free sessions. A binge behaves like a FINALE - watch your fill, put the phone down. The opposite of a trap.

NUMBERS: 13.8% vs 2.2% (6.3x, p=.001); ~10-day return; dwell 33s vs 27s during; 10 vs 18 min remaining after (p=.001), matched within person.
IF ASKED "maybe binges just happen at end of evening?": possibly - we can't fully separate cause from timing. Either way the trap story predicts the opposite of what we see.`
  },
  {
    img: "fig8_prediction.png",
    notes:
`OPEN: "Last test: can you see a binge coming? And if so, what does the warning look like?"

TALK: Simple models flag "a binge starts now" from only the preceding minutes - strictly past-predicts-future, per person. The flags beat random guessing seventeen-fold. The interesting part is WHAT predicts: the stream already drifting closer together; the viewer slowing down, lingering; several videos in a row from the same creator; and - most telling - the viewer just liked, followed or searched for something. The binge doesn't ambush people. It ANNOUNCES ITSELF AS A CHOICE BEING MADE. Honesty note (on the slide): binge-starts are so rare that even a 17x model is wrong 99 times in 100 - evidence about mechanism, not a tool to build alerts on.

NUMBERS: lift 16.7x (top 1% of alerts); behaviour-only 9.8x; onset base rate 0.05% of videos.
IF ASKED "could platforms use this to push binges?": the signal is weak and its strongest parts are the user's own deliberate actions, which platforms already observe directly.`
  },
  {
    img: "fig6_no_rabbit_hole.png",
    notes:
`OPEN: "And finally - the rabbit hole itself. We went looking for it directly. Here is every binge we found, plotted by its shape."

TALK: The defining feature of a rabbit hole isn't similarity - it's DESCENT: each video like the last, but the chain drifting steadily away from where it began. We measure exactly that: how far each binge travelled (left-right) and how directed its path was (bottom-top). A genuine descent sits in the red zone: travelled far, in a straight line. 389 binges. Ten definitions. THAT ZONE IS EMPTY. People dwell on a topic and stay until they leave. They are not led anywhere.

CLOSE: "Binges are real, nearly universal, brief, benign, habitual, half creator-loyalty, finale-shaped, self-announced. The binge that actually occurs is six minutes of beauty reviews from a creator you follow - at the end of the evening's scroll. That's the rabbit hole, in 73 real histories. (pause) Concern about recommender systems is legitimate - but it should aim at what evidence shows, and at timescales we haven't yet studied, not at an image these data simply do not contain."

IF ASKED "platforms off the hook?": No - three limits: minutes-to-hours not months (slow drift = next study); 73 volunteers, not the populations of most concern; watched, not offered.`
  },
];




async function main() {
  const pres = new pptxgen();
  pres.layout = "LAYOUT_16x9";
  pres.author = "FYP Research Platform";
  pres.title = "Six minutes of beauty reviews - what TikTok rabbit holes actually look like";

  for (const s of SLIDES) {
    const slide = pres.addSlide();
    slide.background = { color: BG };
    slide.addImage({ path: path.join(FIG_DIR, s.img), x: 0, y: 0, w: 10, h: 5.625 });
    slide.addNotes(s.notes);
  }

  // Closing slide: limits + where next, styled like the fig0 stat cards.
  const close = pres.addSlide();
  close.background = { color: BG };
  close.addText("What this study cannot say", {
    x: 0.55, y: 0.45, w: 9, h: 0.7, fontSize: 30, bold: true, color: FG,
    fontFace: "Trebuchet MS", margin: 0,
  });
  const cards = [
    { head: "Minutes, not months", body: "We detect narrowing within sittings. A slow drift of someone's diet over months is a different study - and our next one.", color: ACCENT },
    { head: "Volunteers, not the world", body: "73 donors, mostly Australian, ~6 months of history each. Patterns, not population rates - and not the groups of most concern.", color: ACCENT2 },
    { head: "Watched, not offered", body: "A viewing history cannot separate “the algorithm offered it” from “the user sought it out”. No causal claim about the algorithm is made.", color: BAD },
  ];
  cards.forEach((c, i) => {
    const x = 0.55 + i * 3.05;
    close.addShape(pres.shapes.ROUNDED_RECTANGLE, {
      x, y: 1.45, w: 2.85, h: 2.5, fill: { color: PANEL },
      line: { color: c.color, width: 1.5 }, rectRadius: 0.08,
    });
    close.addText(c.head, {
      x: x + 0.2, y: 1.65, w: 2.45, h: 0.55, fontSize: 16, bold: true,
      color: c.color, fontFace: "Trebuchet MS", margin: 0,
    });
    close.addText(c.body, {
      x: x + 0.2, y: 2.25, w: 2.45, h: 1.55, fontSize: 11.5, color: FG,
      fontFace: "Calibri", margin: 0, valign: "top",
    });
  });
  close.addText([
    { text: "Where next:  ", options: { bold: true, color: GOOD } },
    { text: "map the unmapped 75% of viewing · test the slow (cross-month) drift · run the same anatomy on other platforms",
      options: { color: FG } },
  ], { x: 0.55, y: 4.35, w: 9, h: 0.5, fontSize: 14, fontFace: "Calibri", margin: 0 });
  close.addText("All code, data definitions and the full technical results: FYP research platform · PAPER_DRAFT.md · FINDINGS_v1.md", {
    x: 0.55, y: 5.05, w: 9, h: 0.35, fontSize: 10, italic: true, color: MUTED,
    fontFace: "Calibri", margin: 0,
  });
  close.addNotes(
`CLOSING SLIDE - the three honest limits, said plainly:
1. Minutes not months: this design detects within-sitting narrowing; slow cross-month drift is a different phenomenon and the natural next study.
2. Volunteers: deep data on a modest, willing, mostly-Australian group; claims are about patterns, not rates in the population - and not about the groups of most concern.
3. Watched not offered: we cannot separate algorithmic offering from user seeking; our creator-loyalty and self-initiation findings make the deliberate reading more plausible, but no causal claim about the algorithm is possible from viewing data alone.

END LINE: "The honest summary: we went looking for the rabbit hole where it is said to live - inside real people's feeds, at the timescale of a scroll - and what we found instead was six minutes of beauty reviews. The work now is to look where we haven't: longer timescales, more platforms, the unmapped majority of videos."`);

  await pres.writeFile({ fileName: OUT });
  console.log("wrote", OUT);
}

main().catch((e) => { console.error(e); process.exit(1); });
