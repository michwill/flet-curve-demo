"""The pool's own numbers: which ones exist, and what they mean.

Two things are easy to get wrong here and neither shows up as an error.
The **scales differ between parameters** -- a fee is a fraction of 1e10,
`gamma` is 1e18 fixed point, the off-peg multiplier is 1e10 fixed point --
so a single denominator applied to all of them produces numbers that look
plausible and are wrong by six orders of magnitude. And the parameters a
pool *has* depend on its family, which the registry name does not settle.

The values below were read off mainnet (see `curve/parameters.py` for the
table), so these tests are pinned to real pools rather than to invented
integers.
"""

from __future__ import annotations

import pytest

from curve import explorers
from curve.models import Coin, Pool
from curve.parameters import PARAMETERS, Kind, format_value, rows
from curve.pool import INDEXED_PARAMETERS, PoolCallFailed, PoolContract
from wallet.base import WalletError

# -- scales ----------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,raw,expected",
    [
        # A, as Curve's own UI shows it: a plain integer carrying whatever
        # multiplier its family uses.
        (Kind.INTEGER, 4_000, "4,000"),               # 3pool
        (Kind.INTEGER, 1_707_629, "1,707,629"),       # tricryptoUSDC
        # Fees are tenths of a basis point: 1e10 is 100%.
        (Kind.PERCENT, 1_500_000, "0.0150%"),         # 3pool: 0.015%
        (Kind.PERCENT, 3_825_607, "0.0383%"),         # tricryptoUSDC
        (Kind.PERCENT, 146_000_000, "1.4600%"),       # YB cbBTC: 1.46%
        (Kind.PERCENT, 30_000_000, "0.3000%"),        # an out_fee
        # 1e18 fixed point.
        (Kind.RATIO, 11_809_167_828_997, "1.18092e-05"),   # tricrypto gamma
        (Kind.RATIO, 500_000_000_000_000, "0.0005"),       # its fee_gamma
        (Kind.RATIO, 65_003_125_444_859_976_272_179, "65,003.1"),  # BTC price
        # 1e10 fixed point, and read as a multiplier rather than a rate.
        (Kind.MULTIPLIER, 100_000_000_000, "10x"),    # PayPool, off peg
        (Kind.MULTIPLIER, 20_000_000_000, "2x"),
    ],
)
def test_each_scale_is_the_one_that_parameter_uses(kind, raw, expected) -> None:
    assert format_value(kind, raw) == expected


def test_a_multiplier_stays_ascii() -> None:
    """The web build's font has no glyph for U+00D7 and draws a tofu box.
    Everything user-visible in this app is ASCII bar one interpunct."""
    assert format_value(Kind.MULTIPLIER, 20_000_000_000).isascii()


# -- which parameters a pool has -------------------------------------------


def test_rows_are_in_table_order_and_skip_what_is_missing() -> None:
    """A StableSwap pool has no gamma. That is not a failed read, it is
    what a StableSwap pool is, so no row appears for it."""
    stable = rows({"A": 4_000, "fee": 1_500_000})
    assert [parameter.key for parameter, _ in stable] == ["A", "fee"]

    crypto = rows(
        {
            "fee": 3_825_607,
            "A": 1_707_629,
            "gamma": 11_809_167_828_997,
            "mid_fee": 3_000_000,
            "out_fee": 30_000_000,
            "fee_gamma": 500_000_000_000_000,
            "price_scale": 64_996_211_703_193_777_726_748,
        }
    )
    # Table order, not the order they were read or the order of the dict.
    assert [parameter.key for parameter, _ in crypto] == [
        "A", "gamma", "fee", "mid_fee", "out_fee", "fee_gamma", "price_scale",
    ]


def test_nothing_answered_is_no_rows_rather_than_a_row_of_dashes() -> None:
    assert rows({}) == []


def test_every_parameter_has_a_note() -> None:
    """The label alone does not explain `fee_gamma` to anybody."""
    assert all(parameter.note for parameter in PARAMETERS)


# -- reading them ----------------------------------------------------------


def make_pool() -> Pool:
    pool = Pool(
        address="0x" + "11" * 20,
        name="Test",
        chain="ethereum",
        chain_id=1,
        registry="factory_tricrypto",
        lp_token="0x" + "11" * 20,
        coins=[Coin("0x" + f"{i:02x}" * 20, f"C{i}", 18, index=i) for i in range(2)],
    )
    pool.onchain_coins = 2
    return pool


class ScriptedProvider:
    """Answers the selectors it knows and nothing else.

    Which is exactly what a pool does: a method it does not implement
    returns empty data rather than an error, and `_read` turns that into
    a `PoolCallFailed`.
    """

    def __init__(self, answers: dict[str, int]) -> None:
        from curve import abi

        self.abi = abi
        self.answers = answers
        self.asked: list[str] = []

    async def call(self, to: str, data: str) -> str:
        self.asked.append(data)
        for name, value in self.answers.items():
            if data.startswith(self.abi.encode_parameter(name)):
                return "0x" + f"{value:064x}"
            if data.startswith(self.abi.encode_indexed_parameter(name, 0)[:10]):
                return "0x" + f"{value:064x}"
        return "0x"


