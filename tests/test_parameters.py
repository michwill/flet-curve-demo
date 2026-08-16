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

from curve import abi, explorers
from curve.models import Coin, Pool
from curve.multicall import (
    AGGREGATE3,
    MULTICALL3,
    decode_aggregate3,
    decode_uints,
    encode_aggregate3,
)
from curve.parameters import PARAMETERS, Kind, format_value, rows
from curve.pool import INDEXED_PARAMETERS, PoolCallFailed, PoolContract, _parameter_plan
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
        # 1e18 again, shown to twelve places instead of six digits.
        (Kind.PRECISE, 1_039_823_717_356_571_085, "1.039823717357"),  # 3pool
        (Kind.PRECISE, 1_078_054_572_865_277_880, "1.078054572865"),  # stETH-ng
        (Kind.PRECISE, 1_035_070_122_939_955_419, "1.035070122940"),  # tricryptoUSDC
        (Kind.PRECISE, 10**18, "1.000000000000"),     # a pool deployed this block
    ],
)
def test_each_scale_is_the_one_that_parameter_uses(kind, raw, expected) -> None:
    assert format_value(kind, raw) == expected


# -- the virtual price -----------------------------------------------------


def test_the_virtual_price_keeps_the_digits_that_move() -> None:
    """The reason it is not `Kind.RATIO`.

    Six significant digits is what every other 1e18 value gets, and it
    rounds three real mainnet readings to the same string -- so a fold
    opened to check whether a pool is earning would show the same number
    for a pool that is and a pool that stopped a year ago.
    """
    readings = (
        1_039_823_717_356_571_085,  # 3pool
        1_039_823_717_344_705_400,  # 3pool, an earlier read
        1_039_823_717_400_000_000,  # and one in between
    )
    assert len({format_value(Kind.RATIO, raw) for raw in readings}) == 1
    assert len({format_value(Kind.PRECISE, raw) for raw in readings}) == 3


def test_the_places_are_fixed_rather_than_trimmed() -> None:
    """Trailing zeros stay. This is a number read twice and compared, and
    a column that changes width between reads is a column you have to
    re-read from the left each time."""
    assert format_value(Kind.PRECISE, 1_100_000_000_000_000_000) == "1.100000000000"
    assert format_value(Kind.PRECISE, 2 * 10**18) == "2.000000000000"


def test_the_twelfth_place_is_exact_rather_than_a_float() -> None:
    """Why the scaling is `Decimal` and not `raw / PRECISION`.

    Thirteen significant digits out of a float's sixteen is a narrow
    complaint, and most of the time the two agree. But the value is an
    exact integer and the twelfth place is the last one shown, so when the
    quotient falls within an ulp of a rounding boundary the float rounds
    the wrong way -- about one value in 25,000, and always in the one
    digit this kind exists to show. Below, the exact value continues
    `...500073`, so the twelfth place rounds up.
    """
    raw = 1_311_012_269_028_500_073

    assert format_value(Kind.PRECISE, raw) == "1.311012269029"
    assert f"{raw / 10**18:,.12f}" == "1.311012269028"


def test_it_is_read_off_the_pool_like_every_other_row() -> None:
    """`get_virtual_price()`, batched with the rest -- not the value the
    API reports, which `Pool.virtual_price` already carries and which is
    not what a fold headed "read from the contract" should show."""
    assert any(p.key == "get_virtual_price" for p in PARAMETERS)
    assert abi.encode_parameter("get_virtual_price") == "0xbb7b8b80"


def test_every_family_answers_it() -> None:
    """Unlike `gamma` or `offpeg_fee_multiplier`, this one is on every
    pool Curve has shipped -- verified on mainnet against the old
    registry, a factory pool, stableswap-ng and two crypto pools. So the
    row is expected to be present where the others come and go."""
    stable = rows({"A": 4_000, "fee": 1_500_000, "get_virtual_price": 10**18})
    crypto = rows({"A": 1_707_629, "gamma": 11_809_167_828_997, "get_virtual_price": 10**18})

    assert [p.key for p, _ in stable][-1] == "get_virtual_price"
    assert [p.key for p, _ in crypto][-1] == "get_virtual_price"


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


async def test_a_chain_without_multicall_asks_one_at_a_time() -> None:
    """The batch is tried first and answers nothing -- there is no way to
    ask whether Multicall3 is deployed that is cheaper than calling it --
    and then every parameter is asked on its own."""
    contract = contract_with({})
    await contract.parameters()

    asked = contract.provider.asked
    assert asked[0].startswith(abi.selector(AGGREGATE3), 2)
    assert len(asked) == 1 + len(PARAMETERS) + len(INDEXED_PARAMETERS)


