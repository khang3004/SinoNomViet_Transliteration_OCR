# OCR Review

A review console for auditing another team's Hán-Nôm transcriptions.

They hand over ~9,000 calligraphy images in a public Google Drive folder, a
`ground_truth.jsonl` and a `ground_truth.xlsx`. Reviewing all of them is not the
plan. The app draws a **study sample of 500**, hands each reviewer a disjoint
slice of it, and records — image by image — whether their `ground_truth`
actually matches the photograph.

The output is the number their files cannot give you: **how accurate their
labels are**, and therefore how much to trust every model score computed
against them.

```
9,000 images ─► study sample (500) ─► reviewers ─► verdicts ─► reviews.xlsx
                stratified, capped     disjoint
```

The study is the unit that matters. It is drawn once, it is the denominator of
every progress bar, and reviewers are never handed anything outside it.

## What a reviewer sees

The image on the left; on the right, their `ground_truth`, the `gemini` output
with its distance from that label, and the `deepseek` output for context. Then
one question: **does their transcription match the image?**

| Verdict | Meaning | Correction required |
|---|---|---|
| `correct` | matches the image | no — the server adopts their label as the truth |
| `minor` | small errors: variant forms, a stray character | yes |
| `wrong` | substantially wrong | yes |
| `unreadable` | image too damaged or unclear to judge | no |
| `not_an_image` | broken, missing, or not a photograph | no — dropped and replaced |

Three buttons sit beside the verdicts: **Later** leaves the image in your queue
to come back to, **Swap for another** takes it out of the study for good and
hands you a different one from the same band, and **Prefill** drafts the whole
form with Gemini.

### Prefill

Sends the picture, their `ground_truth`, and the Gemini OCR line with its
agreement percentage, and fills in every control: the corrected text, the
verdict, the Gemini rating, and a one-line note. It is a **draft** — nothing is
saved until the reviewer looks at the image and presses Save.

The draft is made internally consistent before it reaches the form, because a
half-filled or self-contradicting one costs more time than it saves. A verdict
of `correct` always carries their text verbatim; a `minor`/`wrong` whose
correction did not actually change anything becomes `correct` (the text is
trusted over the label); `unreadable` and `not_an_image` carry no transcription.

Needs `GEMINI_API_KEY`. Without it the button reports that and everything else
works. `GEMINI_MODEL` selects the model — change it if the API rejects the
default as unknown or unavailable to your key; the error names the model.

A caution worth stating plainly: this app exists so that a *person* judges
machine output. A reviewer who accepts drafts without reading the image turns
the audit into one model grading another, and the resulting accuracy figure
means nothing.

Gemini gets a lighter `good` / `partial` / `bad` rating alongside. DeepSeek is
shown but not scored.

The **Corrected** box sits directly under Gemini, pre-seeded with their
transcription so a reviewer edits two characters instead of retyping twenty.
DeepSeek sits below the metadata, for context only.

Pre-seeding that box makes one contradiction reachable — calling a label wrong
while the text still says exactly what was just called wrong — so the server
refuses `minor`/`wrong` when the correction is unchanged from their label
(whitespace-insensitively). Together with the fact that **a verdict is only
recorded when the reviewer chooses one**, that is what stops clicking through
from masquerading as a perfect score. `not_an_image` does not count as a review
— the image leaves the study and a replacement is drawn for it.

## How the study is drawn

A uniform 500 out of 9,000 would spend most of its effort on rows where the
model and the label already agree, which teaches nothing. So the draw is
stratified on **disagreement** — how far their Gemini output sits from their own
ground truth:

| Band | Definition | Share | At 500 |
|---|---|---|---|
| `exact` | identical after whitespace folding | 15% | 75 |
| `near` | ≥ 90% character accuracy | 20% | 100 |
| `far` | 50–90% | 25% | 125 |
| `poor` | < 50% | 25% | 125 |
| `empty` | one side blank | 15% | 75 |

