"""What the Swap tab says when a quoted route cannot be sent.

A route that quotes is not always a route that can be *shipped*: the router
refuses to encode one whose legs are too small to carry a minimum rate, which
is it declining to send something it cannot protect rather than anything
having gone wrong.  The quote itself is still the chain's own number.
"""

from __future__ import annotations

from ui.swap_page import _why_unsendable


class EncodingError(RuntimeError):
    """Named as the router names it, since the mapping keys off that."""


def test_a_leg_too_small_to_protect_is_said_in_one_line():
    """The router's own message is written for a terminal.

    Verbatim from a real refusal -- it names both pools, the wei in and out of
    each, and the flag that would ship it unprotected.  Right for someone
    debugging a route, and far too much under a swap button.
    """
    exc = EncodingError(
        "leg(s) 3 on 0xf05Bc5C38a8F5E3D98c72f11bC59E713F8a32228 (9329968064 "
        "in, 0 out), 5 on 0x8273Cb2cF9AF3228fD14AF25B5B1De2A9676C372 "
        "(35540624523651 in, 35 out) produce too little for a minimum rate to "
        "bound -- one unit of the output is more than the tolerance.  A leg "
        "worth that little is not worth executing; re-solve without it, or "
        "pass allow_unbounded to ship it unprotected"
    )
    said = _why_unsendable(exc)
    assert said == "This route has a leg too small to protect, so it cannot be sent"
    assert "0x" not in said and "allow_unbounded" not in said


def test_another_encoding_refusal_still_says_something_useful():
    assert _why_unsendable(EncodingError("32 legs is the most")) == (
        "This route cannot be packed into one call"
    )


def test_anything_else_is_reported_rather_than_hidden():
    said = _why_unsendable(RuntimeError("the endpoint went away\nand the rest"))
    assert said.startswith("This route cannot be sent: the endpoint went away")
    assert "\n" not in said, "one line, whatever the exception did"
