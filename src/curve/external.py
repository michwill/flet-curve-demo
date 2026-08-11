"""Point campaigns that no API knows about, kept in Curve's own frontend.

Some of the largest reasons to be in a Curve pool are not on either
Curve API and not on Merkl either: a protocol says "LP here and we will
count it towards our points at 30x", and the only machine-readable record
of that is a directory of JSON files in curve-frontend --

  https://github.com/curvefi/curve-frontend/tree/main/packages/external-rewards/src/campaigns

23 files at the time of writing, one per platform, 51 KB in total, listed
by `campaign-list.json` beside them. That manifest is what makes this
readable at all: the directory itself can only be enumerated through the
GitHub API, which rate-limits an unauthenticated caller at 60 requests an
hour and would spend one of those per page load.

So it is a manifest fetch and then one small file per platform, issued
together. `raw.githubusercontent.com` sends `access-control-allow-origin: *`
and `max-age=300`, so the browser build reads it directly and the CDN
absorbs the fan-out.

**`campaignEnd` cannot be believed, and this was measured rather than
assumed.** 121 of the 122 `lp` entries have an end date already in the
past -- 119 of them the same round `1770000000` (2 February 2026) -- while
Curve's own site shows every one of them. Two things in `index.ts`
explain that: an entry whose `campaignStart` is `"0"` returns early and is
never date-checked at all, and the check the rest get reads the seconds as
milliseconds (`new Date(+pool.campaignStart)`), putting 2026 in 1970. So
nothing has ever read that field, nobody has had reason to maintain it,
and a client that started reading it correctly would show an empty list
on a page where Curve shows twenty campaigns.

The date is therefore kept on the record and not filtered on, which is
parity with the frontend these files are written for. `campaignStart` *is*
honoured: no entry currently fails it, so it costs nothing today and is
right the first time somebody schedules one.

Two other things upstream's shape does not say outright:

  * `action` is `lp`, `supply` or `borrow`. Only `lp` is about a pool --
    the other two are lending markets, which this app does not have -- and
    an entry can carry an empty action, which is nothing at all;
  * `multiplier` is free text, not a number. `30x` is the common case but
    `15+`, `0-1x`, `crvUSD` and `tangent points` are all in there, so it
    is shown verbatim or not at all.

`network` is Curve's own chain name (`ethereum`, `fraxtal`, `plasma`),
which is what this app keys chains by everywhere else, and `address` is
the pool's -- checked against the v2 detail endpoint for every Ethereum
`lp` entry, all of which resolved to a pool.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: The `main` branch, raw. A tag would be reproducible and would also stop
#: reporting campaigns the day it went stale, which is the opposite of
#: what this file is for.
EXTERNAL_BASE = (
    "https://raw.githubusercontent.com/curvefi/curve-frontend"
    "/main/packages/external-rewards/src"
)

#: The list of campaign files. Read rather than hardcoded: a new platform
#: is a new file plus a line here, and a list kept locally would show the
#: same nothing for it as for a platform that has no campaign at all.
MANIFEST = f"{EXTERNAL_BASE}/campaign-list.json"

#: The only action that describes a pool. `supply` and `borrow` belong to
#: lending markets, which this app does not list.
LP_ACTION = "lp"

#: A file name and nothing else -- see `parse_manifest`.
_SAFE_NAME = re.compile(r"[A-Za-z0-9._-]+")

#: A multiplier that reads as one: `30x`, `2.5x`, `0-1x`, `15+`.
_MULTIPLIER = re.compile(r"[\d.]+(-[\d.]+)?[x+]")


@dataclass(frozen=True, slots=True)
class ExternalCampaign:
    """One platform's offer on one pool.

    Named for upstream's own package. Mostly points -- 121 of the 135
    entries in those files were tagged that way when this was written --
    but a handful are tagged `tokens`, and neither kind carries a rate,
    so they are shown the same way: who is paying, at what multiplier,
    and where to go and look.
    """

    platform: str
    #: Where the platform itself reports what you have earned. This is the
    #: whole point of carrying these: points cannot be claimed here, or
    #: anywhere else in this app, so the useful thing is the door.
    dashboard: str
    network: str
    address: str
    #: Free text: `30x`, `15+`, `crvUSD`, or empty.
    multiplier: str = ""
    #: The campaign's own line, where it has one -- e.g. "LP tokens staked
    #: in gauge are excluded from Ethena campaign", which is exactly the
    #: kind of thing a depositor needs before staking and will find
    #: nowhere else.
    note: str = ""
    tags: tuple[str, ...] = ()
    #: Unix seconds, or 0 for "no stated window".
    starts: int = 0
    #: Unix seconds. Recorded, deliberately not enforced -- see the module
    #: docstring for the count that settles why.
    ends: int = 0

    @property
    def points(self) -> bool:
        """Points rather than tokens, which is the default where neither is said."""
        return "tokens" not in self.tags

    @property
    def label(self) -> str:
        """`Ethena 30x`, or the platform alone where the multiplier is prose.

        Roughly a fifth of the multipliers are not multipliers -- `crvUSD`,
        `jane`, `tangent points` -- and concatenating those gives "3Jane
        jane" and "Tangent tangent points". A rate is only appended when
        it reads as one.
        """
        if _MULTIPLIER.fullmatch(self.multiplier):
            return f"{self.platform} {self.multiplier}"
        return self.platform

    def describe(self) -> str:
        """The long form, for a tooltip: who, what, and any caveat.

        The per-pool note is the part worth carrying. "LP tokens staked in
        gauge are excluded from Ethena campaign" changes what somebody
        should do with the Stake tab, and it is written down in exactly
        one place on the internet.
        """
        kind = "points" if self.points else "token rewards"
        rate = f" at {self.multiplier}" if _MULTIPLIER.fullmatch(self.multiplier) else ""
        line = f"{self.platform} {kind}{rate} for providing liquidity."
        return f"{line} {self.note}" if self.note else line

    def started_by(self, now: float) -> bool:
        """Has this begun? An unstated start has always been running."""
        return not self.starts or now >= self.starts


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _text(value: Any) -> str:
    """A field upstream writes as the string "null" when it means nothing."""
    text = str(value or "").strip()
    return "" if text.lower() == "null" else text


def parse_manifest(payload: Any) -> list[str]:
    """The campaign file names, in the order upstream lists them.

    Each name goes on the end of a URL, so it is checked rather than
    trusted: a plain `Something.json` and nothing with a slash, a dot
    segment or a scheme in it. The file is somebody else's and is fetched
    over the network, which is enough reason not to let it choose which
    host this ends up asking.
    """
    if not isinstance(payload, list):
        return []
    names = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        name = _text(entry.get("campaign"))
        if name.endswith(".json") and _SAFE_NAME.fullmatch(name):
            names.append(name)
    return names


def parse_campaign(payload: Any, *, now: float | None = None) -> list[ExternalCampaign]:
    """One platform's file, as the pools it currently covers.

    Everything that is not a started `lp` entry is dropped here rather
    than in the view: a borrow campaign on a lending market has no pool
    page in this app to appear on.
    """
    if not isinstance(payload, dict):
        return []
    when = time.time() if now is None else now
    platform = _text(payload.get("platform"))
    dashboard = _text(payload.get("dashboardLink"))
    found = []
    for raw in payload.get("pools") or []:
        if not isinstance(raw, dict) or _text(raw.get("action")) != LP_ACTION:
            continue
        address = _text(raw.get("address"))
        network = _text(raw.get("network"))
        if not address or not network:
            continue
        campaign = ExternalCampaign(
            platform=platform or "?",
            dashboard=dashboard,
            network=network.lower(),
            address=address,
            multiplier=_text(raw.get("multiplier")),
            note=_text(raw.get("description")),
            tags=tuple(_text(t) for t in raw.get("tags") or [] if _text(t)),
            starts=_int(raw.get("campaignStart")),
            ends=_int(raw.get("campaignEnd")),
        )
        if campaign.started_by(when):
            found.append(campaign)
    return found


def by_pool(
    campaigns: Iterable[ExternalCampaign],
) -> dict[tuple[str, str], list[ExternalCampaign]]:
    """Group by `(chain name, lowercased pool address)`.

    Keyed by both because an address is only unique within a chain, and
    two of these campaigns do name the same pool address on two chains.
    """
    index: dict[tuple[str, str], list[ExternalCampaign]] = {}
    for campaign in campaigns:
        index.setdefault((campaign.network, campaign.address.lower()), []).append(
            campaign
        )
    return index
