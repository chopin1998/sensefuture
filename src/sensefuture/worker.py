"""One serial owner, bounded command queue, no automatic reconnect or write retry."""

import itertools
import math
import queue
import time

from PyQt6.QtCore import QThread, pyqtSignal

from .algorithms import ALGORITHMS
from .catalog import BY_COMMAND
from .device import Client
from .protocol import DeviceError


class DeviceWorker(QThread):
    connected = pyqtSignal(bool, str)
    values = pyqtSignal(dict)
    write_finished = pyqtSignal(str, bool)
    sample = pyqtSignal(float, float, dict)
    message = pyqtSignal(str)
    wire = pyqtSignal(str)
    stopped = pyqtSignal(bool)
    algorithm_state = pyqtSignal(dict)
    algorithm_parameters = pyqtSignal(dict)

    def __init__(self, config, interval=0.5):
        super().__init__()
        self.config, self.interval = config, interval
        self.commands = queue.PriorityQueue(maxsize=256)
        self.sequence = itertools.count()
        self.controller = None
        self.host_channel = None
        self.host_target = None
        self.host_settings = None
        self.host_key = None

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
                _, _, operation, args = self.commands.get_nowait()
                if operation == "write":
                    self.write_finished.emit(args[0].key(args[1]), False)
            except queue.Empty:
                break

    def stop_outputs(self):
        self.clear_pending()
        self.submit("stop", priority=0)

    def host_write(self, client, command, value):
        p = BY_COMMAND[command]
        result = client.write(p, self.host_channel, value)
        self.values.emit({p.key(self.host_channel): result})

    def stop_algorithm(self, client, reason):
        ch, self.controller = self.host_channel, None
        if ch is None:
            return
        confirmed = False
        try:
            self.host_write(client, "ENABLE", 0)
            self.host_write(client, "PWMDUTY", 0)
            confirmed = True
        except Exception as exc:
            self.message.emit(f"算法停止后关断未确认：{exc}")
        finally:
            self.host_channel = None
            self.algorithm_state.emit(
                {
                    "state": "idle" if confirmed else "failed",
                    "message": reason
                    + ("；输出关闭已确认" if confirmed else "；输出关闭未确认，请检查设备"),
                    "channel": ch,
                }
            )

    def start_algorithm(self, client, key, ch, target, settings):
        if self.host_channel is not None:
            raise ValueError("已有算法占用设备")
        spec = ALGORITHMS[key]
        spec.validate(settings)
        if self.interval >= settings["max_gap"]:
            raise ValueError("读取周期必须小于算法最大采样间隔")
        if ch not in range(1, client.channels + 1):
            raise ValueError("通道不存在")
        if not settings["min_temp"] <= target <= settings["max_temp"]:
            raise ValueError("目标超出算法温度边界")
        # Record ownership before any transaction so partial startup can be shut down.
        self.host_channel = ch
        self.host_key = key
        self.host_target, self.host_settings = target, dict(settings)
        data = client.sample()
        temp = data[f"TC{ch}:TCADJTEMP"] / 100000
        if data["ERRORCODE"] or not settings["min_temp"] <= temp <= settings["max_temp"]:
            raise ValueError("设备故障或当前温度超出算法边界")
        if client.read(BY_COMMAND["CONTMODE"]) != 0:
            raise ValueError("请先将通道联动模式设为独立")
        if client.read(BY_COMMAND["ONSENSOR"], ch) != 1:
            raise ValueError("请先启用传感器开路短路保护 ONSENSOR=1")
        limit = client.read(BY_COMMAND["LIMITED"], ch)
        if not 0 <= limit <= 100:
            raise ValueError("设备输出上限读回无效")
        # Inherit the confirmed hardware setting; never overwrite LIMITED.
        self.host_settings["limit"] = limit
        self.values.emit({f"TC{ch}:LIMITED": limit})
        self.host_write(client, "ENABLE", 0)
        self.host_write(client, "MODE", 3)
        self.host_write(client, "PWMDUTY", 0)
        self.host_write(client, "TG", round(target * 100000))
        self.controller = spec.factory(dict(self.host_settings))
        self.host_write(client, "ENABLE", 1)
        self.algorithm_state.emit(
            {
                "state": "running",
                "channel": ch,
                "message": f"{spec.name} · 通道 {ch} · 继承设备输出上限 {limit}%",
            }
        )

    def update_algorithm_settings(self, changes):
        if self.controller is None or not self.host_key:
            raise ValueError("算法未运行")
        spec = ALGORITHMS[self.host_key]
        allowed = {field.key for field in spec.fields if field.live}
        if not changes or not set(changes) <= allowed:
            raise ValueError("参数运行时锁定")
        candidate = {field.key: self.host_settings[field.key] for field in spec.fields}
        candidate.update(changes)
        spec.validate(candidate)
        reconfigure = getattr(self.controller, "reconfigure", None)
        if not callable(reconfigure):
            raise ValueError("算法不支持实时调参")
        candidate["limit"] = self.host_settings["limit"]
        reconfigure(dict(candidate))
        self.host_settings = candidate
        return {field.key: candidate[field.key] for field in spec.fields}

    def control_sample(self, client, now, data):
        if self.controller is None:
            return
        k = f"TC{self.host_channel}:"
        settings = self.host_settings
        temp = data[k + "TCADJTEMP"] / 100000
        if (
            data["ERRORCODE"]
            or data[k + "ENABLE"] != 1
            or data[k + "MODE"] != 3
            or data[k + "TG"] != round(self.host_target * 100000)
            or not settings["min_temp"] <= temp <= settings["max_temp"]
        ):
            raise ValueError("设备故障、控制状态改变或温度越界")
        output = self.controller.step(now, temp, self.host_target)
        if not math.isfinite(output) or abs(output) > settings["limit"]:
            raise ValueError("算法产生无效或超限输出")
        self.host_write(client, "PWMDUTY", round(output * 20000))
        self.algorithm_state.emit(
            {
                "state": "running",
                "channel": self.host_channel,
                "monotonic": now,
                "temperature": temp,
                "target": self.host_target,
                "reference": self.controller.reference,
                "output": output,
                "message": "上位机算法运行中",
            }
        )

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
                        self.stop_algorithm(client, "断开连接，算法停止")
                        break
                    if op == "host_start":
                        self.start_algorithm(client, *args)
                    elif op == "host_stop":
                        # A stop can overtake a queued start. Still close the selected
                        # channel instead of leaving its pre-existing output enabled.
                        if self.host_channel is None and args:
                            if args[0] not in range(1, client.channels + 1):
                                raise ValueError("通道不存在")
                            self.host_channel = args[0]
                        self.stop_algorithm(client, "已停止算法")
                        continue
                    elif op == "host_config":
                        changes = args[0]
                        try:
                            values = self.update_algorithm_settings(changes)
                            self.algorithm_parameters.emit(
                                {
                                    "accepted": True,
                                    "changes": changes,
                                    "values": values,
                                    "algorithm": self.host_key,
                                }
                            )
                        except ValueError as exc:
                            values = {
                                key: value
                                for key, value in (self.host_settings or {}).items()
                                if key != "limit"
                            }
                            self.algorithm_parameters.emit(
                                {
                                    "accepted": False,
                                    "changes": changes,
                                    "values": values,
                                    "algorithm": self.host_key,
                                    "message": str(exc),
                                }
                            )
                    elif op == "host_target":
                        if self.controller is None:
                            raise ValueError("算法未运行")
                        target = args[0]
                        if (
                            not self.host_settings["min_temp"]
                            <= target
                            <= self.host_settings["max_temp"]
                        ):
                            raise ValueError("目标超出算法温度边界")
                        self.host_write(client, "TG", round(target * 100000))
                        self.host_target = target
                    if op == "interval":
                        self.interval = args[0]
                        if self.controller and self.interval >= self.host_settings["max_gap"]:
                            raise ValueError("读取周期超过算法允许间隔")
                    elif op == "read":
                        p, ch = args
                        self.values.emit({p.key(ch): client.read(p, ch)})
                    elif op == "write":
                        p, ch, value = args
                        if self.host_channel is not None:
                            raise ValueError("算法运行期间禁止其他参数写入，请先停止算法")
                        result = client.write(p, ch, value)
                        self.values.emit({p.key(ch): result})
                        self.write_finished.emit(p.key(ch), True)
                        self.message.emit(f"已确认 {p.key(ch)} = {p.display(result)} {p.unit}")
                        if p.command in ("ADDRESS", "BOUNDTABLEONE", "BOUNDTABLETWO", "RESET"):
                            self.message.emit("连接配置 / 出厂设置已写入，请按设备新配置重连")
                            break
                    elif op == "bulk":
                        result = client.bulk(*args)
                        self.values.emit(result)
                        self.message.emit(f"批量查询 {args[0]}={args[1]}：{result}")
                    elif op == "stop":
                        if self.host_channel is None:
                            self.algorithm_state.emit(
                                {"state": "idle", "message": "算法已取消，正在关闭全部输出"}
                            )
                        self.stop_algorithm(client, "关闭全部输出，算法停止")
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
                        # Let an exit request close the port immediately after stop
                        # confirmation, without another polling transaction in between.
                        continue
                    if time.monotonic() >= next_poll:
                        started = time.monotonic()
                        data = client.sample()
                        self.values.emit(data)
                        sampled = time.monotonic()
                        self.sample.emit(time.time(), sampled, data)
                        if self.controller and sampled - started > self.host_settings["max_gap"]:
                            raise ValueError("采样耗时超过算法允许间隔")
                        self.control_sample(client, sampled, data)
                        next_poll = max(started + self.interval, time.monotonic() + 0.005)
                except DeviceError as exc:
                    self.stop_algorithm(client, f"设备通信错误：{exc}")
                    if op == "write":
                        self.write_finished.emit(args[0].key(args[1]), False)
                    self.message.emit(f"设备拒绝：{exc}")
                    next_poll = time.monotonic() + max(0.5, self.interval)
                    if op == "stop":
                        self.stopped.emit(False)
                except ValueError as exc:
                    had_host = self.host_channel is not None
                    # A competing write is rejected without cancelling a valid controller.
                    if op != "write":
                        self.stop_algorithm(client, f"算法停止：{exc}")
                    if op == "host_start" and not had_host:
                        self.algorithm_state.emit({"state": "idle", "message": str(exc)})
                    if op == "write":
                        self.write_finished.emit(args[0].key(args[1]), False)
                    self.message.emit(str(exc))
        except Exception as exc:
            self.message.emit(f"通信中止：{exc}；设备输出状态未知")
            self.stopped.emit(False)
        finally:
            if client:
                self.stop_algorithm(client, "通信结束，算法停止")
                client.close()
            self.connected.emit(False, "已断开；断开串口不会自动关闭设备输出")
