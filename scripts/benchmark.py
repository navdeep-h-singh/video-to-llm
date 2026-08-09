#!/usr/bin/env python3
"""Measure what it costs to give a model a video, four ways.

The number in `HANDOFF.md` §10 — an 85% saving against sending frames — is
real and comes from one video. One chart screencast is close to the best case
for this tool: dense speech, a mostly static screen, and 1,488 frames that
compress to a small document. Publishing that figure as a general claim on the
strength of n=1 would be the kind of marketing this project has otherwise
avoided.

This runs the same arithmetic over as many videos as you give it and prints a
table you can publish, including the spread. Run it on videos of genuinely
different kinds — a lecture, a screencast, an interview, a conference talk, a
tutorial — because the whole point is to find out how much the answer moves.

    uv run python scripts/benchmark.py --output-root /tmp/bench \\
        lecture.mp4 interview.mp4 screencast.mp4

It processes each video without descriptions by default, because descriptions
run at roughly 31 s/picture locally and the token comparison does not need them.
Pass --describe local to include them, and expect it to take hours.

Nothing here is used by the application. It is a measurement tool, and it writes
its raw results as JSON so the numbers behind a published claim can be checked.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.collections.tokens import CHARS_PER_TOKEN, estimate_tokens  # noqa: E402
from app.core.config import load_settings  # noqa: E402

#: Gemini-style native video: one frame per second plus an audio track. Both
#: figures are Google's published rates for their own encoding, and they are
#: what makes this row not like-for-like — this app samples every two seconds by
#: default, so it is reading half as many pictures. Quoted without that caveat
#: the comparison flatters us.
NATIVE_VIDEO_TOKENS_PER_SECOND = 258 + 32

#: A 768px image on a tile-based vision model. Frames are extracted at 1280x720
#: and the provider copies at 768 wide, which is what would actually be sent.
TOKENS_PER_IMAGE = 1_393


@dataclass
class Measurement:
    name: str
    duration_seconds: float
    frame_count: int
    document_characters: int
    transcript_characters: int
    described: int

    wall_clock_seconds: float

    @property
    def assembled_tokens(self) -> int:
        return round(self.document_characters / CHARS_PER_TOKEN)

    @property
    def transcript_tokens(self) -> int:
        return round(self.transcript_characters / CHARS_PER_TOKEN)

    @property
    def native_video_tokens(self) -> int:
        return round(self.duration_seconds * NATIVE_VIDEO_TOKENS_PER_SECOND)

    @property
    def frames_tokens(self) -> int:
        return self.frame_count * TOKENS_PER_IMAGE

    @property
    def saving_against_frames(self) -> float:
        """The honest headline: against sending the frames yourself.

        This is the realistic alternative for Claude and OpenAI, neither of
        which accepts video at all.
        """
        if not self.frames_tokens:
            return 0.0
        return 1.0 - (self.assembled_tokens / self.frames_tokens)

    @property
    def saving_against_native(self) -> float:
        if not self.native_video_tokens:
            return 0.0
        return 1.0 - (self.assembled_tokens / self.native_video_tokens)


def measure(video: Path, *, output_root: Path, interval: float, describe: str) -> Measurement:
    from app.pipeline.frames import MANIFEST_FILENAME
    from app.pipeline.transcribe import TRANSCRIPT_FILENAME
    from app.services.headless import process_videos

    settings = load_settings().with_output_root(output_root)
    provider = "ollama_local" if describe == "local" else "none"

    started = time.monotonic()
    result = process_videos(
        settings,
        paths=[video],
        name=video.stem,
        interval_ms=round(interval * 1000),
        provider=provider,
        model_id=settings.visual_analysis.model_for(provider) if provider != "none" else "",
    )
    elapsed = time.monotonic() - started

    if not result.documents:
        raise RuntimeError(f"{video.name}: {'; '.join(result.problems) or 'produced no document'}")

    document = result.documents[0]
    folder = document.parent

    manifest = json.loads((folder / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    transcript_path = folder / TRANSCRIPT_FILENAME
    transcript_text = ""
    if transcript_path.exists():
        payload = json.loads(transcript_path.read_text(encoding="utf-8"))
        transcript_text = "\n".join(s.get("text", "") for s in payload.get("segments", []))

    described = 0
    visual = folder / "visual_results.json"
    if visual.exists():
        described = len(json.loads(visual.read_text(encoding="utf-8")).get("descriptions", []))

    return Measurement(
        name=video.name,
        duration_seconds=float(manifest.get("duration_seconds") or 0.0),
        frame_count=int(manifest.get("frame_count") or 0),
        document_characters=len(document.read_text(encoding="utf-8")),
        transcript_characters=len(transcript_text),
        described=described,
        wall_clock_seconds=elapsed,
    )


def render(measurements: list[Measurement]) -> str:
    lines: list[str] = []
    lines.append("")
    lines.append(
        f"{'video':28} {'length':>9} {'frames':>8} {'native':>11} "
        f"{'as frames':>11} {'this app':>10} {'vs frames':>10}"
    )
    lines.append("-" * 94)
    for m in measurements:
        length = f"{int(m.duration_seconds) // 60}:{int(m.duration_seconds) % 60:02d}"
        lines.append(
            f"{m.name[:28]:28} {length:>9} {m.frame_count:>8,} "
            f"{m.native_video_tokens:>11,} {m.frames_tokens:>11,} "
            f"{m.assembled_tokens:>10,} {m.saving_against_frames:>9.0%}"
        )

    if measurements:
        savings = sorted(m.saving_against_frames for m in measurements)
        median = savings[len(savings) // 2]
        lines.append("-" * 94)
        lines.append(
            f"n={len(measurements)}  median saving vs frames {median:.0%}  "
            f"range {savings[0]:.0%}–{savings[-1]:.0%}"
        )
        lines.append("")
        lines.append("The 'native' column samples 1 fps; this app sampled every")
        lines.append("--interval seconds. That comparison is not like-for-like and")
        lines.append("should not be quoted without the caveat. 'vs frames' is the")
        lines.append("honest headline: it is what Claude and OpenAI would actually cost,")
        lines.append("since neither accepts video.")
        lines.append("")
        lines.append("Token figures are estimates (characters/3.6), not real")
        lines.append("tokenisation. Wall clock is this machine only.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("videos", nargs="+", type=Path)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--describe", choices=("none", "local"), default="none")
    parser.add_argument("--json", type=Path, default=None, help="Write raw results here")
    args = parser.parse_args()

    measurements: list[Measurement] = []
    for video in args.videos:
        if not video.exists():
            print(f"skipping {video}: no such file", file=sys.stderr)
            continue
        print(f"processing {video.name} …", file=sys.stderr)
        try:
            measurements.append(
                measure(
                    video,
                    output_root=args.output_root,
                    interval=args.interval,
                    describe=args.describe,
                )
            )
        except Exception as error:  # noqa: BLE001 - a measurement tool, keep going
            print(f"  failed: {error}", file=sys.stderr)

    if not measurements:
        print("Nothing measured.", file=sys.stderr)
        return 1

    print(render(measurements))

    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "chars_per_token": CHARS_PER_TOKEN,
                    "tokens_per_image": TOKENS_PER_IMAGE,
                    "native_tokens_per_second": NATIVE_VIDEO_TOKENS_PER_SECOND,
                    "estimation": estimate_tokens("").method,
                    "measurements": [
                        {
                            **asdict(m),
                            "assembled_tokens": m.assembled_tokens,
                            "transcript_tokens": m.transcript_tokens,
                            "native_video_tokens": m.native_video_tokens,
                            "frames_tokens": m.frames_tokens,
                            "saving_against_frames": round(m.saving_against_frames, 4),
                        }
                        for m in measurements
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nRaw results: {args.json}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
