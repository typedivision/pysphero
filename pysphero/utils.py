import logging
from queue import Queue, Empty
from threading import Thread
from time import time

from gatt import Device, DeviceManager

from pysphero.core import Sphero
from pysphero.constants import Toy
from pysphero.exceptions import PySpheroNotFoundError

from typing import NamedTuple, Generator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    ScanItemQueue = Queue[ScanItem]
else:
    ScanItemQueue = Queue

logger = logging.getLogger(__name__)

class ScanItem(NamedTuple):
    """
    Info class for discovered devices.
    """

    mac_address: str
    name: str

class AnyDeviceManager(DeviceManager):
    """
    Device manager implementation derived from gatt base class for device discovery.
    """

    def __init__(self, adapter_name: str) -> None:
        super().__init__(adapter_name)
        self.queue: Queue[ScanItem] = Queue()

    def device_discovered(self, device: Device) -> None:
        """
        Callback from gatt when a device was discovered.
        """
        logger.debug("Discovered %s [%s]" % (device.alias(), device.mac_address))
        self.queue.put_nowait(ScanItem(device.mac_address, device.alias()))


def toy_scanner(name: str, toy_type: Toy = Toy.unknown, timeout: float = 3.0) -> Sphero:
    """
    Sphero toy discovery and initialization.
    """
    logger.debug("Search for devices")
    manager = AnyDeviceManager(adapter_name='hci0')
    manager.start_discovery()
    Thread(target=manager.run).start()

    for scan_item in _queue_iter(manager.queue, timeout):
        # none when timeout
        if not scan_item:
            break

        if (name == scan_item.name):
            manager.stop()
            return Sphero(scan_item.mac_address, toy_type)

    manager.stop()
    raise PySpheroNotFoundError("Device not found")


def _queue_iter(queue: ScanItemQueue, timeout: float) -> Generator[ScanItem, None, None]:
    """
    Discovered device reactive iterator with timeout.
    """
    stop_time = time() + timeout
    while time() <= stop_time:
        try:
            scan_item = queue.get(timeout=0.2)
        except Empty:
            continue

        logger.debug("Found device %s", scan_item)
        yield scan_item
