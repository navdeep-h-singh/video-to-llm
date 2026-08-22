"""Building a collection: Mode A (one document) and Mode B (context packs).

Both modes reuse existing `assembled.txt` files verbatim. Nothing is
re-extracted, re-transcribed, or re-described, and **no provider is ever
contacted** — a collection build is local, free, and takes seconds.

Mode A concatenates in exact collection order with strong boundaries.

Mode B cuts the same content into numbered parts that fit a context budget.
Whole videos are kept together by default: a model reading half a video with no
indication that it is half will summarise it as if it were whole. Splitting is
opt-in per build, cuts at section boundaries rather than mid-sentence, and
carries a small explicit overlap so a reader moving between parts keeps the
thread.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from app.collections.model import (
    Collection,
    CollectionMode,
    CollectionSource,
    collection_dir,
    next_version,
)
from app.collections.tokens import (
    ESTIMATION_METHOD,
    ESTIMATION_VERSION,
    PACK_OVERHEAD_TOKENS,
    TokenEstimate,
    estimate_tokens,
)
from app.core.artifacts import relative_to_root, sha256_file, write_json, write_text
from app.core.db import new_id, utc_now
from app.core.logging import get_logger

logger = get_logger(__name__)

FULL_FILENAME = "collection_assembled.txt"
MANIFEST_FILENAME = "collection_manifest.json"
README_FILENAME = "collection_readme.md"
PACK_MANIFEST_FILENAME = "collection-pack-manifest.json"

PACKING_ALGORITHM = "whole-video-first, split at section boundaries when permitted"
PACKING_VERSION = 1

#: Repeated at the head of a continuation part so a reader picking it up mid-video
#: has the immediately preceding context.
OVERLAP_CHARACTERS = 2000


class CollectionBuildError(RuntimeError):
    pass


@dataclass
class LoadedSource:
    source: CollectionSource
    text: str
    frames_dir: Path | None = None

    @property
    def estimate(self) -> TokenEstimate:
        return estimate_tokens(self.text)


@dataclass
class Pack:
    number: int
    videos: list[str] = field(default_factory=list)
    text: str = ""
    token_estimate: int = 0
    boundaries: list[dict] = field(default_factory=list)
    note: str = ""

    @property
    def filename(self) -> str:
        return f"collection-pack-{self.number:03d}.md"


@dataclass
class BuildResult:
    directory: Path
    version: int
    mode: str
    files: list[Path] = field(default_factory=list)
    packs: list[Pack] = field(default_factory=list)
    total_tokens: int = 0
    warnings: list[str] = field(default_factory=list)
    manifest_sha256: str = ""

    @property
    def pack_count(self) -> int:
        return len(self.packs)


def format_duration(seconds: float) -> str:
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def load_sources(
    collection: Collection, output_root: Path, *, imported_dirs: dict | None = None
) -> list[LoadedSource]:
    """Read each source's assembled text, in collection order."""
    loaded: list[LoadedSource] = []

    for source in sorted(collection.sources, key=lambda s: s.sequence):
        directory: Path | None = None
        if source.output_dir:
            directory = Path(output_root) / source.output_dir
        elif imported_dirs and source.job_video_id in imported_dirs:
            directory = Path(imported_dirs[source.job_video_id])

        if directory is None or not (directory / "assembled.txt").is_file():
            logger.warning(
                "%s has no assembled document; it will be noted as missing",
                source.display_name,
            )
            loaded.append(
                LoadedSource(
                    source=source,
                    text=(
                        f"[{source.display_name} could not be included: its "
                        "assembled document was not found.]\n"
                    ),
                )
            )
            continue

        frames = directory / "frames"
        loaded.append(
            LoadedSource(
                source=source,
                text=(directory / "assembled.txt").read_text(encoding="utf-8"),
                frames_dir=frames if frames.is_dir() else None,
            )
        )

    return loaded


# ── Mode A: one document ──────────────────────────────────────────────────


