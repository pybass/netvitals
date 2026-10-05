"""Typed wrapper around pyobjc for a macOS menu bar item and its window: TrayApp, MenuItem, MenuSeparator, Window.

Objective-C selectors, NSObject subclassing, and run-loop timers stay in here, so the tray above it
reads as menu rows and Python callbacks. AppKit may be touched from the main thread only:
`call_on_main` is the one way in from any other thread. Nothing here knows what netvitals measures.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Self

# pyobjc resolves framework classes at runtime and ships no stubs; the missing-import errors for
# these modules are silenced in pyproject.toml.
import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSBackingStoreBuffered,
    NSButton,
    NSColor,
    NSControlSizeSmall,
    NSImage,
    NSImageTrailing,
    NSLineBreakByTruncatingTail,
    NSMenu,
    NSMenuItem,
    NSProgressIndicator,
    NSProgressIndicatorStyleSpinning,
    NSStatusBar,
    NSTextField,
    NSVariableStatusItemLength,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSMakeRect, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer
from PyObjCTools import AppHelper

from netvitals.core.errors import AppError

# Window geometry, in points. The width is fixed: rows are replaced while the window is being read,
# and a window that also changed its width with every result would never sit still.
_WINDOW_WIDTH = 460.0
_WINDOW_MARGIN = 16.0  # Between the window edge and its content, and above the footer
_ROW_HEIGHT = 22.0
_LABEL_WIDTH = 104.0  # The label column; the values start right after it
_SPINNER_SIZE = 16.0  # What AppKit draws for a small spinning indicator
_SPINNER_GAP = 6.0  # Between a value and the spinner after it


def call_on_main[**P](callback: Callable[P, None], *args: P.args, **kwargs: P.kwargs) -> None:
    """Schedule *callback* on the main thread and return at once; safe to call from any thread.

    Calls made from one thread run in the order they were made.
    """
    AppHelper.callAfter(callback, *args, **kwargs)


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
        self.button_callback: Callable[[], None] | None = None
        return self

    def menuItemClicked_(self, sender: object) -> None:  # noqa: N802  # Objective-C selector name
        """Dispatch a click to the callback registered under the sender's tag."""
        ns_sender: Any = sender  # NSMenuItem: .tag() needs dynamic access
        self.callbacks[ns_sender.tag()]()

    def timerFired_(self, _timer: object) -> None:  # noqa: N802  # Objective-C selector name
        """Run the poll callback on every tick."""
        if self.timer_callback is not None:
            self.timer_callback()

    def buttonClicked_(self, _sender: object) -> None:  # noqa: N802  # Objective-C selector name
        """Run the button callback on every click."""
        if self.button_callback is not None:
            self.button_callback()


@dataclass(frozen=True, slots=True)
class Row:
    """One line of a Window: a label, its value, and whether the value is still on its way."""

    label: str
    value: str = ""
    busy: bool = False  # Draws a spinner after the value; alone in the row while the value is empty


