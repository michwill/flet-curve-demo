"""Make Flutter-side failures diagnosable.

`FletTestApp` keeps only the last 256 KB of the Flutter test process's
output and prints it as a "tail" when something goes wrong. The Flet client
runs with debug logging on, and a single control patch for this app's pool
list is tens of kilobytes -- so the widget exception that actually failed
the test scrolls off long before the tail is printed, leaving nothing but
`Test failed. See exception logs above.`

Raising the cap keeps the exception in the buffer. These are class-level
private attributes, hence the name-mangled spelling.
"""

from __future__ import annotations

from flet.testing.flet_test_app import FletTestApp

FletTestApp._FletTestApp__flutter_output_limit = 8 * 1024 * 1024
FletTestApp._FletTestApp__flutter_output_line_limit = 16 * 1024
