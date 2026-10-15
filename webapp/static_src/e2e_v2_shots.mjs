import puppeteer from 'puppeteer-core';

const EDGE = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const BASE = 'http://127.0.0.1:8000';

const browser = await puppeteer.launch({ executablePath: EDGE, headless: 'new' });
const page = await browser.newPage();
await page.setViewport({ width: 1440, height: 900 });

// 登录页
await page.goto(BASE + '/login');
await new Promise(r => setTimeout(r, 400));
await page.screenshot({ path: 'screenshots/v2_login.png' });
console.log('[OK] 登录页');

await page.evaluate(() => {
  document.querySelector('input[name="username"]').value = 'admin';
  document.querySelector('input[name="password"]').value = 'admin123';
});
await page.$eval('form[action="/login"]', f => f.submit());
await page.waitForNavigation();

// 管理后台
await page.goto(BASE + '/admin');
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: 'screenshots/v2_admin_home.png' });
console.log('[OK] 管理后台');

// 发票列表
await page.goto(BASE + '/invoices');
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: 'screenshots/v2_invoices.png' });
console.log('[OK] 发票列表');

// 明细页
await page.goto(BASE + '/details?check_status=pending');
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: 'screenshots/v2_details.png' });
console.log('[OK] 明细页');

// 审计日志
await page.goto(BASE + '/admin/audit');
await new Promise(r => setTimeout(r, 500));
await page.screenshot({ path: 'screenshots/v2_audit.png' });
console.log('[OK] 审计日志');

await browser.close();
console.log('截图完成');
