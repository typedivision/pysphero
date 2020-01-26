import logging
import asyncio
from asyncio import AbstractEventLoop, Future
from concurrent.futures.thread import ThreadPoolExecutor
from threading import Event, Thread
from time import sleep
from typing import Callable, List, Tuple, Dict

from gatt import DeviceManager, Device, Characteristic

from pysphero.constants import Api2Error, SpheroCharacteristic, Toy
from pysphero.device_api import Animatronics, Sensor, UserIO, ApiProcessor, Power, SystemInfo
from pysphero.driving import Driving
from pysphero.exceptions import PySpheroApiError, PySpheroRuntimeError, PySpheroTimeoutError, PySpheroException
from pysphero.helpers import cached_property
from pysphero.packet import Packet

logger = logging.getLogger(__name__)

class SpheroDevice(Device):
    """
    Sphero device implementation derived from gatt base class for communication.
    """

    def __init__(self, mac_address: str, manager: DeviceManager, request_loop: AbstractEventLoop) -> None:
        super().__init__(mac_address=mac_address, manager=manager)
        self._characteristics: Dict[str, Characteristic] = {}
        self._write_event = Event()
        self._write_error: str = None
        self._data: List[int] = []
        self._request_loop = request_loop
        self._response_futures: Dict[Tuple[int, int], Future[Packet]] = {}
        self._notify_callbacks: Dict[Tuple[int, int], Callable[[Packet], None]] = {}

    def build_packet(self) -> None:
        """
        Create and emit sphero packet from raw bytes.
        """
        try:
            packet = Packet.from_response(self._data)
        except Exception as e:
            logger.error("Build packet exception: %s" % e)
        else:
            logger.debug(f"Received {packet}")

        self._data = []

        response_fut = self._response_futures.pop(packet.id, None)
        if response_fut and not response_fut.cancelled():
            if packet:
                self._request_loop.call_soon_threadsafe(response_fut.set_result, packet)
            else:
                self._request_loop.call_soon_threadsafe(
                    response_fut.set_exception, PySpheroRuntimeError("Build packet failed")
                )

        notify_cb = self._notify_callbacks.get(packet.id);
        if notify_cb and packet:
            notify_cb(packet)

    def connect(self):
        """
        Request to gatt to connect, wait for resolving services and characteristics.
        """
        super().connect()
        for _ in range(10):
            sleep(0.5)
            if self.is_services_resolved():
                resolved = True
                break
        if not resolved:
            raise PySpheroTimeoutError("Timeout error resolving device services")

    def write(self, uuid: str, packet: Packet, timeout: float = 2.0, future: Future[Packet] = None) -> None:
        """
        Write a packet to a characteristic given by its uuid and wait for write done event.
        """
        ch = self._characteristics.get(uuid)
        if not ch:
            ch = self.get_characteristic(uuid)
            if not ch:
                raise PySpheroRuntimeError("Characteristic not found")
            self._characteristics[uuid] = ch
            ch.enable_notifications()

        if future:
            self._response_futures[packet.id] = future

        self._write_event.clear()
        ch.write_value(packet.build())

        if not self._write_event.wait(timeout):
            raise PySpheroTimeoutError("Timeout write characteristic")

        if self._write_error:
            raise PySpheroRuntimeError("Write characteristic failed: %s" % self._write_error)

    def set_notification(self, packet_id: Tuple[int, int], notify_cb: Callable[[Packet], None]) -> None:
        """
        Set a notification callback for a sphero packet of given id.
        """
        self._notify_callbacks[packet_id] = notify_cb

    def get_characteristic(self, uuid: str) -> Characteristic:
        """
        Get the characteristic object for the given uuid.
        """
        for service in self.services:
            for characteristic in service.characteristics:
                if characteristic.uuid == uuid:
                    logger.debug("Found Characteristic %s" % uuid)
                    return characteristic
        logger.debug("Characteristic not found [%s]" % uuid)
        return None

    def connect_succeeded(self):
        """
        Callback from gatt when connect succeeded.
        """
        super().connect_succeeded()
        logger.debug("Connected [%s]" % self.mac_address)

    def connect_failed(self, error):
        """
        Callback from gatt when connect failed.
        """
        super().connect_failed(error)
        logger.error("Connection failed: %s" % error)

    def disconnect_succeeded(self):
        """
        Callback from gatt when disconnect succeeded.
        """
        super().disconnect_succeeded()
        logger.debug("Disconnected")

    def services_resolved(self):
        """
        Callback from gatt when resolving services finished.
        """
        super().services_resolved()
        logger.debug("Resolved services")
        for service in self.services:
            logger.debug("Service [%s]" % service.uuid)
            for characteristic in service.characteristics:
                logger.debug("Characteristic [%s]" % characteristic.uuid)

    def characteristic_write_value_succeeded(self, characteristic):
        """
        Callback from gatt when write to characteristic succeeded, clear error emit write done event.
        """
        logger.debug("Write succeeded [%s]" % characteristic.uuid)
        self._write_error = None
        self._write_event.set()

    def characteristic_write_value_failed(self, characteristic, error):
        """
        Callback from gatt when write to characteristic failed, set error and emit write done event.
        """
        logger.error("Write failed [%s]: %s" % (characteristic.uuid, error))
        self._write_error = error
        self._write_event.set()

    def characteristic_enable_notifications_succeeded(self, characteristic):
        """
        Callback from gatt when enable notification succeeded.
        """
        logger.debug("Notification succeeded [%s]" % characteristic.uuid)

    def characteristic_enable_notifications_failed(self, characteristic, error):
        """
        Callback from gatt when enable notification failed.
        """
        logger.error("Nofification failed [%s]: %s" % (characteristic.uuid, error))

    def characteristic_value_updated(self, characteristic, value):
        """
        Callback from gatt when value was updated, call the create packet function for received data.
        """
        for b in value:
            #logger.debug(f"Received {b:#04x}")
            self._data.append(b)
            if b == Packet.end:
                if len(self._data) < 6:
                    raise PySpheroRuntimeError(f"Very small packet {[hex(x) for x in self._data]}")
                self.build_packet()


