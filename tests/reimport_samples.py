# -*- coding: utf-8 -*-
"""重启后向运行中的服务重新导入 9 个样例（走 /admin/upload）。"""

from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SAMPLES_DIR = ROOT / "柬埔寨清关例子"
FILES = [
    SAMPLES_DIR / "海运例子 清关资料 货代提单" / "DKT_202608_FB338_26LF0845.xlsm",
    SAMPLES_DIR / "海运例子 清关资料 货代提单" / "DKT_202609_FB338_26LF0846.xlsm",
    SAMPLES_DIR / "海运例子 清关资料 货代提单" / "VCO_202608_FB338_26LF0845-1.xlsm",
    SAMPLES_DIR / "海运例子 清关资料 货代提单" / "VCO_202609_FB338_26LF0846-1.xlsm",
    SAMPLES_DIR / "海运例子 清关资料2" / "DKT_202608_FB982_YS2026081502.xlsm",
    SAMPLES_DIR / "空运例子 清关资料" / "DKT_202609_FB932_2609007.xlsm",
    SAMPLES_DIR / "空运例子 清关资料" / "VCO_202609_FB932_2609006.xlsm",
    SAMPLES_DIR / "陆运例子 清关资料" / "VCO_202609_FB429_LY260905-1DM.xlsm",
    SAMPLES_DIR / "VCO_202609_FB675_D202609007.xlsm",
]

BASE = "http://127.0.0.1:8000"


def main() -> None:
    client = httpx.Client(base_url=BASE, follow_redirects=False, timeout=60)
    r = client.post("/login", data={"username": "admin", "password": "admin123"})
    assert r.status_code == 303, (r.status_code, r.text[:200])

    import re
    r = client.get("/invoices")
    csrf = re.search(r'name="csrf"\s+value="([^"]+)"', r.text).group(1)

    files = [("files", (p.name, p.read_bytes(), "application/octet-stream")) for p in FILES if p.exists()]
    r = client.post("/admin/upload", data={"csrf": csrf}, files=files)
    assert r.status_code == 200, (r.status_code, r.text[:500])
    ok = (r.text.count("New</td>") + r.text.count("Replaced existing invoice")
          + r.text.count("Overwrote existing invoice"))
    print(f"upload status={r.status_code}, 文件数={len(files)}")

    for p in FILES:
        assert p.exists(), p
    r = client.get("/invoices")
    print("invoices page:", r.status_code, "D202609007" in r.text)


if __name__ == "__main__":
    main()
