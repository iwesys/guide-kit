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

import json

from recommendation_registry import RawV0Candidate, compute_content_hash

CLUB_POST_SELECTION_BASIS = "connected_source"

# Path B (programmatic selection): material_selector.py's catalogs carry no
# lesson prose, only a short assignment recipe (seed_id/bottleneck/domain/
# priority/description/covers_gaps) -- there is nothing resembling delivered
# text to hash at selection time. content_hash for this type hashes the
# recipe itself instead: re-selecting the same catalog entry always yields
# the same hash, and editing the catalog entry always changes it, even
# though two separate AI renders of the same recipe's lesson text would not
# (that's the intended dedup semantics, not a limitation to work around).
PROGRAM_FRAGMENT_SELECTION_BASIS = "bottleneck"
# Keep in sync with the same-named tuple in DS-autonomous-agents/scripts/material_selector.py
# (separate repo, no shared import) — a field added only there silently drops out of the
# hash here instead of raising, since the dict comprehension below ignores unknown keys.
_RECIPE_PAYLOAD_FIELDS = ("seed_id", "bottleneck", "domain", "priority", "description", "covers_gaps")


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


def build_program_fragment_candidate(fragment: dict) -> RawV0Candidate:
    """
    fragment: the frozen six-field recipe payload for one selected catalog
    entry, as returned by material_selector.py's collect_fragment_payload()
    (DS-autonomous-agents) -- keys: seed_id, bottleneck, domain, priority,
    description, covers_gaps.
    """
    recipe = json.dumps(
        {field: fragment[field] for field in _RECIPE_PAYLOAD_FIELDS},
        sort_keys=True,
        ensure_ascii=False,
    )
    return RawV0Candidate(
        source_ref_type="program_fragment",
        source_ref_ref=fragment["seed_id"],
        selection_basis=PROGRAM_FRAGMENT_SELECTION_BASIS,
        content_hash=compute_content_hash(recipe),
        topic_ref=None,
    )
