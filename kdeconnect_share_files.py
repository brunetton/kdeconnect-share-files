#!/usr/bin/env python3
"""Share one or more files with KDE Connect devices from a single GTK4 window.

The application talks to the running kdeconnectd daemon over the session bus
(org.kde.kdeconnect) instead of shelling out to kdeconnect-cli, so failures are
reported as real errors and no extra process is spawned.

The window walks through three steps:
  1. wait while the daemon lists the available devices
  2. pick the device that should receive the files
  3. wait while the files are handed over to the daemon, then close the window:
     KDE Connect reports no transfer result, so there is nothing else to say.
     KDE Connect shows the progress and the outcome of the transfer itself.
"""

import gettext
import os
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Adw", "1")  # noqa: E402
gi.require_version("Gtk", "4.0")  # noqa: E402

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # type: ignore  # noqa: E402

APP_ID = "org.kdeconnect.sharefile"
LOCALE_DIR = Path(__file__).resolve().parent / "locale"

gettext.bindtextdomain(APP_ID, LOCALE_DIR)
gettext.textdomain(APP_ID)
_ = gettext.gettext
N_ = gettext.ngettext

# KDE Connect DBus API.
KDE_CONNECT_SERVICE = "org.kde.kdeconnect"
DAEMON_PATH = "/modules/kdeconnect"
DEVICES_PATH = "/modules/kdeconnect/devices"
DAEMON_IFACE = "org.kde.kdeconnect.daemon"
DEVICE_IFACE = "org.kde.kdeconnect.device"
SHARE_IFACE = "org.kde.kdeconnect.device.share"
PROPERTIES_IFACE = "org.freedesktop.DBus.Properties"
SHARE_PLUGIN = "kdeconnect_share"
# Plugins are exported as child objects of their device: the share plugin lives
# at /modules/kdeconnect/devices/<id>/share, never on the device object itself.
SHARE_PLUGIN_PATH = "share"

DBUS_CALL_TIMEOUT_MS = 25_000
# When no device shows up right away, ask the daemon for a network discovery and
# keep polling for a little while before giving up.
DISCOVERY_TIMEOUT = 10.0
DISCOVERY_POLL_INTERVAL = 1.0
# The share plugin does not report transfer progress over DBus: the daemon
# acknowledges the request as soon as the payloads are queued. The third step is
# therefore an indeterminate wait, kept on screen long enough to be readable.
SENDING_STEP_MIN_DURATION = 0.7

STEP_DISCOVERING = "discovering"
STEP_SELECT = "select"
STEP_SENDING = "sending"
STEP_RESULT = "result"

# Icon names are tried in order and the first one present in the active icon
# theme wins, see resolve_icon_name(). Order matters: a theme can provide an icon
# that GTK4 still draws as a blank image, which happens with the symbolic SVG
# icons of elementary-xfce (used by Xfce) for phone/tablet/computer/send-to and
# for dialog-error. Their non-symbolic counterpart renders fine, hence the
# non-symbolic name always comes first here. All names below were checked
# against the Adwaita and the elementary-xfce themes.
DEVICE_ICONS = {
    "phone": ("phone", "phone-symbolic"),
    "tablet": ("tablet", "tablet-symbolic"),
    "desktop": ("computer", "computer-symbolic"),
    "laptop": ("laptop", "computer", "laptop-symbolic", "computer-symbolic"),
    "tv": ("tv", "video-display", "tv-symbolic", "video-display-symbolic"),
}
FALLBACK_DEVICE_ICONS = (
    "send-to",
    "document-send",
    "send-to-symbolic",
    "document-send-symbolic",
)
RESULT_ICONS = {
    "warning": (
        "dialog-warning",
        "dialog-warning-symbolic",
        "dialog-error",
        "dialog-error-symbolic",
    ),
    "error": (
        "dialog-error",
        "dialog-error-symbolic",
        "dialog-warning",
        "dialog-warning-symbolic",
    ),
}


class KdeConnectError(RuntimeError):
    """Raised when the daemon cannot be reached or refuses a request."""


