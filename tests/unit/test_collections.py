"""Collections.

Three properties carry the feature:

1. **Source versions are immutable.** Reprocessing a video must not change an
   existing collection — a citation that silently changes is worse than none.
2. **No provider is ever contacted.** Building is local, free, and instant.
3. **Whole videos stay together** unless the user permits a split, because a
   model reading half a video with no indication of that will summarise it as
   though it were whole.
"""

from __future__ import annotations

import json

import pytest

from app.collections.build import (
    FULL_FILENAME,
    MANIFEST_FILENAME,
    PACK_MANIFEST_FILENAME,
    README_FILENAME,
    CollectionBuildError,
    build_collection,
    load_sources,
    split_at_sections,
)
from app.collections.model import (
    CollectionMode,
    WarningState,
    assess_source,
    available_sources,
    collection_dir,
    create_collection,
    list_collections,
    load_collection,
    next_version,
    set_sources,
)
from app.collections.tokens import (
    CHARS_PER_TOKEN,
    estimate_tokens,
    fits,
)
from app.core.db import open_database, utc_now


@pytest.fixture
def db(tmp_path):
    connection = open_database(tmp_path / "out")
    connection.execute(
        "INSERT INTO jobs (id, name, status, output_root, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?)",
        ("j1", "Source job", "completed", str(tmp_path / "out"), utc_now(), utc_now()),
    )
    yield connection
    connection.close()


@pytest.fixture
def root(tmp_path):
    return tmp_path / "out"


def add_video(
    connection,
    root,
    video_id,
    *,
    name=None,
    sequence=0,
    version=1,
    status="completed",
    body="assembled content",
    frames=True,
    descriptions=True,
    old_schema=False,
    duration=600.0,
):
    """Register a processed video and write its output."""
    name = name or f"{video_id}.mp4"
    directory = root / "j1" / video_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "assembled.txt").write_text(body + "\n", "utf-8")

    if frames:
        frame_dir = directory / "frames"
        frame_dir.mkdir(exist_ok=True)
        (frame_dir / "000000_t000000.jpg").write_bytes(b"\xff\xd8")

    if descriptions:
        from app.providers.base import schema_hash

        (directory / "visual_results.json").write_text(
            json.dumps(
                {
                    "descriptions": [
                        {
                            "index": 0,
                            "schema_hash": "old00000000000" if old_schema else schema_hash(),
                        }
                    ]
                }
            ),
            "utf-8",
        )

    connection.execute(
        "INSERT INTO job_videos (id, job_id, source_path, display_name, sequence,"
        " version, is_active_version, status, duration_seconds, output_dir,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            video_id,
            "j1",
            f"/src/{name}",
            name,
            sequence,
            version,
            1,
            status,
            duration,
            f"j1/{video_id}",
            utc_now(),
            utc_now(),
        ),
    )
    return directory


def make_collection(connection, root, video_ids, *, mode=CollectionMode.FULL, **kwargs):
    collection_id = create_collection(connection, name="Week 6", mode=mode, **kwargs)
    sources = [assess_source(connection, vid, root) for vid in video_ids]
    set_sources(connection, collection_id, [s for s in sources if s])
    return load_collection(connection, collection_id)


# ── Token estimation ──────────────────────────────────────────────────────


def test_estimation_scales_with_length():
    short = estimate_tokens("a" * 360)
    long = estimate_tokens("a" * 3600)
    assert long.tokens == pytest.approx(short.tokens * 10, rel=0.01)


def test_estimation_uses_the_documented_ratio():
    assert estimate_tokens("x" * 3600).tokens == round(3600 / CHARS_PER_TOKEN)


def test_estimates_are_labelled_as_estimates():
    estimate = estimate_tokens("some text")
    assert "about" in estimate.label
    assert "estimate" in estimate.disclaimer.lower()
    assert "guarantee" in estimate.disclaimer.lower()


def test_empty_text_estimates_zero():
    assert estimate_tokens("").tokens == 0


def test_fits_compares_against_a_budget():
    assert fits(estimate_tokens("x" * 360), 200) is True
    assert fits(estimate_tokens("x" * 36000), 200) is False


# ── Creating and ordering ─────────────────────────────────────────────────


def test_a_collection_is_created_and_loaded(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])

    assert collection.name == "Week 6"
    assert len(collection.sources) == 1
    assert collection.sources[0].display_name == "v1.mp4"


def test_a_collection_needs_a_name(db):
    with pytest.raises(ValueError, match="needs a name"):
        create_collection(db, name="   ")


