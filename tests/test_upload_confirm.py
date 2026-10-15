# -*- coding: utf-8 -*-
"""验证两段式上传：重复 Invoice No. 时先出确认页，勾选后才覆盖。"""

import re
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"
SAMPLE = ROOT / "柬埔寨清关例子" / "VCO_202609_FB675_D202609007.xlsm"


def get_csrf(client: httpx.Client) -> str:
    r = client.get("/admin/upload")
    m = re.search(r'name="csrf"\s+value="([^"]+)"', r.text)
    return m.group(1)


def main() -> None:
    client = httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)
    r = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert r.status_code == 303, r.status_code

    csrf = get_csrf(client)

    # 上传已存在的发票号 -> 应出确认页
    with SAMPLE.open("rb") as fh:
        r = client.post("/admin/upload", data={"csrf": csrf},
                        files=[("files", (SAMPLE.name, fh, "application/octet-stream"))])
    body = r.text
    assert "发现重复 Invoice No." in body, body[:800]
    assert "overwrite-check" in body
    assert SAMPLE.name in body
    print("[OK] 重复检测 -> 确认页出现")

    # 点确认（勾选覆盖）-> 入库
    csrf2 = get_csrf(client)
    path_json = str(SAMPLE).replace("\\", "\\\\")
    r = client.post("/admin/upload", data={
        "csrf": csrf2,
        "confirm": "1",
        "pending_json": '[{"file": "%s", "path": "%s", "invoice_no": "D202609007", "overwrite": true}]'
                        % (SAMPLE.name, path_json),
    })
    assert "覆盖已有发票" in r.text, r.text[:800]
    print("[OK] 确认覆盖 -> 入库成功")

    # 不勾选 -> 应提示跳过
    csrf3 = get_csrf(client)
    with SAMPLE.open("rb") as fh:
        r = client.post("/admin/upload", data={"csrf": csrf3},
                        files=[("files", (SAMPLE.name, fh, "application/octet-stream"))])
    assert "发现重复 Invoice No." in r.text
    csrf4 = get_csrf(client)
    r = client.post("/admin/upload", data={
        "csrf": csrf4, "confirm": "1",
        "pending_json": '[{"file": "%s", "path": "%s", "invoice_no": "D202609007", "overwrite": false}]'
                        % (SAMPLE.name, path_json),
    })
    assert "已跳过" in r.text, r.text[:800]
    print("[OK] 不勾选 -> 跳过，未覆盖")

    print("\n两段式上传测试全部通过")


if __name__ == "__main__":
    main()