@dataclass(frozen=True)
class Device:
    """A reachable, paired device that can receive files."""

    id: str
    name: str
    kind: str


def device_kind_label(kind: str) -> str:
    if kind == "phone":
        return _("Phone")
    if kind == "tablet":
        return _("Tablet")
    if kind == "desktop":
        return _("Computer")
    return _("Device")


def resolve_icon_name(candidates: tuple[str, ...], display: Gdk.Display | None) -> str:
    """Return the first candidate name the active icon theme provides.

    Gtk.IconTheme is the standard way to ask GTK which icon names exist; the
    names themselves follow the freedesktop icon naming specification.
    """
    if display is not None:
        theme = Gtk.IconTheme.get_for_display(display)
        for name in candidates:
            if theme.has_icon(name):
                return name
    return candidates[0]


def device_icon_name(kind: str, display: Gdk.Display | None = None) -> str:
    return resolve_icon_name(DEVICE_ICONS.get(kind, FALLBACK_DEVICE_ICONS), display)


def path_to_url(raw: str) -> str:
    """Turn a command line argument or a dropped path into a shareable URL."""
    if "://" in raw:
        return raw
    return Gio.File.new_for_path(os.path.abspath(os.path.expanduser(raw))).get_uri()


def url_to_path(url: str):
    """Return the local path of a file:// URL, or None for any other URL."""
    if not url.startswith("file://"):
        return None
    parsed = urlparse(url)
    if parsed.netloc not in ("", "localhost"):
        return None
    return unquote(parsed.path)


def split_inputs(raw_inputs) -> tuple[list[str], list[str]]:
    """Split inputs into shareable URLs and rejected (missing) entries."""
    urls: list[str] = []
    missing: list[str] = []
    for raw in raw_inputs:
        url = path_to_url(raw)
        path = url_to_path(url)
        if path is not None and not os.path.exists(path):
            missing.append(raw)
        elif url not in urls:
            urls.append(url)
    return urls, missing


def display_name(url: str) -> str:
    path = url_to_path(url)
    return os.path.basename(path) if path else url


def summarize_files(urls: list[str]) -> str:
    names = [display_name(url) for url in urls]
    if len(names) <= 3:
        return ", ".join(names)
    return _("%(files)s and %(count)d more") % {
        "files": ", ".join(names[:3]),
        "count": len(names) - 3,
    }


