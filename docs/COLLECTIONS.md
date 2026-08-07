# Collections

A collection is a saved, explicitly ordered set of videos you have already
processed. Building one is **local, free, non-destructive, and takes seconds**.
Nothing is extracted, transcribed, or described again, and no provider is
contacted at any point.

## Versions are pinned

This is the property the whole feature rests on.

A collection records not just *which* video it uses but **which version of that
video's output**. Reprocessing a source later creates a new version and leaves
every existing collection exactly as it was.

A collection is a citation of specific evidence. A citation that silently changes
when its source is revised is worse than no citation at all.

To use newer output, rebuild the collection deliberately or make a new one. Both
are cheap.

## Building one

1. **Choose videos** — anything completed, including work brought in from an
   earlier run.
2. **Set the order** — yours, explicitly. Never inferred from filename or date.
3. **Choose versions** — the active one by default; you may pick an earlier one.
4. **Choose the shape** — one document, or parts sized to fit.
5. **Check and build.**

There is no heavy confirmation step. The operation is local, free, and changes
nothing that already exists, so demanding confirmation would train you to click
through warnings that matter.

## Warnings never block

| Warning | Means |
|---|---|
| Some pictures have no description | The video finished with gaps |
| No descriptions | Pictures and words only — the local-only default |
| Described with older wording | A different prompt or schema was used |
| Pictures are missing | The text is fine; the picture folder moved |

Every one of these permits inclusion. You are told what is imperfect and you
decide. Refusing would strand work that is perfectly good for most purposes.

## Mode A — one document

`collection_assembled.txt`, every video in your order, each wrapped in a boundary
that names its source and version:

```xml
<video sequence="2" source_video_id="..." processed_version="2">
  <title>capture_1030.mp4</title>
  <duration>00:51:33</duration>
  ...
</video>
```

Plus `collection_manifest.json` (ordered sources, versions, checksums, warnings,
token method, output checksums) and `collection_readme.md`.

## Mode B — parts that fit

`collection-pack-001.md`, `-002.md`, … sized to a budget you set.

**Usable budget = your model's limit − what you hold back for the prompt and
reply.**

**Whole videos stay together by default.** A model reading half a video with no
indication that it is half will summarise it as though it were whole. Pack
boundaries are preferred between videos.

If a single video is larger than one whole pack, you are told and it gets its
own oversized part — unless you allow splitting. When you do:

- cuts land on section boundaries, never mid-sentence;
- each continuation repeats a little of the previous part in a
  `<continues_from_previous_part>` block, so the thread is not lost;
- the split, its parts, and the overlap size are all recorded in the manifest.

## Sizes are estimates

Every token figure is an estimate, computed from a documented character ratio and
labelled as an estimate wherever it appears. Real tokenisation is model-specific.
The estimate errs slightly high, so packs come out a little smaller than they
need to be — the safe direction, since the alternative is a pack that will not
fit.

## Where output goes

```
collections/<collection-id>/v<version>/
```

Never merged into a processed video's own folder. Merging them would make
collection output look like part of a video's archive, and deleting one would
silently damage the other. Each build gets its own version directory; earlier
builds are never overwritten.

## Not built, deliberately

No embeddings, vector index, retrieval chat, automatic grouping, automatic
summaries, cloud sync, or collaboration. The manifests carry enough structure
that these could be added later without reprocessing anything.
