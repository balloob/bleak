"""
BlueZ D-Bus manager module
--------------------------

This module contains code for the global BlueZ D-Bus object manager that is
used internally by Bleak.
"""

import asyncio
import logging
import os
from typing import (
    Any,
    Callable,
    Coroutine,
    Dict,
    Iterable,
    List,
    NamedTuple,
    Set,
    Tuple,
    cast,
)

from dbus_next import BusType, Message, MessageType, Variant
from dbus_next.aio.message_bus import MessageBus
from typing_extensions import TypedDict, Literal

from ...exc import BleakError
from . import defs
from .advertisement_monitor import AdvertisementMonitor, OrPatternLike
from .signals import MatchRules, add_match
from .utils import assert_reply, unpack_variants

logger = logging.getLogger(__name__)


# D-Bus properties for interfaces
# https://github.com/bluez/bluez/blob/master/doc/adapter-api.txt


class Adapter1(TypedDict):
    Address: str
    Name: str
    Alias: str
    Class: int
    Powered: bool
    Discoverable: bool
    Pairable: bool
    PairableTimeout: int
    DiscoverableTimeout: int
    Discovering: int
    UUIDs: List[str]
    Modalias: str
    Roles: List[str]
    ExperimentalFeatures: List[str]


# https://github.com/bluez/bluez/blob/master/doc/advertisement-monitor-api.txt


class AdvertisementMonitor1(TypedDict):
    Type: str
    RSSILowThreshold: int
    RSSIHighThreshold: int
    RSSILowTimeout: int
    RSSIHighTimeout: int
    RSSISamplingPeriod: int
    Patterns: List[Tuple[int, int, bytes]]


class AdvertisementMonitorManager1(TypedDict):
    SupportedMonitorTypes: List[str]
    SupportedFeatures: List[str]


# https://github.com/bluez/bluez/blob/master/doc/battery-api.txt


class Battery1(TypedDict):
    SupportedMonitorTypes: List[str]
    SupportedFeatures: List[str]


# https://github.com/bluez/bluez/blob/master/doc/device-api.txt


class Device1(TypedDict):
    Address: str
    AddressType: str
    Name: str
    Icon: str
    Class: int
    Appearance: int
    UUIDs: List[str]
    Paired: bool
    Bonded: bool
    Connected: bool
    Trusted: bool
    Blocked: bool
    WakeAllowed: bool
    Alias: str
    Adapter: str
    LegacyPairing: bool
    Modalias: str
    RSSI: int
    TxPower: int
    ManufacturerData: Dict[int, bytes]
    ServiceData: Dict[str, bytes]
    ServicesResolved: bool
    AdvertisingFlags: bytes
    AdvertisingData: Dict[int, bytes]


# https://github.com/bluez/bluez/blob/master/doc/gatt-api.txt


class GattService1(TypedDict):
    UUID: str
    Primary: bool
    Device: str
    Includes: List[str]
    # Handle is server-only and not available in Bleak


class GattCharacteristic1(TypedDict):
    UUID: str
    Service: str
    Value: bytes
    WriteAcquired: bool
    NotifyAcquired: bool
    Notifying: bool
    Flags: List[
        Literal[
            "broadcast",
            "read",
            "write-without-response",
            "write",
            "notify",
            "indicate",
            "authenticated-signed-writes",
            "extended-properties",
            "reliable-write",
            "writable-auxiliaries",
            "encrypt-read",
            "encrypt-write",
            # "encrypt-notify" and "encrypt-indicate" are server-only
            "encrypt-authenticated-read",
            "encrypt-authenticated-write",
            # "encrypt-authenticated-notify", "encrypt-authenticated-indicate",
            # "secure-read", "secure-write", "secure-notify", "secure-indicate"
            # are server-only
            "authorize",
        ]
    ]
    MTU: int
    # Handle is server-only and not available in Bleak