class KdeConnectClient:
    """Thin, blocking wrapper around the KDE Connect DBus API.

    Every method is meant to be called from a worker thread.
    """

    def __init__(self):
        self._connection = None
        self._lock = threading.Lock()

    def _conn(self) -> Gio.DBusConnection:
        with self._lock:
            if self._connection is None:
                try:
                    self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
                except GLib.Error as error:
                    raise KdeConnectError(
                        _("No session bus is available: %s") % error.message
                    ) from error
            return self._connection

    def _call(self, path, iface, method, parameters=None, reply_type=None):
        try:
            reply = self._conn().call_sync(
                KDE_CONNECT_SERVICE,
                path,
                iface,
                method,
                parameters,
                reply_type,
                Gio.DBusCallFlags.NONE,
                DBUS_CALL_TIMEOUT_MS,
                None,
            )
        except GLib.Error as error:
            if error.matches(Gio.DBusError, Gio.DBusError.SERVICE_UNKNOWN):
                raise KdeConnectError(
                    _("The KDE Connect daemon (kdeconnectd) is not running.")
                ) from error
            if error.matches(
                Gio.DBusError, Gio.DBusError.UNKNOWN_OBJECT
            ) or error.matches(Gio.DBusError, Gio.DBusError.UNKNOWN_INTERFACE):
                # The daemon only exports a plugin while it is loaded, so this
                # means the device went away between listing and use.
                raise KdeConnectError(
                    _(
                        "The device no longer provides this KDE Connect feature; "
                        "it probably went offline or was disconnected."
                    )
                ) from error
            raise KdeConnectError(error.message) from error

        if reply_type is None:
            return None

        unpacked = reply.unpack()
        if unpacked is None:
            return None
        if not isinstance(unpacked, tuple):
            return (unpacked,)
        return unpacked

    @staticmethod
    def _device_path(device_id: str) -> str:
        return f"{DEVICES_PATH}/{device_id}"

    @staticmethod
    def _share_path(device_id: str) -> str:
        """Path of the share plugin: plugins live below the device object."""
        return f"{DEVICES_PATH}/{device_id}/{SHARE_PLUGIN_PATH}"

    def _property(self, device_id: str, name: str):
        """Read a device property such as "type" or "isReachable"."""
        value = self._call(
            self._device_path(device_id),
            PROPERTIES_IFACE,
            "Get",
            GLib.Variant("(ss)", (DEVICE_IFACE, name)),
            GLib.VariantType.new("(v)"),
        )
        if value is None or not isinstance(value, tuple) or len(value) != 1:
            raise KdeConnectError(_("The device did not return a usable value."))
        # Reply is a single variant ("v"); GLib.Variant.unpack() already returns
        # the contained value, e.g. a bool for isReachable (never str() it).
        return value[0]

    def _can_share(self, device_id: str) -> bool:
        path = self._device_path(device_id)
        try:
            supported = self._call(
                path,
                DEVICE_IFACE,
                "hasPlugin",
                GLib.Variant("(s)", (SHARE_PLUGIN,)),
                GLib.VariantType.new("(b)"),
            )
            enabled = self._call(
                path,
                DEVICE_IFACE,
                "isPluginEnabled",
                GLib.Variant("(s)", (SHARE_PLUGIN,)),
                GLib.VariantType.new("(b)"),
            )
        except KdeConnectError:
            # Device running an older daemon: trust it and let the transfer fail
            # with a proper message if it cannot receive files.
            return True

        if supported is None or not isinstance(supported, tuple) or len(supported) != 1:
            return True
        if enabled is None or not isinstance(enabled, tuple) or len(enabled) != 1:
            return True
        return bool(supported[0]) and bool(enabled[0])

    def _kind(self, device_id: str) -> str:
        try:
            return str(self._property(device_id, "type"))
        except KdeConnectError:
            return ""

    def _available_devices(self) -> list[Device]:
        names_value = self._call(
            DAEMON_PATH,
            DAEMON_IFACE,
            "deviceNames",
            GLib.Variant("(bb)", (True, True)),
            GLib.VariantType.new("(a{ss})"),
        )
        if (
            names_value is None
            or not isinstance(names_value, tuple)
            or len(names_value) != 1
        ):
            return []
        names = names_value[0]
        devices = [
            Device(id=device_id, name=name, kind=self._kind(device_id))
            for device_id, name in names.items()
            if self._can_share(device_id)
        ]
        devices.sort(key=lambda device: device.name.casefold())
        return devices

    def list_devices(self, timeout: float = DISCOVERY_TIMEOUT) -> list[Device]:
        """List the devices that are paired, reachable and able to receive files."""
        deadline = time.monotonic() + timeout
        refresh_requested = False
        while True:
            devices = self._available_devices()
            if devices or time.monotonic() >= deadline:
                return devices
            if not refresh_requested:
                self._call(
                    DAEMON_PATH,
                    DAEMON_IFACE,
                    "forceOnNetworkChange",
                    GLib.Variant("()", ()),
                )
                refresh_requested = True
            time.sleep(DISCOVERY_POLL_INTERVAL)

    def send_urls(self, device_id: str, urls: list[str]) -> None:
        """Ask the daemon to send the given URLs to the given device."""
        self._call(
            self._share_path(device_id),
            SHARE_IFACE,
            "shareUrls",
            GLib.Variant("(as)", (urls,)),
        )

    def is_reachable(self, device_id: str) -> bool:
        """Whether the daemon currently considers the device reachable."""
        return bool(self._property(device_id, "isReachable"))