def contract_with(answers: dict[str, int]) -> PoolContract:
    return PoolContract(ScriptedProvider(answers), make_pool(), "0x" + "22" * 20)


async def test_a_stableswap_pool_answers_two_of_them() -> None:
    contract = contract_with({"A": 4_000, "fee": 1_500_000})
    assert await contract.parameters() == {"A": 4_000, "fee": 1_500_000}


async def test_a_crypto_pool_answers_most_of_them() -> None:
    answers = {
        "A": 1_707_629,
        "gamma": 11_809_167_828_997,
        "fee": 3_825_607,
        "mid_fee": 3_000_000,
        "out_fee": 30_000_000,
        "fee_gamma": 500_000_000_000_000,
    }
    assert await contract_with(answers).parameters() == answers


async def test_a_pool_that_answers_nothing_is_empty_not_an_error() -> None:
    """An address with no code on this chain, for instance -- which is
    what browsing one network with a wallet on another used to look
    like."""
    assert await contract_with({}).parameters() == {}


async def test_an_indexed_price_is_found_after_the_plain_one_fails() -> None:
    """Tricrypto pools hold several prices and take an index; twocrypto
    and the stable factories hold one and take none. The registry does
    not say which, so both spellings are tried."""
    from curve import abi

    class OnlyIndexed(ScriptedProvider):
        async def call(self, to: str, data: str) -> str:
            self.asked.append(data)
            if data.startswith(abi.encode_indexed_parameter("price_oracle", 0)[:10]):
                return "0x" + f"{65_003_125_444_859_976_272_179:064x}"
            return "0x"

    contract = PoolContract(OnlyIndexed({}), make_pool(), "")
    values = await contract.parameters()

    assert values == {"price_oracle": 65_003_125_444_859_976_272_179}


async def test_only_the_prices_are_tried_twice() -> None:
    """Nine parameters, and eight of them have one spelling. Trying a
    second for each would double the reads a pool page makes."""
    contract = contract_with({})
    await contract.parameters()

    asked = contract.provider.asked
    assert len(asked) == len(PARAMETERS) + len(INDEXED_PARAMETERS)


async def test_a_provider_that_cannot_read_at_all_is_not_fatal() -> None:
    """No wallet and no public node for the chain. The page keeps its
    addresses, which need no chain at all."""

    class Dead:
        async def call(self, to: str, data: str) -> str:
            raise WalletError("No public node is known for this network.")

    assert await PoolContract(Dead(), make_pool(), "").parameters() == {}


async def test_a_reverting_read_does_not_stop_the_others() -> None:
    from wallet.base import RpcError

    class Reverts(ScriptedProvider):
        async def call(self, to: str, data: str) -> str:
            from curve import abi

            if data.startswith(abi.encode_parameter("gamma")):
                raise RpcError(-32000, "execution reverted")
            return await super().call(to, data)

    contract = PoolContract(Reverts({"A": 4_000, "fee": 1_500_000}), make_pool(), "")
    assert await contract.parameters() == {"A": 4_000, "fee": 1_500_000}


def test_the_reader_and_the_table_agree_on_names() -> None:
    """`parameters()` iterates the table, so a typo in either would show
    up as a parameter that is read and never displayed."""
    assert set(INDEXED_PARAMETERS) <= {parameter.key for parameter in PARAMETERS}


# -- explorers -------------------------------------------------------------


def test_a_known_chain_gets_its_own_explorer() -> None:
    assert explorers.address_url(1, "0xabc") == "https://etherscan.io/address/0xabc"
    assert explorers.address_url(100, "0xabc").startswith("https://gnosisscan.io")


def test_a_chain_that_publishes_one_wins_over_the_table() -> None:
    """The Lite chains say which explorer they use, and they are exactly
    the chains a hardcoded table would be wrong about."""
    url = explorers.address_url(146, "0xabc", "https://custom.example/")
    assert url == "https://custom.example/address/0xabc"


def test_an_unknown_chain_still_gets_a_link() -> None:
    """blockscan searches an address across chains. Not a real explorer
    for any one of them, and better than a dead link."""
    assert explorers.address_url(31337, "0xabc").startswith(explorers.FALLBACK)


def test_no_address_is_no_link() -> None:
    assert explorers.address_url(1, "") == ""


async def test_it_does_not_raise_on_a_pool_call_failure() -> None:
    """`parameters()` swallows exactly two exception types; anything else
    would reach a page that has no handler for it."""
    assert issubclass(PoolCallFailed, WalletError)