def test_the_order_is_the_one_given_not_alphabetical(db, root):
    # Order comes from the user. Two recordings from the same morning have no
    # inherent sequence, and guessing wrong reverses the narrative.
    add_video(db, root, "zulu", name="zulu.mp4")
    add_video(db, root, "alpha", name="alpha.mp4", sequence=1)

    collection = make_collection(db, root, ["zulu", "alpha"])
    assert [s.display_name for s in collection.sources] == ["zulu.mp4", "alpha.mp4"]


def test_reordering_replaces_the_sequence(db, root):
    add_video(db, root, "v1")
    add_video(db, root, "v2", sequence=1)
    collection = make_collection(db, root, ["v1", "v2"])

    reversed_sources = list(reversed(collection.sources))
    set_sources(db, collection.id, reversed_sources)

    reloaded = load_collection(db, collection.id)
    assert [s.display_name for s in reloaded.sources] == ["v2.mp4", "v1.mp4"]
    assert [s.sequence for s in reloaded.sources] == [0, 1]


def test_collections_are_listed(db, root):
    add_video(db, root, "v1")
    make_collection(db, root, ["v1"])
    assert len(list_collections(db)) == 1


def test_loading_an_unknown_collection_returns_nothing(db):
    assert load_collection(db, "no-such-collection") is None


# ── Warnings permit inclusion ─────────────────────────────────────────────


def test_a_complete_video_has_no_warning(db, root):
    add_video(db, root, "v1")
    assert assess_source(db, "v1", root).warning_state == WarningState.OK


def test_a_video_with_gaps_warns_but_is_included(db, root):
    add_video(db, root, "v1", status="completed_with_gaps")
    source = assess_source(db, "v1", root)

    assert source.warning_state == WarningState.GAPS
    assert source.has_warning is True
    assert source.warning_detail


def test_a_video_with_no_descriptions_warns_but_is_included(db, root):
    add_video(db, root, "v1", descriptions=False)
    assert assess_source(db, "v1", root).warning_state == WarningState.NO_VISUAL


def test_older_wording_warns_but_is_included(db, root):
    add_video(db, root, "v1", old_schema=True)
    assert assess_source(db, "v1", root).warning_state == WarningState.PROVENANCE_MISMATCH


def test_missing_pictures_warn_but_are_included(db, root):
    add_video(db, root, "v1", frames=False)
    assert assess_source(db, "v1", root).warning_state == WarningState.MISSING_ARTIFACTS


def test_every_warning_state_still_builds(db, root):
    # Not one of them blocks. The user is told and decides.
    add_video(db, root, "v1", status="completed_with_gaps")
    add_video(db, root, "v2", sequence=1, descriptions=False)
    add_video(db, root, "v3", sequence=2, old_schema=True)

    collection = make_collection(db, root, ["v1", "v2", "v3"])
    result = build_collection(db, collection, output_root=root)

    assert (result.directory / FULL_FILENAME).is_file()
    assert len(result.warnings) >= 3


def test_available_sources_lists_completed_videos(db, root):
    add_video(db, root, "v1")
    add_video(db, root, "v2", sequence=1, status="completed_with_gaps")
    assert len(available_sources(db, root)) == 2


# ── Immutable versions ────────────────────────────────────────────────────


def test_a_collection_pins_the_source_version(db, root):
    add_video(db, root, "v1", version=3)
    collection = make_collection(db, root, ["v1"])
    assert collection.sources[0].source_version == 3


def test_reprocessing_a_source_does_not_change_an_existing_collection(db, root):
    """The defining property.

    A collection is a citation of specific evidence. A citation that silently
    changes when its source is revised is worse than no citation at all.
    """
    add_video(db, root, "v1", version=1, body="the original content")
    collection = make_collection(db, root, ["v1"])
    first = build_collection(db, collection, output_root=root)
    original_text = (first.directory / FULL_FILENAME).read_text("utf-8")

    # A later reprocess: new version row, and the old one is no longer active.
    db.execute("UPDATE job_videos SET is_active_version = 0 WHERE id = 'v1'")
    add_video(db, root, "v1_v2", name="v1.mp4", version=2, body="revised content")

    reloaded = load_collection(db, collection.id)
    assert reloaded.sources[0].job_video_id == "v1"
    assert reloaded.sources[0].source_version == 1

    # And the built artifact on disk is untouched.
    assert (first.directory / FULL_FILENAME).read_text("utf-8") == original_text
    assert "revised content" not in original_text


