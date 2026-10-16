"""Synchronous client. Only the I/O thread owns this object and its port."""

import math
import struct
import time

import serial

from .catalog import BY_COMMAND, PARAMETERS
from .protocol import (
    AsciiStream,
    DeviceError,
    ProtocolError,
    ascii_request,
    rtu_request,
    rtu_response,
    with_crc,
)


class Simulator:
    """Wire-level deterministic demo, not a model of the real stage or its precision."""

    def __init__(self):
        self.values = {}
        for p in PARAMETERS:
            for ch in (1, 2) if p.channel else (1,):
                self.values[p.key(ch)] = max(p.minimum, min(p.maximum, 0))
        defaults = {
            "TG": 4000000,
            "TCADJTEMP": 2500000,
            "ENABLE": 0,
            "LIMITED": 30,
            "KP": 3000,
            "KI": 150,
            "KD": 0,
            "RP": 10000,
            "BX": 395000,
            "NTCRP": 10000000000,
            "PTRP": 2000000000,
            "PT1000RP": 1000000,
            "PTA": 3908300,
            "PTB": -577500,
            "PTC": -41830,
            "ONSENSOR": 1,
            "OVERTEMPUP": 8000000,
            "OVERTEMPLOWER": 0,
            "SETCURRENT": 50,
            "POWERMODE": 2,
        }
        for ch in (1, 2):
            for cmd, val in defaults.items():
                self.values[f"TC{ch}:{cmd}"] = val
        self.values.update(
            TEC=5, FPV=130, ADDRESS=1, SINTERIORTEMP=29, OVERTVPT=70, OVERTTEMP=1, FPWM=2
        )
        self.last = time.monotonic()
        self.pending = bytearray()

    @property
    def in_waiting(self):
        return len(self.pending)

    def tick(self):
        now = time.monotonic()
        dt, self.last = min(now - self.last, 2), now
        for ch in (1, 2):
            k = f"TC{ch}:"
            temp = self.values[k + "TCADJTEMP"] / 100000
            goal = self.values[k + "TG"] / 100000 if self.values[k + "ENABLE"] else 25
            power = max(
                -self.values[k + "LIMITED"], min(self.values[k + "LIMITED"], (goal - temp) * 15)
            )
            if self.values[k + "MODE"] == 3:
                power = self.values[k + "PWMDUTY"] / 20000
                if not self.values[k + "ENABLE"]:
                    power = 0
                # Match the reported stage polarity: negative manual voltage heats.
                goal = 25 - power * 0.7
            else:
                self.values[k + "PWMDUTY"] = int(power * 20000) if self.values[k + "ENABLE"] else 0
            temp += (goal - temp) * (1 - math.exp(-dt / 18))
            self.values[k + "TCADJTEMP"] = round(temp * 100000)
            self.values[k + "RESISTOR"] = round(
                10000 * math.exp(3950 * (1 / (temp + 273.15) - 1 / 298.15)) * 1e6
            )
            self.values[k + "CURRENT"] = round(abs(power) * 20)

    def write(self, request):
        self.tick()
        if request.endswith(b"@"):
            key, val = request[:-1].decode().split("=")
            if key == "INQUIRE":
                response = "".join(f"OK{k}={v}@" for k, v in self.values.items() if k != "RESET")
            elif key == "DATADEMAND":
                response = ""
                for ch in (1, 2):
                    prefix = f"TC{ch}:"
                    for cmd in ("TCADJTEMP", "RESISTOR"):
                        response += f"{prefix}{cmd}={self.values[prefix + cmd]}@"
                    pwm = self.values[prefix + "PWMDUTY"]
                    response += f"{prefix}{'PWM' if val == '1' else 'OUTV'}={pwm if val == '1' else pwm * 1200}@"
                response += "SINTERIORTEMP=29@"
            elif key not in self.values:
                response = "ERROR@"
            else:
                if val != "?":
                    self.values[key] = int(val)
                response = f"OK{key}={self.values[key]}@\r\n"
            self.pending.extend(response.encode())
        else:
            station, func, address, count = struct.unpack(">BBHH", request[:6])
            match = next(
                (
                    (p, ch)
                    for p in PARAMETERS
                    if p.rtu
                    for ch in ((1, 2) if p.channel else (1,))
                    if p.register(ch) == address
                ),
                None,
            )
            if not match:
                self.pending.extend(with_crc(bytes([station, func | 128, 2])))
            else:
                p, ch = match
                if count != p.bits // 16:
                    self.pending.extend(with_crc(bytes([station, func | 128, 3])))
                elif func == 16:
                    self.values[p.key(ch)] = int.from_bytes(request[7:-2], "big", signed=p.signed)
                    self.pending.extend(with_crc(request[:6]))
                else:
                    payload = self.values[p.key(ch)].to_bytes(p.bits // 8, "big", signed=p.signed)
                    self.pending.extend(with_crc(bytes([station, 3, len(payload)]) + payload))
        return len(request)

    def read(self, size=1):
        # Exercise fragmented reads even in demo mode.
        count = min(size, 17, len(self.pending))
        result = bytes(self.pending[:count])
        del self.pending[:count]
        if not count:
            time.sleep(0.005)
        return result

    def close(self):
        self.pending.clear()


class Client:
    def __init__(
        self,
        port,
        baud=38400,
        mode="ASCII",
        station=1,
        channels=2,
        timeout=1.0,
        log=lambda direction, data: None,
    ):
        self.mode, self.station, self.channels = mode, station, channels
        self.timeout, self.log, self.last = timeout, log, 0.0
        self.baud = baud
        self.port = (
            Simulator()
            if port == "SIMULATOR"
            else serial.Serial(
                port,
                baudrate=baud,
                bytesize=8,
                parity="N",
                stopbits=1,
                timeout=0.025,
                write_timeout=0.5,
                exclusive=True,
            )
        )

    def close(self):
        self.port.close()

    def exchange(self, request, expected=None, parameter=None, bulk=False):
        # Minimum inter-frame silence, including for the fastest supported UART.
        delay = 0.005 - (time.monotonic() - self.last)
        if delay > 0:
            time.sleep(delay)
        self.log("TX", request)
        if self.port.write(request) != len(request):
            raise ProtocolError("串口未完整发送")
        deadline = time.monotonic() + (
            max(self.timeout, 4096 * 10 / self.baud + 0.5)
            if request.startswith(b"INQUIRE=")
            else self.timeout
        )
        buffer, values, parser = bytearray(), {}, AsciiStream()
        last_rx = None
        while time.monotonic() < deadline:
            chunk = self.port.read(max(1, self.port.in_waiting))
            if chunk:
                self.log("RX", chunk)
                last_rx = time.monotonic()
                buffer.extend(chunk)
                if self.mode == "ASCII":
                    values.update(parser.feed(chunk))
                    if not bulk and expected.issubset(values):
                        self.last = time.monotonic()
                        return values
                else:
                    if len(buffer) >= 3:
                        size = 5 if buffer[1] & 128 else (8 if request[1] == 16 else buffer[2] + 5)
                        if len(buffer) >= size:
                            result = rtu_response(bytes(buffer), request, parameter)
                            self.last = time.monotonic()
                            return result
            elif self.mode == "ASCII" and bulk and last_rx and time.monotonic() - last_rx > 0.12:
                if not values or parser.buffer.strip(b"\x00\r\n "):
                    raise ProtocolError("批量应答不完整")
                if expected and not expected.issubset(values):
                    raise ProtocolError("批量应答缺少必要字段")
                self.last = time.monotonic()
                return values
        raise ProtocolError("设备响应超时；连接将关闭以避免迟到应答错配")

    def read(self, p, ch=1):
        if "R" not in p.access:
            raise ValueError("只写命令")
        if self.mode == "ASCII":
            key = p.key(ch)
            return self.exchange(ascii_request(key), {key})[key]
        return self.exchange(rtu_request(p, ch, self.station), parameter=p)

    def write(self, p, ch, value):
        if "W" not in p.access or not p.minimum <= value <= p.maximum:
            raise ValueError("只读参数或超出协议范围")
        if self.mode == "ASCII":
            key = p.key(ch)
            result = self.exchange(ascii_request(key, value), {key})[key]
            if result != value:
                raise DeviceError(f"设备应答值 {result} 与写入值 {value} 不同")
        else:
            self.exchange(rtu_request(p, ch, self.station, value), parameter=p)
        # Link-changing commands may take effect immediately; read back after reconnect.
        if "R" in p.access and p.command not in ("ADDRESS", "BOUNDTABLEONE", "BOUNDTABLETWO"):
            actual = self.read(p, ch)
            if actual != value:
                raise DeviceError(f"写后读回为 {actual}，期望 {value}")
        return value

    def bulk(self, command, value):
        if self.mode != "ASCII":
            raise ValueError("INQUIRE / DATADEMAND 仅适用于 ASCII")
        expected = {"SINTERIORTEMP"} if command == "DATADEMAND" else {"TC1:TG"}
        return self.exchange(ascii_request(command, value), expected, bulk=True)

    def sample(self):
        values = {}
        if self.mode == "ASCII":
            values.update(self.bulk("DATADEMAND", 2))
            required = {
                f"TC{ch}:{cmd}"
                for ch in range(1, self.channels + 1)
                for cmd in ("TCADJTEMP", "RESISTOR", "OUTV")
            }
            if not required.issubset(values):
                raise ProtocolError("关键数据缺少通道字段；请核对通道数量")
        for ch in range(1, self.channels + 1):
            cmds = (
                ("TG", "ENABLE", "MODE", "PWMDUTY")
                if self.mode == "ASCII"
                else ("TCADJTEMP", "RESISTOR", "TG", "ENABLE", "MODE", "PWMDUTY")
            )
            for cmd in cmds:
                p = BY_COMMAND[cmd]
                values[p.key(ch)] = self.read(p, ch)
        for cmd in ("ERRORCODE", "SINTERIORTEMP"):
            values[cmd] = self.read(BY_COMMAND[cmd])
        return values
