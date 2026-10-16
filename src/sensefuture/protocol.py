"""Wire codecs; no Qt or serial dependencies."""

import re
import struct

from .catalog import Parameter


class ProtocolError(Exception):
    pass


class DeviceError(ProtocolError):
    """Valid device rejection; unlike framing errors this does not desynchronise RTU."""


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def with_crc(data: bytes) -> bytes:
    return data + crc16(data).to_bytes(2, "little")


def ascii_request(key: str, value: int | None = None) -> bytes:
    if not re.fullmatch(r"(?:TC[12]:)?[A-Z][A-Z0-9]*", key):
        raise ValueError("非法命令名")
    return f"{key}={'?' if value is None else value}@".encode("ascii")


class AsciiStream:
    """Handles arbitrary fragments, mixed OK prefixes, multi-channel bulk replies.

    The INQUIRE example omits @ between TEC and the following OKTC1; tolerate
    that vendor example without accepting arbitrary garbage as a successful reply.
    """

    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> dict[str, int | str]:
        self.buffer.extend(data)
        if len(self.buffer) > 32768:
            raise ProtocolError("ASCII 接收缓冲区超限")
        result = {}
        while b"@" in self.buffer:
            frame, _, rest = self.buffer.partition(b"@")
            self.buffer = bytearray(rest)
            try:
                text = frame.decode("ascii").strip("\x00\r\n \t")
            except UnicodeDecodeError as exc:
                raise ProtocolError("ASCII 响应包含非 ASCII 字节") from exc
            if not text:
                continue
            pattern = r"(?:OK:?)?((?:TC[12]:)?[A-Z][A-Z0-9]*)=([^=@]+?)(?=OK|$)"
            pairs = re.findall(pattern, text)
            if not pairs and text.upper().startswith(("ERR", "FAIL")):
                raise DeviceError(text)
            if not pairs:
                raise ProtocolError(f"无法解析响应 {text!r}")
            if re.sub(pattern, "", text):
                raise ProtocolError(f"响应包含无法识别的内容 {text!r}")
            for key, value in pairs:
                if key.startswith("OK"):
                    key = key[2:]
                if re.fullmatch(r"-?\d+", value):
                    result[key] = int(value)
                elif key == "TEC" and re.fullmatch(r"[0-9]+[A-Za-z][A-Za-z0-9.]*", value):
                    result[key] = value
                else:
                    raise ProtocolError(f"参数 {key} 的应答值无效：{value!r}")
        return result


def rtu_request(p: Parameter, channel: int, station: int, value: int | None = None) -> bytes:
    if not 1 <= station <= 247:
        raise ValueError("Modbus 站号必须为 1~247")
    address, count = p.register(channel), p.bits // 16
    if value is None:
        if "R" not in p.access:
            raise ValueError("只写命令不可读取")
        return with_crc(struct.pack(">BBHH", station, 3, address, count))
    if "W" not in p.access or not p.minimum <= value <= p.maximum:
        raise ValueError("参数只读或数值超出范围")
    payload = value.to_bytes(p.bits // 8, "big", signed=p.signed)
    return with_crc(struct.pack(">BBHHB", station, 16, address, count, len(payload)) + payload)


def rtu_response(frame: bytes, request: bytes, p: Parameter) -> int | None:
    if len(frame) < 5 or with_crc(frame[:-2]) != frame:
        raise ProtocolError("Modbus CRC 或长度错误")
    if frame[0] != request[0]:
        raise ProtocolError("Modbus 站号不匹配")
    if frame[1] == (request[1] | 0x80):
        if len(frame) != 5:
            raise ProtocolError("Modbus 异常帧长度错误")
        raise DeviceError(f"Modbus 异常码 {frame[2]}")
    if frame[1] != request[1]:
        raise ProtocolError("Modbus 功能码不匹配")
    if request[1] == 16:
        if len(frame) != 8 or frame[2:6] != request[2:6]:
            raise ProtocolError("写入地址或寄存器数量不匹配")
        return None
    size = p.bits // 8
    if frame[2] != size or len(frame) != size + 5:
        raise ProtocolError("Modbus 读取字节数不匹配")
    return int.from_bytes(frame[3:-2], "big", signed=p.signed)