Two constraints ride along:

- **Per-post cap** (default 2). One prolific page can contribute dozens of
  images; without a cap the study would describe that page, not the corpus.
- **No overlap.** Reviewers never share a record, so a claimed image is gone
  from the pool until it is released.

Shortfalls redistribute: if a band runs dry, its quota moves to the bands that
still have depth rather than silently returning a short study.

**An image can leave the study, but the study never shrinks.** Two things take
one out:

- `not_an_image` — the picture is broken or is not a photograph;
- **Swap for another** — the reviewer would simply rather not review this one.

Either way it is recorded (with who asked for it, and why) so it can never be
handed out again, a fresh image from the *same band* takes its place, and that
replacement goes straight to the same reviewer. The study still ends with 500
judged images rather than 493, its band shape is unchanged, and the reviewer's
own mix of easy and hard images stays comparable to everyone else's.

A word of warning about **Swap**: reviewers removing images they would rather
not judge moves what the audit measures — towards "accuracy on the images
reviewers were willing to look at". The same-band replacement limits the drift
to within a band, and every swap is counted per reviewer in the Reviewers table
and on the study panel, so the size of the effect is visible rather than
invisible. It is still worth agreeing as a team when a swap is legitimate.

Tune it with `SAMPLE_SIZE`, `SAMPLE_TARGETS`, `SAMPLE_PER_POST_CAP` and
`SAMPLE_BATCH` (how many a reviewer claims per click).

### Growing a study

Three admin actions touch the study, and only one of them is destructive:

| Action | Effect |
|---|---|
| **Add never-seen images** | Draws N more the study has never used, in the same band proportions. Every existing image, review and claim is kept; the target grows |
| **Top up** | Refills only what was dropped as unusable or swapped out. Never goes above the target |
| **Redraw study from scratch** | Replaces membership wholesale. Reviews stay on disk, but images that drop out stop counting |

Adding is the one to reach for when reviewers need images nobody has seen. The
cost is that the denominator moves — a study extended from 500 to 1000 reports
`126 of 1000`, not `126 of 500`. Records used by any earlier draw, including
ones dropped or swapped out, are never drawn again.

### Moving work between reviewers

Claims are exclusive, so one reviewer claiming a large batch and then not
working through it starves everyone else — there is nothing left to claim even
though most of the study is unreviewed. **Reviewers → Move unreviewed work**
hands one person's outstanding claims to another, optionally capped at N.

Only outstanding claims move. Reviews are keyed on the record rather than on the
assignment, so finished work stays credited to whoever did it.

## Accuracy, reported twice

Every comparison reports two numbers, because they diverge exactly where it
matters:

- **`cer_accuracy`** = `1 − distance / len(reference)` — the standard CER
  convention. Quote this one outside the project.
- **`max_accuracy`** = `1 − distance / max(len(a), len(b))` — the convention
  the upstream team's `Task.xlsx` uses.

When a model hallucinates extra text the hypothesis is longer, so `max` divides
by the larger number and flatters the very failure this audit exists to find.

Whitespace is folded before comparison: in Hán-Nôm transcription line breaks
record layout, not content. Text is NFC-normalised, so a decomposed character
is not counted as an edit.

Once a reviewer has audited a record, **their correction becomes the reference**
and both their `ground_truth` and `gemini` are measured against it. That is
where `ground_truth_accuracy` and `gemini_accuracy` on the dashboard come from.

## The data it reads

The two files are complementary, and join on `image` — the only field both
share:

```jsonc
// ground_truth.jsonl — model outputs, no post identity
{ "image": "...", "ground_truth": "...", "label": "...",
  "gemini": [{"text": "..."}], "deepseek": [{"text": "..."}] }
```

```
ground_truth.xlsx — post identity
post_id | image | caption | ground_truth | gemini_ocr | post_link
```

