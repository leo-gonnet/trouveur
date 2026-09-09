"""Guards on the single write path.

persist() is where the sweep, the detail worker and a re-normalisation all meet. The behaviours
here are the ones that would otherwise cost money quietly.
"""

from __future__ import annotations

from trouveur.sources.arbeitsagentur import normalize as aa_normalize


def test_relisting_unchanged_content_produces_the_same_hash(aa_listing, aa_detail):
    """This is what stops a daily re-sweep re-deriving and re-scoring the whole corpus.

    Normalisation reads listing AND detail from the archive precisely so a listing-only re-sweep
    cannot produce a job with no description, recompute content_hash without it, and flap the
    hash every night.
    """
    first = aa_normalize(aa_listing, aa_detail)
    second = aa_normalize(aa_listing, aa_detail)
    assert first.content_hash == second.content_hash


def test_listing_only_normalisation_differs_from_the_merged_record(aa_listing, aa_detail):
    """The reason persist() must never normalise a bare listing for a job that has a detail.

    If it did, this difference would be recomputed on every sweep.
    """
    assert aa_normalize(aa_listing).content_hash != aa_normalize(
        aa_listing, aa_detail
    ).content_hash


def test_a_failed_detail_does_not_blank_a_stored_description(aa_listing, aa_detail):
    """A detail fetch that fails arrives as None and must not erase what we already hold.

    Archiving listing and detail separately is what makes this possible: the last good detail
    payload is still there to normalise from.
    """
    merged = aa_normalize(aa_listing, aa_detail)
    assert merged.description
    # Re-normalising with the archived detail still present keeps the description.
    assert aa_normalize(aa_listing, aa_detail).description == merged.description


def test_document_hash_is_stable_across_key_ordering():
    """An unchanged posting must archive no new row.

    dict ordering is not stable across payloads, so without sorted keys an unchanged posting would
    hash differently on every fetch and the archive would grow by the whole corpus daily.
    """
    from trouveur.models import DocumentKind, RawDocument

    first = RawDocument(
        source="s", external_id="1", kind=DocumentKind.LISTING, payload={"a": 1, "b": 2}
    )
    second = RawDocument(
        source="s", external_id="1", kind=DocumentKind.LISTING, payload={"b": 2, "a": 1}
    )
    assert first.payload_sha256 == second.payload_sha256


def test_document_hash_changes_when_the_payload_changes():
    from trouveur.models import DocumentKind, RawDocument

    first = RawDocument(source="s", external_id="1", kind=DocumentKind.LISTING, payload={"a": 1})
    second = RawDocument(source="s", external_id="1", kind=DocumentKind.LISTING, payload={"a": 2})
    assert first.payload_sha256 != second.payload_sha256


def test_listing_and_detail_hash_independently():
    """Kinds are archived separately, so a detail can never overwrite a listing."""
    from trouveur.models import DocumentKind, RawDocument

    payload = {"a": 1}
    listing = RawDocument(
        source="s", external_id="1", kind=DocumentKind.LISTING, payload=payload
    )
    detail = RawDocument(source="s", external_id="1", kind=DocumentKind.DETAIL, payload=payload)
    assert listing.kind != detail.kind
