# Six minutes of beauty reviews: what TikTok "rabbit holes" actually look like in 73 donated watch histories

*Working paper draft. Plain-language companion to the technical findings
(`FINDINGS_v1.md`) and the study design (`RESEARCH_DESIGN.md`). All numbers in
this document are taken from those analyses.*

---

## Abstract

The "rabbit hole" is one of the most influential ideas in the public debate
about social media: the worry that recommendation algorithms pull users into
ever-narrower tunnels of similar content, with political radicalisation as the
feared destination. The image is vivid, but direct evidence from real viewing
behaviour has been scarce. We examined 73 donated TikTok watch histories —
4.3 million watched videos — and searched them for *binges*: stretches of
viewing where one video after another is about essentially the same thing. We
measured "the same thing" using a machine-built map of video meaning, not
hand-picked topic labels, and we tested every finding against what random
chance would produce in the same person's viewing.

Binges turn out to be real, and most people have them: four out of five donors
showed at least one stretch of focused viewing that could not be explained by
chance. But everything else about them contradicts the rabbit-hole image. They
are brief — typically five videos and about six minutes. They are rare — for
the typical binger they add up to about *one fifth of one percent* of total
watch time. They are benign — the topics people binge are beauty reviews,
recipes, craft tutorials and travel clips, while political content is the one
tested topic that is *not* binged beyond its ordinary share of viewing. Half
of all binges are mostly videos from a single creator, suggesting that
following a person you like — not an algorithmic topic spiral — drives much of
the phenomenon. Binges recur like appointments: the same person returns to the
same topic roughly every ten days. And far from trapping viewers, binges
behave like finales: people watch more intently during them, then end their
session sooner afterwards. In two years of donated viewing we found not a
single episode with the signature the rabbit-hole narrative predicts — a
directed path leading step by step away from where it began.

---

## 1. Introduction: a powerful image, thin evidence

When people worry about TikTok, they often reach for the same image. A viewer
watches one video, the algorithm serves another like it, and step by step the
feed narrows until the viewer is somewhere they never chose to go — deeper,
darker, more extreme. The image has a name, the rabbit hole, and it shapes
news coverage, parliamentary hearings and platform regulation alike.

For all its influence, the image makes specific, testable claims that have
rarely been tested against what people actually watch. It claims that
sequences of highly similar content occur (otherwise there is no hole). It
claims they are directed — that they *lead* somewhere, rather than circling
in place. It claims the algorithm initiates them, with the user as passenger.
And, in its politically charged version, it claims they tend toward
radicalising content. Each claim can be checked, if one has two things that
have historically been hard to get: real viewing histories rather than
simulated ones, and a way of measuring "similar content" that does not depend
on a researcher's hand-picked categories.

This study had both. We analysed watch histories donated for research by real
TikTok users, and we measured content similarity with a machine-built "map of
meaning" covering a quarter of a million videos. We then asked five plain
questions. Do stretches of near-identical viewing actually occur? How common
are they? What are they like — how long, about what, driven by whom? Do the
people who have them differ from the people who don't? And can you see one
coming before it starts?

## 2. The data: 73 donated watch histories

Our data come from a research platform that collects *data donations*:
TikTok users export their own platform data and contribute it to science.
Each donation contains the user's viewing history — which videos they watched
and when, to the second — along with their likes, follows, searches and
comments. The histories are long: typically about six months of viewing, and
over twenty thousand watched videos per person.

Knowing *that* someone watched a video is not the same as knowing what the
video was about. For that, the platform separately collects the videos
themselves and has them described in detail by a large language model: what
happens on screen, what is said, what text and music appear. From these
descriptions, each video receives a position in a high-dimensional "semantic
space" — a map of meaning in which two videos sit close together when they
are about similar things, regardless of language or superficial differences.
A quarter of a million videos (260,196) have a place on this map.

Two honest limitations follow from this setup, and they frame everything
below. First, **coverage**: only videos that have been collected and described
can be placed on the map. For the typical donor in our analysis, about a
quarter of watched videos are mapped; a binge happening entirely among
unmapped videos would be invisible to us. All our prevalence figures are
therefore lower bounds, and we verified that our conclusions hold among the
donors with the best coverage. Second, **selection**: people who donate their
data to research are not a random sample of TikTok users. Our numbers describe
these 73 viewers; they are evidence about what is *typical and possible*, not
a census.

From 133 donated collections we kept the 73 with enough viewing, enough days
and enough mapped videos to analyse responsibly, excluding a small number of
collections that were research artefacts rather than personal feeds.

