"""Declarative command catalog. RD105_SPEC_通讯协议_v1.3.0.pdf, sections 3 and 4.

Ranges are wire values. They are not the safe operating range of the connected stage.
"""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Parameter:
    command: str
    label: str
    group: str
    address: int | None
    bits: int = 16
    signed: bool = False
    minimum: int = 0
    maximum: int = 65535
    scale: int = 1
    unit: str = ""
    access: str = "RW"
    channel: bool = True
    note: str = ""
    rtu: bool = True

    def key(self, channel: int = 1) -> str:
        if channel not in (1, 2):
            raise ValueError("通道必须为 1 或 2")
        return f"TC{channel}:{self.command}" if self.channel else self.command

    def register(self, channel: int = 1) -> int:
        self.key(channel)
        if self.address is None or not self.rtu:
            raise ValueError("此命令没有已确认的 Modbus 映射，请使用 ASCII")
        return self.address + (channel - 1) * 0x1000 if self.channel else self.address

    def encode(self, text: str) -> int:
        try:
            number = Decimal(text) * self.scale
            if not number.is_finite() or number != number.to_integral_value():
                raise ValueError("数值小数位超出协议分辨率")
            value = int(number)
        except InvalidOperation as exc:
            raise ValueError("请输入有效数值") from exc
        if not self.minimum <= value <= self.maximum:
            raise ValueError(
                f"范围 {self.display(self.minimum)} ~ {self.display(self.maximum)} {self.unit}"
            )
        return value

    def display(self, value: int | str) -> str:
        if self.command == "TEC" and isinstance(value, str):
            return value
        if self.unit == "°C":
            return format(Decimal(value) / self.scale, ".3f")
        return format(Decimal(value) / self.scale, "f")


def p(
    command,
    label,
    group,
    address,
    bits=16,
    signed=False,
    lo=0,
    hi=65535,
    scale=1,
    unit="",
    access="RW",
    channel=True,
    note="",
    rtu=True,
):
    return Parameter(
        command,
        label,
        group,
        address,
        bits,
        signed,
        lo,
        hi,
        scale,
        unit,
        access,
        channel,
        note,
        rtu,
    )