class GattDescriptor1(TypedDict):
    UUID: str
    Characteristic: str
    Value: bytes
    Flags: List[
        Literal[
            "read",
            "write",
            "encrypt-read",
            "encrypt-write",
            "encrypt-authenticated-read",
            "encrypt-authenticated-write",
            # "secure-read" and "secure-write" are server-only and not available in Bleak
            "authorize",
        ]
    ]
    # Handle is server-only and not available in Bleak


AdvertisementCallback = Callable[[str, Device1], None]
"""
A callback that is called when advertisement data is received.

Args:
    arg0: The D-Bus object path of the device.
    arg1: The D-Bus properties of the device object.
"""


class CallbackAndState(NamedTuple):
    """
    Encapsulates an :data:`AdvertisementCallback` and some state.
    """

    callback: AdvertisementCallback
    """
    The callback.
    """

    adapter_path: str
    """
    The D-Bus object path of the adapter associated with the callback.
    """

    seen_devices: Set[str]
    """
    Set of device D-Bus object paths that have been "seen" already by this callback.
    """


# set of org.bluez.Device1 property names that come from advertising data
_ADVERTISING_DATA_PROPERTIES = {
    "AdvertisingData",
    "AdvertisingFlags",
    "ManufacturerData",
    "Name",
    "ServiceData",
    "UUIDs",
}