## 3. What counts as a binge? Making the question precise

"Rabbit hole" is a metaphor, and metaphors cannot be counted. The core of this
study was turning the metaphor into a definition that a computer can apply
and a sceptic can challenge.

We define a **binge episode** as a run of at least four *different* videos,
watched back to back within one sitting, in which each new video is about
essentially the same thing as the videos just before it — where "the same
thing" means unusually close together on the semantic map. The run continues
as long as the next video stays close to the recent ones, and ends when the
viewer moves on (or the sitting ends). A few decisions deserve plain-language
justification:

- **Different videos.** Watching the same video five times in a loop is not a
  binge; it is a rewatch. Early in the analysis, rewatching masqueraded as our
  strongest "binge" signal until we excluded it. Every episode in this study
  consists of distinct videos.
- **Within one sitting.** A binge should not span a night's sleep. We use the
  natural breaks between sessions (15 minutes of inactivity) as hard episode
  boundaries.
- **How close is "close"?** Any threshold is a judgment call. We calibrated
  ours by having the lead researcher read a sample of twenty candidate
  episodes — the clearest cases, typical cases and borderline cases — and
  judge whether they read as genuine binges. (They did; the borderline cases
  were genuinely borderline.) We then re-ran the entire analysis under
  stricter and looser thresholds to check which conclusions depend on the
  choice. Only one number moves materially: how many donors have at least one
  episode (between 59% and 92% across definitions). Everything else — how
  short binges are, how rare, what they are about, who drives them — holds
  across the board.

**The shuffle test.** Even in random viewing, four similar videos could
occasionally land in a row by luck, the way ten coin flips sometimes produce
four heads in a row. To rule this out, we took each person's actual viewing
and shuffled its order two hundred times, re-running the binge detector on
each shuffled history. If binges were coincidences of volume, the shuffled
histories would contain them too. They essentially never do: shuffled
histories average 0.02 episodes per person, against one to seventy-five in
the real ones. When our detector finds a binge, it reflects something true
about *when* videos were watched, not just *what* was watched.

**The matched-draw test.** Saying "beauty content is binged a lot" is
meaningless if a donor simply watches a lot of beauty content. So every claim
about what binges contain is compared against a baseline built from the same
person's own viewing: we repeatedly drew random sets of videos from each
donor's diet, matched in size to their actual episodes, and asked how the real
episodes differ from these random sets. Differences from this baseline are
about *bingeing*, not about taste.

## 4. Findings

### 4.1 Binges are real — and nearly everyone has them

Sixty of the 73 donors had at least one binge episode, and for 57 of them
(78%) the count exceeds what their own shuffled viewing could plausibly
produce. Bingeing is not a fringe behaviour of a susceptible few; it is a
near-universal feature of how people use the platform. In total we found 389
episodes.

### 4.2 But bingeing is a tiny fraction of viewing

Here the rabbit-hole image meets its first contradiction. For the typical
donor with binges, all episodes together account for **about 0.2% of total
watch time** — roughly one minute in every eight hours of viewing. Even the
heaviest binger among the 73 spent under five percent of their watch time in
episodes. People binge the way people occasionally eat a whole packet of
biscuits: really, but rarely, and it is not how they eat.

The typical episode is five videos long and lasts about six minutes. The
longest we observed ran to a few dozen videos — notable precisely because it
was exceptional.

### 4.3 What people binge — and what they don't

If the algorithm pulled people down holes, one might expect the holes to lead
toward the provocative: outrage, conspiracy, politics. What we found instead
reads like a newsagent's lifestyle shelf. The content most over-represented
in binges, relative to each person's ordinary viewing, is: beauty product
reviews (six times its ordinary share), religious sermons (six and a half
times), product reviews, recipe tutorials, personal-care tutorials, craft
videos, travel clips, financial-advice explainers. What these have in common
is *format*: instructional, serial, review-like — content where one video
naturally invites the next, the way one episode of a series invites another.

And the exception proves the rule. Of the fifteen most-binged topics we
tested, exactly one failed to exceed its chance share: **political content**.
Our donors watched plenty of politics — they just did not binge it. A second,
independent test agrees: comparing each person's binges against their own
overall diet, binge content is slightly *less* political than what the same
person ordinarily watches, and no different in sensitive content. Whatever
binges are, on this platform and in these histories, they are not
doomscrolling.

### 4.4 Half of all binges are one creator