Either file alone produces a usable corpus; together they produce a complete
one. Header spelling and case are tolerated (`FB Caption`, `Gemini OCR`, `Link`).

Rows are dropped at ingest, with a counted reason, when the `image` is blank or
is not an image file, or when there is no `ground_truth` to audit.

### Post ids

Their filenames are `<base64_post_id>_<idx>.jpg`. The base64 decodes to a
Facebook story id:

```
UzpfSTEwMDAwMDU5MzExMzI1ODpWSzoyNzgzNTQ4OTgyNjA5MzEwMA==
  -> S:_I100000593113258:VK:27835489826093100
  -> https://www.facebook.com/permalink.php?story_fbid=27835489826093100&id=100000593113258
```

So every image links straight back to its post, whether or not the xlsx supplied
a `post_link`. An id that does not decode yields no link rather than a guessed
one — a half-built URL would send a reviewer to the wrong post silently.

Standard base64 contains `+`, `/` and `=`, none of which survive a URL path, so
paths and URLs carry a **slug** form (`+`→`-`, `/`→`_`, padding dropped). The raw
id stays intact everywhere else, because it is the join key against their data.

## Images

Their Drive folder is public. That makes **downloading** trivial — a file in a
publicly shared folder is readable from
`drive.google.com/uc?export=download&id=…` with no credentials at all, and the
app never authenticates to fetch one.

**Listing** the folder is the hard part, and Google offers exactly one workable
route:

| Route | Works | Limit |
|---|---|---|
| `files.list` + API key | **no** — `401 API keys are not supported by this API` | — |
| `files.list` + service account | yes, paginated | none |
| public `embeddedfolderview` page | yes, no credentials | **5,500 files, no paging** |

The API key path is a dead end no matter how the key is configured: unlike most
Google APIs, Drive demands a principal rather than just a project. Don't spend
time on it.

So with only `GOOGLE_DRIVE_FOLDER_ID` set, the app scrapes the public folder
page — fine for a smaller folder, and enough to get started. Past 5,500 files
that listing silently stops, so the app reports `truncated` and says so on the
dashboard rather than quietly auditing a partial corpus. Setting
`GOOGLE_SERVICE_ACCOUNT` (a path to a service-account JSON key, or the JSON
itself) switches to `files.list` and lifts the cap.

**The study is drawn only from images the index can resolve.** A truncated index
therefore costs coverage, never reviewer time: nobody is handed a thumbnail that
cannot load. The dashboard shows indexed-vs-corpus so the gap is visible.

Reviewers never load from Drive directly either. The app mirrors each image to
local disk the first time someone opens it and serves it from `/img/*`
thereafter: the folder is not exposed in devtools, a room full of reviewers does
not rate-limit it, and the second person to open an image gets it instantly.
Nobody waits for nine thousand downloads a 500-image study will never touch.

`/img/*` is HMAC-signed rather than cookie-gated, so an `<img>` tag loads
without a session round-trip. A forged or expired signature is a 403 before the
filesystem is touched. If Drive serves an HTML interstitial instead of bytes —
what happens when a file is not actually public — it is refused rather than
cached, so a broken thumbnail always has an explanation.

## Accounts

The **super admin** comes from the environment (`APP_USERNAME`,
`APP_PASSWORD_HASH`) and cannot be created, renamed or disabled through the UI —
so a mistake in the user file can never lock everyone out. Reviewers are added
by an admin under **Admin → Add a reviewer** and stored in `data/users.json` as
bcrypt hashes.

Accounts are disabled, never deleted: every review references its reviewer by
name, and removing the account would orphan that attribution in the export.

Everyone can see everyone else's completed reviews — that is the
**Everyone's reviews** tab — but only the holder of a record can judge it.

## Running it

```bash
cp .env.example .env      # then fill in the blanks
docker compose up -d --build
docker compose logs -f review
```

Generate the two secrets and the password hash:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

