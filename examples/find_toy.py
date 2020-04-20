from time import sleep

from pysphero.constants import Toy
from pysphero.utils import toy_scanner

sphero_name = "SB-AC4B"
sphero_type = Toy.sphero_bolt

def main():
    print("Search for Sphero...")
    with toy_scanner(sphero_name, sphero_type, adapter = 'hci1') as sphero:
        print(f"Found {sphero.mac_address}")
        sphero.power.wake()
        sleep(2)
        sphero.power.enter_soft_sleep()


if __name__ == "__main__":
    main()
