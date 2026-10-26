"""解析 Windows 快捷方式（.lnk）。

为什么要自己写？
  项目定了"只用标准库"。而 .lnk 的格式（微软公开文档 MS-SHLLINK）
  就是一个可以按字节读的结构，用 struct 解析即可，不必引入 pywin32。

它只做一件事：把 .lnk 拆开，尽力读出"指向哪个程序、带什么参数、工作目录在哪"。
读不出来就老实说读不出来（resolved_ok=False），**绝不猜**。

重要：启动快捷方式**不依赖**这里的解析结果。
真正启动时统一交给 os.startfile()，让 Windows 自己去解释快捷方式——
参数、工作目录、带空格和中文的路径、MSI 广告式快捷方式，系统都比我们处理得对。
这里解析出来的路径只用于两件事：识别这是哪个应用、在授权卡片上显示真实目标。

明确不处理的情况（写清楚，免得以后有人以为漏了）：
  - LinkTargetIDList（Shell 项目列表）：结构复杂，只当作次要线索跳过
  - 指向 shell:AppsFolder 的商店应用快捷方式：本来就没有文件系统路径
  - MSI 广告式快捷方式：没有 LocalBasePath
"""

from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

_HEADER_SIZE = 0x4C  # 76
# {00021401-0000-0000-C000-000000000046}
_LNK_CLSID = bytes(
    (0x01, 0x14, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
     0xC0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46)
)

# LinkFlags 里我们用得到的几位
_HAS_LINK_TARGET_ID_LIST = 0x00000001
_HAS_LINK_INFO = 0x00000002
_HAS_NAME = 0x00000004
_HAS_RELATIVE_PATH = 0x00000008
_HAS_WORKING_DIR = 0x00000010
_HAS_ARGUMENTS = 0x00000020
_HAS_ICON_LOCATION = 0x00000040
_IS_UNICODE = 0x00000080

# ExtraData 里的块签名
_ENV_VAR_BLOCK = 0xA0000001     # EnvironmentVariableDataBlock
_TERMINAL_BLOCK = 0x00000000

# EnvironmentVariableDataBlock 内部是**固定长度**的，布局如下：
#   +0   BlockSize(4)   +4   BlockSignature(4)
#   +8   TargetAnsi(260 字节)
#   +268 TargetUnicode(520 字节，UTF-16LE)
# 两份内容是一样的，一份 ANSI 一份 Unicode。
# ⚠️ 必须分开按各自的编码读——拿 ANSI 那 260 字节去按 UTF-16 解，
#    会解出一串中文乱码（"C:\...\cmd.exe" 会变成 "㩃坬浩潤…"），
#    而乱码会让"危险程序黑名单"完全失效，因为它比对的是文件名。
_ENV_ANSI_LEN = 260
_ENV_UNICODE_LEN = 520


@dataclass
class LnkInfo:
    """从一个 .lnk 里读到的信息。"""

    target: str = ""          # 尽力解析出的目标路径（可能为空）
    args: list[str] = field(default_factory=list)
    workdir: str = ""
    resolved_ok: bool = False  # 目标是否真的存在
    note: str = ""             # 没解析出来时，说明原因


def _decode(raw: bytes, unicode: bool) -> str:
    if unicode:
        return raw.decode("utf-16-le", "replace")
    try:
        return raw.decode("mbcs", "replace")
    except (LookupError, UnicodeDecodeError):  # 非 Windows 时的兜底
        return raw.decode("latin-1", "replace")


def _read_null_string(data: bytes, offset: int, *, unicode: bool) -> str:
    """从 offset 读一个以 \\0 结尾的字符串。"""
    if offset <= 0 or offset >= len(data):
        return ""
    if unicode:
        end = offset
        while end + 1 < len(data) and data[end:end + 2] != b"\x00\x00":
            end += 2
        return _decode(data[offset:end], True)
    end = data.find(b"\x00", offset)
    if end < 0:
        end = len(data)
    return _decode(data[offset:end], False)


def _read_string_data(data: bytes, pos: int, *, unicode: bool) -> tuple[str, int]:
    """StringData：先是 2 字节字符数，然后是字符本身。"""
    if pos + 2 > len(data):
        return "", 0
    count, = struct.unpack_from("<H", data, pos)
    pos += 2
    nbytes = count * 2 if unicode else count
    raw = data[pos:pos + nbytes]
    return _decode(raw, unicode), 2 + nbytes


def _parse_link_info(data: bytes, pos: int) -> tuple[str, int]:
    """解析 LinkInfo 块，返回（LocalBasePath 拼出来的目标, 这一块占了多少字节）。"""
    if pos + 4 > len(data):
        return "", 0
    size, = struct.unpack_from("<I", data, pos)
    if size < 0x1C or pos + size > len(data):
        return "", max(size, 0)

    header_size, = struct.unpack_from("<I", data, pos + 4)
    flags, = struct.unpack_from("<I", data, pos + 8)
    local_base_offset, = struct.unpack_from("<I", data, pos + 16)
    common_suffix_offset, = struct.unpack_from("<I", data, pos + 24)
    local_base_unicode = common_suffix_unicode = 0
    if header_size >= 0x24 and size >= 0x24:
        local_base_unicode, = struct.unpack_from("<I", data, pos + 28)
        common_suffix_unicode, = struct.unpack_from("<I", data, pos + 32)

    target = ""
    if flags & 0x1:  # VolumeIDAndLocalBasePath：才有本地路径
        base = _read_null_string(data, pos + local_base_unicode, unicode=True) \
            if local_base_unicode else ""
        if not base:
            base = _read_null_string(data, pos + local_base_offset, unicode=False)
        suffix = _read_null_string(data, pos + common_suffix_unicode, unicode=True) \
            if common_suffix_unicode else ""
        if not suffix:
            suffix = _read_null_string(data, pos + common_suffix_offset, unicode=False)
        target = (base + suffix).strip()
    return target, size


