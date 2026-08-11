"""Point campaigns from curve-frontend, and the date field that lies.

These files are the only machine-readable record that a protocol counts a
Curve position towards its points, and the interesting thing about them is
what happens if you read them carefully: **121 of the 122 `lp` entries had
an end date already in the past** when this was written, 119 of them the
same round `1770000000`, while Curve's own site showed every one of them.

Upstream never reads the field -- an entry whose `campaignStart` is `"0"`
returns early and is never date-checked, and the rest get their seconds
read as milliseconds -- so nobody has had reason to keep it current. A
client that started enforcing it would show an empty section on a page
where Curve shows twenty campaigns. So the date is carried and not
enforced, and this file is where that decision is pinned down, because it
is the sort of thing a later reader would "fix".

The payloads are trimmed from the real `Ethena.json` and its neighbours.
"""

from __future__ import annotations

from curve.external import ExternalCampaign, by_pool, parse_campaign, parse_manifest

#: Well after the `1770000000` (February 2026) that 119 entries carry.
NOW = 1_790_000_000.0

ETHENA = {
    "campaignName": "",
    "platform": "Ethena",
    "description": "Points for providing liquidity.",
    "platformImageId": "ethena.png",
    "dashboardLink": "https://app.ethena.fi/liquidity",
    "pools": [
        {
            "id": "null",
            "action": "lp",
            "description": "null",
            "campaignStart": "0",
            "campaignEnd": "1770000000",
            "address": "0xf8db2accdef8e7a26b0e65c3980adc8ce11671a4",
            "network": "ethereum",
            "multiplier": "30x",
            "tags": ["points"],
            "lock": "true",
        },
        {
            "id": "null",
            "action": "lp",
            "description": "LP tokens staked in gauge are excluded from Ethena campaign.",
            "campaignStart": "0",
            "campaignEnd": "1770000000",
            "address": "0x1c34204fcfe5314dcf53be2671c02c35db58b4e3",
            "network": "arbitrum",
            "multiplier": "30x",
            "tags": ["points"],
            "lock": "false",
        },
        {
            "id": "null",
            "action": "borrow",
            "description": "null",
            "campaignStart": "0",
            "campaignEnd": "1770000000",
            "address": "0x74f88baa966407b50c10b393bbd789639effe78b",
            "network": "ethereum",
            "multiplier": "20x",
            "tags": ["points"],
            "lock": "false",
        },
    ],
}


def test_an_expired_end_date_does_not_hide_a_campaign() -> None:
    """The measurement in the module docstring, as a test.

    Enforcing `campaignEnd` empties the feature. Both entries here carry
    a date twenty years-worth of seconds in the past by the clock this
    runs at, and both must survive.
    """
    found = parse_campaign(ETHENA, now=NOW)
    assert [c.address for c in found] == [
        "0xf8db2accdef8e7a26b0e65c3980adc8ce11671a4",
        "0x1c34204fcfe5314dcf53be2671c02c35db58b4e3",
    ]


def test_a_campaign_that_has_not_started_is_not_shown() -> None:
    """`campaignStart` is honoured, unlike its neighbour.

    No entry currently fails it, so this costs nothing today and is right
    the first time somebody schedules one.
    """
    scheduled = {
        **ETHENA,
        "pools": [{**ETHENA["pools"][0], "campaignStart": str(int(NOW) + 86_400)}],
    }
    assert parse_campaign(scheduled, now=NOW) == []


def test_only_pool_campaigns_survive() -> None:
    """`supply` and `borrow` are lending markets, which this app has none of."""
    actions = {c.address for c in parse_campaign(ETHENA, now=NOW)}
    assert "0x74f88baa966407b50c10b393bbd789639effe78b" not in actions


def test_the_per_pool_note_is_kept() -> None:
    """The one that changes what somebody should do with the Stake tab.

    "LP tokens staked in gauge are excluded" is written down in exactly
    one place on the internet, and this is it.
    """
    arbitrum = parse_campaign(ETHENA, now=NOW)[1]
    assert "excluded from Ethena campaign" in arbitrum.describe()


def test_the_string_null_is_not_a_description() -> None:
    """Upstream writes an absent field as the four letters `null`."""
    assert parse_campaign(ETHENA, now=NOW)[0].note == ""


def test_a_multiplier_is_only_appended_when_it_reads_as_one() -> None:
    """A fifth of them are prose: `crvUSD`, `jane`, `tangent points`.

    Concatenating those gives "3Jane jane" and "Tangent tangent points".
    """
    def label(multiplier: str) -> str:
        return ExternalCampaign(
            platform="Ethena",
            dashboard="",
            network="ethereum",
            address="0x1",
            multiplier=multiplier,
        ).label

    assert label("30x") == "Ethena 30x"
    assert label("2.5x") == "Ethena 2.5x"
    assert label("0-1x") == "Ethena 0-1x"
    assert label("15+") == "Ethena 15+"
    assert label("tangent points") == "Ethena"
    assert label("crvUSD") == "Ethena"
    assert label("") == "Ethena"


def test_points_is_the_default_kind() -> None:
    """121 of 135 entries are tagged points; a handful say tokens.

    Neither carries a rate, so the distinction is only in the wording --
    but calling a token campaign "points" would be wrong in the one
    direction somebody might act on.
    """
    base = {"platform": "P", "dashboard": "", "network": "ethereum", "address": "0x1"}
    assert ExternalCampaign(**base, tags=("points",)).points
    assert ExternalCampaign(**base, tags=()).points
    assert not ExternalCampaign(**base, tags=("tokens",)).points
    assert "token rewards" in ExternalCampaign(**base, tags=("tokens",)).describe()


def test_grouping_is_by_chain_and_address() -> None:
    """An address is only unique within a chain, and two of these repeat."""
    index = by_pool(parse_campaign(ETHENA, now=NOW))
    assert ("ethereum", "0xf8db2accdef8e7a26b0e65c3980adc8ce11671a4") in index
    assert ("arbitrum", "0x1c34204fcfe5314dcf53be2671c02c35db58b4e3") in index
    assert ("arbitrum", "0xf8db2accdef8e7a26b0e65c3980adc8ce11671a4") not in index


def test_the_manifest_will_not_choose_which_host_to_ask() -> None:
    """Each name goes on the end of a URL, and the file is somebody else's."""
    assert parse_manifest([{"campaign": "Ethena.json"}]) == ["Ethena.json"]
    assert parse_manifest([{"campaign": "../../../etc/passwd.json"}]) == []
    assert parse_manifest([{"campaign": "https://elsewhere.example/x.json"}]) == []
    assert parse_manifest([{"campaign": "Ethena.ts"}]) == []
    assert parse_manifest("not a list") == []


def test_a_broken_file_costs_that_platform_and_no_more() -> None:
    assert parse_campaign(None, now=NOW) == []
    assert parse_campaign({"platform": "P"}, now=NOW) == []
    # An entry with no address is not a campaign about anything.
    assert parse_campaign(
        {"platform": "P", "pools": [{"action": "lp", "network": "ethereum"}]}, now=NOW
    ) == []
