# -*- coding: utf-8 -*-
"""E2E: 两类账号核对闭环 + 权限边界。"""

import re
from pathlib import Path

import httpx

BASE = "http://127.0.0.1:8000"
CSRF_RE = re.compile(r'name="csrf"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="csrf"')


def csrf_from(html: str) -> str:
    m = CSRF_RE.search(html)
    if not m:
        return ""
    return m.group(1) or m.group(2) or ""


def login(client: httpx.Client, u: str, p: str) -> int:
    r = client.get("/login")
    return client.post(
        "/login",
        data={"username": u, "password": p, "csrf": csrf_from(r.text)},
        follow_redirects=False,
    ).status_code


def main() -> None:
    fails: list[str] = []

    def check(cond: bool, label: str) -> None:
        print(("[OK] " if cond else "[X] ") + label)
        if not cond:
            fails.append(label)

    # 重置核对状态，保证可重复运行
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from webapp.models import (
        CHECK_PASSED,
        CHECK_PENDING,
        Invoice as _Inv,
        SessionLocal as _SL,
    )

    db = _SL()
    db.query(_Inv).update({_Inv.check_status: CHECK_PENDING, _Inv.check_note: ""})
    db.commit()
    db.close()

    # --- type2 只读查看 ---
    c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    check(login(c, "type2", "test1234") == 303, "type2 登录")
    r = c.get("/details?invoice_id=1")
    check(r.status_code == 200, "type2 打开发票1明细")
    check("核对状态" not in r.text, "明细页不再显示核对状态")
    check("核对通过" not in r.text and 'name="status"' not in r.text,
          "明细页不再显示核对标记按钮")

    r = c.post(
        "/details/1/check",
        data={"csrf": "x", "status": "failed", "note": "不应生效"},
        follow_redirects=False,
    )
    check(r.status_code in (404, 405), "核对标记接口已移除")

    r = c.get("/details?check_status=failed")
    check(r.status_code == 200 and "核对状态" not in r.text, "状态筛选 UI 已移除")

    # 手动输入搜索发票号：唯一命中自动选中（2609006 无相似号）
    r = c.get("/details?inv_q=2609006")
    check("Fabric Invoice No.:</span> <b>2609006</b>" in r.text, "搜索发票号唯一命中自动选中")
    check('name="inv_q"' in r.text and 'value="2609006"' in r.text, "搜索框保留输入值")

    # 模糊匹配多命中（26LF0845 匹配 26LF0845 和 26LF0845-1）：不自动选中
    r = c.get("/details?inv_q=26LF0845")
    check("26LF0845-1" in r.text and "26LF0845" in r.text, "模糊搜索多命中不自动选中")

    # 客户名模糊搜索多命中：不自动选中，下拉只显示匹配项
    r = c.get("/details?inv_q=EASTEX")
    check("全部发票" in r.text, "多命中时不自动选中")
    check("EASTEX GARMENT" in r.text and "DAKOTA" not in r.text.split("<select")[1].split("</select>")[0],
          "下拉只含匹配客户的发票")

    # 无匹配
    r = c.get("/details?inv_q=NOTEXIST999")
    check("没有匹配" in r.text, "无匹配时显示提示")
    c.close()

    # --- type1 视角 ---
    c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    check(login(c, "type1", "test1234") == 303, "type1 登录")
    r = c.get("/invoices")
    check(r.status_code == 200, "type1 列表页正常")
    check("核对" not in r.text.split("<tbody")[0], "列表不再显示核对列")
    check("data-check-status" not in r.text and "data-invoice-no" in r.text,
          "勾选框只携带发票号，不再携带核对状态")
    check("已被核对标记为" not in r.text, "编辑/导出前拦截提示已移除")
    r = c.get("/invoices?check_status=failed")
    check(r.status_code == 200, "列表页状态筛选参数兼容（忽略）")
    r = c.get("/details", follow_redirects=False)
    check(r.status_code == 403, "type1 无权访问 /details")
    c.close()

    # --- type2 边界 ---
    c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    login(c, "type2", "test1234")
    r = c.get("/invoices", follow_redirects=False)
    check(r.status_code == 403, "type2 无权访问 /invoices")
    c.close()

    # --- admin 两侧都能看 ---
    c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    check(login(c, "admin", "admin123") == 303, "admin 登录")
    r = c.get("/invoices")
    check(r.status_code == 200 and "data-check-status" not in r.text, "admin 列表页正常（无核对列）")
    r = c.get("/details")
    check(r.status_code == 200 and "核对状态" not in r.text, "admin 明细页正常（无核对状态）")
    c.close()

    # --- 批量核对接口已移除 ---
    c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    login(c, "type2", "test1234")
    r = c.get("/details?check_status=pending")
    check("批量核对" not in r.text, "明细页不再显示批量核对清单")
    r = c.post("/details/batch-check", data={"ids": "1,2", "csrf": "x"},
               follow_redirects=False)
    check(r.status_code in (404, 405), "批量核对接口已移除")
    c.close()

    # --- 人工字段落库 ---
    from webapp.service import save_manual_values

    db = _SL()
    inv = db.query(_Inv).filter(_Inv.invoice_no == "2609007").one()
    save_manual_values(db, [inv], {"date": "2026-09-30", "bill_no": "TEST-001",
                                   "supplier": "EASTEX"})
    db.refresh(inv)
    check("TEST-001" in inv.manual_values, "save_manual_values 写入 manual_values")
    # 快照接口返回 manual
    c = httpx.Client(base_url=BASE, timeout=30, follow_redirects=False)
    login(c, "type1", "test1234")
    r = c.get(f"/api/invoices/snapshot?ids={inv.id}")
    data = r.json()
    check(data.get("ok") and data.get("manual", {}).get("bill_no") == "TEST-001",
          "快照接口返回存储的人工字段")
    c.close()
    db.close()

    # --- 重导入自动重置 ---
    db = _SL()
    inv = db.query(_Inv).filter(_Inv.invoice_no == "D202609007").one_or_none()
    if inv is None:
        inv = db.query(_Inv).first()
    inv.check_status = CHECK_PASSED
    inv.checked_by = "type2"
    db.commit()
    before = inv.check_status

    from webapp.importer import import_source_file
    from pathlib import Path as P

    src = next(P("data/uploads").glob("*D202609007*.xlsm"), None) or inv.source_file
    import_source_file(db, P(src), None)
    db.refresh(inv)
    check(inv.check_status == CHECK_PENDING and inv.check_note == "" and inv.checked_by is None,
          f"重导入自动重置待核对（{before} -> {inv.check_status}）")
    db.close()

    print()
    if fails:
        print("FAILED:", len(fails))
        for f in fails:
            print("  -", f)
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