def _scan_extra_data(data: bytes, pos: int) -> str:
    """在 ExtraData 块里找环境变量目标，返回展开后的路径。

    很多系统快捷方式（"命令提示符""字符映射表"之类）没有 LinkInfo，
    目标就藏在 EnvironmentVariableDataBlock 里，所以这一段不是可有可无的。
    """
    steps = 0
    while pos + 8 <= len(data) and steps < 64:
        steps += 1
        block_size, = struct.unpack_from("<I", data, pos)
        signature, = struct.unpack_from("<I", data, pos + 4)
        if block_size < 8 or pos + block_size > len(data):
            break
        if signature == _TERMINAL_BLOCK:
            break
        if signature == _ENV_VAR_BLOCK:
            body = data[pos + 8:pos + block_size]
            text = ""
            # 先读 Unicode 那一份
            if len(body) >= _ENV_ANSI_LEN + _ENV_UNICODE_LEN:
                text = _decode(body[_ENV_ANSI_LEN:_ENV_ANSI_LEN + _ENV_UNICODE_LEN], True)
            if not text.strip() and len(body) >= _ENV_ANSI_LEN:
                text = _decode(body[:_ENV_ANSI_LEN], False)
            text = text.split("\x00")[0].strip()
            expanded = os.path.expandvars(text)
            # 展开后还留着 % 说明环境变量没法解析，不算数
            if expanded and "%" not in expanded:
                return expanded
        pos += block_size
    return ""


def _split_args(text: str) -> list[str]:
    """按 Windows 的规矩拆参数：双引号里面的空格不算分隔。"""
    args: list[str] = []
    cur = ""
    in_quotes = False
    for ch in text.strip():
        if ch == '"':
            in_quotes = not in_quotes
        elif ch.isspace() and not in_quotes:
            if cur:
                args.append(cur)
                cur = ""
        else:
            cur += ch
    if cur:
        args.append(cur)
    return args


def parse_lnk(path: str | Path) -> LnkInfo:
    """解析一个 .lnk。任何问题都用 resolved_ok=False + note 说明，不抛异常。"""
    target_path = Path(path)
    try:
        data = target_path.read_bytes()
    except OSError as exc:
        return LnkInfo(note=f"读不到这个快捷方式：{exc}")

    try:
        return _parse(data, target_path)
    except (struct.error, ValueError, IndexError) as exc:
        return LnkInfo(note=f"快捷方式格式看不懂（{type(exc).__name__}）")


def _parse(data: bytes, lnk_path: Path) -> LnkInfo:
    if len(data) < _HEADER_SIZE:
        return LnkInfo(note="文件太短，不像快捷方式")
    header_size, = struct.unpack_from("<I", data, 0)
    if header_size != _HEADER_SIZE:
        return LnkInfo(note=f"文件头大小不对（{header_size}）")
    if data[4:20] != _LNK_CLSID:
        return LnkInfo(note="不是标准快捷方式（CLSID 不匹配）")

    flags, = struct.unpack_from("<I", data, 20)
    pos = _HEADER_SIZE

    # 1) LinkTargetIDList：跳过。结构复杂，不当主解析路径。
    if flags & _HAS_LINK_TARGET_ID_LIST:
        idlist_size, = struct.unpack_from("<H", data, pos)
        pos += 2 + idlist_size

    # 2) LinkInfo：本地路径最主要就是从这儿来
    link_target = ""
    if flags & _HAS_LINK_INFO:
        link_target, consumed = _parse_link_info(data, pos)
        pos += consumed

    # 3) 字符串块，顺序是固定的
    is_unicode = bool(flags & _IS_UNICODE)
    strings: dict[str, str] = {}
    for flag, key in (
        (_HAS_NAME, "name"),
        (_HAS_RELATIVE_PATH, "relative_path"),
        (_HAS_WORKING_DIR, "workdir"),
        (_HAS_ARGUMENTS, "arguments"),
        (_HAS_ICON_LOCATION, "icon"),
    ):
        if flags & flag:
            text, consumed = _read_string_data(data, pos, unicode=is_unicode)
            pos += consumed
            strings[key] = text

    # 4) ExtraData：环境变量块
    env_target = _scan_extra_data(data, pos)

    # ---- 拼出候选目标，按可信度排序 ----
    candidates: list[str] = []
    for cand in (link_target, env_target):
        cand = cand.strip()
        if cand and cand not in candidates:
            candidates.append(cand)

    relative = strings.get("relative_path", "").strip()
    if relative:
        # 相对路径是相对"快捷方式所在的目录"算的
        try:
            joined = str((lnk_path.parent / relative).resolve())
            if joined not in candidates:
                candidates.append(joined)
        except OSError:
            pass

    workdir = os.path.expandvars(strings.get("workdir", "").strip())
    args = _split_args(strings.get("arguments", ""))

    if not candidates:
        return LnkInfo(
            args=args,
            workdir=workdir,
            note="这个快捷方式里没有可解析的文件路径"
                 "（可能是商店应用，或是 MSI 广告式快捷方式）",
        )

    # 优先挑一个真的存在的
    for cand in candidates:
        try:
            if Path(cand).is_file():
                return LnkInfo(target=cand, args=args, workdir=workdir, resolved_ok=True)
        except OSError:
            continue

    # 都不存在：还是把最可信的那个记下来，但标明没解析成功
    return LnkInfo(
        target=candidates[0],
        args=args,
        workdir=workdir,
        resolved_ok=False,
        note="解析出了路径，但那个文件现在不存在",
    )
