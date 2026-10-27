"""lpowerd-tray - StatusNotifierItem applet.

Speaks the StatusNotifierItem + com.canonical.dbusmenu protocols directly
over D-Bus, so it needs no AppIndicator/GTK libraries and shows up in the
COSMIC status area, KDE, GNOME (AppIndicator extension), XFCE, waybar...
"""

import json
import logging
import os
import signal
import sys

import dbus
import dbus.service
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

from . import BUS_NAME, INTERFACE, OBJECT_PATH

log = logging.getLogger("lpowerd-tray")

SNI_IFACE = "org.kde.StatusNotifierItem"
MENU_IFACE = "com.canonical.dbusmenu"
PROPS = dbus.PROPERTIES_IFACE

PROFILE_LABELS = {"quiet": "Quiet", "balance": "Balanced", "performance": "Performance",
                  "extreme": "Extreme", "custom": "Custom"}
ID_HEADER, ID_DETAIL, ID_CONS, ID_RAPID, ID_FULL, ID_PROFILE, ID_OPEN, ID_QUIT = 1, 2, 10, 11, 12, 20, 98, 99
PROFILE_BASE = 100


PROFILE_ICONS = {"quiet": "power-profile-power-saver-symbolic",
                 "balance": "power-profile-balanced-symbolic",
                 "performance": "power-profile-performance-symbolic"}


def icon_for(st):
    # Deliberately not a battery icon: COSMIC already shows one right next to us.
    if not st:
        return "battery-missing-symbolic"
    if st["thermal"]["tripped"]:
        return "battery-caution-symbolic"
    return PROFILE_ICONS.get(st["profile"]["active"], "power-profile-balanced-symbolic")


def summary(st):
    if not st:
        return "lpowerd not running", "Start it with: systemctl start lpowerd"
    b, ch, th, pr = st["battery"], st["charge"], st["thermal"], st["profile"]
    title = f"Battery {b.get('capacity', '?')}% · {b.get('status', '')}"
    parts = []
    if ch["conservation"] and ch["limit"] == ch["conservation_level"]:
        parts.append(f"Conservation {ch['conservation_level']}%")
    elif ch["limit"] < 100:
        parts.append(f"Limit {ch['limit']}%")
    if ch["rapid_charge"]:
        parts.append("Rapid charge")
    if pr["active"]:
        parts.append(PROFILE_LABELS.get(pr["active"], pr["active"]))
    if th["temperature"] is not None:
        parts.append(f"{th['temperature']:.0f}°C" + (" ⚠ guard" if th["tripped"] else ""))
    return title, " · ".join(parts)


