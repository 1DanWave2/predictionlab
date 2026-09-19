"""Refresh the auto-generated blocks of README.md / README.ru.md from results.json.

Blocks are delimited by HTML comments and everything outside them is left alone:

    <!-- auto:table_h1_bands -->
    ...regenerated...
    <!-- /auto:table_h1_bands -->

Blocks: datawindow, table_h1_bands, table_horizons, table_h1_cuts.
Run after analyze.py. Idempotent.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = HERE / "results.json"
DATA_WINDOW = HERE / "data-window.txt"
BANDS = [("0.00-0.05", "under 5¢", "до 5¢"), ("0.05-0.10", "5–10¢", "5–10¢"),
         ("0.10-0.20", "10–20¢", "10–20¢"), ("0.00-0.20", "all under 20¢", "все до 20¢")]
CATS = [("sports", "sports", "спорт"), ("politics_macro", "politics and macro", "политика и макро"),
        ("crypto", "crypto", "крипта"), ("culture", "culture", "культура"), ("other", "other", "прочее")]


def pct(x: float, signed: bool = False) -> str:
    return f"{x*100:+.2f}%" if signed else f"{x*100:.2f}%"


def ci(v: dict) -> str:
    return f"[{v['ci_lo']*100:+.2f}, {v['ci_hi']*100:+.2f}]"


def block_datawindow(r: dict, ru: bool) -> str:
    built = "?"
    if DATA_WINDOW.exists():
        m = re.search(r"built: (.+)", DATA_WINDOW.read_text())
        built = m.group(1).strip() if m else "?"
    n_m, n_o = r["markets"], r["observations"]
    if ru:
        return (f"*Данные обновляются каждую ночь: на {built} — {n_m:,} рынков с наблюдениями, {n_o:,} наблюдений. "
                f"Таблицы ниже пересчитаны на этих данных; текст написан по состоянию на 18.09.2026 "
                f"(182 503 рынка) и правится руками, когда заголовочное число меняет знак.*").replace(",", " ")
    return (f"*Data refreshes nightly: as of {built} there are {n_m:,} markets with observations and {n_o:,} observations. "
            f"The tables below are recomputed on that data; the prose was written on 2026-09-18 "
            f"(182,503 markets) and is revised by hand when a headline number changes sign.*")


def block_table_h1_bands(r: dict, ru: bool) -> str:
    fb = r["fade_by_horizon"]["1"]
    if ru:
        head = "| Сторона по цене | n | implied | realized | продажа гросс | после комиссии | после комиссии + 1¢ |"
    else:
        head = "| Side priced | n | implied | realized | sell gross | sell after fee | sell after fee + 1¢ |"
    rows = [head, "|---|---|---|---|---|---|---|"]
    for key, en, rus in BANDS:
        v = fb[key]
        if not v.get("n"):
            continue
        rows.append(f"| {rus if ru else en} | {v['n']:,} | {pct(v['implied'])} | {pct(v['realized'])} | "
                    f"{pct(v['gross']['ret'], True)} {ci(v['gross'])} | {pct(v['net_fee']['ret'], True)} | {pct(v['net']['ret'], True)} |")
    out = "\n".join(rows)
    return out.replace(",", " ") if ru else out


def block_table_horizons(r: dict, ru: bool) -> str:
    hs = sorted(int(h) for h in r["fade_by_horizon"])
    if ru:
        head = "| Дней до события | " + " | ".join(f"{b[2]}: гросс / нетто" for b in BANDS[:3]) + " |"
    else:
        head = "| Days before event | " + " | ".join(f"{b[1]}: gross / net" for b in BANDS[:3]) + " |"
    rows = [head, "|---|" + "---|" * 3]
    for h in hs:
        cells = []
        for key, _, _ in BANDS[:3]:
            v = r["fade_by_horizon"][str(h)][key]
            cells.append(f"{pct(v['gross']['ret'], True)} / {pct(v['net']['ret'], True)} (n={v['n']:,})" if v.get("n") else "—")
        rows.append(f"| {h} | " + " | ".join(cells) + " |")
    out = "\n".join(rows)
    return out.replace(",", " ") if ru else out


def block_table_h1_cuts(r: dict, ru: bool) -> str:
    h = "1"
    rows = [("| Разрез | implied | realized | продажа гросс |" if ru else "| Cut | implied | realized | sell gross |"), "|---|---|---|---|"]

    def add(label: str, v: dict) -> None:
        if v.get("n"):
            rows.append(f"| {label} | {pct(v['implied'])} | {pct(v['realized'])} | {pct(v['gross']['ret'], True)} {ci(v['gross'])} |")

    cat = r["by_category_longshots"][h]
    for key, en, rus in CATS:
        if key in cat:
            add(f"{rus if ru else en} (n = {cat[key]['n']:,})", cat[key])
    vol = r["by_volume"][h]
    add("нижняя треть по объёму" if ru else "lowest volume tercile", vol.get("low", {}))
    add("верхняя треть по объёму" if ru else "highest volume tercile", vol.get("high", {}))
    life = r["by_life"][h]
    add("срок жизни рынка больше 60 дней" if ru else "market lifetime over 60 days", life.get(">60d", {}))
    add("срок жизни 3–14 дней" if ru else "market lifetime 3–14 days", life.get("3-14d", {}))
    nr = r["neg_risk"][h]
    add("бинарный рынок" if ru else "binary market", nr.get("False", {}))
    add("участник negative-risk группы" if ru else "negative-risk group member", nr.get("True", {}))
    out = "\n".join(rows)
    return out.replace(",", " ") if ru else out


BLOCKS = {"datawindow": block_datawindow, "table_h1_bands": block_table_h1_bands,
          "table_horizons": block_table_horizons, "table_h1_cuts": block_table_h1_cuts}


def render(path: Path, r: dict, ru: bool) -> int:
    text = path.read_text(encoding="utf-8")
    n = 0
    for name, fn in BLOCKS.items():
        pat = re.compile(rf"(<!-- auto:{name} -->\n)(.*?)(\n<!-- /auto:{name} -->)", re.S)
        if pat.search(text):
            text, k = pat.subn(lambda m: m.group(1) + fn(r, ru) + m.group(3), text)
            n += k
    path.write_text(text, encoding="utf-8")
    return n


def main() -> None:
    r = json.loads(RESULTS.read_text())
    for fname, ru in (("README.md", False), ("README.ru.md", True)):
        n = render(HERE / fname, r, ru)
        print(f"{fname}: {n} blocks rendered", file=sys.stderr)


if __name__ == "__main__":
    main()
