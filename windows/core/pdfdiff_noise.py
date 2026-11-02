# -*- coding: utf-8 -*-
"""
pdfdiff_noise.py —— 判「一条差异是不是只差了几个装饰性符号」
==================================================================
用户 2026-09-29 原话：

> 「那种一个一个的小点点，这种非文字类的，而且也没有任何含义的标点符号，
>  没必要标记出来。」

实测 GRA-4118 rev06→rev07 里正是这三条噪声：

| 清单条目 | 旧侧 | 新侧 | 性质 |
|---|---|---|---|
| #5 | （无） | `•` | 单独成条的装饰点 |
| #6 | （无） | `•` | 同上 |
| #1 | `• 以FAS对临床试验中…` | `以FAS对临床试验中…` | 只在句首少了一个 `•` |

它们剥掉符号后两侧一模一样 —— 报出来只会让人以为「这句改了」。

## 规则表（组 → 生效位置 → 依据）

| 组 | 字符 | 生效位置 | 依据 |
|---|---|---|---|
| `bullet` | `• ∙ ◦ ∘ ‣ ⁃ ▪ ▫` | **任意位置** | 列表点 / 项目符号，中文技术文档里不承载语义 |
| `shape`  | `○ ● ◉ ◎ ◇ ◆ ■ □ ▸ ▹ ► ▶ ◄ ★ ☆ ✦ ✧ ✱ ✲ ✳ ✴ ❖ ※ ♦ ♠ ♥ ♣` | **任意位置** | 装饰几何块，同上 |
| `lead`   | `· ‧ ･ ・` | **仅行首** | ⚠️ 中点是**人名分隔符**（`迈克尔·乔丹`），只有出现在行首才是列表点 |
| `zero`   | `\\u200b \\u200c \\u200d \\ufeff \\u00ad` | **任意位置** | 零宽字符 / 软连字符，排版工具留下的残渣 |

**不纳入**（有语义，动了必须报）：连字符 `-`、破折号 `—`、范围号 `~ ≥ ≤`、
百分号、括号、冒号、逗号、顿号、斜杠、星号 `*`（可能是脚注引用）。

## 判定口径
「两侧剥掉装饰符号后**完全相同**（含两侧都为空）」→ 这条差异只剩装饰符号 → 舍去。

三条护栏：
1. 两侧**一个装饰符号都没有** → 不判（那是空白/标点的事，归各自的忽略选项管）；
2. 剥掉后仍不同 → 不判（`• 5mg` vs `• 10mg` 是真改动）；
3. 只对 `added / removed / moved` 生效 —— 位置移动、整页增删不受影响。

⚠️ 这是**事后过滤**（在清单生成之后），不改任何比对逻辑：判定链一分未动，
只是不把纯装饰差异端到人眼前。想要原样清单时把 `Options.drop_noise` 关掉即可。
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Sequence, Tuple

# ---------------------------------------------------------------- 规则表
#: 组名 → (说明, 字符集, 是否仅行首生效)
NOISE_TABLE: Dict[str, Tuple[str, str, bool]] = {
    "bullet": ("列表点 / 项目符号", "•∙◦∘‣⁃▪▫", False),
    "shape":  ("装饰几何块", "○●◉◎◇◆■□▸▹►▶◄★☆✦✧✱✲✳✴❖※♦♠♥♣", False),
    "lead":   ("行首的次要点（中点可能是人名分隔符）", "·‧･・", True),
    "zero":   ("零宽字符 / 软连字符",
               "\u200b\u200c\u200d\ufeff\u00ad", False),
}

#: 适用的差异类型（其余类型不参与判定）
APPLY_KINDS = ("added", "removed", "moved")


def _chars(pos_lead: bool) -> str:
    out = []
    for _name, (_desc, chars, lead_only) in NOISE_TABLE.items():
        if lead_only == pos_lead:
            out.append(chars)
    return "".join(out)


ANY_RE = re.compile("[" + re.escape(_chars(False)) + "]+")
LEAD_RE = re.compile(r"^[\s" + re.escape(_chars(True)) + r"]+")
_WS_RE = re.compile(r"\s+")


def _hit_re() -> re.Pattern:
    hits = "".join(chars for _d, chars, _l in NOISE_TABLE.values())
    return re.compile("[" + re.escape(hits) + "]")


HIT_RE = _hit_re()


# ---------------------------------------------------------------- 提取
def strip_noise(s: str) -> str:
    """剥掉装饰符号：任意位置的直接删；「仅行首」的只在行首删。

    逐行处理 —— 中点是行首才删，所以不能对整段做一次 `sub`。
    """
    if not s:
        return ""
    out: List[str] = []
    for line in s.split("\n"):
        t = ANY_RE.sub("", line)
        t = LEAD_RE.sub("", t)          # ^[\s·‧･・]+  —— 行首的空白与次要点
        out.append(t)
    return "\n".join(out)


def noise_only(old: str, new: str) -> bool:
    """这条差异是不是**只剩装饰符号**（剥掉后两侧一模一样）。"""
    old, new = old or "", new or ""
    if old == new:
        return False                                    # 本来就没差异
    if not (HIT_RE.search(old) or HIT_RE.search(new)):  # 护栏 1：两侧都没装饰符号
        return False
    # 比较时按「忽略空白」口径 —— 剥掉 `A ▪ B` 里的 `▪` 会留下双空格，
    # 而空白差异本来就被 `ignore_whitespace` 忽略，不归这条规则管。
    return _WS_RE.sub("", strip_noise(old)) == _WS_RE.sub("", strip_noise(new))


#: 组名 → 该组字符的单字符探针（预编译，避免每次现编正则）
PROBE: Dict[str, re.Pattern] = {
    name: re.compile("[" + re.escape(chars) + "]")
    for name, (_desc, chars, _lead) in NOISE_TABLE.items()
}


def _lead_hit(s: str, probe: re.Pattern) -> bool:
    """「仅行首」组：只看每行**被剥掉的那截前缀**里有没有该组字符。

    不能对整行搜 —— 那样 `迈克尔·乔丹` 也会算命中。
    """
    for ln in s.split("\n"):
        m = LEAD_RE.match(ln)
        if m and probe.search(m.group(0)):
            return True
    return False


def hit_groups(old: str, new: str) -> List[str]:
    """命中了哪几组（写进清单备注 / 统计用）。"""
    out: List[str] = []
    for name, (_desc, _chars, lead_only) in NOISE_TABLE.items():
        probe = PROBE[name]
        for s in (old or "", new or ""):
            if (_lead_hit(s, probe) if lead_only else probe.search(s)):
                out.append(name)
                break
    return out


def drop_noise(items: Sequence[Any],
               enabled: bool = True) -> Tuple[List[Any], int]:
    """过滤掉「只剩装饰符号」的条目，返回 `(保留的条目, 舍去条数)`。"""
    if not enabled:
        return list(items), 0
    kept: List[Any] = []
    dropped = 0
    for it in items:
        if (getattr(it, "kind", "") in APPLY_KINDS
                and noise_only(it.old, it.new)):
            dropped += 1
            continue
        kept.append(it)
    return kept, dropped


# ==================================================================
# 自检
# ==================================================================
def self_test() -> None:
    ok = fail = 0

    def chk(name, cond, extra=""):
        nonlocal ok, fail
        if cond:
            ok += 1
        else:
            fail += 1
            print(f"  ✗ {name}{('　→ ' + extra) if extra else ''}")

    # ① 用户实测的三条 —— 必须舍去
    chk("单独的项目符号（新增）", noise_only("", "•"))
    chk("行首少一个项目符号", noise_only("• 以FAS对临床试验中…", "以FAS对临床试验中…"))
    chk("行首少一个菱形点", noise_only("♦ 用充压装置使球囊导管产生负压；",
                                       "用充压装置使球囊导管产生负压；"))
    chk("行中夹装饰块", noise_only("A ▪ B", "A B"))

    # ② 真改动必须留下
    chk("装饰点相同但数字变了 → 留", not noise_only("• 5mg", "• 10mg"))
    chk("破折号有语义 → 留", not noise_only("2025—2026", "20252026"))
    chk("连字符有语义 → 留", not noise_only("GRA-4118", "GRA4118"))
    chk("冒号/括号有语义 → 留", not noise_only("压力：10", "压力 10"))

    # ③ 中点只在行首才算装饰（人名分隔符必须留住）
    chk("人名中点 → 留", not noise_only("迈克尔·乔丹", "迈克尔乔丹"))
    chk("行首中点 → 舍", noise_only("· 附注：本页无", "附注：本页无"))
    chk("行中中点 → 留", not noise_only("甲·乙", "甲乙"))

    # ④ 两侧都没有装饰符号 → 不判（空白/标点差异归各自的忽略选项）
    chk("纯空格差异 → 留", not noise_only("A B", "AB"))
    chk("纯标点差异 → 留", not noise_only("A，B", "A、B"))

    # ⑤ 零宽字符 / 软连字符
    chk("零宽空格差异 → 舍", noise_only("正常\u200b文本", "正常文本"))
    chk("软连字符差异 → 舍", noise_only("re\u00adturn", "return"))

    # ⑥ 整条都是符号
    chk("整条只有符号 → 舍", noise_only("•", ""))
    chk("符号行带空白 → 舍", noise_only("  •  ", ""))

    # ⑦ 剥离函数本身
    chk("strip 保留正文", strip_noise("♦ 装入带环芯轴") == "装入带环芯轴")
    chk("strip 多行逐行处理", strip_noise("• 第一\n· 第二") == "第一\n第二")
    chk("strip 不动人名", strip_noise("迈克尔·乔丹") == "迈克尔·乔丹")

    # ⑧ 条目级过滤：只吃适用的 kind
    class It:
        def __init__(self, kind, old="", new=""):
            self.kind, self.old, self.new = kind, old, new

    items = [It("added", "", "•"), It("removed", "•", ""),
             It("added", "", "真新增"),
             It("moved", "", "•"), It("page_added", "", "•")]
    kept, n = drop_noise(items)
    chk("舍去 3 条纯装饰（含 moved）", n == 3 and len(kept) == 2,
        f"n={n} kept={len(kept)}")
    chk("moved 的装饰符号同样被舍（2026-10-12 moved 加入 APPLY_KINDS）",
        not any(x.kind == "moved" for x in kept))
    chk("page_added 不受影响", any(x.kind == "page_added" for x in kept))
    chk("真新增留下", any(x.new == "真新增" for x in kept))

    # ⑨ 关掉开关 = 原样返回
    kept2, n2 = drop_noise(items, enabled=False)
    chk("开关关闭时原样返回", n2 == 0 and len(kept2) == len(items))

    # ⑩ 命中组别
    chk("命中组别可读", hit_groups("", "•") == ["bullet"], str(hit_groups("", "•")))

    print(f"装饰符号自检：{ok} 项通过，{fail} 项失败")
    raise SystemExit(1 if fail else 0)


if __name__ == "__main__":
    self_test()
