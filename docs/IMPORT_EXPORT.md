# Bringing work in, taking output out

## Importing earlier work

```bash
uv run video-to-llm import /path/to/an/earlier/output/folder
```

Finds every processed video underneath and registers it, so it can go into a
collection without being processed again.

**Import never modifies the folder it reads.** Nothing is moved, rewritten, or
renamed. If an import is wrong, delete the rows — the original output is
untouched. There is a test that hashes every file before and after and compares,
because an import that damages what it reads is unrecoverable.

A folder counts as a processed video when it contains `assembled.txt`. That file
is only written once a video finished assembly, so its presence is the cheapest
reliable signal there is something worth importing.

Two things are skipped deliberately: copies inside `analysis_input/`, and folders
already imported. Either would put the same video into a collection twice without
saying so.

### Compatibility is reported, not enforced

| Reported | Means |
|---|---|
| Fits with the rest | Complete and current |
| No descriptions | Pictures and words only |
| Older wording | Described under a different prompt or schema |
| Pictures missing | The text is fine; the picture folder moved |

None of these refuses the import. Refusing would strand work that is fine for
most purposes.

## Taking output out

Everything is already plain files in the output folder. There is no export step
to run and no proprietary container to open.

### The handoff folder

Each job gets `analysis_input/`:

```
analysis_input/
  01_capture_0914_assembled.txt
  02_screen_1145_assembled.txt
  master_assembled.txt            multi-video jobs only
  frames/01_capture_0914/         referenced, not copied
  README.md                       how picture numbers map to files
  analysis_input_manifest.json
```

The README explains the mapping, because it is not obvious: text refers to
`picture 47`, counting from 1, while files are named from 0.

```
picture 1   ->  000000_t000000.jpg
picture 47  ->  000046_t000132.jpg
```

### Referenced, not copied

Pictures are symlinked. A 1,265-frame video is about 1.7 GB, and copying would
double a job's disk cost for no benefit. Where symlinks are unavailable — Windows
without Developer Mode, some network filesystems — it copies instead, so the
folder always works.

For a folder you can move or send elsewhere, ask for a portable export, which
copies deliberately.

### What is never included

Your original videos, and any audio taken from them. The output folder holds only
what was produced: text, pictures, and records of how they were made.

No API key, in any form, in any file.

## Moving the output folder

Safe. Every artifact path in the database is stored relative to the output root,
so moving the whole tree does not invalidate anything. Point the application at
the new location and start the worker; reconciliation confirms what is there.