PARAMETERS = [
    p("TG", "目标温度", "常用控制", 0x1000, 32, True, -40000000, 100000000, 100000, "°C"),
    p(
        "TCADJTEMP",
        "测量温度 / 外部温度输入",
        "常用控制",
        0x1002,
        32,
        True,
        -40000000,
        100000000,
        100000,
        "°C",
        note="写入是外部温度输入，不是目标温度；需配合传感器保护配置。999999999 表示无传感器。",
    ),
    p("RESISTOR", "传感器电阻", "传感器", 0x1004, 64, False, 1, 500000000000, 1000000, "Ω", "R"),
    p("ENABLE", "输出使能", "常用控制", 0x1100, hi=1, note="0 关闭；1 开启"),
    p(
        "MODE",
        "输出模式",
        "常用控制",
        0x1101,
        hi=3,
        note="0 制冷+加热；1 仅制冷；2 仅加热；3 手动电压百分比",
    ),
    p("PIDPOL", "输出极性", "输出配置", 0x1102, hi=1, note="0 正极性；1 反极性"),
    p(
        "PWMDUTY",
        "手动输出",
        "输出配置",
        0x1103,
        64,
        True,
        -2000000,
        2000000,
        20000,
        "%",
        note="仅在 MODE=3 下手动控制。",
    ),
    p(
        "AUTOPID",
        "PID 自整定",
        "PID",
        0x1107,
        hi=1,
        note="0 关闭；1 自整定；文档值 2 为保留，不提供写入。",
    ),
    p("SPEED", "温变速率", "输出配置", 0x1108, hi=10000, scale=1000, unit="°C/s", note="0 不限速"),
    p("FDEADV", "正向起始电压幅值", "输出配置", 0x110A, hi=400, scale=200, unit="%"),
    p("BDEADV", "反向起始电压幅值", "输出配置", 0x110B, hi=400, scale=200, unit="%"),
    p(
        "ONSENSOR",
        "传感器开路 / 短路保护",
        "保护",
        0x110C,
        signed=True,
        hi=1,
        note="1 启用；0 允许外部温度输入",
    ),
    p("LIMITED", "最大输出电压百分比", "常用控制", 0x110E, signed=True, hi=90, unit="%"),
    p("STARTUPDELAY", "上电延时", "输出配置", 0x110F, lo=3, hi=180, unit="s"),
    p("POWERMODE", "上电输出策略", "保护", 0x1110, hi=2, note="0 记忆状态；1 上电开启；2 上电关闭"),
    p(
        "CURRENT",
        "输出电流",
        "输出配置",
        0x1111,
        hi=900000000,
        scale=1000,
        unit="A",
        access="R",
        rtu=False,
        note="协议 uint16 与范围矛盾，RTU 暂禁用。",
    ),
    p("SETCURRENT", "最大输出电流", "保护", 0x1112, lo=5, hi=150, scale=10, unit="A"),
    *[
        p(cmd, label, "PID", addr, 32, hi=9000000)
        for cmd, label, addr in (
            ("KP", "比例 P", 0x1200),
            ("KI", "积分 I", 0x1202),
            ("KD", "微分 D", 0x1204),
        )
    ],
    p(
        "POLYOMIAL",
        "温度计算模型",
        "传感器",
        0x1300,
        hi=3,
        note="0 NTC B；1 PT；2 Steinhart-Hart；3 MF501（协议原拼写）",
    ),
    p("BX", "NTC B 值", "传感器", 0x1301, 32, lo=100000, hi=5000000, scale=100),
    p("RP", "NTC R0", "传感器", 0x1303, 32, lo=1, hi=9000000, unit="Ω"),
    p(
        "NTCRP",
        "NTC 内部参考电阻",
        "传感器",
        0x1305,
        64,
        lo=1,
        hi=11000000000,
        scale=1000000,
        unit="Ω",
    ),
    p("PT1000RP", "PT R0", "传感器", 0x1309, 32, hi=10000000, scale=1000, unit="Ω"),
    p("PTA", "PT 系数 A", "传感器", 0x130B, 32, True, -9000000, 9000000, 10**9),
    p("PTB", "PT 系数 B", "传感器", 0x130D, 32, True, -9000000, 9000000, 10**12),
    p("PTC", "PT 系数 C", "传感器", 0x130F, 32, True, -90000, 90000, 10**16),
    p(
        "PTRP",
        "PT 内部参考电阻",
        "传感器",
        0x1311,
        64,
        lo=1,
        hi=2100000000,
        scale=1000000,
        unit="Ω",
    ),
    *[
        p(
            f"POLA{i}",
            f"多项式 A{i} 尾数",
            "多项式",
            0x1315 + i * 5,
            64,
            True,
            -99999999999999,
            99999999999999,
            10**13,
            note="尾数 = 原始整数 / 10^13；最终系数 = 尾数 × 10^指数。",
        )
        for i in range(8)
    ],
    *[
        p(f"POLEA{i}", f"多项式 A{i} 指数", "多项式", 0x1319 + i * 5, 16, True, -100, 100)
        for i in range(8)
    ],
    p(
        "OVERTEMPUP",
        "传感器温度上限",
        "保护",
        0x133D,
        32,
        True,
        -300000000,
        500000000,
        100000,
        "°C",
    ),
    p(
        "OVERTEMPLOWER",
        "传感器温度下限",
        "保护",
        0x133F,
        32,
        True,
        -300000000,
        500000000,
        100000,
        "°C",
    ),
    *[
        p(
            f"MF501{letter}",
            f"MF501 系数 {letter}",
            "传感器",
            addr,
            64,
            True,
            -(10**14),
            10**14,
            1000000,
            rtu=False,
            note="协议 int64 / 2 寄存器矛盾，RTU 暂禁用。",
        )
        for letter, addr in (("A", 0x1342), ("B", 0x1346), ("C", 0x134A))
    ],
    p("RESET", "恢复出厂设置", "通用", 0x0000, lo=1, hi=1, access="W", channel=False),
    p("TEC", "设备型号", "通用", 0x0001, hi=255, access="R", channel=False),
    p(
        "ADDRESS",
        "RS485 站号",
        "通用",
        0x0002,
        lo=0,
        hi=255,
        channel=False,
        note="协议配置范围 0~255；Modbus 单播连接仅支持站号 1~247。写入后按新站号重连。",
    ),
    p(
        "SINTERIORTEMP",
        "控制器内部温度",
        "通用",
        0x0003,
        16,
        True,
        -20,
        120,
        unit="°C",
        access="R",
        channel=False,
    ),
    p(
        "CONTMODE",
        "通道联动模式",
        "通用",
        0x0004,
        signed=True,
        hi=3,
        channel=False,
        note="0 独立；1 温差；2 输出跟随；3 温差+跟随",
    ),
    p("ERRORCODE", "设备故障位", "通用", 0x0007, access="R", channel=False),
    *[
        p(
            cmd,
            label,
            "通用",
            addr,
            hi=7,
            channel=False,
            note="0 4800；1 9600；2 19200；3 38400；4 57600；5 115200；6 230400；7 460800。写入后重连。",
        )
        for cmd, label, addr in (
            ("BOUNDTABLEONE", "TTL 波特率代码", 0x0008),
            ("BOUNDTABLETWO", "RS485 波特率代码", 0x0009),
        )
    ],
    p("OVERTVPT", "控制器过温降额阈值", "保护", 0x000A, lo=40, hi=100, unit="°C", channel=False),
    p(
        "OVERTTEMP",
        "传感器越限动作",
        "保护",
        0x000B,
        hi=1,
        channel=False,
        note="0 继续输出；1 停止输出",
    ),
    p("FPV", "固件版本代码", "通用", 0x000C, lo=100, hi=9999, access="R", channel=False),
    p(
        "FPWM",
        "PWM 频率代码",
        "通用",
        0x000D,
        hi=3,
        channel=False,
        note="0 0.5 Hz；1 1 Hz；2 10 Hz；3 100 Hz",
    ),
]
BY_COMMAND = {item.command: item for item in PARAMETERS}

ERROR_BITS = {
    0: "控制器过温降额",
    1: "控制器过温停机",
    2: "欠压",
    3: "过压",
    5: "通道 1 温度越限",
    6: "通道 1 限流",
    9: "通道 2 温度越限",
    10: "通道 2 限流",
}


def decode_errors(value: int) -> str:
    return (
        "、".join(ERROR_BITS.get(i, f"未知位 {i}") for i in range(16) if value & (1 << i))
        or "无故障"
    )
