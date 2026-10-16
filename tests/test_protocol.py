import unittest
from decimal import Decimal

from sensefuture.catalog import BY_COMMAND, PARAMETERS, decode_errors
from sensefuture.device import Client
from sensefuture.protocol import (
    AsciiStream,
    DeviceError,
    ProtocolError,
    ascii_request,
    crc16,
    rtu_request,
    rtu_response,
    with_crc,
)


class CodecTests(unittest.TestCase):
    def test_manual_crc_vectors(self):
        p = BY_COMMAND["TG"]
        self.assertEqual(rtu_request(p, 1, 1).hex(), "010310000002c0cb")
        self.assertEqual(rtu_request(p, 1, 1, 2500000).hex(), "01101000000204002625a0c54c")
        self.assertEqual(crc16(bytes.fromhex("010310000002")), 0xCBC0)
        self.assertEqual(
            rtu_response(bytes.fromhex("010304002625a00110"), rtu_request(p, 1, 1), p), 2500000
        )
        self.assertIsNone(
            rtu_response(bytes.fromhex("0110100000024508"), rtu_request(p, 1, 1, 2500000), p)
        )

    def test_ascii_arbitrary_fragmentation(self):
        parser = AsciiStream()
        result = {}
        for byte in b"OKTC1:TG=-123450@\r\nTC2:RESISTOR=10000000000@OKERRORCODE=0@":
            result.update(parser.feed(bytes([byte])))
        self.assertEqual(result, {"TC1:TG": -123450, "TC2:RESISTOR": 10000000000, "ERRORCODE": 0})

    def test_inquire_missing_delimiter_in_manual(self):
        result = AsciiStream().feed(b"OKTEC=215OKTC1:PT1000RP=1000000@TC2:PT1000RP=1000000@")
        self.assertEqual(result["TEC"], 215)
        self.assertEqual(result["TC1:PT1000RP"], 1000000)

    def test_live_207l_response_variants(self):
        parser = AsciiStream()
        result = {}
        for byte in b"OKTEC=207L@\r\n\x00OK:FPV=5007@\r\n\x00OKERRORCODE=0@\r\n\x00":
            result.update(parser.feed(bytes([byte])))
        self.assertEqual(result, {"TEC": "207L", "FPV": 5007, "ERRORCODE": 0})
        self.assertEqual(BY_COMMAND["TEC"].display(result["TEC"]), "207L")
        for frame in (b"OKTC1:TG=40L@", b"OKTEC=garbage@"):
            with self.assertRaises(ProtocolError):
                AsciiStream().feed(frame)

    def test_not_emit_partial_number(self):
        stream = AsciiStream()
        self.assertEqual(stream.feed(b"OKTC1:TG=25"), {})
        self.assertEqual(stream.feed(b"00000@"), {"TC1:TG": 2500000})

    def test_reject_garbage_and_invalid_encoding(self):
        for frame in (b"junkTC1:TG=3@", b"garbage@", b"\xff@"):
            with self.subTest(frame=frame), self.assertRaises(ProtocolError):
                AsciiStream().feed(frame)
        with self.assertRaises(DeviceError):
            AsciiStream().feed(b"ERROR@")
        with self.assertRaises(ProtocolError):
            AsciiStream().feed(b"a" * 32769)

    def test_decimal_round_trip_no_float_truncation(self):
        p = BY_COMMAND["TG"]
        self.assertEqual(p.encode("40.01000"), 4001000)
        self.assertEqual(p.encode("-0.00001"), -1)
        for v in ("NaN", "Infinity", "40.000001", "1000.00001", "nonsense"):
            with self.subTest(v=v), self.assertRaises(ValueError):
                p.encode(v)
        for p in PARAMETERS:
            for raw in (p.minimum, p.maximum):
                self.assertEqual(p.encode(p.display(raw)), raw)
        self.assertEqual(BY_COMMAND["PTC"].encode("-4.183e-12"), -41830)
        self.assertEqual(Decimal(BY_COMMAND["POLA0"].display(54120000000000)), Decimal("5.412"))

    def test_signed_rtu_and_channel_offset(self):
        p = BY_COMMAND["TG"]
        req = rtu_request(p, 2, 7)
        self.assertEqual(req[2:4], bytes.fromhex("2000"))
        frame = with_crc(b"\x07\x03\x04" + (-123450).to_bytes(4, "big", signed=True))
        self.assertEqual(rtu_response(frame, req, p), -123450)
        p = BY_COMMAND["RESISTOR"]
        req = rtu_request(p, 2, 1)
        frame = with_crc(b"\x01\x03\x08" + (10000000000).to_bytes(8, "big"))
        self.assertEqual(rtu_response(frame, req, p), 10000000000)

    def test_reject_bad_crc_station_function_count_and_ack(self):
        p = BY_COMMAND["TG"]
        req = rtu_request(p, 1, 1)
        frames = [
            bytes.fromhex("010304002625a00000"),
            with_crc(b"\x02\x03\x04\x00\x26\x25\xa0"),
            with_crc(b"\x01\x04\x04\x00\x26\x25\xa0"),
            with_crc(b"\x01\x03\x02\x00\x00"),
        ]
        for frame in frames:
            with self.subTest(frame=frame), self.assertRaises(ProtocolError):
                rtu_response(frame, req, p)
        with self.assertRaises(ProtocolError):
            rtu_response(with_crc(bytes.fromhex("011020000002")), rtu_request(p, 1, 1, 2500000), p)
        with self.assertRaises(DeviceError):
            rtu_response(with_crc(b"\x01\x83\x02"), req, p)

    def test_catalog_access_guards(self):
        self.assertEqual(len(PARAMETERS), 62)
        self.assertEqual(len(BY_COMMAND), len(PARAMETERS))
        with self.assertRaises(ValueError):
            rtu_request(BY_COMMAND["RESISTOR"], 1, 1, 5)
        for cmd in ("MF501A", "MF501B", "MF501C", "CURRENT"):
            with self.subTest(cmd=cmd), self.assertRaises(ValueError):
                rtu_request(BY_COMMAND[cmd], 1, 1)
        with self.assertRaises(ValueError):
            rtu_request(BY_COMMAND["TG"], 1, 0)
        with self.assertRaises(ValueError):
            ascii_request("TC1:TG=2@RESET")
        with self.assertRaises(ValueError):
            BY_COMMAND["AUTOPID"].encode("2")
        self.assertIn("通道 1", decode_errors(32))
        self.assertIn("未知位 15", decode_errors(32768))


