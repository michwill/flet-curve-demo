"""Several calls in one wallet prompt, for everything that offers one.

Four places hand a wallet more than one call: the pool panels (an approval
and the action it is for), the Swap tab, the veCRV lock, and the portfolio
claim -- which carries no approval at all and batches one transaction per
gauge factory.  What goes in the batch differs at all four, and so does what
each says while it is in flight.  What must not differ is here:

- the capability is read before anything is built, and any failure is a no;
- an approval goes in front of the call that spends it;
- a batch is never approvals alone.  An allowance granted with nothing to
  spend it is one left standing, which is the thing an exact approval exists
  to avoid;
- `atomicRequired` is false, because a wallet that will do the sequence
  without promising atomicity is still worth one prompt instead of two;
- the wait is `wait_for_batch`, which asks after the batch's own id.  A batch
  has no transaction hash until it lands, and on a wallet that is not atomic
  it ends up with several.

Nothing here draws anything.  `waiting` is handed the batch id the moment the
wallet accepts it, so each caller says so in its own words.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from curve import confirm
from wallet import batch
from wallet.base import WalletProvider


class NotBatchable(Exception):
    """This action cannot be handed over as one batch, and why.

    Raised out of a collection rather than returned, because it has to stop
    the action where it stands: everything after the wait it interrupted
    would be built against state that has not moved.
    """


class Batching:
    """Whether the wallet takes several calls in one prompt, and the prompt.

    A mixin, because the four callers are a panel, a page, a view and the app
    itself: they share no ancestor and each keeps its own state, but all four
    want the same flag and the same three steps.
    """

    #: What the last capability read said, or None if there has not been one.
    #: Unannotated on purpose -- these are mixed into Flet controls, which are
    #: dataclasses, and an annotated class attribute would become a field.
    _batches = None

    @property
    def batches(self) -> bool:
        """What the wallet last said, and False before it has been asked.

        For drawing, which cannot wait on a round trip; `wallet_batches` is
        the question itself.
        """
        return self._batches is True

    async def wallet_batches(
        self,
        provider: WalletProvider,
        account: str,
        chain_id: int,
        *,
        fresh: bool = False,
    ) -> bool:
        """Whether this wallet takes several calls in one prompt (EIP-5792).

        Remembered: a capability belongs to the wallet and the chain, and a
        press should not wait on a read already done.  `forget_batching`
        drops it where either can move under an open panel, and `fresh` is
        for a caller that would rather ask every time than keep it.

        Any failure is a no.  A wallet that has never heard of
        `wallet_getCapabilities` raises, and one whose answer cannot be read
        is no better than one that refuses -- either way the path that works
        is the one that prompts twice.
        """
        if not account:
            return False
        if fresh or self._batches is None:
            self._batches = await batch.supported(provider, account, chain_id)
        return bool(self._batches)

    def forget_batching(self) -> None:
        """A different wallet, or a different chain, answers for itself."""
        self._batches = None

    async def collected(
        self, contract: Any, action: Callable[[], Awaitable[Any]]
    ) -> list[batch.Call]:
        """The calls `action` would send, without sending any of them.

        Rather than a second description of every action, the action itself
        runs with its sends diverted into a list -- which is what `collecting`
        on the contracts is for, and means the call that goes in the batch is
        the one the button would have sent.

        Exactly one comes back or this refuses.  More than one means the
        action sent twice without waiting, none means it sent nothing, and a
        batch can promise nothing about either.
        """
        with contract.collecting() as calls:
            await action()
            collected = list(calls)
        if len(collected) != 1:
            raise NotBatchable(
                f"the action collected {len(collected)} calls, not one"
            )
        return collected

    async def one_prompt(
        self,
        provider: WalletProvider,
        account: str,
        chain_id: int,
        *,
        approvals: Sequence[batch.Call] = (),
        action: Sequence[batch.Call],
        waiting: Callable[[str], None],
        interval: float = confirm.POLL_INTERVAL,
    ) -> int:
        """Hand the wallet the lot, and wait for what becomes of it.

        The approvals go first, because the call that spends an allowance
        cannot run before the call that grants it.  Returns the last block
        the batch touched, which is what a caller re-reads against.
        """
        if not action:
            # Approvals alone would leave an allowance standing with nothing
            # to spend it, which is the one outcome worse than two prompts.
            raise NotBatchable("a batch of approvals with nothing to spend them")
        batch_id = await batch.send(
            provider, account, chain_id, [*approvals, *action]
        )
        waiting(batch_id)
        # Through the module rather than by name: `wait_for_batch` is the
        # seam the tests stand a fake wallet behind.
        return await confirm.wait_for_batch(provider, batch_id, interval=interval)


__all__ = ["Batching", "NotBatchable"]
