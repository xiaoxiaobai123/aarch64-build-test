from pyModbusTCP.client import ModbusClient
import log_config
logger = log_config.setup_logging()
class PLCBase:
    """Modbus TCP communication class for PLC using pymodbus."""

    def __init__(self, plc_ip: str, port: int = 502):
        """
        Initialize the PLC communicator.

        :param ip_address: IP address of the Modbus server (PLC).
        :param port: Port of the Modbus server (PLC). Default is 502.
        """
        self.client = ModbusClient(host=plc_ip, port=502, auto_open=True, timeout=1)
        self.connected = self.client.open()
        if self.connected:
            logger.info(f"[PLC] connected {plc_ip}")
        else:
            logger.error(f"[PLC] connect failed {plc_ip}")

    
    def read_status(self, address, count=1):
        """
        Read a specified number of consecutive holding registers starting from a given address.

        :param address: The starting address of the first register to read.
        :param count: The number of registers to read (default is 1).
        :return: A single value if count is 1, otherwise a list of register values or None if an error occurs.
        """
        try:
            if count <= 125:
                # Read within the valid range
                registers = self.client.read_holding_registers(address, count)
                if registers:
                    if count == 1:
                        return registers[0]  # Return a single value if only one register is read
                    else:
                        return registers  # Return a list of values if more than one register is read
                else:
                    logger.error(f"Failed to read status from PLC at address {address} with count {count}")
                    return None
            else:
                # Handle reading more than 125 registers
                max_registers = 125
                filedata = []
                start_address = address
                remaining_count = count

                while remaining_count > 0:
                    read_count = min(max_registers, remaining_count)
                    registers = self.client.read_holding_registers(start_address, read_count)
                    if registers is None:
                        logger.error(
                            f"Failed to read status from PLC at address {start_address} with count {read_count}")
                        return None
                    filedata.extend(registers)
                    start_address += read_count
                    remaining_count -= read_count

                if count == 1:
                    return filedata[0]  # Return a single value if only one register was requested
                else:
                    return filedata  # Return a list of values if more than one register was requested
        except Exception as e:
            logger.exception(f"Error reading status from PLC at address {address} with count {count}: {e}")
            return None

    def write_status(self, address, value):
        try:
            # Check if the address is one of the signed registers
            if address in [124, 125, 126]:
                # Handle signed values
                if not (-32768 <= value <= 32767):
                    logger.exception(
                        f"Error during monitoring and triggering detection: reg_value out of range for signed register (valid from -32768 to 32767): {value}")
                    return False
                # Convert signed value to unsigned for writing
                if value < 0:
                    value = 65536 + value
            else:
                # Handle unsigned values
                if not (0 <= value <= 65535):
                    logger.exception(
                        f"Error during monitoring and triggering detection: reg_value out of range for unsigned register (valid from 0 to 65535): {value}")
                    return False

            # Write the single register value
            result = self.client.write_single_register(address, value)
            return result
        except Exception as e:
            logger.exception(f"Error during monitoring and triggering detection: {e}")
            return False

    def write_multiple_registers(self, address, values):
        """
        Write multiple consecutive holding registers starting from a given address.
        Handle both signed and unsigned values.

        :param address: The starting address of the first register to write.
        :param values: A list of values to write to consecutive registers.
        :return: True if write is successful, otherwise False.
        """
        try:
            max_registers = 123
            start_address = address
            remaining_values = values

            while remaining_values:
                write_values = remaining_values[:max_registers]

                # Convert signed values to unsigned
                processed_values = []
                for value in write_values:
                    if value < 0:
                        value = 65536 + value  # Convert signed to unsigned
                    processed_values.append(value)

                result = self.client.write_multiple_registers(start_address, processed_values)
                if not result:
                    logger.error(
                        f"Failed to write values to PLC at address {start_address} with values {processed_values}")
                    return False

                start_address += len(write_values)
                remaining_values = remaining_values[max_registers:]

            return True
        except Exception as e:
            logger.exception(f"Error writing multiple registers to PLC at address {address} with values {values}: {e}")
            return False
    
    def close(self):
        """
        Close the Modbus client connection.
        """
        if self.client.is_open:  # 注意这里没有括号
            self.client.close()
            logger.info("PLC connection closed")
        else:
            logger.warning("PLC connection already closed")