class ClientTests(unittest.TestCase):
    def test_all_catalog_ascii_read_and_write(self):
        c = Client("SIMULATOR")
        try:
            for p in PARAMETERS:
                for ch in (1, 2) if p.channel else (1,):
                    with self.subTest(command=p.command, ch=ch):
                        if "R" in p.access:
                            self.assertIsInstance(c.read(p, ch), int)
                        if "W" in p.access and p.command not in ("RESET", "TCADJTEMP"):
                            value = c.read(p, ch)
                            c.write(p, ch, value)
        finally:
            c.close()

    def test_ascii_and_rtu_measure_and_stop(self):
        for mode in ("ASCII", "RTU"):
            with self.subTest(mode=mode):
                c = Client("SIMULATOR", mode=mode)
                c.write(BY_COMMAND["TG"], 2, -123450)
                self.assertEqual(c.read(BY_COMMAND["TG"], 2), -123450)
                c.write(BY_COMMAND["ENABLE"], 1, 1)
                c.write(BY_COMMAND["ENABLE"], 1, 0)
                s = c.sample()
                self.assertEqual(s["TC1:ENABLE"], 0)
                self.assertEqual(s["TC2:TG"], -123450)
                self.assertGreater(s["TC1:RESISTOR"], 2**32)
                c.close()

    def test_bulk_modes(self):
        c = Client("SIMULATOR")
        self.assertIn("TC2:TG", c.bulk("INQUIRE", 1))
        self.assertIn("TC1:PWM", c.bulk("DATADEMAND", 1))
        self.assertIn("TC2:OUTV", c.bulk("DATADEMAND", 2))
        c.close()

    def test_timeout_and_incomplete_bulk(self):
        class Silent:
            in_waiting = 0

            def write(self, data):
                return len(data)

            def read(self, size):
                return b""

            def close(self):
                pass

        c = Client("SIMULATOR", timeout=0.02)
        c.port = Silent()
        with self.assertRaises(ProtocolError):
            c.read(BY_COMMAND["TG"])
        c.close()

    def test_write_readback_mismatch(self):
        c = Client("SIMULATOR")
        c.read = lambda p, ch: 123
        with self.assertRaises(DeviceError):
            c.write(BY_COMMAND["TG"], 1, 2500000)
        c.close()


if __name__ == "__main__":
    unittest.main()
