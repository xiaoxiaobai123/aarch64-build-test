import platform
import sys

import pymodbus
from pymodbus.client import ModbusSerialClient, ModbusTcpClient


def main() -> None:
    print("Hello, world!")
    print(f"Python: {sys.version.splitlines()[0]}")
    print(f"Platform: {platform.platform()}")
    print(f"Machine: {platform.machine()}")
    print(f"pymodbus: {pymodbus.__version__}")
    print(f"TCP client class: {ModbusTcpClient.__name__}")
    print(f"Serial client class: {ModbusSerialClient.__name__}")


if __name__ == "__main__":
    main()