class BlueZManager:
    """
    BlueZ D-Bus object manager.

    Use :func:`bleak.backends.bluezdbus.get_global_bluez_manager` to get the global instance.
    """

    def __init__(self):
        self._bus = MessageBus(bus_type=BusType.SYSTEM)
        self._bus_lock = asyncio.Lock()

        # dict of object path: dict of interface name: dict of property name: property value
        self._properties: Dict[str, Dict[str, Dict[str, Any]]] = {}

        self._advertisement_callbacks: List[CallbackAndState] = []

    async def async_init(self):
        """
        Connects to the D-Bus message bus and begins monitoring signals.

        It is safe to call this method multiple times. If the bus is already
        connected, no action is performed.
        """
        async with self._bus_lock:
            if self._bus.connected:
                return

            await self._bus.connect()

            try:
                # Add signal listeners

                self._bus.add_message_handler(self._parse_msg)

                rules = MatchRules(
                    interface=defs.OBJECT_MANAGER_INTERFACE,
                    member="InterfacesAdded",
                    arg0path="/org/bluez/",
                )
                reply = await add_match(self._bus, rules)
                assert_reply(reply)

                rules = MatchRules(
                    interface=defs.OBJECT_MANAGER_INTERFACE,
                    member="InterfacesRemoved",
                    arg0path="/org/bluez/",
                )
                reply = await add_match(self._bus, rules)
                assert_reply(reply)

                rules = MatchRules(
                    interface=defs.PROPERTIES_INTERFACE,
                    member="PropertiesChanged",
                    path_namespace="/org/bluez",
                )
                reply = await add_match(self._bus, rules)
                assert_reply(reply)

                # get existing objects after adding signal handlers to avoid
                # race condition

                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path="/",
                        member="GetManagedObjects",
                        interface=defs.OBJECT_MANAGER_INTERFACE,
                    )
                )
                assert_reply(reply)

                # dictionary is replaced in case AddInterfaces was received first
                self._properties = {
                    path: unpack_variants(interfaces)
                    for path, interfaces in reply.body[0].items()
                }

                logger.debug(f"initial properties: {self._properties}")

            except BaseException:
                # if setup failed, disconnect
                await self._bus.disconnect()
                raise

    async def active_scan(
        self,
        adapter_path: str,
        filters: Dict[str, Variant],
        callback: AdvertisementCallback,
    ) -> Callable[[], Coroutine]:
        """
        Configures the advertisement data filters and starts scanning.

        Args:
            adapter_path: The D-Bus object path of the adapter to use for scanning.
            filters: A dictionary of filters to pass to ``SetDiscoveryFilter``.
            callback: A callable that will be called when new advertisement data is received.

        Returns:
            An async function that is used to stop scanning and remove the filters.
        """
        async with self._bus_lock:
            # If the adapter doesn't exist, then the message calls below would
            # fail with "method not found". This provides a more informative
            # error message.
            if adapter_path not in self._properties:
                raise BleakError(f"adapter '{adapter_path.split('/')[-1]}' not found")

            callback_and_state = CallbackAndState(callback, adapter_path, set())
            self._advertisement_callbacks.append(callback_and_state)

            try:
                # Apply the filters
                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=adapter_path,
                        interface=defs.ADAPTER_INTERFACE,
                        member="SetDiscoveryFilter",
                        signature="a{sv}",
                        body=[filters],
                    )
                )
                assert_reply(reply)

                # Start scanning
                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=adapter_path,
                        interface=defs.ADAPTER_INTERFACE,
                        member="StartDiscovery",
                    )
                )
                assert_reply(reply)

                async def stop() -> None:
                    async with self._bus_lock:
                        reply = await self._bus.call(
                            Message(
                                destination=defs.BLUEZ_SERVICE,
                                path=adapter_path,
                                interface=defs.ADAPTER_INTERFACE,
                                member="StopDiscovery",
                            )
                        )
                        assert_reply(reply)

                        # remove the filters
                        reply = await self._bus.call(
                            Message(
                                destination=defs.BLUEZ_SERVICE,
                                path=adapter_path,
                                interface=defs.ADAPTER_INTERFACE,
                                member="SetDiscoveryFilter",
                                signature="a{sv}",
                                body=[{}],
                            )
                        )
                        assert_reply(reply)

                        self._advertisement_callbacks.remove(callback_and_state)

                return stop
            except BaseException:
                # if starting scanning failed, don't leak the callback
                self._advertisement_callbacks.remove(callback_and_state)
                raise

    async def passive_scan(
        self,
        adapter_path: str,
        filters: List[OrPatternLike],
        callback: AdvertisementCallback,
    ) -> Callable[[], Coroutine]:
        """
        Configures the advertisement data filters and starts scanning.

        Args:
            adapter_path: The D-Bus object path of the adapter to use for scanning.
            filters: A list of "or patterns" to pass to ``org.bluez.AdvertisementMonitor1``.
            callback: A callable that will be called when new advertisement data is received.

        Returns:
            An async function that is used to stop scanning and remove the filters.
        """
        async with self._bus_lock:
            # If the adapter doesn't exist, then the message calls below would
            # fail with "method not found". This provides a more informative
            # error message.
            if adapter_path not in self._properties:
                raise BleakError(f"adapter '{adapter_path.split('/')[-1]}' not found")

            callback_and_state = CallbackAndState(callback, adapter_path, set())
            self._advertisement_callbacks.append(callback_and_state)

            try:
                monitor = AdvertisementMonitor(filters)

                # this should be a unique path to allow multiple python interpreters
                # running bleak and multiple scanners within a single interpreter
                monitor_path = f"/org/bleak/{os.getpid()}/{id(monitor)}"

                reply = await self._bus.call(
                    Message(
                        destination=defs.BLUEZ_SERVICE,
                        path=adapter_path,
                        interface=defs.ADVERTISEMENT_MONITOR_MANAGER_INTERFACE,
                        member="RegisterMonitor",
                        signature="o",
                        body=[monitor_path],
                    )
                )

                if (
                    reply.message_type == MessageType.ERROR
                    and reply.error_name == "org.freedesktop.DBus.Error.UnknownMethod"
                ):
                    raise BleakError(
                        "passive scanning on Linux requires BlueZ >= 5.55 with --experimental enabled and Linux kernel >= 5.10"
                    )

                assert_reply(reply)

                # It is important to export after registering, otherwise BlueZ
                # won't use the monitor
                self._bus.export(monitor_path, monitor)

                async def stop():
                    async with self._bus_lock:
                        self._bus.unexport(monitor_path, monitor)

                        reply = await self._bus.call(
                            Message(
                                destination=defs.BLUEZ_SERVICE,
                                path=adapter_path,
                                interface=defs.ADVERTISEMENT_MONITOR_MANAGER_INTERFACE,
                                member="UnregisterMonitor",
                                signature="o",
                                body=[monitor_path],
                            )
                        )
                        assert_reply(reply)

                        self._advertisement_callbacks.remove(callback_and_state)

                return stop

            except BaseException:
                # if starting scanning failed, don't leak the callback
                self._advertisement_callbacks.remove(callback_and_state)
                raise

    def _parse_msg(self, message: Message):
        """
        Handles callbacks from dbus_next.
        """

        if message.message_type != MessageType.SIGNAL:
            return

        logger.debug(
            f"received D-Bus signal: {message.interface}.{message.member} ({message.path}): {message.body}"
        )

        # type hints
        obj_path: str
        interfaces_and_props: Dict[str, Dict[str, Variant]]
        interfaces: List[str]
        interface: str
        changed: Dict[str, Variant]
        invalidated: List[str]

        if message.member == "InterfacesAdded":
            obj_path, interfaces_and_props = message.body

            for interface, props in interfaces_and_props.items():
                unpacked_props = unpack_variants(props)
                self._properties.setdefault(obj_path, {})[interface] = unpacked_props

                # If this is a device and it has advertising data properties,
                # then it should mean that this device just started advertising.
                # Previously, we just relied on RSSI updates to determine if
                # a device was actually advertising, but we were missing "slow"
                # devices that only advertise once and then go to sleep for a while.
                if interface == defs.DEVICE_INTERFACE:
                    self._run_advertisement_callbacks(
                        obj_path, cast(Device1, unpacked_props), unpacked_props.keys()
                    )
        elif message.member == "InterfacesRemoved":
            obj_path, interfaces = message.body

            for interface in interfaces:
                del self._properties[obj_path][interface]
        elif message.member == "PropertiesChanged":
            assert message.path is not None

            interface, changed, invalidated = message.body

            try:
                self_interface = self._properties[message.path][interface]
            except KeyError:
                # This can happen during initialization. The "PropertyChanged"
                # handler is attached before "GetManagedObjects" is called
                # and so self._properties may not yet be populated.
                # This is not a problem. We just discard the property value
                # since "GetManagedObjects" will return a newer value.
                pass
            else:
                self_interface.update(unpack_variants(changed))

                for name in invalidated:
                    del self_interface[name]

                if interface == defs.DEVICE_INTERFACE:
                    self._run_advertisement_callbacks(
                        message.path, cast(Device1, self_interface), changed.keys()
                    )

    def _run_advertisement_callbacks(
        self, device_path: str, device: Device1, changed: Iterable[str]
    ) -> None:
        """
        Runs any registered advertisement callbacks.

        Args:
            device_path: The D-Bus object path of the remote device.
            device: The current D-Bus properties of the device.
            changed: A list of properties that have changed since the last call.
        """
        for (
            callback,
            adapter_path,
            seen_devices,
        ) in self._advertisement_callbacks:
            # filter messages from other adapters
            if not device_path.startswith(adapter_path):
                continue

            first_time_seen = False

            if device_path not in seen_devices:
                first_time_seen = True
                seen_devices.add(device_path)

            # Only do advertising data callback if this is the first time the
            # device has been seen or if an advertising data property changed.
            # Otherwise we get a flood of callbacks from RSSI changing.
            if first_time_seen or not _ADVERTISING_DATA_PROPERTIES.isdisjoint(changed):
                # TODO: this should be deep copy, not shallow
                callback(device_path, cast(Device1, device.copy()))


async def get_global_bluez_manager() -> BlueZManager:
    """
    Gets the initialized global BlueZ manager instance.
    """

    if not hasattr(get_global_bluez_manager, "instance"):
        setattr(get_global_bluez_manager, "instance", BlueZManager())

    instance: BlueZManager = getattr(get_global_bluez_manager, "instance")

    await instance.async_init()

    return instance