class Menu(dbus.service.Object):
    def __init__(self, bus, app):
        super().__init__(bus, "/MenuBar")
        self.app = app
        self.revision = 1

    def _items(self):
        st = self.app.status
        title, detail = summary(st)
        items = [
            (ID_HEADER, {"label": title, "enabled": False}),
            (ID_DETAIL, {"label": detail or " ", "enabled": False}),
            (3, {"type": "separator"}),
        ]
        if st:
            hw, ch, pr = st["hardware"], st["charge"], st["profile"]
            if hw["conservation"]:
                on = ch["conservation"] and ch["limit"] == ch["conservation_level"]
                items.append((ID_CONS, {"label": f"Conservation mode ({ch['conservation_level']}%)",
                                        "toggle-type": "checkmark", "toggle-state": int(on)}))
            if hw["rapid_charge"]:
                items.append((ID_RAPID, {"label": "Rapid charge", "toggle-type": "checkmark",
                                         "toggle-state": int(ch["rapid_charge"])}))
            if ch["limit"] < 100:
                items.append((ID_FULL, {"label": "Charge to 100% once",
                                        "enabled": not ch["full_once"] and st["on_ac"]}))
            if pr["choices"]:
                items.append((4, {"type": "separator"}))
                subs = []
                for i, name in enumerate(pr["choices"]):
                    subs.append((PROFILE_BASE + i, {
                        "label": PROFILE_LABELS.get(name, name), "toggle-type": "radio",
                        "toggle-state": int(name == pr["active"])}))
                items.append((ID_PROFILE, {
                    "label": "Profile: " + PROFILE_LABELS.get(pr["active"], pr["active"] or "?"),
                    "children-display": "submenu"}, subs))
        items += [(5, {"type": "separator"}),
                  (ID_OPEN, {"label": "Open Power Control…", "icon-name": "preferences-system-symbolic"}),
                  (ID_QUIT, {"label": "Quit tray"})]
        return items

    @staticmethod
    def _node(item):
        iid, props = item[0], item[1]
        children = item[2] if len(item) > 2 else []
        return dbus.Struct((dbus.Int32(iid), dbus.Dictionary(props, signature="sv"),
                            dbus.Array([Menu._node(ch) for ch in children], signature="v")),
                           signature="ia{sv}av")

    def _flat(self):
        out = {}
        def walk(items):
            for it in items:
                out[it[0]] = it[1]
                if len(it) > 2:
                    walk(it[2])
        walk(self._items())
        return out

    def changed(self):
        self.revision += 1
        self.LayoutUpdated(dbus.UInt32(self.revision), dbus.Int32(0))

    @dbus.service.method(MENU_IFACE, in_signature="iias", out_signature="u(ia{sv}av)")
    def GetLayout(self, parent_id, depth, names):
        items = self._items()
        if parent_id != 0:
            items = next((it[2] for it in items if it[0] == parent_id and len(it) > 2), [])
        root = (int(parent_id), {"children-display": "submenu"}, items)
        return dbus.UInt32(self.revision), self._node(root)

    @dbus.service.method(MENU_IFACE, in_signature="aias", out_signature="a(ia{sv})")
    def GetGroupProperties(self, ids, names):
        flat = self._flat()
        ids = ids or list(flat)
        return dbus.Array([dbus.Struct((dbus.Int32(i), dbus.Dictionary(flat[i], signature="sv")))
                           for i in ids if i in flat], signature="(ia{sv})")

    @dbus.service.method(MENU_IFACE, in_signature="is", out_signature="v")
    def GetProperty(self, iid, name):
        return self._flat().get(iid, {}).get(name, "")

    @dbus.service.method(MENU_IFACE, in_signature="isvu")
    def Event(self, iid, event_id, data, ts):
        if event_id == "clicked":
            GLib.idle_add(self.app.on_click, int(iid))

    @dbus.service.method(MENU_IFACE, in_signature="a(isvu)", out_signature="ai")
    def EventGroup(self, events):
        for iid, ev, data, ts in events:
            self.Event(iid, ev, data, ts)
        return dbus.Array([], signature="i")

    @dbus.service.method(MENU_IFACE, in_signature="i", out_signature="b")
    def AboutToShow(self, iid):
        # Battery % changes without a kernel event; fetch fresh data on open.
        return self.app.refresh_sync()

    @dbus.service.method(MENU_IFACE, in_signature="ai", out_signature="aiai")
    def AboutToShowGroup(self, ids):
        return dbus.Array([], signature="i"), dbus.Array([], signature="i")

    @dbus.service.signal(MENU_IFACE, signature="ui")
    def LayoutUpdated(self, revision, parent):
        pass

    @dbus.service.signal(MENU_IFACE, signature="a(ia{sv})a(ias)")
    def ItemsPropertiesUpdated(self, updated, removed):
        pass

    @dbus.service.method(PROPS, in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.GetAll(iface)[prop]

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return {"Version": dbus.UInt32(3), "TextDirection": "ltr", "Status": "normal",
                "IconThemePath": dbus.Array([], signature="s")}


class Item(dbus.service.Object):
    def __init__(self, bus, app):
        super().__init__(bus, "/StatusNotifierItem")
        self.app = app

    def props(self):
        st = self.app.status
        title, detail = summary(st)
        tooltip = dbus.Struct(("", dbus.Array([], signature="(iiay)"), title, detail),
                              signature="sa(iiay)ss")
        return {
            "Category": "Hardware",
            "Id": "lpowerd",
            "Title": "Power Control",
            "Status": "Active",
            "IconName": icon_for(st),
            "IconThemePath": "",
            "ToolTip": tooltip,
            "ItemIsMenu": False,
            "Menu": dbus.ObjectPath("/MenuBar"),
            "WindowId": dbus.Int32(0),
        }

    @dbus.service.method(PROPS, in_signature="ss", out_signature="v")
    def Get(self, iface, prop):
        return self.props()[prop]

    @dbus.service.method(PROPS, in_signature="s", out_signature="a{sv}")
    def GetAll(self, iface):
        return self.props()

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def Activate(self, x, y):
        GLib.idle_add(self.app.open_gui)

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def SecondaryActivate(self, x, y):
        pass

    @dbus.service.method(SNI_IFACE, in_signature="ii")
    def ContextMenu(self, x, y):
        pass

    @dbus.service.method(SNI_IFACE, in_signature="is")
    def Scroll(self, delta, orientation):
        pass

    @dbus.service.signal(SNI_IFACE)
    def NewIcon(self):
        pass

    @dbus.service.signal(SNI_IFACE)
    def NewToolTip(self):
        pass

    @dbus.service.signal(SNI_IFACE, signature="s")
    def NewStatus(self, status):
        pass


class App:
    def __init__(self):
        self.session = dbus.SessionBus()
        self.daemon_bus = (self.session if os.environ.get("LPOWERD_BUS") == "session"
                           else dbus.SystemBus())
        self.status = None
        self.name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._bus_name = dbus.service.BusName(self.name, self.session)
        self.menu = Menu(self.session, self)
        self.item = Item(self.session, self)

        self.daemon_bus.add_signal_receiver(self.on_status, "StatusChanged", INTERFACE,
                                            BUS_NAME, OBJECT_PATH)
        self.daemon_bus.watch_name_owner(BUS_NAME, self.on_daemon_owner)
        self.session.watch_name_owner("org.kde.StatusNotifierWatcher", self.on_watcher)

    def on_watcher(self, owner):
        if not owner:
            return
        try:
            w = self.session.get_object("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher")
            w.RegisterStatusNotifierItem(self.name, dbus_interface="org.kde.StatusNotifierWatcher")
            log.info("registered with status notifier watcher")
        except dbus.DBusException as e:
            log.warning("could not register tray item: %s", e)

    def on_daemon_owner(self, owner):
        if owner:
            self.refresh()
        else:
            self.update(None)

    def iface(self):
        return dbus.Interface(self.daemon_bus.get_object(BUS_NAME, OBJECT_PATH, introspect=False),
                              INTERFACE)

    def refresh(self):
        try:
            self.iface().GetStatus(reply_handler=self.on_status,
                                   error_handler=lambda e: self.update(None))
        except dbus.DBusException:
            self.update(None)

    def refresh_sync(self):
        try:
            st = json.loads(str(self.iface().GetStatus(timeout=1)))
        except dbus.DBusException:
            return False
        if st == self.status:
            return False
        self.status = st
        self.item.NewIcon()
        self.item.NewToolTip()
        self.menu.revision += 1
        return True

    def on_status(self, s):
        self.update(json.loads(str(s)))

    def update(self, st):
        self.status = st
        self.item.NewIcon()
        self.item.NewToolTip()
        self.menu.changed()

    def call(self, method, *args):
        def err(e):
            log.warning("%s failed: %s", method, e.get_dbus_message() if hasattr(e, "get_dbus_message") else e)
            self.notify("Power Control", str(e.get_dbus_message() if hasattr(e, "get_dbus_message") else e))
            self.refresh()
        getattr(self.iface(), method)(*args, reply_handler=lambda *a: self.refresh(),
                                      error_handler=err, timeout=300)

    def notify(self, title, body):
        try:
            n = self.session.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
            n.Notify("lpowerd", dbus.UInt32(0), "battery-caution-symbolic", title, body,
                     dbus.Array([], signature="s"), dbus.Dictionary({}, signature="sv"), 5000,
                     dbus_interface="org.freedesktop.Notifications")
        except dbus.DBusException:
            pass

    def on_click(self, iid):
        st = self.status
        if iid == ID_QUIT:
            self.loop.quit()
        elif iid == ID_OPEN:
            self.open_gui()
        elif not st:
            return False
        elif iid == ID_CONS:
            ch = st["charge"]
            self.call("SetConservation", not (ch["conservation"] and ch["limit"] == ch["conservation_level"]))
        elif iid == ID_RAPID:
            self.call("SetRapidCharge", not st["charge"]["rapid_charge"])
        elif iid == ID_FULL:
            self.call("ChargeFullOnce")
        elif iid >= PROFILE_BASE:
            choices = st["profile"]["choices"]
            if iid - PROFILE_BASE < len(choices):
                self.call("SetProfile", choices[iid - PROFILE_BASE])
        return False

    def open_gui(self):
        # lpowerd-gui is a single-instance Gtk.Application: a second launch
        # just raises the existing window.
        here = os.path.dirname(os.path.realpath(sys.argv[0]))
        exe = os.path.join(here, "lpowerd-gui")
        try:
            GLib.spawn_async([exe if os.access(exe, os.X_OK) else "lpowerd-gui"],
                             flags=GLib.SpawnFlags.SEARCH_PATH)
        except GLib.Error as e:
            self.notify("Power Control", f"Could not open settings: {e.message}")
        return False

    def run(self):
        self.loop = GLib.MainLoop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, self.loop.quit)
        self.loop.run()


def main():
    logging.basicConfig(level=logging.INFO, format="%(name)s: %(message)s", stream=sys.stderr)
    DBusGMainLoop(set_as_default=True)
    App().run()
