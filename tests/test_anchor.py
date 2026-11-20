"""Anchor-correction tracing: which archived cues a corrected PDT affects."""
from __future__ import annotations

from app.timeutil import iso_from_ms, parse_iso_ms

#: The news channel starts numbering at 2026-11-20T00:00:00Z with 6 s
#: segments, so sequence N starts N*6 seconds past midnight.
def seq_utc(seq: int) -> str:
    return iso_from_ms(parse_iso_ms("2026-11-20T00:00:00Z") + seq * 6000)


def set_pdt_every(harness, every: int, playlist: str | None = None) -> None:
    body: dict = {"channel": "news", "every": every}
    if playlist:
        body["playlist"] = playlist
    response = harness.client.post("/api/demo/pdt-every", json=body)
    assert response.status_code == 200, response.text


def shift_pdt(harness, seq: int, shift_ms: int) -> dict:
    response = harness.client.post(
        "/api/demo/pdt-shift",
        json={"channel": "news", "seq": seq, "shift_ms": shift_ms},
    )
    assert response.status_code == 200, response.text
    return response.json()["pdt_shift"]


def anchor_impact(harness, playlist: str, anchor_seq: int, original: str, corrected: str) -> dict:
    response = harness.client.get(
        "/api/channels/news/anchor-impact",
        params={
            "playlist": playlist,
            "anchor_seq": anchor_seq,
            "original": original,
            "corrected": corrected,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_every_segment_anchored_records_itself(harness):
    harness.capture_channel("news")
    payload = anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:25.500Z")

    assert payload["query"]["offset_ms"] == 1500
    assert [segment["seq"] for segment in payload["affected"]] == [4]
    segment = payload["affected"][0]
    assert segment["direct"] is True
    assert segment["start_utc"] == "2026-11-20T00:00:24.000Z"
    assert segment["shift_ms"] == 1500
    assert segment["basis_playlist"] == "video"
    assert segment["basis_anchor_pdt_utc"] == "2026-11-20T00:00:24.000Z"
    assert payload["unknown"] == []


def test_derived_segments_trace_to_the_shared_anchor(harness):
    set_pdt_every(harness, 4)
    harness.capture_channel("news")

    # Window is #4..#11; PDT only on #4 and #8.
    payload = anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:26.500Z")
    assert [segment["seq"] for segment in payload["affected"]] == [4, 5, 6, 7]
    by_seq = {segment["seq"]: segment for segment in payload["affected"]}
    assert by_seq[4]["direct"] is True
    assert all(not segment["direct"] for seq, segment in by_seq.items() if seq != 4)
    # Every affected segment shifts by the anchor's delta, and the archived
    # starts are the pre-correction ones.
    assert all(segment["shift_ms"] == 2500 for segment in payload["affected"])
    assert by_seq[5]["start_utc"] == "2026-11-20T00:00:30.000Z"
    assert by_seq[7]["start_utc"] == "2026-11-20T00:00:42.000Z"

    other = anchor_impact(harness, "video", 8, seq_utc(8), "2026-11-20T00:00:50.000Z")
    assert [segment["seq"] for segment in other["affected"]] == [8, 9, 10, 11]


def test_trace_still_answers_after_anchor_slid_out(harness):
    set_pdt_every(harness, 4)
    harness.capture_channel("news")
    harness.advance(8)  # window 12..19: anchors #4 and #8 have slid out
    harness.capture_channel("news")

    # The broadcaster corrects the slid-out anchor afterwards.
    info = shift_pdt(harness, 4, 2500)
    assert info["original_utc"] == "2026-11-20T00:00:24.000Z"
    assert info["corrected_utc"] == "2026-11-20T00:00:26.500Z"

    payload = anchor_impact(harness, "video", 4, info["original_utc"], info["corrected_utc"])
    assert [segment["seq"] for segment in payload["affected"]] == [4, 5, 6, 7]
    assert all(segment["shift_ms"] == 2500 for segment in payload["affected"])


def test_restart_keeps_the_archived_basis(harness):
    set_pdt_every(harness, 4)
    harness.capture_channel("news")
    before = anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:26.500Z")

    harness.restart()

    after = anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:26.500Z")
    assert after["affected"] == before["affected"]
    assert after["unknown"] == before["unknown"]


def test_stale_playlist_does_not_disturb_recorded_basis(harness):
    set_pdt_every(harness, 4)
    harness.capture_channel("news")
    harness.advance(8)
    harness.capture_channel("news")
    shift_pdt(harness, 4, 2500)

    # A stale CDN copy of the old playlist must not rewrite captured rows.
    response = harness.client.post("/api/demo/stale", json={"channel": "news", "segments": 8})
    assert response.status_code == 200, response.text
    harness.capture_channel("news")

    payload = anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:26.500Z")
    assert [segment["seq"] for segment in payload["affected"]] == [4, 5, 6, 7]


def test_segments_without_basis_are_listed_as_unknown(harness):
    harness.capture_channel("news")
    # Simulate a legacy row archived before basis bookkeeping existed.
    with harness.db.connect() as conn:
        conn.execute(
            """
            UPDATE segments
               SET basis_playlist=NULL, basis_anchor_seq=NULL, basis_anchor_pdt_ms=NULL
             WHERE channel_id='news' AND seq=6
            """
        )

    payload = anchor_impact(harness, "video", 6, seq_utc(6), "2026-11-20T00:00:37.000Z")
    assert payload["affected"] == []
    assert [segment["seq"] for segment in payload["unknown"]] == [6]
    assert payload["unknown"][0]["start_utc"] == "2026-11-20T00:00:36.000Z"


def test_pending_segment_updates_basis_even_when_start_is_unchanged(harness):
    set_pdt_every(harness, 4)
    harness.set_gap(5, True)
    harness.capture_channel("news")  # #5 pending, derived from anchor #4
    row = harness.db.get_segment("news", 5)
    assert row.status == "pending"
    assert row.basis_anchor_seq == 4

    # The source re-anchors every segment; #5's start UTC does not change,
    # but its basis must follow the new anchor (itself).
    set_pdt_every(harness, 1)
    harness.capture_channel("news")  # #5 still pending: the gap persists
    row = harness.db.get_segment("news", 5)
    assert row.status == "pending"
    assert row.start_ms == parse_iso_ms("2026-11-20T00:00:30.000Z")
    assert row.basis == ("video", 5, parse_iso_ms("2026-11-20T00:00:30.000Z"))


def test_late_capture_keeps_the_basis_from_observation(harness):
    set_pdt_every(harness, 4)
    harness.set_gap(6, True)
    harness.capture_channel("news")
    harness.advance(8)  # #6 slides out and turns missing
    harness.capture_channel("news")
    assert harness.db.get_segment("news", 6).status == "missing"

    # The segment arrives late; the capture must keep the basis recorded
    # when the segment was still in the playlist.
    harness.set_gap(6, False)
    harness.capture_channel("news")
    row = harness.db.get_segment("news", 6)
    assert row.status == "captured"
    assert row.basis == ("video", 4, parse_iso_ms("2026-11-20T00:00:24.000Z"))

    payload = anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:26.500Z")
    assert 6 in [segment["seq"] for segment in payload["affected"]]


def test_subs_playlist_supplies_start_when_video_has_no_pdt(harness):
    set_pdt_every(harness, 0, playlist="video")
    harness.capture_channel("news")

    row = harness.db.get_segment("news", 4)
    assert row.status == "captured"
    assert row.basis_playlist == "subs"
    assert row.basis_anchor_seq == 4

    subs = anchor_impact(harness, "subs", 4, seq_utc(4), "2026-11-20T00:00:25.000Z")
    assert [segment["seq"] for segment in subs["affected"]] == [4]
    video = anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:25.000Z")
    assert video["affected"] == []


def test_original_utc_must_match_the_recorded_basis(harness):
    set_pdt_every(harness, 4)
    harness.capture_channel("news")
    # Same anchor, but a "before" value that was never recorded: no match.
    payload = anchor_impact(harness, "video", 4, "2026-11-20T00:00:25.000Z", "2026-11-20T00:00:26.500Z")
    assert payload["affected"] == []


def test_anchor_impact_validates_parameters(harness):
    base = "/api/channels/news/anchor-impact"
    good = {
        "playlist": "video",
        "anchor_seq": 4,
        "original": seq_utc(4),
        "corrected": seq_utc(4),
    }
    response = harness.client.get(base, params={**good, "playlist": "audio"})
    assert response.status_code == 400
    response = harness.client.get(base, params={**good, "original": "not-a-time"})
    assert response.status_code == 400
    response = harness.client.get(base, params={**good, "anchor_seq": -1})
    assert response.status_code == 400
    response = harness.client.get(base, params={"playlist": "video"})
    assert response.status_code == 422
    response = harness.client.get("/api/channels/nope/anchor-impact", params=good)
    assert response.status_code == 404


def test_anchor_impact_is_read_only(harness):
    set_pdt_every(harness, 4)
    harness.capture_channel("news")
    archive_before = harness.archive("news")
    segments_before = harness.client.get("/api/channels/news/segments").json()

    anchor_impact(harness, "video", 4, seq_utc(4), "2026-11-20T00:00:26.500Z")

    assert harness.archive("news") == archive_before
    assert harness.client.get("/api/channels/news/segments").json() == segments_before