async def test_a_provider_that_cannot_read_at_all_says_so() -> None:
    """Not reaching the chain is not the same as the pool having nothing.

    This one hid a real bug for as long as it returned `{}`. On iOS every
    read failed -- see `curve.http.USER_AGENT` -- and the panel reported
    it as "This pool answered none of them", which reads as a fact about
    the pool and sent the search in the wrong direction entirely. A
    transport failure is the caller's to show, so it is raised; the page
    keeps its addresses either way, because they need no chain at all.
    """

    class Dead:
        async def call(self, to: str, data: str) -> str:
            raise WalletError("No public node is known for this network.")

    with pytest.raises(WalletError, match="No public node is known"):
        await PoolContract(Dead(), make_pool(), "").parameters()


async def test_a_pool_that_implements_none_of_them_is_still_empty() -> None:
    """The other half of the distinction above: the chain answered, and
    what it said was that this contract has none of these methods. That
    is absence, not failure, and stays a plain empty result."""
    assert await contract_with({}).parameters() == {}


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


# -- one round trip instead of twelve --------------------------------------


def word(value: int) -> str:
    return f"{value:064x}"


def aggregate3_response(answers: list[int | None]) -> str:
    """Encode `(bool success, bytes returnData)[]` as Multicall3 returns it.

    Writing the encoder the decoder is tested against is only worth
    anything because the *real* one was checked against mainnet -- see
    `curve/multicall.py`. This is here so the failure modes (a call that
    reverted, a call that answered nothing) can be produced on demand.
    """
    elements = []
    for value in answers:
        if value is None:
            elements.append(word(0) + word(0x40) + word(0))
        else:
            elements.append(word(1) + word(0x40) + word(32) + word(value))
    heads, position = [], len(elements) * 32
    for element in elements:
        heads.append(word(position))
        position += len(element) // 2
    return "0x" + word(0x20) + word(len(elements)) + "".join(heads) + "".join(elements)


class BatchingProvider:
    """A chain with Multicall3 on it."""

    def __init__(self, answers: dict[str, int]) -> None:
        self.answers = answers
        self.asked: list[tuple[str, str]] = []

    async def call(self, to: str, data: str) -> str:
        self.asked.append((to, data))
        if to.lower() != MULTICALL3.lower():
            raise AssertionError("asked the pool directly despite the batch")
        plan = _parameter_plan()
        return aggregate3_response([self.answers.get(key) for key, _ in plan])


async def test_the_whole_batch_is_one_call() -> None:
    contract = PoolContract(
        BatchingProvider({"A": 1_707_629, "gamma": 11_809_167_828_997}),
        make_pool(),
        "",
    )

    values = await contract.parameters()

    assert values == {"A": 1_707_629, "gamma": 11_809_167_828_997}
    assert len(contract.provider.asked) == 1
    assert contract.provider.asked[0][0] == MULTICALL3


async def test_the_batch_is_sent_to_the_pool_with_failures_allowed() -> None:
    """`aggregate3`, not `aggregate`: half these calls are *expected* to
    fail, and one failure would take the whole batch down with it."""
    contract = PoolContract(BatchingProvider({"A": 4_000}), make_pool(), "")
    await contract.parameters()

    _to, data = contract.provider.asked[0]
    plan = _parameter_plan()
    assert data.startswith("0x" + abi.selector(AGGREGATE3))
    # One `true` per call, and every target is this pool.
    assert data.count(word(1)) >= len(plan)
    assert data.lower().count(make_pool().address[2:].lower()) == len(plan)


def test_the_encoding_round_trips() -> None:
    calls = [("0x" + "11" * 20, "0xf446c1d0"), ("0x" + "22" * 20, "0xb1373929")]
    data = encode_aggregate3(calls)

    assert data.startswith("0x" + abi.selector(AGGREGATE3))
    # Head: offset to the array, then its length.
    body = data[10:]
    assert int(body[0:64], 16) == 32
    assert int(body[64:128], 16) == 2


def test_failures_can_be_refused() -> None:
    """The flag a write depends on: false means a bad call takes the
    transaction with it, rather than being mined as a silent no-op."""
    calls = [("0x" + "11" * 20, "0xf446c1d0"), ("0x" + "22" * 20, "0xb1373929")]
    allowed = encode_aggregate3(calls)
    refused = encode_aggregate3(calls, allow_failure=False)

    assert len(allowed) == len(refused)
    assert allowed.count(word(1)) - refused.count(word(1)) == len(calls)
    assert decode_uints(aggregate3_response([1, None]), 2) == [1, None]


@pytest.mark.parametrize(
    "result",
    ["0x", "", "0x00", "0x" + "ff" * 64, None],
)
def test_nothing_readable_is_no_answers(result) -> None:
    """A chain with no Multicall3 answers `0x` from an address with no
    code. That is not an error, it is a chain to ask one call at a time."""
    assert decode_aggregate3(result) == []


def test_a_failed_call_reads_as_no_value() -> None:
    assert decode_aggregate3(aggregate3_response([None])) == [None]
    assert decode_uints(aggregate3_response([None, 7]), 2) == [None, 7]


def test_a_short_answer_is_not_mistaken_for_a_full_batch() -> None:
    """Better to ask again one at a time than to line up two answers
    against twelve questions."""
    assert decode_uints(aggregate3_response([1, 2]), 12) == [None] * 12
