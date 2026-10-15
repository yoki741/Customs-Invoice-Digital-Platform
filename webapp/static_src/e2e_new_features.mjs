import puppeteer from 'puppeteer-core';

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const BASE = 'http://127.0.0.1:8000';

const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new' });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });

await page.goto(BASE + '/login');
await page.evaluate(() => {
  document.querySelector('input[name="username"]').value = 'admin';
  document.querySelector('input[name="password"]').value = 'admin123';
});
await page.$eval('form[action="/login"]', f => f.submit());
await page.waitForNavigation();

// 审计日志
await page.goto(BASE + '/admin/audit');
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: 'screenshots/admin_audit.png' });
console.log('[OK] 审计日志页');

// 改密码
await page.goto(BASE + '/change-password');
await new Promise(r => setTimeout(r, 300));
await page.screenshot({ path: 'screenshots/change_password.png' });
console.log('[OK] 改密码页');

// 发票列表（日期筛选 + 问题说明）
await page.goto(BASE + '/invoices');
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: 'screenshots/invoices_filters.png' });
console.log('[OK] 列表页（含日期筛选/问题说明）');

// 明细页批量核对
await page.goto(BASE + '/details?check_status=pending');
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: 'screenshots/details_batch.png' });
console.log('[OK] 明细页批量核对');

await browser.close();
console.log('截图完成');