class ShareFileWindow(Adw.ApplicationWindow):
    def __init__(
        self, application: Adw.Application, urls: list[str], missing: list[str]
    ):
        super().__init__(application=application)
        self.set_title(_("Share files with KDE Connect"))
        self.set_default_size(460, 640)

        self._client = KdeConnectClient()
        self._urls = urls
        self._missing = missing
        self._devices: list[Device] = []
        self._device_rows: dict[Gtk.Widget, Device] = {}
        self._selected_device: Device | None = None
        self._discovering = False
        self._sending_started_at = 0.0

        self._stack = Gtk.Stack(
            transition_type=Gtk.StackTransitionType.CROSSFADE,
            transition_duration=200,
        )
        self._stack.add_named(self._build_discovering_page(), STEP_DISCOVERING)
        self._stack.add_named(self._build_select_page(), STEP_SELECT)
        self._stack.add_named(self._build_sending_page(), STEP_SENDING)
        self._stack.add_named(self._build_result_page(), STEP_RESULT)

        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=_("Share files")))
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._stack)
        self.set_content(toolbar)

        drop_target = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_files_dropped)
        self.add_controller(drop_target)

    # -- steps ------------------------------------------------------------

    def _build_discovering_page(self) -> Gtk.Widget:
        page = Adw.StatusPage(
            title=_("Looking for devices…"),
            description=_("Asking KDE Connect which devices can receive files."),
        )
        page.set_child(Adw.Spinner())
        return page

    def _build_select_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(18)
        page.set_margin_bottom(18)
        page.set_margin_start(18)
        page.set_margin_end(18)

        self._select_heading = Gtk.Label(xalign=0, wrap=True)
        self._select_heading.add_css_class("title-4")
        page.append(self._select_heading)

        self._files_summary = Gtk.Label(xalign=0, wrap=True)
        self._files_summary.add_css_class("dim-label")
        page.append(self._files_summary)

        scroller = Gtk.ScrolledWindow(
            vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER
        )
        self._device_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self._device_list.set_activate_on_single_click(False)
        self._device_list.add_css_class("boxed-list")
        self._device_list.connect("row-selected", self._on_device_selected)
        self._device_list.connect("row-activated", self._on_device_activated)
        scroller.set_child(self._device_list)
        page.append(scroller)

        self._send_button = Gtk.Button(label=_("Send"), halign=Gtk.Align.END)
        self._send_button.add_css_class("suggested-action")
        self._send_button.set_sensitive(False)
        self._send_button.connect("clicked", lambda _button: self._start_sending())
        page.append(self._send_button)
        return page

    def _build_sending_page(self) -> Gtk.Widget:
        self._sending_page = Adw.StatusPage()
        self._sending_page.set_child(Adw.Spinner())
        return self._sending_page

    def _build_result_page(self) -> Gtk.Widget:
        self._result_page = Adw.StatusPage()

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(6)
        content.set_margin_bottom(6)

        # The message is a real label instead of the StatusPage description so
        # that error texts can be selected and copied when reporting a problem.
        self._result_message = Gtk.Label(
            justify=Gtk.Justification.CENTER,
            wrap=True,
            selectable=True,
        )
        self._result_message.add_css_class("dim-label")
        content.append(Adw.Clamp(maximum_size=400, child=self._result_message))

        buttons = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6, halign=Gtk.Align.CENTER
        )
        self._retry_button = Gtk.Button(label=_("Try again"))
        self._retry_button.connect("clicked", lambda _button: self._on_retry_clicked())
        self._close_button = Gtk.Button(label=_("Close"))
        self._close_button.add_css_class("suggested-action")
        self._close_button.connect("clicked", lambda _button: self.close())
        buttons.append(self._close_button)
        buttons.append(self._retry_button)

        content.append(buttons)
        self._result_page.set_child(content)
        return self._result_page

    def _show_step(self, step: str) -> None:
        self._stack.set_visible_child_name(step)

    def _show_result(
        self, icon: str, title: str, description: str, retry: bool
    ) -> None:
        self._result_page.set_icon_name(
            resolve_icon_name(RESULT_ICONS[icon], self.get_display())
        )
        self._result_page.set_title(title)
        self._result_message.set_label(description)
        self._result_message.set_visible(bool(description))
        self._retry_button.set_visible(retry)
        self._show_step(STEP_RESULT)
        # GTK selects the whole text of a selectable label when it gains focus,
        # which makes the message harder to read. Give up that focus and the
        # selection as soon as the page is on screen: selecting the text stays
        # possible, it just is not done for the user.
        GLib.idle_add(self._unselect_result_message)

    def _unselect_result_message(self) -> bool:
        self._result_message.select_region(0, 0)
        if self._result_message.has_focus():
            self._close_button.grab_focus()
        return GLib.SOURCE_REMOVE

    # -- entry point ------------------------------------------------------

    def start(self) -> None:
        if self._missing:
            self._show_missing_files()
            return
        self._start_discovery()
        if not self._urls:
            GLib.idle_add(self._choose_files)

    def _show_missing_files(self) -> None:
        self._show_result(
            "error",
            _("File not found"),
            _("These files do not exist: %s") % ", ".join(self._missing),
            retry=False,
        )

    def _choose_files(self) -> bool:
        dialog = Gtk.FileDialog(title=_("Choose the files to share"))
        dialog.open_multiple(self, None, self._on_files_chosen)
        return GLib.SOURCE_REMOVE

    def _on_files_chosen(self, dialog: Gtk.FileDialog, result) -> None:
        try:
            model = dialog.open_multiple_finish(result)
        except GLib.Error:
            # The dialog was dismissed: without files there is nothing to do.
            self.close()
            return
        files = [model.get_item(index) for index in range(model.get_n_items())]
        self._set_files([file.get_uri() for file in files])

    def _on_files_dropped(self, _target, value, _x, _y) -> bool:
        self._set_files([file.get_uri() for file in value.get_files()])
        return True

    def _set_files(self, raw_urls: list[str]) -> None:
        urls, missing = split_inputs(raw_urls)
        self._urls = urls
        self._missing = missing
        if missing:
            self._show_missing_files()
        elif self._devices:
            self._populate_devices()
            self._show_step(STEP_SELECT)
        elif self._discovering:
            self._show_step(STEP_DISCOVERING)
        else:
            self._start_discovery()

    # -- step 1: discover -------------------------------------------------

    def _start_discovery(self) -> None:
        self._devices = []
        self._discovering = True
        self._show_step(STEP_DISCOVERING)
        self._run_async(
            self._client.list_devices,
            self._on_devices_found,
            self._on_discovery_failure,
        )

    def _on_devices_found(self, devices: list[Device]) -> None:
        self._discovering = False
        self._devices = devices
        if not devices:
            self._show_result(
                "warning",
                _("No device available"),
                _(
                    "KDE Connect did not report any paired, reachable device able "
                    "to receive files. Check that the daemon runs and that the "
                    "device is on the same network."
                ),
                retry=True,
            )
            return
        if self._urls:
            self._populate_devices()
            self._show_step(STEP_SELECT)

    def _populate_devices(self) -> None:
        self._device_list.remove_all()
        self._device_rows.clear()
        self._selected_device = None
        self._send_button.set_sensitive(False)

        for device in self._devices:
            action_row = Adw.ActionRow(
                title=device.name,
                subtitle=device_kind_label(device.kind),
                activatable=True,
            )
            action_row.add_prefix(
                Gtk.Image.new_from_icon_name(
                    device_icon_name(device.kind, self.get_display())
                )
            )
            row = Gtk.ListBoxRow()
            row.set_child(action_row)
            self._device_list.append(row)
            self._device_rows[action_row] = device

        count = len(self._urls)
        self._select_heading.set_label(
            N_("Send %d file to…", "Send %d files to…", count) % count
        )
        self._files_summary.set_label(summarize_files(self._urls))
        self._files_summary.set_tooltip_text(
            "\n".join(display_name(url) for url in self._urls)
        )

    # -- step 2: pick the device ------------------------------------------

    def _on_device_selected(
        self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None
    ) -> None:
        device = self._device_rows.get(row.get_child()) if row is not None else None
        self._selected_device = device
        self._send_button.set_sensitive(device is not None)

    def _on_device_activated(self, listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        self._on_device_selected(listbox, row)
        self._start_sending()

    # -- step 3: send -----------------------------------------------------

    def _start_sending(self) -> None:
        device = self._selected_device
        if device is None or not self._urls:
            return

        count = len(self._urls)
        self._sending_page.set_title(
            N_("Sending %d file…", "Sending %d files…", count) % count
        )
        self._sending_page.set_description(
            _("Handing the files over to KDE Connect for “%s”.") % device.name
        )
        self._sending_started_at = time.monotonic()
        self._show_step(STEP_SENDING)

        urls = list(self._urls)

        def hand_over() -> None:
            # The device may be gone since it was listed, and the daemon answers
            # a share request without waiting for it, so check reachability first.
            if not self._client.is_reachable(device.id):
                raise KdeConnectError(_("the device is not reachable"))
            self._client.send_urls(device.id, urls)

        self._run_async(
            hand_over,
            lambda _result: self._on_files_handed_over(),
            lambda error: self._on_send_failure(device, error),
        )

    def _on_files_handed_over(self) -> None:
        """Close the window: KDE Connect reports no transfer result to show.

        The daemon's own window reports the progress and the outcome of the
        transfer, so there is nothing useful left to display here.
        """
        elapsed = time.monotonic() - self._sending_started_at
        remaining = SENDING_STEP_MIN_DURATION - elapsed
        if remaining > 0:
            GLib.timeout_add(int(remaining * 1000), self._close_window)
        else:
            self._close_window()

    def _close_window(self) -> bool:
        self.close()
        return GLib.SOURCE_REMOVE

    # -- step 4: failures -------------------------------------------------

    def _on_discovery_failure(self, error: Exception) -> None:
        self._discovering = False
        self._show_result(
            "error",
            _("Could not look up the devices"),
            str(error),
            retry=True,
        )

    def _on_send_failure(self, device: Device, error: Exception) -> None:
        self._show_result(
            "error",
            _("Could not send the files"),
            _("Sending to %(device)s failed: %(error)s")
            % {"device": device.name, "error": error},
            retry=True,
        )

    def _on_retry_clicked(self) -> None:
        if self._devices:
            self._populate_devices()
            self._show_step(STEP_SELECT)
        else:
            self._start_discovery()

    # -- helpers ----------------------------------------------------------

    def _run_async(self, work, on_success, on_error) -> None:
        """Run a blocking DBus call in a worker thread and report back on the UI thread."""

        def runner() -> None:
            try:
                result = work()
            except Exception as error:  # surfaced to the user by on_error
                GLib.idle_add(self._handle_async_result, on_error, error)
            else:
                GLib.idle_add(self._handle_async_result, on_success, result)

        threading.Thread(target=runner, daemon=True).start()

    def _handle_async_result(self, callback, value) -> bool:
        """Run an async callback, never leaving the window stuck on a stale step."""
        try:
            callback(value)
        except Exception as error:  # a bug in a callback must not freeze the UI
            self._show_result(
                "error",
                _("Unexpected error"),
                str(error),
                retry=False,
            )
        return GLib.SOURCE_REMOVE


class ShareFileApplication(Adw.Application):
    def __init__(self, urls: list[str], missing: list[str]):
        super().__init__(
            application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS
        )
        self._urls = urls
        self._missing = missing

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = ShareFileWindow(self, self._urls, self._missing)
            window.present()
            window.start()
        else:
            window.present()


def main(argv: list[str]) -> int:
    urls, missing = split_inputs(argv[1:])
    application = ShareFileApplication(urls, missing)
    return application.run([argv[0]])


def run() -> int:
    """Entry point of the installed command, see [project.scripts]."""
    return main(sys.argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
