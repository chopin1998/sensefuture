"""One serial owner, bounded command queue, no automatic reconnect or write retry."""

import itertools
import queue
import time

from PyQt6.QtCore import QThread, pyqtSignal

from .catalog import BY_COMMAND
from .device import Client
from .protocol import DeviceError


class DeviceWorker(QThread):
    connected = pyqtSignal(bool, str)
    values = pyqtSignal(dict)
    sample = pyqtSignal(float, float, dict)
    message = pyqtSignal(str)
    wire = pyqtSignal(str)
    stopped = pyqtSignal(bool)

    def __init__(self, config, interval=0.5):
        super().__init__()
        self.config, self.interval = config, interval
        self.commands = queue.PriorityQueue(maxsize=256)
        self.sequence = itertools.count()

    def submit(self, operation, *args, priority=10):
        try:
            self.commands.put_nowait((priority, next(self.sequence), operation, args))
            return True
        except queue.Full:
            self.message.emit("命令队列已满，请等待执行完成")
            return False

    def clear_pending(self):
        while True:
            try:
                self.commands.get_nowait()
            except queue.Empty:
                break

    def stop_outputs(self):
        self.clear_pending()
        self.submit("stop", priority=0)

    def wire_log(self, direction, data):
        rendered = (
            data.decode("ascii", errors="backslashreplace")
            .replace("\r", "\\r")
            .replace("\n", "\\n")
            if self.config["mode"] == "ASCII"
            else data.hex(" ").upper()
        )
        self.wire.emit(f"{direction}  {rendered}")

    def run(self):
        client = None
        try:
            client = Client(**self.config, log=self.wire_log)
            identity = {cmd: client.read(BY_COMMAND[cmd]) for cmd in ("TEC", "FPV")}
            self.values.emit(identity)
            self.connected.emit(
                True, "模拟设备" if self.config["port"] == "SIMULATOR" else self.config["port"]
            )
            next_poll = 0
            while not self.isInterruptionRequested():
                try:
                    _, _, op, args = self.commands.get(timeout=0.01)
                except queue.Empty:
                    op, args = "", ()
                try:
                    if op == "close":
                        break
                    if op == "interval":
                        self.interval = args[0]
                    elif op == "read":
                        p, ch = args
                        self.values.emit({p.key(ch): client.read(p, ch)})
                    elif op == "write":
                        p, ch, value = args
                        result = client.write(p, ch, value)
                        self.values.emit({p.key(ch): result})
                        self.message.emit(f"已确认 {p.key(ch)} = {p.display(result)} {p.unit}")
                        if p.command in ("ADDRESS", "BOUNDTABLEONE", "BOUNDTABLETWO", "RESET"):
                            self.message.emit("连接配置 / 出厂设置已写入，请按设备新配置重连")
                            break
                    elif op == "bulk":
                        result = client.bulk(*args)
                        self.values.emit(result)
                        self.message.emit(f"批量查询 {args[0]}={args[1]}：{result}")
                    elif op == "stop":
                        p = BY_COMMAND["ENABLE"]
                        confirmed = True
                        for ch in range(1, client.channels + 1):
                            try:
                                client.write(p, ch, 0)
                                self.values.emit({p.key(ch): 0})
                            except DeviceError as exc:
                                confirmed = False
                                self.message.emit(f"通道 {ch} 未确认关闭：{exc}")
                        self.stopped.emit(confirmed)
                        self.message.emit(
                            "所有已配置通道输出关闭，设备读回已确认"
                            if confirmed
                            else "部分通道未确认关闭，请检查设备"
                        )
                    if time.monotonic() >= next_poll:
                        started = time.monotonic()
                        data = client.sample()
                        self.values.emit(data)
                        self.sample.emit(time.time(), time.monotonic(), data)
                        next_poll = max(started + self.interval, time.monotonic() + 0.005)
                except DeviceError as exc:
                    self.message.emit(f"设备拒绝：{exc}")
                    next_poll = time.monotonic() + max(0.5, self.interval)
                    if op == "stop":
                        self.stopped.emit(False)
                except ValueError as exc:
                    self.message.emit(str(exc))
        except Exception as exc:
            self.message.emit(f"通信中止：{exc}；设备输出状态未知")
            self.stopped.emit(False)
        finally:
            if client:
                client.close()
            self.connected.emit(False, "已断开；断开串口不会自动关闭设备输出")