class Window:
    """A small window: label/value rows above a footer with a status line and one button.

    Closing the window only hides it, so `show()` brings the same Window back with its rows intact.
    """

    def __init__(self, title: str, *, button: str, on_button: Callable[[], None]) -> None:
        """Build the window without showing it; *on_button* runs on every click of the footer button."""
        self._dispatcher: Any = _Dispatcher.alloc().init()
        self._dispatcher.button_callback = on_button
        self._window: Any = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, _WINDOW_WIDTH, 0), NSWindowStyleMaskTitled | NSWindowStyleMaskClosable, NSBackingStoreBuffered, False
        )
        self._window.setTitle_(title)
        # AppKit frees a window when it closes unless told otherwise, and the next show() would crash.
        self._window.setReleasedWhenClosed_(False)
        self._row_views: list[Any] = []  # Every view of the current rows, to remove them on the next set_rows()

        self._button: Any = NSButton.buttonWithTitle_target_action_(button, self._dispatcher, "buttonClicked:")
        self._button.setKeyEquivalent_("\r")  # The default button: Return clicks it
        self._button.sizeToFit()
        button_size = self._button.frame().size
        self._button.setFrameOrigin_((_WINDOW_WIDTH - _WINDOW_MARGIN - button_size.width, _WINDOW_MARGIN))
        self._footer_height: float = button_size.height
        self._status: Any = NSTextField.labelWithString_(" ")  # A blank, so sizeToFit() yields the height of a line
        self._status.setTextColor_(NSColor.secondaryLabelColor())
        self._status.sizeToFit()
        status_height = self._status.frame().size.height
        self._status.setFrame_(
            NSMakeRect(
                _WINDOW_MARGIN,
                _WINDOW_MARGIN + (button_size.height - status_height) / 2,
                _WINDOW_WIDTH - 3 * _WINDOW_MARGIN - button_size.width,
                status_height,
            )
        )
        self._window.contentView().addSubview_(self._button)
        self._window.contentView().addSubview_(self._status)

        # An accessory app shows no menu bar, but key equivalents are still looked up in the main
        # menu: without these entries Cmd-C copies nothing and Cmd-W does not close the window.
        shortcuts = NSMenu.alloc().init()
        shortcuts.addItemWithTitle_action_keyEquivalent_("Copy", "copy:", "c")
        shortcuts.addItemWithTitle_action_keyEquivalent_("Close", "performClose:", "w")
        holder = NSMenuItem.alloc().init()
        holder.setSubmenu_(shortcuts)
        main_menu = NSMenu.alloc().init()
        main_menu.addItem_(holder)
        NSApplication.sharedApplication().setMainMenu_(main_menu)

        self.set_rows(())
        self._window.center()

    def show(self) -> None:
        """Bring the window to the front, reopening it when it was closed."""
        # An accessory app is never frontmost by itself: without activation the window opens behind
        # whatever the user was working in.
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self._window.makeKeyAndOrderFront_(None)

    def set_rows(self, rows: Sequence[Row]) -> None:
        """Replace every row, and grow or shrink the window downwards to fit them."""
        for view in self._row_views:
            view.removeFromSuperview()
        self._row_views.clear()

        height = 3 * _WINDOW_MARGIN + self._footer_height + len(rows) * _ROW_HEIGHT
        old = self._window.frame()
        new = self._window.frameRectForContentRect_(NSMakeRect(0, 0, _WINDOW_WIDTH, height))
        # The origin is the bottom-left corner: hold the top edge, or the title bar jumps on every change.
        self._window.setFrame_display_(
            NSMakeRect(old.origin.x, old.origin.y + old.size.height - new.size.height, new.size.width, new.size.height), True
        )

        value_x = _WINDOW_MARGIN + _LABEL_WIDTH
        for index, row in enumerate(rows):
            bottom = height - _WINDOW_MARGIN - (index + 1) * _ROW_HEIGHT
            label = NSTextField.labelWithString_(row.label)
            label.setTextColor_(NSColor.secondaryLabelColor())
            self._add_row_view(label, _WINDOW_MARGIN, bottom, _LABEL_WIDTH)
            spinner_x = value_x
            if row.value:
                value = NSTextField.labelWithString_(row.value)
                value.setSelectable_(True)
                value.setLineBreakMode_(NSLineBreakByTruncatingTail)
                value.sizeToFit()
                # Room for a spinner is always kept, so a long value truncates the same with and without one.
                room = _WINDOW_WIDTH - _WINDOW_MARGIN - value_x - _SPINNER_SIZE - _SPINNER_GAP
                width = min(value.frame().size.width, room)
                self._add_row_view(value, value_x, bottom, width)
                spinner_x += width + _SPINNER_GAP
            if row.busy:
                spinner = NSProgressIndicator.alloc().init()
                spinner.setStyle_(NSProgressIndicatorStyleSpinning)
                spinner.setControlSize_(NSControlSizeSmall)
                spinner.startAnimation_(None)
                self._add_row_view(spinner, spinner_x, bottom, _SPINNER_SIZE)

    def _add_row_view(self, view: object, x: float, row_bottom: float, width: float) -> None:
        """Place *view* in its row, centered vertically at its own natural height."""
        ns_view: Any = view  # NSView: its methods need dynamic access
        ns_view.sizeToFit()
        view_height = ns_view.frame().size.height
        ns_view.setFrame_(NSMakeRect(x, row_bottom + (_ROW_HEIGHT - view_height) / 2, width, view_height))
        self._window.contentView().addSubview_(ns_view)
        self._row_views.append(ns_view)

    def set_status(self, text: str) -> None:
        """Replace the status line in the footer."""
        self._status.setStringValue_(text)

    def set_button_enabled(self, enabled: bool) -> None:
        """Enable or grey out the footer button."""
        self._button.setEnabled_(enabled)


class TrayApp:
    """A macOS menu bar item: a text label, an icon beside it, a dropdown menu, and a poll timer."""

    def __init__(self, title: str) -> None:
        """Create the status bar item and the callback dispatcher."""
        self._nsapp = NSApplication.sharedApplication()  # The process-wide AppKit application
        # Accessory: a menu bar item with no Dock tile and no app menu.
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