def build_full_document(collection: Collection, sources: list[LoadedSource]) -> str:
    """Concatenate in exact collection order, with strong boundaries."""
    total_duration = sum(s.source.duration_seconds for s in sources)

    lines = [
        "=" * 72,
        collection.name,
        "=" * 72,
    ]
    if collection.description:
        lines.append(collection.description)
        lines.append("")
    lines += [
        f"Videos            {len(sources)}",
        f"Total length      {format_duration(total_duration)}",
        f"Warnings          {collection.warning_count}",
        "",
        "The videos appear in the order you set. Each is wrapped in a clear",
        "boundary so a reader knows where one ends and the next begins.",
        "",
    ]

    for position, loaded in enumerate(sources):
        source = loaded.source
        lines.append("")
        lines.append(
            f'<video sequence="{position + 1}" '
            f'source_video_id="{source.job_video_id}" '
            f'processed_version="{source.source_version}">'
        )
        lines.append(f"  <title>{source.display_name}</title>")
        lines.append(f"  <duration>{format_duration(source.duration_seconds)}</duration>")
        if source.has_warning:
            lines.append(f"  <note>{source.warning_label}</note>")
        lines.append("")
        lines.append(loaded.text.rstrip("\n"))
        lines.append("</video>")
        lines.append("")

    return "\n".join(lines) + "\n"


# ── Mode B: context packs ─────────────────────────────────────────────────


def split_at_sections(text: str, budget_chars: int) -> list[str]:
    """Cut *text* into chunks no larger than *budget_chars*, at section breaks.

    Section headings written by assembly start with the box-drawing rule, so
    they are the natural seams. Falling back to paragraph breaks, then to a hard
    cut, guarantees progress — a chunk that cannot be split would otherwise loop
    forever.
    """
    if len(text) <= budget_chars:
        return [text]

    chunks: list[str] = []
    remaining = text

    while len(remaining) > budget_chars:
        window = remaining[:budget_chars]

        cut = window.rfind("\n── ")
        if cut <= 0:
            cut = window.rfind("\n\n")
        if cut <= 0:
            # No natural seam. Cut at the last line break so we never split a
            # line in half; failing that, cut hard so the loop terminates.
            cut = window.rfind("\n")
        if cut <= 0:
            cut = budget_chars

        chunks.append(remaining[:cut])
        remaining = remaining[cut:]

    if remaining.strip():
        chunks.append(remaining)

    return chunks


def build_packs(
    collection: Collection,
    sources: list[LoadedSource],
    *,
    budget_tokens: int,
    allow_split: bool = False,
) -> tuple[list[Pack], list[str]]:
    """Pack the sources into parts that fit *budget_tokens*."""
    if budget_tokens <= 0:
        raise CollectionBuildError(
            "The usable size is zero or less. Raise the model's limit or lower "
            "the amount you are holding back for the prompt."
        )

    warnings: list[str] = []
    packs: list[Pack] = []
    current = Pack(number=1)

    def flush() -> None:
        nonlocal current
        if current.text.strip():
            packs.append(current)
            current = Pack(number=len(packs) + 1)

    budget_chars = int(budget_tokens * 3.6)

    for position, loaded in enumerate(sources):
        source = loaded.source
        header = (
            f'\n<video sequence="{position + 1}" '
            f'source_video_id="{source.job_video_id}" '
            f'processed_version="{source.source_version}">\n'
            f"  <title>{source.display_name}</title>\n"
            f"  <duration>{format_duration(source.duration_seconds)}</duration>\n\n"
        )
        footer = "\n</video>\n"
        whole = header + loaded.text.rstrip("\n") + footer
        whole_tokens = estimate_tokens(whole).tokens + PACK_OVERHEAD_TOKENS

        if whole_tokens <= budget_tokens:
            if current.token_estimate + whole_tokens > budget_tokens:
                # Prefer a boundary between videos.
                flush()
            current.text += whole
            current.token_estimate += whole_tokens
            current.videos.append(source.display_name)
            current.boundaries.append(
                {
                    "sequence": position + 1,
                    "display_name": source.display_name,
                    "source_video_id": source.job_video_id,
                    "processed_version": source.source_version,
                    "split": False,
                    "part": None,
                }
            )
            continue

        # The video alone is bigger than a whole pack.
        if not allow_split:
            warnings.append(
                f"{source.display_name} is too big to fit in one part "
                f"(about {whole_tokens:,} tokens against a budget of "
                f"{budget_tokens:,}). It has been placed in a part of its own, "
                "which will be over the limit. Allow splitting, or raise the "
                "limit, to avoid this."
            )
            flush()
            current.text = whole
            current.token_estimate = whole_tokens
            current.videos.append(source.display_name)
            current.boundaries.append(
                {
                    "sequence": position + 1,
                    "display_name": source.display_name,
                    "source_video_id": source.job_video_id,
                    "processed_version": source.source_version,
                    "split": False,
                    "oversized": True,
                    "part": None,
                }
            )
            flush()
            continue

        flush()
        chunks = split_at_sections(loaded.text, budget_chars - len(header) - 4000)
        previous_tail = ""

        for part_number, chunk in enumerate(chunks, start=1):
            part_header = header.replace(">\n", f' part="{part_number}" of="{len(chunks)}">\n', 1)
            body = chunk
            if previous_tail:
                body = (
                    "  <continues_from_previous_part>\n"
                    f"{previous_tail}\n"
                    "  </continues_from_previous_part>\n\n" + chunk
                )

            piece = part_header + body.rstrip("\n") + footer
            current.text = piece
            current.token_estimate = estimate_tokens(piece).tokens + PACK_OVERHEAD_TOKENS
            current.videos.append(f"{source.display_name} (part {part_number})")
            current.boundaries.append(
                {
                    "sequence": position + 1,
                    "display_name": source.display_name,
                    "source_video_id": source.job_video_id,
                    "processed_version": source.source_version,
                    "split": True,
                    "part": part_number,
                    "of": len(chunks),
                    "overlap_characters": len(previous_tail),
                }
            )
            current.note = (
                f"{source.display_name} was too long for one part, so it was cut "
                "at a natural break. The opening repeats the end of the previous "
                "part on purpose."
            )
            flush()
            previous_tail = chunk[-OVERLAP_CHARACTERS:]

        warnings.append(
            f"{source.display_name} was split across {len(chunks)} parts at "
            "section boundaries, with an overlap between them."
        )

    flush()
    return packs, warnings


