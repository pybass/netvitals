"""Typed wrapper around pyobjc for a macOS menu bar item: TrayApp, MenuItem, MenuSeparator.

Objective-C selectors, NSObject subclassing, and run-loop timers stay in here, so the tray above it
reads as menu rows and Python callbacks. Everything runs on the main thread — there is no
cross-thread plumbing — and nothing here knows what netvitals measures.
"""

from collections.abc import Callable, Sequence
from typing import Any, Self

# pyobjc resolves framework classes at runtime and ships no stubs; the missing-import errors for
# these modules are silenced in pyproject.toml.
import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSImage,
    NSImageTrailing,
    NSMenu,
    NSMenuItem,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

from netvitals.core.errors import AppError


class MenuSeparator:
    """A separator line in the menu."""


class MenuItem:
    """One menu row, wrapping the NSMenuItem it builds on creation.

    The NSMenuItem is the only state — there is no shadow copy of the title to drift out of sync.
    A row without a callback is an info line: it is what makes the row unclickable *and* what greys
    it out, so the two can never be set inconsistently.
    """

    def __init__(self, title: str, *, callback: Callable[[], None] | None = None, hidden: bool = False) -> None:
        """Build the row; without a *callback* it is a non-interactive info line."""
        self.callback = callback  # Invoked on click; None makes this an info line
        # The selector is resolved against the dispatcher that TrayApp.set_menu() attaches.
        self.ns_item: Any = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "menuItemClicked:" if callback is not None else None, ""
        )
        self.ns_item.setEnabled_(callback is not None)
        self.ns_item.setHidden_(hidden)

    def set_title(self, title: str) -> None:
        """Replace the displayed text."""
        self.ns_item.setTitle_(title)

    def set_hidden(self, hidden: bool) -> None:
        """Show or hide the row, without rebuilding the menu."""
        self.ns_item.setHidden_(hidden)


class _Dispatcher(NSObject):  # type: ignore[misc]  # pyobjc ships no stubs, so mypy sees NSObject as Any
    """Routes Objective-C target-action and timer callbacks to plain Python callables.

    NSMenuItem carries an integer tag, and that is all the routing needed: the tag is the row's
    index in `callbacks`.
    """

    def init(self) -> Self:
        """Initialize the callback slots — pyobjc calls this instead of __init__."""
        self = objc.super(_Dispatcher, self).init()  # noqa: PLW0642  # reassigning self is pyobjc's documented init pattern
        self.callbacks: list[Callable[[], None]] = []
        self.timer_callback: Callable[[], None] | None = None
        return self

    def menuItemClicked_(self, sender: object) -> None:  # noqa: N802  # Objective-C selector name
        """Dispatch a click to the callback registered under the sender's tag."""
        ns_sender: Any = sender  # NSMenuItem: .tag() needs dynamic access
        self.callbacks[ns_sender.tag()]()

    def timerFired_(self, _timer: object) -> None:  # noqa: N802  # Objective-C selector name
        """Run the poll callback on every tick."""
        if self.timer_callback is not None:
            self.timer_callback()


class TrayApp:
    """A macOS menu bar item: a text label, an icon beside it, a dropdown menu, and a poll timer."""

    def __init__(self, title: str) -> None:
        """Create the status bar item and the callback dispatcher."""
        self._nsapp = NSApplication.sharedApplication()  # The process-wide AppKit application
        # Accessory: a menu bar item with no Dock tile and no app menu — this process has no windows.
        self._nsapp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)  # Our slot in the bar
        self._button = self._item.button()  # Title and image host since 10.10; NSStatusItem.setTitle_ is deprecated
        self._button.setTitle_(title)
        self._button.setImagePosition_(NSImageTrailing)  # Label first, then the icon
        self._icons: dict[str, Any] = {}  # NSImage per symbol name: the poll asks for the same handful forever
        self._dispatcher: Any = _Dispatcher.alloc().init()  # Objective-C side of every callback

    def set_title(self, title: str) -> None:
        """Replace the menu bar text."""
        self._button.setTitle_(title)

    def set_icon(self, symbol: str) -> None:
        """Show the named SF Symbol as the item's icon.

        Template images: macOS renders them in the menu bar's own foreground color, so they follow
        light and dark, invert under the highlight when the menu opens, and match the size of every
        other icon in the bar — none of which a text glyph in the title can do.

        Raises AppError when the running macOS does not know *symbol*: the lookup answers None, and
        the bare AttributeError that would follow says nothing about which name was wrong.
        """
        if symbol not in self._icons:
            image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(symbol, None)
            if image is None:
                raise AppError(f"unknown SF Symbol: {symbol}")
            image.setTemplate_(True)
            self._icons[symbol] = image
        self._button.setImage_(self._icons[symbol])

    def set_menu(self, items: Sequence[MenuItem | MenuSeparator]) -> None:
        """Build the dropdown from *items*, in display order."""
        menu = NSMenu.alloc().init()
        # Off, or AppKit decides enablement by itself and quietly overrules every setEnabled_ call.
        menu.setAutoenablesItems_(False)
        for item in items:
            if isinstance(item, MenuSeparator):
                menu.addItem_(NSMenuItem.separatorItem())
                continue
            if item.callback is not None:
                item.ns_item.setTarget_(self._dispatcher)
                item.ns_item.setTag_(len(self._dispatcher.callbacks))
                self._dispatcher.callbacks.append(item.callback)
            menu.addItem_(item.ns_item)
        self._item.setMenu_(menu)

    def start_timer(self, interval_sec: float, callback: Callable[[], None]) -> None:
        """Call *callback* every *interval_sec* — including while the menu is open.

        Common modes rather than the default one: an open menu puts the run loop into event
        tracking, where a default-mode timer stops firing. An open menu is the one place the
        numbers must stay live, so freezing them exactly while they are being read is not an option.
        """
        self._dispatcher.timer_callback = callback
        timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            interval_sec, self._dispatcher, "timerFired:", None, True
        )
        NSRunLoop.currentRunLoop().addTimer_forMode_(timer, NSRunLoopCommonModes)

    def quit(self) -> None:
        """Terminate the application."""
        self._nsapp.terminate_(None)

    def run(self) -> None:
        """Enter the AppKit event loop; returns only when the application terminates."""
        AppHelper.installMachInterrupt()  # Lets Ctrl-C reach us when the tray runs in the foreground
        AppHelper.runEventLoop()
