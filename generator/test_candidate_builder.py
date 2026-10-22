"""Tests for candidate_builder.py (WP-521 Ф14, Path A: club posts; Path B: program fragments)."""

import json

from candidate_builder import build_club_post_candidate, build_program_fragment_candidate
from recommendation_registry import compute_content_hash


def test_maps_club_post_fields_into_raw_v0_shape():
    post = {"discourse_topic_id": 12345, "content": "hello world"}
    candidate = build_club_post_candidate(post)
    d = candidate.to_dict()
    assert d["source_ref"] == {"type": "club_post", "ref": "12345"}
    assert d["selection_basis"] == "connected_source"
    assert d["content_hash"] == compute_content_hash("hello world")
    assert d["topic_ref"] is None
    assert d["contract_version"] == "raw-v0"


def test_content_hash_tracks_post_content_not_topic_id():
    same_topic_edited = {"discourse_topic_id": 12345, "content": "hello world v2"}
    original = {"discourse_topic_id": 12345, "content": "hello world"}
    assert build_club_post_candidate(original).content_hash != build_club_post_candidate(
        same_topic_edited
    ).content_hash


_SAMPLE_FRAGMENT = {
    "seed_id": "lr-rhy-1",
    "bottleneck": "rhy",
    "domain": "self",
    "priority": 10,
    "description": "Rhythm (rhy) x personal domain: daily slot practice",
    "covers_gaps": [{"slot": "cp.rhy", "coverage": "full"}],
}


def test_maps_program_fragment_fields_into_raw_v0_shape():
    candidate = build_program_fragment_candidate(_SAMPLE_FRAGMENT)
    d = candidate.to_dict()
    assert d["source_ref"] == {"type": "program_fragment", "ref": "lr-rhy-1"}
    assert d["selection_basis"] == "bottleneck"
    assert d["topic_ref"] is None
    assert d["contract_version"] == "raw-v0"
    expected_recipe = json.dumps(
        {k: _SAMPLE_FRAGMENT[k] for k in ("seed_id", "bottleneck", "domain", "priority", "description", "covers_gaps")},
        sort_keys=True,
        ensure_ascii=False,
    )
    assert d["content_hash"] == compute_content_hash(expected_recipe)


def test_content_hash_is_stable_for_the_same_recipe():
    """Re-selecting the same catalog entry must yield the same hash, regardless of
    dict key insertion order (sort_keys canonicalization)."""
    reordered = dict(reversed(list(_SAMPLE_FRAGMENT.items())))
    assert build_program_fragment_candidate(_SAMPLE_FRAGMENT).content_hash == (
        build_program_fragment_candidate(reordered).content_hash
    )


def test_content_hash_changes_when_recipe_description_changes():
    """A real edit to the catalog entry must be visible as drift, not silently ignored."""
    edited = dict(_SAMPLE_FRAGMENT, description="A different assignment entirely")
    assert build_program_fragment_candidate(_SAMPLE_FRAGMENT).content_hash != (
        build_program_fragment_candidate(edited).content_hash
    )