# ── What a build would produce, without producing it ──────────────────────


@dataclass(frozen=True)
class BuildPreview:
    """The answer to "what do I get if I press build", computed before pressing.

    Every number comes from the same functions the build itself calls. A second
    estimator — in the browser, say, where it would be quicker — would be a
    second implementation of the packing rule, and the moment the two disagreed
    the preview would be telling the user something the build then contradicted.
    """

    video_count: int
    total_duration_seconds: float
    total_tokens: int
    #: ``None`` in one-document mode, where there are no parts to count.
    pack_count: int | None
    warnings: list[str]
    problem: str = ""

    @property
    def duration_label(self) -> str:
        return format_duration(self.total_duration_seconds)

    @property
    def token_label(self) -> str:
        return f"about {self.total_tokens:,} tokens"


def preview_build(
    collection: Collection,
    *,
    output_root: Path,
    imported_dirs: dict | None = None,
) -> BuildPreview:
    """Run the build's reading and packing, and write nothing."""
    sources = load_sources(collection, output_root, imported_dirs=imported_dirs)

    warnings = [
        f"{s.source.display_name}: {s.source.warning_detail}"
        for s in sources
        if s.source.has_warning and s.source.warning_detail
    ]
    duration = sum(s.source.duration_seconds for s in sources)

    if collection.mode == CollectionMode.FULL:
        content = build_full_document(collection, sources)
        return BuildPreview(
            video_count=len(sources),
            total_duration_seconds=duration,
            total_tokens=estimate_tokens(content).tokens,
            pack_count=None,
            warnings=warnings,
        )

    try:
        packs, pack_warnings = build_packs(
            collection,
            sources,
            budget_tokens=collection.usable_budget,
            allow_split=collection.allow_video_split,
        )
    except CollectionBuildError as error:
        # A preview reports an unbuildable setting rather than raising: the user
        # is still filling the form in, and the point of showing this before the
        # build is to let them fix it without a failed attempt first.
        return BuildPreview(
            video_count=len(sources),
            total_duration_seconds=duration,
            total_tokens=0,
            pack_count=None,
            warnings=warnings,
            problem=str(error),
        )

    return BuildPreview(
        video_count=len(sources),
        total_duration_seconds=duration,
        total_tokens=sum(p.token_estimate for p in packs),
        pack_count=len(packs),
        warnings=warnings + pack_warnings,
    )


