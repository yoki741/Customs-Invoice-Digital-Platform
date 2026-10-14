import puppeteer from 'puppeteer-core';

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const BASE = process.env.BASE_URL || 'http://127.0.0.1:8000';
const IDS = process.env.INVOICE_IDS || '1';

const browser = await puppeteer.launch({
  executablePath: EDGE,
  headless: 'new',
  args: ['--no-sandbox', '--disable-gpu', '--window-size=1600,1000'],
  defaultViewport: { width: 1600, height: 1000 },
});

try {
  const page = await browser.newPage();
  page.on('pageerror', (e) => console.log('[pageerror]', e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') console.log('[console.error]', m.text());
  });

  await page.goto(`${BASE}/login`, { waitUntil: 'networkidle0' });
  await page.type('input[name="username"]', 'admin');
  await page.type('input[name="password"]', 'admin123');
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'networkidle0' }),
    page.$eval('form[action="/login"]', (f) => f.submit()),
  ]);
  console.log('[OK] 登录 ->', page.url());

  await page.goto(`${BASE}/invoices`, { waitUntil: 'networkidle0' });
  console.log('[OK] 发票列表');

  await page.goto(`${BASE}/invoices/edit?ids=${IDS}`, { waitUntil: 'networkidle0' });
  await page.waitForFunction(
    () => (document.getElementById('status') || {}).textContent?.includes('已就绪'),
    { timeout: 60000 },
  );
  await page.waitForSelector('#grid canvas', { timeout: 60000 });
  console.log('[OK] 编辑页就绪，画布已渲染');

  const info = await page.evaluate(() => {
    const fields = {};
    document.querySelectorAll('#form-fields [data-key]').forEach((el) => {
      fields[el.dataset.key] = el.value;
    });
    const pkl = {
      e24: InvoiceGrid.getCellValue('PKL1', 'E24'),
      k24: InvoiceGrid.getCellValue('PKL1', 'K24'),
      a33: InvoiceGrid.getCellValue('PKL1', 'A33'),
    };
    const inv = {
      j10: InvoiceGrid.getCellValue('INV1', 'J10'),
      b17: InvoiceGrid.getCellValue('INV1', 'B17'),
      a18: InvoiceGrid.getCellValue('INV1', 'A18'),
      d24: InvoiceGrid.getCellValue('INV1', 'D24'),
    };
    return { fields, pkl, inv, file: document.getElementById('m-file').textContent,
             weight: document.getElementById('m-weight').textContent };
  });
  console.log('[OK] 表单默认值:', JSON.stringify(info.fields));
  console.log('[OK] INV1:', JSON.stringify(info.inv));
  console.log('[OK] PKL1:', JSON.stringify(info.pkl));
  console.log('[OK] 输出信息:', info.file, '|', info.weight);

  const exported = await page.evaluate(async (ids) => {
    const form = {};
    document.querySelectorAll('#form-fields [data-key]').forEach((el) => {
      form[el.dataset.key] = el.value.trim();
    });
    form.date = '2026-09-10';
    form.bill_no = '2609007';
    form.supplier = 'VANCO';
    form.ship_mode = 'by Truck';
    form.place_receipt = 'SHAOXING,CHINA';
    form.port_loading = 'HUU NGHI BORDER, VIET NAM';
    form.container_info = 'SZLU9863221/KAL0586\nPART OF 1X45HQ';
    const r = await fetch(`/api/invoices/export?ids=${ids}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ form }),
    });
    return await r.json();
  }, IDS);
  console.log('[OK] 导出:', exported.ok, exported.filename,
    exported.customs_weight, exported.total_amount);

  if (!exported.ok) throw new Error(exported.error || '导出失败');

  const dl = await page.evaluate(async (url) => {
    const r = await fetch(url);
    const buf = await r.arrayBuffer();
    return { status: r.status, bytes: buf.byteLength };
  }, exported.download);
  console.log('[OK] 下载:', dl.status, dl.bytes, 'bytes');

  await page.goto(`${BASE}/details`, { waitUntil: 'networkidle0' });
  console.log('[OK] 明细页');

  console.log('\n端到端浏览器测试通过');
} finally {
  await browser.close();
}
