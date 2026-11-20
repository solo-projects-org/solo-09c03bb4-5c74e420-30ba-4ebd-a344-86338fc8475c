"""Capture service: turn the live origin into an append-only archive.

``CaptureService.capture`` performs one pass for one channel:

1. fetch the master playlist and resolve the video + subtitle playlists,
2. register every segment seen, and mark pending segments that slid out of the
   window as ``missing`` (unless the playlist itself is stale),
3. fetch every unresolved segment (in-window first, then gaps), storing cues
   and marking each one ``captured`` -- including segments with no captions.

Every observed segment also records its *derivation basis*: which playlist
supplied the start UTC (the video playlist, or the subtitle playlist as
fallback), which segment's ``EXT-X-PROGRAM-DATE-TIME`` anchored it, and that
anchor's value at the time.  The basis is stored with the captured cues so a
later PDT correction can be traced to exactly the segments it affects.

Playlist observation and the subsequent fetches are guarded by a per-channel
lock so rapid clicks serialise instead of racing.  Every database transition is
monotonic (see :mod:`app.db`), which is what keeps repeated captures and stale
playlists from rolling a status back.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .db import CueRow, Database
from .hls import map_cues_to_utc, parse_master, parse_media_playlist, parse_vtt
from .timeutil import now_ms

log = logging.getLogger(__name__)

MAX_FETCH_PER_PASS = 60


@dataclass
class CaptureResult:
    channel_id: str
    fetched: int = 0
    captured: int = 0
    empty_segments: int = 0
    failed: int = 0
    marked_missing: list[int] = field(default_factory=list)
    stale_playlist: bool = False
    unresolved_after: int = 0
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "channel_id": self.channel_id,
            "fetched": self.fetched,
            "captured": self.captured,
            "empty_segments": self.empty_segments,
            "failed": self.failed,
            "unresolved_after": self.unresolved_after,
            "marked_missing": self.marked_missing,
            "stale_playlist": self.stale_playlist,
            "notes": self.notes,
        }


class CaptureService:
    def __init__(
        self,
        db: Database,
        timeout_s: float = 10.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self.db = db
        self.timeout_s = timeout_s
        self.transport = transport
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, channel_id: str) -> asyncio.Lock:
        if channel_id not in self._locks:
            self._locks[channel_id] = asyncio.Lock()
        return self._locks[channel_id]

    async def capture(self, channel_id: str, master_url: str, name: str) -> CaptureResult:
        async with self._lock(channel_id):
            result = CaptureResult(channel_id=channel_id)
            self.db.upsert_channel(channel_id, name, master_url, now_ms())
            async with httpx.AsyncClient(
                timeout=self.timeout_s,
                follow_redirects=True,
                transport=self.transport,
            ) as client:
                await self._capture_locked(client, channel_id, master_url, name, result)
            return result

    async def _capture_locked(
        self,
        client: httpx.AsyncClient,
        channel_id: str,
        master_url: str,
        name: str,
        result: CaptureResult,
    ) -> None:
        try:
            response = await client.get(master_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            result.notes.append(f"master playlist unreachable: {exc}")
            return

        try:
            master = parse_master(response.text, str(response.url))
        except ValueError as exc:
            result.notes.append(f"master playlist is invalid: {exc}")
            return
        if master.subtitle_uri is None:
            result.notes.append("master playlist has no subtitle rendition")
            return

        try:
            video_response = await client.get(master.video_uri)
            video_response.raise_for_status()
            subs_response = await client.get(master.subtitle_uri)
            subs_response.raise_for_status()
        except httpx.HTTPError as exc:
            result.notes.append(f"media playlist unreachable: {exc}")
            return

        try:
            video = parse_media_playlist(video_response.text, str(video_response.url))
            subs = parse_media_playlist(subs_response.text, str(subs_response.url))
        except ValueError as exc:
            result.notes.append(f"media playlist is invalid: {exc}")
            return

        subs_by_seq = {segment.seq: segment for segment in subs.segments}
        video_by_seq = {segment.seq: segment for segment in video.segments}

        observed: list[dict] = []
        ordered_seqs: list[int] = []
        for seq in sorted(set(video_by_seq) | set(subs_by_seq)):
            # The video playlist is authoritative.  A segment the video
            # playlist cannot anchor (no PDT and no derivable start) falls
            # back to the subtitle playlist's own PDT; the playlist that
            # actually supplied the start is recorded as the basis.
            video_segment = video_by_seq.get(seq)
            sub_segment = subs_by_seq.get(seq)
            if video_segment is not None and video_segment.start_ms is not None:
                chosen, basis_playlist = video_segment, "video"
            elif sub_segment is not None and sub_segment.start_ms is not None:
                chosen, basis_playlist = sub_segment, "subs"
            else:
                continue
            duration = (
                sub_segment.duration_ms
                if sub_segment is not None
                else video_segment.duration_ms
                if video_segment is not None
                else None
            )
            if duration is None:
                continue
            observed.append(
                {
                    "seq": seq,
                    "start_ms": chosen.start_ms,
                    "end_ms": chosen.start_ms + duration,
                    "sub_uri": sub_segment.uri
                    if sub_segment is not None
                    else _sub_uri_for(master.subtitle_uri, seq),
                    "basis_playlist": basis_playlist,
                    "basis_anchor_seq": chosen.anchor_seq,
                    "basis_anchor_pdt_ms": chosen.anchor_pdt_ms,
                }
            )
            ordered_seqs.append(seq)

        if not observed:
            result.notes.append("playlists contained no segments with a UTC anchor")
            return

        stale, newly_missing = self.db.observe_playlist(
            channel_id,
            observed,
            min(ordered_seqs),
            max(ordered_seqs) + 1,
            now_ms(),
        )
        result.stale_playlist = stale
        result.marked_missing = newly_missing
        if stale:
            result.notes.append(
                "playlist is older than the newest one already observed; "
                "no segments were marked missing"
            )

        # Fetch every not-yet-captured segment, including ones that have
        # already slid out of the live window (there is no playlist entry for
        # them any more, but the stored subtitle URI lets a late re-fetch heal
        # a pending/missing segment).  Segments currently in the window go
        # first so they are secured before they can slide out; retries of older
        # gaps follow.
        window = set(ordered_seqs)
        todo = self.db.uncaptured_segments(channel_id)
        todo.sort(key=lambda segment: (segment.seq not in window, segment.seq))

        fetched = 0
        for segment in todo:
            if fetched >= MAX_FETCH_PER_PASS:
                result.notes.append("fetch limit reached; remaining segments stay pending")
                break
            fetched += 1
            outcome = await self._fetch_segment(client, channel_id, segment)
            if outcome == "captured":
                result.captured += 1
            elif outcome == "empty":
                result.captured += 1
                result.empty_segments += 1
            else:
                result.failed += 1
        result.fetched = fetched
        result.unresolved_after = len(todo) - result.captured

    async def _fetch_segment(self, client, channel_id, segment) -> str:
        try:
            response = await client.get(segment.sub_uri)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            self.db.mark_fetch_failed(
                channel_id, segment.seq, _describe_error(exc), now_ms()
            )
            return "failed"

        vtt = parse_vtt(response.text)
        if not vtt.valid:
            self.db.mark_fetch_failed(channel_id, segment.seq, "invalid WEBVTT file", now_ms())
            return "failed"

        cues = map_cues_to_utc(vtt, segment.start_ms)
        cue_rows = [
            CueRow(seq=segment.seq, start_ms=cue.start_ms, end_ms=cue.end_ms, text=cue.text)
            for cue in cues
        ]
        # Store the derivation basis actually used for this capture together
        # with the cues and the captured status.  For a late capture the row
        # carries the basis kept from its last playlist observation.
        self.db.mark_captured(
            channel_id,
            segment.seq,
            cue_rows,
            captions_empty=len(cue_rows) == 0,
            now=now_ms(),
            basis=segment.basis,
        )
        return "empty" if not cue_rows else "captured"


def _describe_error(exc: httpx.HTTPError) -> str:
    """A short, operator-readable reason for a failed segment fetch."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    return type(exc).__name__


def _sub_uri_for(subs_playlist_url: str, seq: int) -> str:
    base = subs_playlist_url.rsplit("/", 1)[0]
    return f"{base}/subs/{seq}.vtt"


def capture_service_from_settings(settings, db: Database) -> CaptureService:
    return CaptureService(db, timeout_s=settings.request_timeout_s)
