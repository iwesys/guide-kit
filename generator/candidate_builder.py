"""
Candidate builder, Path A: assembles a raw-v0 RawV0Candidate from a participant's
own club post (connect_source, WP-545).

Consensus from the WP-521 peer session (Claude + Kimi + Codex, 2026-10-22): the
module docstring of recommendation_registry.py explicitly scoped this builder out,
because the session that wrote it found no verified field-provenance mapping from
the bottleneck/qualification selection code (planner.py / source_finder.py) to
raw-v0's five fields. That blocker is specific to the programmatic selection path
(Path B, material_selector.py) -- a club post never goes through that selection
code at all, so Path A needed its own, separate mapping, not a fix to Path B's.

selection_basis="connected_source" is a new value (the existing bottleneck|
qualification pair describes a pedagogical choice the system made; a club post's
candidacy instead traces to the connect_source mechanism, WP-545, that makes the
post visible to the pipeline in the first place). topic_ref stays None: no
classifier yet maps a club post to a topic reference (open item, not blocking
this path -- RawV0Candidate already treats topic_ref as optional).

Rationale (Kimi's round-5 challenge, same session): named this "connected_source"
rather than invent fresh vocabulary (candidates discussed: participant_shared,
community_share, linked_source) because it names the one mechanism already on
record (WP-545) that makes this candidate exist at all -- a name grounded in an
existing, named mechanism beats a new synonym for the same idea. Recorded the
decision here, inline, instead of a separate ADR: this file's sibling,
recommendation_registry.py, already documents its own peer-session consensus the
same way, in its own docstring -- following that precedent over introducing a new
artifact type for a one-field naming call.
"""

from recommendation_registry import RawV0Candidate, compute_content_hash

CLUB_POST_SELECTION_BASIS = "connected_source"


def build_club_post_candidate(post: dict) -> RawV0Candidate:
    """
    post: a club post record as read back from the forum (keys: "content",
    "discourse_topic_id" -- the same topic-id-based reference already used
    for club post links elsewhere, e.g. aist_bot_newarchitecture/handlers/discourse.py).
    """
    return RawV0Candidate(
        source_ref_type="club_post",
        source_ref_ref=str(post["discourse_topic_id"]),
        selection_basis=CLUB_POST_SELECTION_BASIS,
        content_hash=compute_content_hash(post["content"]),
        topic_ref=None,
    )
