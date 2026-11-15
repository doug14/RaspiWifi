import logging
from socket import if_nametoindex


PORTAL_HOSTNAME = "acesso.local"
AVAHI = "org.freedesktop.Avahi"
ENTRY_GROUP = AVAHI + ".EntryGroup"
PROTOCOL_UNSPECIFIED = -1
PUBLISH_NO_REVERSE = 16
GROUP_COLLISION = 3
GROUP_FAILURE = 4


class PortalMdns:
    def __init__(self, interface, address):
        from gi.repository import Gio, GLib

        self.gio = Gio
        self.glib = GLib
        self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        self.interface = interface
        self.address = address
        self.owner = None
        self.group = None
        self.last_error = None

    def call(self, destination, path, interface, method, signature=None, values=()):
        params = self.glib.Variant(signature, values) if signature else None
        return self.bus.call_sync(destination, path, interface, method, params, None,
                                  self.gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()

    def update(self, enabled):
        if not enabled and self.group is None:
            return
        try:
            self._update(enabled)
        except (self.glib.Error, OSError, RuntimeError) as error:
            message = str(error)
            if message != self.last_error:
                logging.warning("mDNS %s: %s", PORTAL_HOSTNAME, message)
            self.last_error = message
        else:
            self.last_error = None

    def _update(self, enabled):
        owners, = self.call("org.freedesktop.DBus", "/org/freedesktop/DBus",
                            "org.freedesktop.DBus", "ListQueuedOwners", "(s)", (AVAHI,))
        owner = owners[0] if owners else None
        if owner != self.owner:
            self.owner = owner
            self.group = None
        if not enabled:
            self._free()
            return
        if owner is None:
            raise RuntimeError("Avahi is unavailable")
        if self.group is not None:
            state, = self.call(owner, self.group, ENTRY_GROUP, "GetState")
            if state == GROUP_COLLISION:
                raise RuntimeError("Another device is already using this name")
            if state != GROUP_FAILURE:
                return
            self._free()

        index = if_nametoindex(self.interface)
        self.group, = self.call(owner, "/", AVAHI + ".Server", "EntryGroupNew")
        try:
            self.call(owner, self.group, ENTRY_GROUP, "AddAddress", "(iiuss)",
                      (index, PROTOCOL_UNSPECIFIED, PUBLISH_NO_REVERSE, PORTAL_HOSTNAME, self.address))
            self.call(owner, self.group, ENTRY_GROUP, "Commit")
        except self.glib.Error:
            self._free()
            raise

    def _free(self):
        if self.group is not None:
            self.call(self.owner, self.group, ENTRY_GROUP, "Free")
            self.group = None
