"""Exercise actual pyserial I/O through an OS pseudo terminal; no physical device."""

import os
import pty
import select
import threading
import time
import unittest

from sensefuture.catalog import BY_COMMAND
from sensefuture.device import Client
from sensefuture.protocol import with_crc


class SerialTests(unittest.TestCase):
    def test_fragmented_ascii_and_rtu_on_real_serial_backend(self):
        for mode in ("ASCII", "RTU"):
            with self.subTest(mode=mode):
                master, slave = pty.openpty()
                stop = threading.Event()
                errors = []

                def emulate():
                    buffer = bytearray()
                    try:
                        while not stop.is_set():
                            if not select.select([master], [], [], 0.05)[0]:
                                continue
                            buffer.extend(os.read(master, 256))
                            if mode == "ASCII" and b"@" in buffer:
                                if bytes(buffer) != b"TC2:TG=?@":
                                    raise AssertionError(buffer)
                                reply = b"OKTC2:TG=-123450@\r\n"
                                buffer.clear()
                            elif mode == "RTU" and len(buffer) >= 8:
                                if bytes(buffer[:6]) != bytes.fromhex("010320000002"):
                                    raise AssertionError(buffer)
                                reply = with_crc(
                                    bytes.fromhex("010304")
                                    + (-123450).to_bytes(4, "big", signed=True)
                                )
                                buffer.clear()
                            else:
                                continue
                            for byte in reply:
                                os.write(master, bytes([byte]))
                                time.sleep(0.001)
                    except Exception as exc:
                        errors.append(exc)

                thread = threading.Thread(target=emulate)
                thread.start()
                client = None
                try:
                    client = Client(os.ttyname(slave), mode=mode)
                    self.assertEqual(client.read(BY_COMMAND["TG"], 2), -123450)
                    self.assertEqual(errors, [])
                finally:
                    stop.set()
                    thread.join(1)
                    if client:
                        client.close()
                    os.close(master)
                    os.close(slave)
