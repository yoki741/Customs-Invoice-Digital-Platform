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

// 空运发票 2609006（id=7），模板自带隐藏 Sheet1
await page.goto(BASE + '/invoices/edit?ids=7');
await new Promise(r => setTimeout(r, 3500));
const text = await page.evaluate(() => document.body.innerText.slice(0, 1200));
console.log('--- page text ---');
console.log(text);
await page.screenshot({ path: 'screenshots/edit_air_no_sheet1.png' });
await browser.close();
console.log('截图完成');