def render_pack(collection: Collection, pack: Pack, total_packs: int) -> str:
    """One pack as a self-describing markdown document."""
    lines = [
        f"# {collection.name} — part {pack.number} of {total_packs}",
        "",
        f"- **Contains:** {', '.join(pack.videos) if pack.videos else 'index only'}",
        f"- **Size:** about {pack.token_estimate:,} tokens (an estimate)",
    ]
    if collection.target_model_label:
        lines.append(f"- **Sized for:** {collection.target_model_label}")
    if pack.note:
        lines.append(f"- **Note:** {pack.note}")
    lines += [
        "",
        "Read the parts in order. Each is a slice of one collection, and the",
        "boundaries below say exactly which video and which version each piece",
        "came from.",
        "",
        "---",
        "",
        pack.text.strip(),
        "",
    ]
    return "\n".join(lines)


# ── Readme and manifest ───────────────────────────────────────────────────


def build_readme(
    collection: Collection,
    sources: list[LoadedSource],
    *,
    mode: str,
    packs: list[Pack],
    total_tokens: int,
    warnings: list[str],
) -> str:
    total_duration = sum(s.source.duration_seconds for s in sources)

    lines = [
        f"# {collection.name}",
        "",
    ]
    if collection.description:
        lines += [collection.description, ""]

    lines += [
        f"- **Videos:** {len(sources)}",
        f"- **Total length:** {format_duration(total_duration)}",
        f"- **Size:** about {total_tokens:,} tokens (an estimate)",
        "",
        "Built on this computer from work that was already done. Nothing was",
        "sent anywhere, nothing was processed again, and there is no charge.",
        "",
        "## The videos, in order",
        "",
    ]

    for position, loaded in enumerate(sources):
        source = loaded.source
        marker = "" if not source.has_warning else f" — **{source.warning_label}**"
        lines.append(
            f"{position + 1}. **{source.display_name}** "
            f"({format_duration(source.duration_seconds)}, "
            f"version {source.source_version}){marker}"
        )
        if source.warning_detail:
            lines.append(f"   - {source.warning_detail}")

    lines.append("")

    if mode == CollectionMode.PACKS:
        lines += [
            "## The parts",
            "",
            "Read them in order.",
            "",
        ]
        for pack in packs:
            lines.append(
                f"- `{pack.filename}` — {', '.join(pack.videos) or 'index'} "
                f"(about {pack.token_estimate:,} tokens)"
            )
        lines.append("")
    else:
        lines += [
            "## The document",
            "",
            f"`{FULL_FILENAME}` holds every video, one after another, in the order",
            "above.",
            "",
        ]

    if warnings:
        lines += ["## Worth knowing", ""]
        lines += [f"- {w}" for w in warnings]
        lines.append("")

    lines += [
        "## About the sizes",
        "",
        "Token counts are estimates. The exact number depends on the model you",
        "use, so treat them as a guide rather than a guarantee.",
        "",
        "## Versions",
        "",
        "Each video above is pinned to the exact version of its output that",
        "existed when this collection was built. Processing a video again later",
        "creates a new version and leaves this collection unchanged. To use newer",
        "output, rebuild this collection or make a new one.",
        "",
    ]

    return "\n".join(lines)


