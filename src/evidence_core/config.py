"""配置加载：极简 YAML 子集解析器 + 三份配置的读取入口。

为什么自己写解析器而不用 PyYAML：
既有的 target-evidence-model 与 delivery-gate-model 都是「纯标准库、零第三方依赖」，
本项目沿用同一约束，保证三个模型能在同一个干净环境里跑，也便于交付。

支持的 YAML 子集：嵌套映射（缩进）、标量、行内列表 [a, b]、块列表（- item）、
注释、引号、以及 `|` 块字符串。不支持锚点、多文档、复杂流式语法——配置里也用不到。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "configs"


# ----------------------------------------------------------------------------
# 极简 YAML 解析
# ----------------------------------------------------------------------------

def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return ""
    if (raw[0] == '"' and raw[-1] == '"') or (raw[0] == "'" and raw[-1] == "'"):
        return raw[1:-1]
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(item) for item in inner.split(",")]
    lowered = raw.lower()
    if lowered in ("true", "yes"):
        return True
    if lowered in ("false", "no"):
        return False
    if lowered in ("null", "none", "~"):
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _strip_comment(line: str) -> str:
    """去掉行尾注释，但保留引号内的 #。"""
    out, quote = [], None
    for ch in line:
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        else:
            out.append(ch)
    return "".join(out).rstrip()


class _Line:
    __slots__ = ("indent", "text")

    def __init__(self, indent: int, text: str) -> None:
        self.indent = indent
        self.text = text


def _tokenize(text: str) -> list[_Line]:
    lines: list[_Line] = []
    raw_lines = text.splitlines()
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        stripped = _strip_comment(raw)
        if not stripped.strip():
            i += 1
            continue
        indent = len(stripped) - len(stripped.lstrip())
        content = stripped.strip()
        # 跨行的行内列表 `provides: [a, b,` ... `c]`：把后续行接上直到括号闭合
        if content.count("[") > content.count("]"):
            j = i + 1
            while j < len(raw_lines) and content.count("[") > content.count("]"):
                content += " " + _strip_comment(raw_lines[j]).strip()
                j += 1
            lines.append(_Line(indent, content))
            i = j
            continue
        # `key: |` 块字符串：把后续更深缩进的行合并成一个标量
        if content.endswith("|"):
            key = content[:-1].rstrip().rstrip(":")
            block: list[str] = []
            j = i + 1
            while j < len(raw_lines):
                nxt = raw_lines[j]
                if not nxt.strip():
                    block.append("")
                    j += 1
                    continue
                nxt_indent = len(nxt) - len(nxt.lstrip())
                if nxt_indent <= indent:
                    break
                block.append(nxt.strip())
                j += 1
            joined = " ".join(part for part in block if part)
            lines.append(_Line(indent, f"{key}: {joined!r}"))
            i = j
            continue
        lines.append(_Line(indent, content))
        i += 1
    return lines


def _parse_block(lines: list[_Line], start: int, indent: int) -> tuple[Any, int]:
    # 块列表
    if start < len(lines) and lines[start].indent == indent and lines[start].text.startswith("- "):
        items: list[Any] = []
        i = start
        while i < len(lines) and lines[i].indent == indent and lines[i].text.startswith("- "):
            item_text = lines[i].text[2:].strip()
            if ":" in item_text and not item_text.startswith(("[", '"', "'")):
                # 列表元素是映射：把它当成一个更深缩进的块
                sub_lines = [_Line(indent + 2, item_text)]
                j = i + 1
                while j < len(lines) and lines[j].indent > indent:
                    sub_lines.append(lines[j])
                    j += 1
                value, _ = _parse_block(sub_lines, 0, indent + 2)
                items.append(value)
                i = j
            else:
                items.append(_parse_scalar(item_text))
                i += 1
        return items, i

    # 映射
    mapping: dict[str, Any] = {}
    i = start
    while i < len(lines):
        line = lines[i]
        if line.indent < indent:
            break
        if line.indent > indent:
            i += 1
            continue
        if line.text.startswith("- "):
            break
        if ":" not in line.text:
            i += 1
            continue
        key, _, rest = line.text.partition(":")
        key = key.strip()
        rest = rest.strip()
        if rest:
            mapping[key] = _parse_scalar(rest)
            i += 1
        else:
            # 值在下一层
            if i + 1 < len(lines) and lines[i + 1].indent > indent:
                value, next_i = _parse_block(lines, i + 1, lines[i + 1].indent)
                mapping[key] = value
                i = next_i
            else:
                mapping[key] = {}
                i += 1
    return mapping, i


def load_yaml(path: str | Path) -> dict[str, Any]:
    """解析配置文件。文件不存在时返回空 dict，由调用方决定是否报错。"""
    p = Path(path)
    if not p.exists():
        return {}
    lines = _tokenize(p.read_text(encoding="utf-8"))
    if not lines:
        return {}
    result, _ = _parse_block(lines, 0, lines[0].indent)
    return result if isinstance(result, dict) else {}


# ----------------------------------------------------------------------------
# 具体配置入口
# ----------------------------------------------------------------------------

def load_cancer_config(cancer_id: str, config_dir: Path | None = None) -> dict[str, Any]:
    """加载癌种配置。这是「换癌种只改配置」的唯一入口。"""
    base = config_dir or CONFIG_DIR
    path = base / "cancer_types" / f"{cancer_id.lower()}.yaml"
    if not path.exists():
        available = sorted(
            p.stem for p in (base / "cancer_types").glob("*.yaml") if not p.stem.startswith("_")
        )
        raise FileNotFoundError(
            f"未找到癌种配置 '{cancer_id}'。已有：{available}。"
            f"新增方法：复制 configs/cancer_types/_template.yaml 为 {cancer_id.lower()}.yaml"
        )
    return load_yaml(path)


def load_source_registry(config_dir: Path | None = None) -> dict[str, Any]:
    base = config_dir or CONFIG_DIR
    return load_yaml(base / "sources.yaml")


def load_agent_config(config_dir: Path | None = None) -> dict[str, Any]:
    base = config_dir or CONFIG_DIR
    return load_yaml(base / "agent.yaml")


def deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """按 'a.b.c' 取嵌套值，缺失返回 default。配置读取到处都要用。"""
    node: Any = data
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
