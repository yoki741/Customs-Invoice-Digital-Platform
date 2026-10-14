# -*- coding: utf-8 -*-
"""对比 core/exporter.py 与 invoice_gui.InvoiceProcessor 的输出，逐单元格 diff。

用法：
    python tests/compare_exe_web.py
"""

import shutil
import sys
import tempfile
import warnings
from pathlib import Path

warnings.simplefilter("ignore")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import openpyxl  # noqa: E402

from core.exporter import Exporter  # noqa: E402
from core.source_reader import read_source_file  # noqa: E402

# 延迟导入 EXE 模块（它 import 了 tkinter，headless 下会失败，所以只在需要时 import）
import invoice_gui  # noqa: E402


SOURCES = [
    ROOT / "柬埔寨清关例子" / "海运例子 清关资料 货代提单" / "DKT_202608_FB338_26LF0845.xlsm",
    ROOT / "柬埔寨清关例子" / "海运例子 清关资料 货代提单" / "VCO_202609_FB338_26LF0846-1.xlsm",
    ROOT / "柬埔寨清关例子" / "空运例子 清关资料" / "DKT_202609_FB932_2609007.xlsm",
    ROOT / "柬埔寨清关例子" / "陆运例子 清关资料" / "VCO_202609_FB429_LY260905-1DM.xlsm",
    ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm",
]

SHEETS = ["INV1", "PKL1"]
IGNORE_CELLS = {
    "INV1": {
        # Web 的 _write_header 会写入 header 联系方式，EXE 不写；属"多做"非"做错"
        "A3", "A4",
    },
    "PKL1": set(),
}  # 已知无害差异


def diff_workbooks(path_a: Path, path_b: Path, label_a: str, label_b: str) -> list[str]:
    """逐单元格比较两个 .xlsm 的 INV1 / PKL1，返回差异描述列表。"""
    diffs: list[str] = []
    try:
        wb_a = openpyxl.load_workbook(str(path_a), data_only=False)
        wb_b = openpyxl.load_workbook(str(path_b), data_only=False)
    except Exception as exc:
        return [f"  [ERROR] 无法打开: {exc}"]

    for sheet in SHEETS:
        if sheet not in wb_a.sheetnames or sheet not in wb_b.sheetnames:
            diffs.append(f"  [{sheet}] 工作表缺失: {label_a}={'Y' if sheet in wb_a.sheetnames else 'N'} "
                         f"{label_b}={'Y' if sheet in wb_b.sheetnames else 'N'}")
            continue
        ws_a, ws_b = wb_a[sheet], wb_b[sheet]
        max_row = max(ws_a.max_row, ws_b.max_row)
        max_col = max(ws_a.max_column, ws_b.max_column)
        for r in range(1, max_row + 1):
            for c in range(1, max_col + 1):
                coord = ws_a.cell(row=r, column=c).coordinate
                if coord in IGNORE_CELLS.get(sheet, set()):
                    continue
                va = ws_a.cell(row=r, column=c).value
                vb = ws_b.cell(row=r, column=c).value
                if va != vb:
                    diffs.append(f"  [{sheet}!{coord}] {label_a}={va!r}  {label_b}={vb!r}")

    wb_a.close()
    wb_b.close()
    return diffs


def run_exe(src: Path, out_dir: Path) -> Path | None:
    """调用 EXE 的 InvoiceProcessor 生成输出，返回生成的 .xlsm 路径。"""
    proc = invoice_gui.InvoiceProcessor(str(ROOT))
    # EXE 需要 manual_data 指定输出目录；不传则走默认
    results = proc.process_files([src], manual_data={"output_dir": str(out_dir)})
    if not results:
        return None
    # 找到刚生成的文件
    candidates = list(out_dir.rglob("*.xlsm"))
    return candidates[0] if candidates else None


def run_web(src: Path, out_dir: Path) -> Path | None:
    """调用 core/exporter.py 生成输出（不传 manual，验证模板回退字段）。"""
    data = read_source_file(src)
    exporter = Exporter()
    result = exporter.build([data], None)
    if not result.ok:
        print(f"  [WEB BUILD FAIL] {result.messages}")
        return None
    out_path = out_dir / f"{result.file_stem}.xlsm"
    exporter.save_xlsm(result.workbook, out_path, src)
    return out_path


def main() -> int:
    total_fail = 0
    for src in SOURCES:
        print(f"\n{'=' * 60}")
        print(f"源文件: {src.name}")
        if not src.exists():
            print("  [SKIP] 文件不存在")
            continue

        with tempfile.TemporaryDirectory() as td:
            exe_dir = Path(td) / "exe"
            web_dir = Path(td) / "web"
            exe_dir.mkdir()
            web_dir.mkdir()

            exe_path = run_exe(src, exe_dir)
            web_path = run_web(src, web_dir)

            if not exe_path:
                print("  [SKIP] EXE 未生成输出")
                continue
            if not web_path:
                print("  [SKIP] Web 未生成输出")
                total_fail += 1
                continue

            print(f"  EXE: {exe_path.name}")
            print(f"  Web: {web_path.name}")

            diffs = diff_workbooks(exe_path, web_path, "EXE", "WEB")
            if not diffs:
                print("  [PASS] 逐单元格一致")
            else:
                total_fail += 1
                print(f"  [FAIL] {len(diffs)} 处差异:")
                for d in diffs[:40]:
                    print(d)
                if len(diffs) > 40:
                    print(f"  ... 还有 {len(diffs) - 40} 处")

    print(f"\n{'=' * 60}")
    print(f"结果: {'全部通过' if total_fail == 0 else f'{total_fail} 个文件有差异'}")
    return total_fail


if __name__ == "__main__":
    sys.exit(main())
