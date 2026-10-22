"""Tests for candidate_builder.py (WP-521 Ф14, Path A: club posts)."""

from candidate_builder import build_club_post_candidate
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
