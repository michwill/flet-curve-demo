"""`safe_update` against a real Flet session, which is where a dropped control shows.

A control swapped out of the tree keeps its parent, so `update()` still reaches
the page and Flet sends a patch the client can no longer place.
"""

from __future__ import annotations

import asyncio

import flet as ft
import msgpack
from flet.controls.base_control import BaseControl
from flet.messaging.connection import Connection
from flet.messaging.protocol import MessageAction, configure_encode_object_for_msgpack
from flet.messaging.session import Session
from flet.pubsub.pubsub_hub import PubSubHub

from ui import safe_update

_encode = configure_encode_object_for_msgpack(BaseControl)


class Wire(Connection):
    """What a client would receive, encoded: encoding leaves the next diff's snapshot."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        super().__init__()
        self.loop = loop
        self.pubsubhub = PubSubHub(loop=loop, executor=None)
        self.frames: list[tuple[int, bytes]] = []
        self.session: Session | None = None  # parents are weak; this keeps the tree alive

    def send_message(self, message) -> None:
        self.frames.append(
            (
                message.body.id if message.action == MessageAction.PATCH_CONTROL else -1,
                msgpack.packb([message.action, message.body], default=_encode),
            )
        )

    def patched(self) -> list[int]:
        return [target for target, _frame in self.frames if target >= 0]

    def carries(self, text: str) -> bool:
        return any(
            text
            in repr(
                msgpack.unpackb(frame, strict_map_key=False, ext_hook=lambda _c, d: d)
            )
            for _target, frame in self.frames
        )


async def _mounted():
    wire = Wire(asyncio.get_running_loop())
    session = wire.session = Session(wire)
    msgpack.packb(session.get_page_patch(), default=_encode)  # the client registering
    label = ft.Text("50 pools")
    pools = ft.Column([label])
    box = ft.Container(content=pools)
    session.page.controls.append(box)
    session.page.update()
    wire.frames.clear()
    return wire, box, pools, label


async def test_a_control_on_screen_is_patched() -> None:
    wire, _box, _pools, label = await _mounted()
    label.value = "100 pools"
    safe_update(label)
    assert wire.patched() == [label._i]


async def test_a_control_swapped_out_is_left_alone() -> None:
    wire, box, _pools, label = await _mounted()
    box.content = ft.Text("swap")
    box.update()
    wire.frames.clear()

    label.value = "100 pools"
    safe_update(label)
    assert wire.patched() == []


async def test_what_changed_while_away_arrives_when_it_is_back() -> None:
    wire, box, pools, label = await _mounted()
    box.content = ft.Text("swap")
    box.update()
    label.value = "100 pools"
    safe_update(label)
    wire.frames.clear()

    box.content = pools
    box.update()
    assert wire.carries("100 pools")
    label.value = "150 pools"
    safe_update(label)
    assert label._i in wire.patched()


async def test_a_control_never_shown_is_left_alone() -> None:
    wire, *_ = await _mounted()
    safe_update(ft.Text("nowhere"))
    assert wire.patched() == []