def build_collection(
    connection: sqlite3.Connection,
    collection: Collection,
    *,
    output_root: Path,
    imported_dirs: dict | None = None,
) -> BuildResult:
    """Build a collection. Local, free, and makes no provider calls."""
    if not collection.sources:
        raise CollectionBuildError("This collection has no videos in it yet.")

    version = next_version(connection, collection.id)
    directory = collection_dir(output_root, collection.id, version)
    directory.mkdir(parents=True, exist_ok=True)

    build_id = new_id()
    connection.execute(
        "INSERT INTO collection_builds (id, collection_id, collection_version, mode,"
        " status, output_dir, token_method, packing_algorithm, packing_version,"
        " created_at) VALUES (?,?,?,?,'running',?,?,?,?,?)",
        (
            build_id,
            collection.id,
            version,
            str(collection.mode),
            relative_to_root(directory, output_root),
            ESTIMATION_METHOD,
            PACKING_ALGORITHM,
            PACKING_VERSION,
            utc_now(),
        ),
    )

    sources = load_sources(collection, output_root, imported_dirs=imported_dirs)
    result = BuildResult(directory=directory, version=version, mode=str(collection.mode))

    result.warnings.extend(
        f"{s.source.display_name}: {s.source.warning_detail}"
        for s in sources
        if s.source.has_warning and s.source.warning_detail
    )

    if collection.mode == CollectionMode.FULL:
        content = build_full_document(collection, sources)
        path = directory / FULL_FILENAME
        write_text(path, content)
        result.files.append(path)
        result.total_tokens = estimate_tokens(content).tokens
    else:
        packs, pack_warnings = build_packs(
            collection,
            sources,
            budget_tokens=collection.usable_budget,
            allow_split=collection.allow_video_split,
        )
        result.packs = packs
        result.warnings.extend(pack_warnings)

        for pack in packs:
            path = directory / pack.filename
            write_text(path, render_pack(collection, pack, len(packs)))
            result.files.append(path)
        result.total_tokens = sum(p.token_estimate for p in packs)

        pack_manifest = directory / PACK_MANIFEST_FILENAME
        write_json(
            pack_manifest,
            {
                "version": 1,
                "collection_id": collection.id,
                "collection_version": version,
                "token_limit": collection.token_limit,
                "reserve_tokens": collection.reserve_tokens,
                "usable_budget": collection.usable_budget,
                "target_model_label": collection.target_model_label,
                "allow_video_split": collection.allow_video_split,
                "packing_algorithm": PACKING_ALGORITHM,
                "packing_version": PACKING_VERSION,
                "token_method": ESTIMATION_METHOD,
                "token_method_version": ESTIMATION_VERSION,
                "pack_count": len(packs),
                "packs": [
                    {
                        "number": p.number,
                        "filename": p.filename,
                        "videos": p.videos,
                        "token_estimate": p.token_estimate,
                        "boundaries": p.boundaries,
                        "note": p.note,
                    }
                    for p in packs
                ],
            },
        )
        result.files.append(pack_manifest)

    readme = directory / README_FILENAME
    write_text(
        readme,
        build_readme(
            collection,
            sources,
            mode=str(collection.mode),
            packs=result.packs,
            total_tokens=result.total_tokens,
            warnings=result.warnings,
        ),
    )
    result.files.append(readme)

    manifest = directory / MANIFEST_FILENAME
    checksums = {path.name: sha256_file(path) for path in result.files if path.is_file()}
    result.manifest_sha256 = write_json(
        manifest,
        {
            "version": 1,
            "collection_id": collection.id,
            "collection_version": version,
            "name": collection.name,
            "description": collection.description,
            "mode": str(collection.mode),
            "built_at": utc_now(),
            "video_count": len(sources),
            "total_duration_seconds": round(sum(s.source.duration_seconds for s in sources), 3),
            "total_tokens_estimate": result.total_tokens,
            "token_method": ESTIMATION_METHOD,
            "token_method_version": ESTIMATION_VERSION,
            "token_limit": collection.token_limit,
            "reserve_tokens": collection.reserve_tokens,
            "usable_budget": collection.usable_budget,
            "allow_video_split": collection.allow_video_split,
            "packing_algorithm": PACKING_ALGORITHM if result.packs else None,
            "packing_version": PACKING_VERSION if result.packs else None,
            "warning_count": len(result.warnings),
            "warnings": result.warnings,
            "sources": [
                {
                    "sequence": position + 1,
                    "job_video_id": s.source.job_video_id,
                    "display_name": s.source.display_name,
                    "processed_version": s.source.source_version,
                    "duration_seconds": s.source.duration_seconds,
                    "assembled_sha256": s.source.assembled_sha256,
                    "warning_state": s.source.warning_state,
                    "warning_detail": s.source.warning_detail,
                }
                for position, s in enumerate(sources)
            ],
            "output_checksums": checksums,
        },
    )
    result.files.append(manifest)

    connection.execute(
        "UPDATE collection_builds SET status = 'completed', pack_count = ?,"
        " total_tokens_est = ?, warning_count = ?, manifest_sha256 = ?,"
        " completed_at = ? WHERE id = ?",
        (
            len(result.packs) or None,
            result.total_tokens,
            len(result.warnings),
            result.manifest_sha256,
            utc_now(),
            build_id,
        ),
    )
    connection.execute(
        "UPDATE collections SET current_version = ?, updated_at = ? WHERE id = ?",
        (version, utc_now(), collection.id),
    )

    logger.info(
        "Built %s version %d: %d file(s), about %d tokens",
        collection.name,
        version,
        len(result.files),
        result.total_tokens,
    )
    return result