class SpheroCore:
    """
    Core class for the device management and request handling.
    """

    def __init__(self, mac_address: str, max_workers: int = 10) -> None:
        logger.debug("Init Sphero Core")
        self._device_manager = DeviceManager('hci0')
        self._request_loop = asyncio.get_event_loop()
        self._device = SpheroDevice(mac_address, self._device_manager, self._request_loop)

        logger.debug('Starting DeviceManager')
        self._device_manager_thread = Thread(target=self._device_manager.run)
        self._device_manager_thread.setDaemon(True)
        self._device_manager_thread.start()

        logger.debug('Connecting Device')
        self._device.connect()

        self._sequence = 0
        self._notify_executor = ThreadPoolExecutor(max_workers=max_workers)
        logger.debug("Sphero Core: successful initialization")

    def close(self) -> None:
        """
        Disconnect device and shutdown communication.
        """
        self._device.disconnect()
        self._device_manager.stop()
        self._request_loop.close()
        self._notify_executor.shutdown(wait=False)

    async def _write_with_response(self, uuid: str, packet: Packet, timeout: float = 5.0) -> Packet:
        """
        Write packet to device characterisic and wait for a response.
        """
        response_fut: Future[Packet] = Future()
        try:
            self._device.write(uuid, packet, timeout, response_fut)
        except:
            response_fut.cancel()
            raise

        try:
            response = await asyncio.wait_for(response_fut, timeout)
        except asyncio.TimeoutError:
            response_fut.cancel()
            raise PySpheroTimeoutError(f"Timeout error for response of {packet}")

        return response

    @property
    def sequence(self) -> int:
        """
        Autoincrement sequence number of packet.
        """
        self._sequence = (self._sequence + 1) % 256
        return self._sequence

    def notify(self, packet: Packet, callback: Callable[[Packet], None]) -> None:
        """
        Register notification callback for packets.
        """
        def notify_cb(response: Packet) -> None:
            logger.debug(f"Notification {response}")
            self._notify_executor.submit(callback, response)

        self._device.set_notification(packet.id, notify_cb)

    def cancel_notify(self, packet: Packet) -> None:
        """
        Remove notification callback for packets.
        """
        self._device.set_notification(packet.id, None)

    def request(self, packet: Packet, with_api_error: bool = True, timeout: float = 5.0) -> Packet:
        """
        Send request packet and wait for response packet.
        """
        packet.sequence = self.sequence
        logger.debug(f"Sending {packet}")

        api_v2 = SpheroCharacteristic.api_v2.value
        request = self._write_with_response(api_v2, packet, timeout)
        response = self._request_loop.run_until_complete(request)

        if with_api_error and response.api_error is not Api2Error.success:
            raise PySpheroApiError(response.api_error)

        return response


class Sphero:
    """
    High-level API to control sphero toy.
    """

    def __init__(self, mac_address: str, toy_type: Toy = Toy.unknown) -> None:
        self.mac_address = mac_address
        self.type = toy_type
        self._sphero_core: SpheroCore = None

    @property
    def sphero_core(self) -> SpheroCore:
        """
        Get sphero device manager.
        """
        if self._sphero_core is None:
            raise PySpheroException("Use Sphero as context manager")

        return self._sphero_core

    @sphero_core.setter
    def sphero_core(self, sphero_core: SpheroCore) -> None:
        """
        Set sphero device manager.
        """
        self._sphero_core = sphero_core

    def __enter__(self) -> Sphero:
        """
        Init sphere device manager.
        """
        self.sphero_core = SpheroCore(self.mac_address)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """
        Deinit sphero device manager.
        """
        self.sphero_core.close()

    @cached_property
    def system_info(self) -> SystemInfo:
        """
        Get sphero system info interface object.
        """
        return SystemInfo(sphero_core = self.sphero_core)

    @cached_property
    def power(self) -> Power:
        """
        Get sphero power interface object.
        """
        return Power(sphero_core = self.sphero_core)

    @cached_property
    def driving(self) -> Driving:
        """
        Get sphero driving interface object.
        """
        return Driving(sphero_core = self.sphero_core)

    @cached_property
    def api_processor(self) -> ApiProcessor:
        """
        Get sphero api processor interface object.
        """
        return ApiProcessor(sphero_core = self.sphero_core)

    @cached_property
    def user_io(self) -> UserIO:
        """
        Get sphero user io interface object.
        """
        return UserIO(sphero_core = self.sphero_core)

    @cached_property
    def sensor(self) -> Sensor:
        """
        Get sphero sensor interface  object.
        """
        return Sensor(sphero_core = self.sphero_core)

    @cached_property
    def animatronics(self) -> Animatronics:
        """
        Get sphero animatronics interface object.
        """
        return Animatronics(sphero_core = self.sphero_core)