For every episode we asked: how many different creators made these videos? In
**48% of binges, at least half the videos came from a single creator**. If
binge-sized sets of videos were drawn at random from the same people's
viewing, that would happen 0.1% of the time. This is the study's strongest
clue about *mechanism*. A run of videos from one creator is what it looks
like when a person finds someone they like and watches through their work —
visiting a profile, tapping through a series. It is creator loyalty: a
human, deliberate pattern, not an algorithmic spiral through a topic. (Our
similarity map was deliberately built without any information about who made
each video, so this result is not baked in by construction — though similar
videos from one creator naturally go together, so we read 48%-versus-0.1% as
an upper bound on the effect with a very wide margin.)

### 4.5 Binges are habits

Bingeing also repeats. Among donors with two or more episodes, the same
person's binges land on the same topic far more often than chance: 13.8% of a
donor's episode pairs share their topic, against 2.2% if topics were dealt
out at random — six times over. The median gap between two same-topic binges
is about **ten days**. Nearly half of repeat bingers have one personal topic
that accounts for the majority of their episodes. The binge is less like a
hole someone falls into and more like a *standing appointment*: every week or
two, back to the beauty reviews, back to the sermons, back to the recipes.

### 4.6 Binges end sessions; they don't extend them

The strongest policy version of the rabbit-hole worry is the *time trap*: the
algorithm finds your weakness and keeps you scrolling. We tested this
directly. During a binge, people do watch more intently — they spend about
six seconds longer on each video than they do elsewhere in the same session.
But after a binge ends, the session winds down *sooner*: about ten minutes of
viewing remain, versus eighteen at the same point in the same person's
binge-free sessions.

A binge, in other words, behaves like a **finale**. The viewer locks in,
watches their fill, and leaves. We are careful about cause and effect here —
it is also possible that binges simply tend to happen in the settled final
stretch of an evening's viewing. But either way, the data show the opposite
of a trap: sessions containing a binge do not run on longer after it.

### 4.7 Can you see a binge coming?

Finally, prediction. If binges were algorithmic ambushes, the moments before
one might look like any other moment. If they are deliberate, the viewer's
own behaviour should give them away — slightly. The latter is what we find. A
model watching the previous few minutes of behaviour can flag binge-starts
about seventeen times better than random guessing. The strongest warning
signs: the stream of videos is already getting more similar; the viewer is
dwelling longer on each video; several recent videos come from the same
creator; and — most tellingly — the viewer has just liked, followed or
searched for something. The binge announces itself as a *choice being made*.

Seventeen times better than guessing sounds impressive, and as evidence about
mechanism it is. As a practical alarm it is useless: binge-starts are so rare
(one in two thousand videos) that even this model is wrong ninety-nine times
out of a hundred when it raises its hand. No one should build an intervention
on it; that is not its point.

### 4.8 The missing rabbit hole

The defining geometric feature of a rabbit hole is *descent*: each video
similar to the last, but the chain drifting steadily away from where it
began, so the viewer ends somewhere new. Our map lets us measure exactly
this — how far each binge travelled, and how directed its path was. Across
all 389 episodes, and across every alternative definition of a binge we
tested, the region of the map where descending spirals would appear is
**empty**. Binges sit inside a tight neighbourhood of meaning and stay there
until the viewer leaves. People dwell; they are not led.

## 5. What this adds up to

Putting the pieces together, the binge that actually occurs in these 73
histories looks like this: *a slow, attentive viewer settles in — often in
the back half of an evening session, often right after liking or searching
for something — and watches ten minutes of beauty reviews, mostly from a
creator they follow. Then they put the phone down. In ten days, they will do
it again.*

Each component of the alarming narrative fails separately. Not directed: zero
descending paths. Not political: the one topic people decline to binge, and
binges are less political than their own ordinary diet. Not a trap: sessions
end sooner after binges, not later. Not an ambush: the clearest precursors
are the viewer's own deliberate acts. And not characteristic of heavy use:
binge-prone donors are the *slower*, more deliberate, narrower-taste viewers,
not the heaviest scrollers.

This coheres with the broader pattern in this research programme: across
several studies of these histories, the feed has proven remarkably
*persistent* — its composition stays stable across a sitting, it does not
visibly chase the viewer's moment-to-moment behaviour, and it neither narrows
nor diversifies systematically as a session unfolds. The focused exception we
have now documented is brief, benign, habitual and substantially
viewer-driven.