def test_each_build_gets_its_own_version_directory(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])

    first = build_collection(db, collection, output_root=root)
    second = build_collection(db, load_collection(db, collection.id), output_root=root)

    assert first.version == 1
    assert second.version == 2
    assert first.directory != second.directory
    assert first.directory.is_dir(), "an earlier build must not be overwritten"


def test_build_output_lives_outside_any_video_directory(db, root):
    # Merging them would make a collection look like part of a video's archive,
    # and deleting one would silently damage the other.
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    result = build_collection(db, collection, output_root=root)

    assert "collections" in result.directory.parts
    assert "j1" not in result.directory.parts


def test_the_version_directory_is_predictable(tmp_path):
    assert collection_dir(tmp_path, "abc", 2).parts[-3:] == ("collections", "abc", "v2")


def test_next_version_counts_builds(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    assert next_version(db, collection.id) == 1
    build_collection(db, collection, output_root=root)
    assert next_version(db, collection.id) == 2


# ── No provider calls ─────────────────────────────────────────────────────


def test_building_never_contacts_a_provider(db, root, monkeypatch):
    """Collections are local, free, and instant, by construction."""

    def explode(*args, **kwargs):
        raise AssertionError("a collection build tried to contact a provider")

    monkeypatch.setattr("app.providers.cloud.build_provider", explode)
    monkeypatch.setattr("app.providers.cloud.CloudProvider.describe", explode)

    add_video(db, root, "v1")
    add_video(db, root, "v2", sequence=1)
    collection = make_collection(db, root, ["v1", "v2"])

    result = build_collection(db, collection, output_root=root)
    assert result.files


def test_building_reuses_the_existing_assembled_text(db, root):
    add_video(db, root, "v1", body="the exact original text")
    collection = make_collection(db, root, ["v1"])
    result = build_collection(db, collection, output_root=root)

    assert "the exact original text" in (result.directory / FULL_FILENAME).read_text("utf-8")


# ── Mode A: one document ──────────────────────────────────────────────────


def test_the_full_document_holds_every_video_in_order(db, root):
    for index, name in enumerate(["first", "second", "third"]):
        add_video(db, root, name, sequence=index, body=f"content of {name}")

    collection = make_collection(db, root, ["first", "second", "third"])
    result = build_collection(db, collection, output_root=root)
    content = (result.directory / FULL_FILENAME).read_text("utf-8")

    assert content.index("content of first") < content.index("content of second")
    assert content.index("content of second") < content.index("content of third")


def test_each_video_carries_its_boundary_and_provenance(db, root):
    add_video(db, root, "v1", version=2)
    collection = make_collection(db, root, ["v1"])
    content = (
        build_collection(db, collection, output_root=root).directory / FULL_FILENAME
    ).read_text("utf-8")

    assert '<video sequence="1"' in content
    assert 'source_video_id="v1"' in content
    assert 'processed_version="2"' in content
    assert "</video>" in content


def test_the_full_build_writes_a_manifest_and_readme(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    result = build_collection(db, collection, output_root=root)

    assert (result.directory / MANIFEST_FILENAME).is_file()
    assert (result.directory / README_FILENAME).is_file()


def test_the_manifest_records_checksums_for_every_output(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    result = build_collection(db, collection, output_root=root)

    manifest = json.loads((result.directory / MANIFEST_FILENAME).read_text("utf-8"))
    assert manifest["output_checksums"]
    assert FULL_FILENAME in manifest["output_checksums"]
    assert manifest["sources"][0]["assembled_sha256"]


def test_the_manifest_records_the_token_method(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    result = build_collection(db, collection, output_root=root)

    manifest = json.loads((result.directory / MANIFEST_FILENAME).read_text("utf-8"))
    assert manifest["token_method"]
    assert manifest["token_method_version"] == 1


def test_the_readme_explains_that_versions_are_pinned(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    result = build_collection(db, collection, output_root=root)

    readme = (result.directory / README_FILENAME).read_text("utf-8")
    assert "leaves this collection unchanged" in readme
    assert "estimate" in readme.lower()


def test_an_empty_collection_cannot_be_built(db, root):
    collection_id = create_collection(db, name="Empty")
    with pytest.raises(CollectionBuildError, match="no videos"):
        build_collection(db, load_collection(db, collection_id), output_root=root)


def test_a_source_whose_document_vanished_is_noted_not_fatal(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    (root / "j1" / "v1" / "assembled.txt").unlink()

    result = build_collection(db, collection, output_root=root)
    content = (result.directory / FULL_FILENAME).read_text("utf-8")
    assert "could not be included" in content


# ── Mode B: context packs ─────────────────────────────────────────────────


def test_packs_keep_whole_videos_together(db, root):
    # A model reading half a video with no indication of that will summarise it
    # as though it were whole.
    for index in range(3):
        add_video(db, root, f"v{index}", sequence=index, body="x" * 3600)

    collection = make_collection(
        db,
        root,
        ["v0", "v1", "v2"],
        mode=CollectionMode.PACKS,
        token_limit=3000,
        reserve_tokens=500,
    )
    result = build_collection(db, collection, output_root=root)

    for pack in result.packs:
        assert not any("part" in video for video in pack.videos)


def test_several_small_videos_share_one_pack(db, root):
    for index in range(3):
        add_video(db, root, f"v{index}", sequence=index, body="x" * 360)

    collection = make_collection(
        db,
        root,
        ["v0", "v1", "v2"],
        mode=CollectionMode.PACKS,
        token_limit=100_000,
        reserve_tokens=1000,
    )
    result = build_collection(db, collection, output_root=root)
    assert result.pack_count == 1
    assert len(result.packs[0].videos) == 3


def test_packs_break_between_videos_when_the_budget_runs_out(db, root):
    for index in range(4):
        add_video(db, root, f"v{index}", sequence=index, body="x" * 7200)

    collection = make_collection(
        db,
        root,
        ["v0", "v1", "v2", "v3"],
        mode=CollectionMode.PACKS,
        token_limit=5000,
        reserve_tokens=500,
    )
    result = build_collection(db, collection, output_root=root)
    assert result.pack_count > 1


def test_an_oversized_video_is_not_split_without_permission(db, root):
    add_video(db, root, "big", body="x" * 100_000)
    collection = make_collection(
        db,
        root,
        ["big"],
        mode=CollectionMode.PACKS,
        token_limit=2000,
        reserve_tokens=200,
        allow_video_split=False,
    )
    result = build_collection(db, collection, output_root=root)

    assert result.pack_count == 1
    assert any("too big" in w for w in result.warnings)
    assert any("Allow splitting" in w for w in result.warnings)


def test_an_oversized_video_is_split_when_permitted(db, root):
    body = "\n\n".join(f"── section {i} ──\n" + ("x" * 4000) for i in range(10))
    add_video(db, root, "big", body=body)

    collection = make_collection(
        db,
        root,
        ["big"],
        mode=CollectionMode.PACKS,
        token_limit=6000,
        reserve_tokens=500,
        allow_video_split=True,
    )
    result = build_collection(db, collection, output_root=root)

    assert result.pack_count > 1
    assert any("part" in v for pack in result.packs for v in pack.videos)
    assert any("split across" in w for w in result.warnings)


def test_a_split_records_its_overlap_and_provenance(db, root):
    body = "\n\n".join(f"── section {i} ──\n" + ("x" * 4000) for i in range(10))
    add_video(db, root, "big", body=body)

    collection = make_collection(
        db,
        root,
        ["big"],
        mode=CollectionMode.PACKS,
        token_limit=6000,
        reserve_tokens=500,
        allow_video_split=True,
    )
    result = build_collection(db, collection, output_root=root)

    manifest = json.loads((result.directory / PACK_MANIFEST_FILENAME).read_text("utf-8"))
    split_boundaries = [b for pack in manifest["packs"] for b in pack["boundaries"] if b["split"]]
    assert split_boundaries
    assert split_boundaries[0]["source_video_id"] == "big"
    assert split_boundaries[0]["processed_version"] == 1
    assert any(b.get("overlap_characters", 0) > 0 for b in split_boundaries[1:])


def test_a_continuation_part_repeats_the_previous_ending(db, root):
    body = "\n\n".join(f"── section {i} ──\n" + ("x" * 4000) for i in range(10))
    add_video(db, root, "big", body=body)

    collection = make_collection(
        db,
        root,
        ["big"],
        mode=CollectionMode.PACKS,
        token_limit=6000,
        reserve_tokens=500,
        allow_video_split=True,
    )
    result = build_collection(db, collection, output_root=root)

    second = (result.directory / result.packs[1].filename).read_text("utf-8")
    assert "continues_from_previous_part" in second


def test_the_usable_budget_is_the_limit_minus_the_reserve(db, root):
    add_video(db, root, "v1")
    collection = make_collection(
        db,
        root,
        ["v1"],
        mode=CollectionMode.PACKS,
        token_limit=200_000,
        reserve_tokens=20_000,
    )
    assert collection.usable_budget == 180_000


def test_a_zero_budget_is_refused_with_advice(db, root):
    add_video(db, root, "v1")
    collection = make_collection(
        db,
        root,
        ["v1"],
        mode=CollectionMode.PACKS,
        token_limit=1000,
        reserve_tokens=1000,
    )
    with pytest.raises(CollectionBuildError, match="Raise the model's limit"):
        build_collection(db, collection, output_root=root)


def test_packs_are_numbered_from_one(db, root):
    for index in range(3):
        add_video(db, root, f"v{index}", sequence=index, body="x" * 7200)

    collection = make_collection(
        db,
        root,
        ["v0", "v1", "v2"],
        mode=CollectionMode.PACKS,
        token_limit=3000,
        reserve_tokens=200,
    )
    result = build_collection(db, collection, output_root=root)

    assert [p.number for p in result.packs] == list(range(1, result.pack_count + 1))
    assert result.packs[0].filename == "collection-pack-001.md"


def test_every_pack_states_what_it_holds(db, root):
    for index in range(2):
        add_video(db, root, f"v{index}", sequence=index, body="x" * 7200)

    collection = make_collection(
        db,
        root,
        ["v0", "v1"],
        mode=CollectionMode.PACKS,
        token_limit=3000,
        reserve_tokens=200,
    )
    result = build_collection(db, collection, output_root=root)

    text = (result.directory / result.packs[0].filename).read_text("utf-8")
    assert "part 1 of" in text
    assert "Contains:" in text
    assert "estimate" in text


def test_the_pack_manifest_records_the_algorithm_and_budget(db, root):
    add_video(db, root, "v1", body="x" * 3600)
    collection = make_collection(
        db,
        root,
        ["v1"],
        mode=CollectionMode.PACKS,
        token_limit=50_000,
        reserve_tokens=5_000,
        target_model_label="a large model",
    )
    result = build_collection(db, collection, output_root=root)

    manifest = json.loads((result.directory / PACK_MANIFEST_FILENAME).read_text("utf-8"))
    assert manifest["packing_algorithm"]
    assert manifest["packing_version"] == 1
    assert manifest["usable_budget"] == 45_000
    assert manifest["target_model_label"] == "a large model"


# ── Splitting mechanics ───────────────────────────────────────────────────


def test_text_within_budget_is_not_split():
    assert split_at_sections("short text", 1000) == ["short text"]


def test_splitting_prefers_section_boundaries():
    text = "── one ──\n" + "a" * 500 + "\n── two ──\n" + "b" * 500
    chunks = split_at_sections(text, 700)
    assert len(chunks) == 2
    assert chunks[1].lstrip().startswith("── two ──")


def test_splitting_falls_back_to_paragraph_breaks():
    text = "a" * 500 + "\n\n" + "b" * 500
    chunks = split_at_sections(text, 700)
    assert len(chunks) == 2


def test_splitting_always_terminates_on_unbreakable_text():
    # No newline anywhere: without a hard-cut fallback this would loop forever.
    chunks = split_at_sections("x" * 5000, 1000)
    assert len(chunks) >= 5
    assert "".join(chunks) == "x" * 5000


def test_splitting_loses_no_content():
    text = "── one ──\n" + "a" * 2000 + "\n\n── two ──\n" + "b" * 2000
    assert "".join(split_at_sections(text, 900)) == text


# ── Loading sources ───────────────────────────────────────────────────────


def test_sources_load_in_collection_order(db, root):
    add_video(db, root, "v1", body="first")
    add_video(db, root, "v2", sequence=1, body="second")
    collection = make_collection(db, root, ["v1", "v2"])

    loaded = load_sources(collection, root)
    assert [s.text.strip() for s in loaded] == ["first", "second"]


def test_a_missing_source_loads_as_a_placeholder(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    (root / "j1" / "v1" / "assembled.txt").unlink()

    loaded = load_sources(collection, root)
    assert "could not be included" in loaded[0].text


def test_build_records_are_written(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    build_collection(db, collection, output_root=root)

    row = db.execute("SELECT * FROM collection_builds").fetchone()
    assert row["status"] == "completed"
    assert row["collection_version"] == 1
    assert row["manifest_sha256"]


def test_the_collection_records_its_current_version(db, root):
    add_video(db, root, "v1")
    collection = make_collection(db, root, ["v1"])
    build_collection(db, collection, output_root=root)

    assert load_collection(db, collection.id).current_version == 1
