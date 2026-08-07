# Descriptions on this computer

Labelled **Local / Experimental** everywhere it appears.

> Frames stay on this device. No provider API charge. Local compute, battery,
> heat, memory use, and processing time apply.

Note the wording: *no provider charge*, not "free". A local run costs real
resources, just not money paid to anyone.

> **Local models may be less reliable for tiny text, dense labels, exact values,
> and strict structured extraction. Review low-confidence results.**

That warning is not boilerplate. A 7B vision model reading a dense chart will
often produce a confident-looking value that is wrong. Anything marked low
confidence is worth checking yourself.

---

## Setting it up

This application **never installs, starts, updates, or bundles Ollama**. It is
yours to manage.

```bash
ollama pull qwen2.5vl:7b
```

The documented baseline is Qwen2.5-VL 7B, but the model identifier is free text.
The suggestion is copyable, not a fixed catalogue.

Then in **Settings → Describing what is on screen**, choose *On this computer*
and use **Check local model**.

## What "Check local model" reports

- Whether anything is answering, and the runtime version.
- Whether that exact model is installed — with the precise `ollama pull` command
  if not.
- Whether it can read pictures, **or that this could not be confirmed**.
- Memory guidance for this machine.

If vision cannot be confirmed you will see **Vision capability not verified**,
not a cheerful assumption, and you will be asked to acknowledge that you are
using it experimentally. Ollama's metadata does not always expose image support
even when it works; the honest answer is that we could not tell.

## Loopback only

Only `127.0.0.1`, `localhost`, and `::1` are accepted, checked numerically so
`127.0.0.2` and `::ffff:127.0.0.1` work while `localhost.evil.example` does not.
Anything else is refused before a request is made.

A "local" model that is quietly on another machine would ship your frames off
this computer while the interface still said they were staying put.

## No key, ever

There is no key field for this provider, no environment variable, and nothing
stored. Asking to store one is refused with an explanation.

## Batch sizes

| Setting | Pictures per request |
|---|---|
| Default | 1 |
| After a successful check | 2 |
| Advanced override | 3–4, with a memory and latency warning |

Never the cloud's 20. Batching twenty pictures against a 7B model on a laptop
exhausts memory long before it saves time, and a request that is silently
accepted and then fails minutes in is worse than one refused up front.

One request at a time. Local transcription and local vision do not run
concurrently unless you deliberately enable it.

### Apple Silicon, roughly 24 GB

Start with 1–2 pictures per request, a 4K–8K context where that is configurable,
and one request at a time. **This is guidance, not a performance guarantee** —
it depends on what else is running.

## No automatic fall back to a service

There is never an automatic fall back from a local model to a cloud one. That
would send your frames off the machine after you chose to keep them on it — a
silent reversal of the one decision this provider exists to honour. It requires
deliberate configuration and a review confirmation.

## What is recorded

Provider (`ollama_local`), endpoint class, model identifier, image policy, batch
size, runtime version, timing, retry history, and the prompt and schema hashes.

No credential, because there is none.
