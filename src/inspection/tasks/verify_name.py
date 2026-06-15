"""从设备 prompt / 配置中抽取 hostname / sysname，与期望名比对。"""

from __future__ import annotations

import re

_HOSTNAME_RE = re.compile(r"(?:sysname|hostname)\s+(\S+)", re.IGNORECASE)


def extract_hostname(raw: str) -> str | None:
    """从配置输出中通过正则提取 hostname（兜底方案，已非主要路径）。"""
    if not raw:
        return None
    m = _HOSTNAME_RE.search(raw)
    return m.group(1) if m else None


def extract_hostname_from_prompt(prompt: str) -> str | None:
    """从 Netmiko find_prompt() 返回的 CLI 提示符中提取设备 hostname。

    无需发送任何命令即可获取，避免设备响应慢导致的超时问题。

    处理逻辑:
      - 华三/华为: 从右向左找最外层 <> 或 [] 括号对，提取括号内内容
                  （兼容 RBM_P<hostname> 等带前缀的场景）
      - 锐捷:     去掉尾部 # 或 >（用户/特权视图）
      - 配置视图:  hostname(config)# → 取括号前的主机名
    """
    if not prompt:
        return None
    prompt = prompt.strip()

    # 从右向左匹配最外层的括号对，提取内部主机名
    for close_bracket, open_bracket in [(">", "<"), ("]", "[")]:
        if prompt.endswith(close_bracket):
            idx = prompt.rfind(open_bracket)
            if idx >= 0:
                prompt = prompt[idx + 1:-1]
            break
    else:
        # 无括号，去掉尾部 # 或 >（锐捷视图）
        prompt = prompt.rstrip("#> ")

    # 处理配置视图 hostname(config)# → 只取括号前的主机名
    if "(" in prompt:
        prompt = prompt.split("(")[0]

    return prompt.strip() or None
