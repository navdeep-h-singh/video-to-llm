# Why every description came back Low

**Investigated:** 9 August 2026.
**Status:** cause found and fixed for the confidence field. A second, separate
problem — the schema itself — is diagnosed here and deliberately **not** fixed.

This document answers what `HANDOFF.md` §8.1 called "the single most important
open question in the product".

---

## What was believed

> All 1,479 came back `confidence: Low` — zero Medium, zero High — and the
> structured fields disagree with themselves across consecutive frames of the
> same chart. On that video the entire signal was in the 53 KB transcript.
> — `HANDOFF.md` §7

The conclusion drawn from that was that the headline feature had produced
nothing usable on its first real workload, and that descriptions could not be
marketed until it was understood.

## What is actually true

**The descriptions were not worthless. Only the confidence field was.**

Re-reading the stored batch artifacts from the real run — read-only, nothing
re-run — the extracted content is largely correct. The first batch, frame 0:

| Field | Value |
|---|---|
| `currency_pair` | `GBPUSD` |
| `timeframe` | `240` |
| `visible_text` | `GBPUSD, 240; British Pound/U.S. Dollar; … GBPUSD 1.44371 -0.01025 (-0.7%); DXY 99.018 +0.202 (+0.2%) …` |
| `visual_description` | `A GBP/USD chart with a downward trend line and support/resistance levels.` |
| `confidence` | `Low` |

That is the correct instrument, the correct timeframe, real quoted prices, and a
fair summary. It is not the output of a model that failed.

Field coverage across all 1,479, measured:

| Field | Unknown | Filled | Distinct values |
|---|---:|---:|---:|
| `timeframe` | 0 | 1,479 | 20 |
| `currency_pair` | 0 | 1,479 | 5 |
| `visible_text` | 0 | 1,479 | 1,052 |
| `visual_description` | 0 | 1,479 | 985 |
| `exact_action` | 813 | 666 | 272 |
| `setup_type` | 804 | 675 | 97 |
| `indicators_and_states` | 1,173 | 306 | 144 |
| **`confidence`** | **0** | **1,479** | **1 — every value `Low`** |

Every other field varies. Confidence has exactly one value in 1,479 samples.
That is not a model judgement; a field that never varies is measuring nothing.

## The cause

The shipped prompt defined confidence in one clause:

> Set confidence to Low whenever you are unsure.

Read literally by a cautious model, that instruction is satisfied by answering
`Low` every time, because it is never *fully* sure of anything. The prompt
offered three levels and defined one of them, in terms of a feeling rather than
anything observable in the picture.

**The model was following the instruction correctly. The instruction was wrong.**

## The experiment

Same five real frames sampled across the 49-minute video, same model
(`qwen2.5vl:7b`), same temperature (0), one frame per request. Only the
confidence paragraph differs.

| Frame | Shipped prompt | Legibility rubric |
|---|---|---|
| `000000_t000000.jpg` | Low | **Medium** |
| `000300_t001000.jpg` | Low | **Medium** |
| `000700_t002320.jpg` | Low | **Medium** |
| `001100_t003640.jpg` | Low | **Medium** |
| `001400_t004640.jpg` | Low | **High** |

The extracted `currency_pair` was identical under both prompts on all five
frames, which is the control: the rubric changed how the model *rated* its
reading, not what it read.

The rubric now shipped:

```
Set confidence by how much of this picture you could actually read:

  High    the main labels and values are crisp and you read them directly
  Medium  you read the main content, but some smaller detail is unclear
  Low     the picture is blurred, cut off, or mostly unreadable

Confidence describes legibility, not how certain you feel in general. A clear
screenshot you transcribed correctly is High even if you are unfamiliar with
the subject.
```

Legibility is something a vision model can assess from the image in front of it.
"How sure do you feel" is not.

`tests/unit/test_visual_prompt.py` guards the property, not the wording:
the prompt must define every level it offers, must anchor confidence to
something observable, and must not reinstate the old clause.

### What this experiment does not establish

- **n = 5 frames, one video, one model.** The direction is unambiguous — a field
  that was constant now varies — but the distribution across a real corpus is
  unmeasured.
- **It does not measure accuracy.** The rubric makes the model report legibility
  honestly; whether a `High` reading is *correct* is a separate question that
  nothing here tests. `HANDOFF.md` §7's "transcription accuracy is unmeasured"
  applies to descriptions too.
- On an abstract test pattern with almost no text, the rubric still returns
  `Low`. That is the rubric working, not failing.

## The second problem, not fixed

**The schema is written for one domain.** Five of its eight content fields —
`timeframe`, `currency_pair`, `indicators_and_states`, `exact_action`,
`setup_type` — describe forex charts. The build spec was written around a
trading course, and the schema never outgrew it.

On the real trading video this is invisible; the fields apply and get filled. On
anything else it is not. Tested against frames of a colour test pattern:

```
currency_pair        'Unknown'
timeframe            'Unknown'
setup_type           'Unknown'
visual_description   'A color test pattern with vertical bars in various colors…'
```

**The good news is that the model declines rather than inventing.** It returns
`Unknown` instead of hallucinating a currency pair onto a video that has none,
which means the schema is *wasteful*, not *dangerous* — three fields of the
prompt, three fields of every response, and three columns of every review screen
spent on nothing.

The bad news is what it looks like on a public repository. A general-purpose
tool that asks a cooking video for its currency pair reads as a tool built for
something else and pointed at you.

### Why it is not fixed here

The confidence fix is one paragraph of one string, verified by experiment,
guarded by a test, and touching nothing else. Generalising the schema is not:
`FrameDescription`, the parser, `SCHEMA_VERSION`, the batch artifacts already on
disk, `enrich.py` (which titles sections from `currency_pair`), `assemble.py`,
`review.html`, and the descriptions of a 15-hour corpus that already exist all
move together. That is a design change with a migration attached, and it should
be made deliberately rather than as a side effect of a prompt investigation.

### The recommended shape

Keep three fields that are true of any video — `visible_text`,
`visual_description`, `confidence` — and make the domain fields a **named
profile** the user chooses per job, defaulting to a general one:

```toml
[visual_analysis]
profile = "general"     # general | trading | slides | code | custom
```

Each profile supplies its own extra keys and its own prompt fragment. The
trading profile preserves exactly today's behaviour and today's schema, so the
existing corpus stays valid and `schema_version` records which profile produced
a description. A `custom` profile taking user-supplied field names is the
natural extension and is what makes the feature interesting to people whose
domain nobody anticipated.

## What this changes for the launch

- Descriptions can be described honestly as working, with the caveats that
  accuracy is unmeasured and local inference is slow (~31 s/frame).
- The claim "the headline feature produced nothing usable" was wrong, and any
  marketing built on that pessimism should be revised.
- The domain-specific schema **must** be addressed before a public launch, or
  the general-purpose positioning will be contradicted by the tool's own output
  on the first non-trading video anybody tries.