Two implications follow. For the public debate: at the timescale of minutes
to hours, in real donated viewing, the rabbit-hole narrative is not merely
unsupported — its components are individually contradicted. Concern about
recommender systems is legitimate, but it should be aimed where evidence
points (and slower, cross-month dynamics remain to be studied), not at an
image these data do not contain. For platform research: the most interesting
discovery here may be the *finale effect* and the creator-loyalty mechanism —
binges as satisfying, self-chosen conclusions rather than compulsive spirals.
If that holds up elsewhere, "binge" may be the wrong word borrowed from the
wrong moral panic.

## 6. What this study cannot say

Honesty about limits, in plain terms:

1. **We see a quarter of the picture.** Only mapped videos count toward
   detection; binges among unmapped videos (often non-English content) are
   invisible. Our prevalence figures are floors, not ceilings. Reassuringly,
   among donors where we see the most (40–77% coverage), every conclusion
   reproduces — and those donors show *more* binges, exactly as undercounting
   predicts.
2. **Donors are volunteers.** Mostly Australian, willing to share their data,
   with histories capped at about six months by the platform's export. The
   patterns are robust within this group; the group is not the world.
3. **We see what was watched, not what was offered.** A viewing history
   cannot separate "the algorithm offered it and the user accepted" from "the
   user sought it out". Our creator-loyalty and self-initiation findings make
   the deliberate reading more plausible, but no causal claim about the
   algorithm is possible from this design.
4. **Definitions matter.** "Binge" had to be given a precise meaning, and the
   one number that depends on the strictness of that meaning is how many
   people have at least one (59–92%). We report the calibrated middle (82%)
   and the full range.
5. **Minutes, not months.** This study detects narrowing within sittings. A
   slow drift of someone's diet over months is a different phenomenon needing
   a different design — and is the natural next study.

## 7. Where this should go next

Three directions follow naturally. First, *coverage*: extending the semantic
map to the unmapped majority of videos (or building a lighter fallback map
from captions and music metadata) would turn our lower bounds into estimates.
Second, *the long timescale*: applying the same episode logic to weeks and
months would test the slow-drift version of the narrative that this design
cannot reach. Third, *other platforms*: the anatomy found here — brief,
habitual, creator-anchored, finale-shaped — is a precise, portable
description; whether YouTube Shorts or Reels binges share it is an open and
answerable question.

---

## Appendix A. The research questions, formally

The study was organised around eleven pre-specified questions, here in plain
form with pointers to the sections answering them: Do low-entropy (focused)
sequences occur beyond chance? (RQ1 → §4.1). How common are they across
people (RQ3 → §4.1) and within a person's viewing (RQ4 → §4.2)? What are they
like — length, topics, formats (RQ5 → §4.3)? Are they stationary clusters or
directed drifts (RQ2 → §4.8)? Who or what initiates them — creator loyalty
versus topic amplification (RQ6 → §4.4)? How do engagement and content
valence behave during them (RQ7 → §§4.3, 4.6)? Do binge-prone donors differ
(RQ8 → §5)? Do binges recur as habits (RQ9 → §4.5)? Do they extend or end
sessions (RQ10 → §4.6)? Can their onset be predicted, and from what (RQ11 →
§4.7)?

## Appendix B. Methods in one paragraph each

**Semantic map.** Each collected video is described in detail by a large
language model (story, speech, on-screen text, objects, sounds, hashtags);
the description is converted to a 1,536-dimensional embedding
(`gemini-embedding-001`). Embeddings are corrected for the model's global
bias and normalised, so that the angle between two videos measures how
different they are about. Creator identity is deliberately excluded from the
description.

**Episode detection.** Within each session, in time order over mapped
videos: a run grows while the next distinct video lies within a similarity
threshold of the average of the recent run members, and is kept as an episode
if it reaches four distinct videos over three minutes. The threshold was
human-calibrated on a stratified sample and varied across the analysis as a
robustness check; repeated plays never count twice; session breaks always end
an episode.

**Inference.** Existence is tested per person against 200 reshuffles of
their own viewing order (a within-person permutation test, corrected for
multiple comparisons across donors). Content claims are tested against
500 random video-sets drawn from each person's own diet, size-matched to
their episodes. Session-effect claims use paired within-person comparisons at
matched session positions. Prediction uses simple transparent models with a
strict past-predicts-future split per person, evaluated with rare-event
metrics.

**Reproducibility.** All code, the full technical results, the design
document and the figure set live alongside this draft
(`FINDINGS_v1.md`, `RESEARCH_DESIGN.md`, `make_figures.py`,
`tmp/figs_presentation/`).