```bash
docker compose run --rm review python -m app.cli --hash-password
```

Then, signed in as the admin:

1. **Upload** `ground_truth.jsonl` and `ground_truth.xlsx`
2. **Ingest** — merges them and reports what was skipped and why
3. **Draw study sample** — fixes which 500 images this audit covers
4. **Index Drive folder** — maps filenames to Drive file ids
5. **Add a reviewer** for each person on the team
6. Everyone presses **Get images** and starts

Step 3 is the one that defines the audit. Reviewing is refused until it has
happened, and redrawing later replaces which images are in the study (reviews
already recorded are kept).

## Exports

Four downloads, from the Admin panel:

| File | What it is |
|---|---|
| `danh_gia.xlsx` | The upstream team's own review format — eight columns, with every character where Label and Corrected disagree coloured red and blue |
| `reviews.xlsx` | The full audit: every field, standard CER accuracies |
| `reviews.csv` | The same, for anything that reads CSV |
| `reviews.jsonl` | The same, one JSON object per line |

**The two spreadsheets deliberately disagree on the arithmetic.**
`danh_gia.xlsx` divides by the longer of the two strings, matching their
`Task.xlsx`, so it can sit beside their existing sheets. `reviews.xlsx` reports
standard CER (divided by the reference), which is the number to quote outside
the project. See *Accuracy, reported twice* above.

`danh_gia.xlsx` also differs in two places worth knowing:

- **Unjudgeable images are left unscored.** `unreadable` and `not_an_image`
  get blank Corrected and blank accuracy rather than 0%, which would drag the
  average down with pictures nobody could read. The reason moves into the Note,
  since this format has no verdict column.
- **The diff is aligned, not positional.** A single inserted character would
  otherwise mark the whole rest of the line as wrong.

Accuracy columns in every file are numbers with a percent format, not `"97.50%"`
strings — a text column cannot be averaged, which is the first thing anyone does
with the sheet. The CSV carries a UTF-8 BOM so Excel does not render the CJK as
mojibake.

## Storage

No database. Everything is files under `DATA_DIR`:

```
corpus/records.jsonl     the merged upstream data — replaced wholesale on ingest
corpus/ingest.json       what the last merge produced and skipped
sample.jsonl             which records are in the study; drops, who dropped
                         them and why, and their replacements
sample.json              the study's size, band shares and per-post cap
assignments.jsonl        append-only claims; the latest row per record wins
reviews.jsonl            append-only verdicts; a changed mind adds a row
users.json               reviewer accounts (bcrypt hashes)
drive_index.json         filename -> Drive file id
images/<shard>/…         mirrored images
uploads/                 the two files as uploaded
```

Append-only and fsynced: a reviewer's edit never destroys what they said before,
two writers cannot interleave into a corrupted record, and a crash loses at most
the line being written. Re-ingesting corrected upstream data keeps existing
reviews attached, because `record_id` is derived from the post id and image
index rather than from row position.

`--workers 1` is required, not a suggestion: assignment is serialised by an
in-process lock, and a second worker would hand the same image to two reviewers.

## Layout

```
app/core/     no web framework, ever — app/cli.py proves it
  postid.py     base64 <-> permalink <-> path-safe slug
  metrics.py    Levenshtein, CER and max-length accuracy
  corpus.py     read + merge their jsonl and xlsx
  sampling.py   stratified draw, per-post cap, shortfall redistribution
  sample.py     the study: membership, drops, replacements
  audit.py      corpus snapshot, assignments, reviews, progress
  drive.py      folder listing and lazy image mirroring
  users.py      accounts
  assist.py     Prefill: drafting a review from the picture with Gemini
  evalsheet.py  the team's "format đánh giá" sheet, with the diff coloured
  jsonlog.py    append-only fsynced logs
app/api/      FastAPI adapter over the above
app/static/   the console
```

## Tests

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest
```
