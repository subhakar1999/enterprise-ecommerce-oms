// Requires: npm install --prefix .browser-tests playwright
// Start scripts/browser_fixture.py first. This tests simulated payments only.
const { chromium } = require('../.browser-tests/node_modules/playwright');
const assert = require('node:assert/strict');
const path = require('node:path');
const fs = require('node:fs');

(async () => {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH || 'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror',error => errors.push(error.message));
  const artifacts = path.resolve('.local-preview');
  fs.mkdirSync(artifacts,{recursive:true});
  try {
    await page.goto('http://127.0.0.1:8012/admin');
    await page.locator('[name=password]').fill('Browser-test-password-2026');
    await page.getByRole('button',{name:'Sign in',exact:true}).click();
    await page.locator('#dashboard').waitFor({state:'visible'});
    await page.getByRole('button',{name:'Products & stock'}).click();
    await page.getByRole('button',{name:'+ Add product',exact:true}).click();
    const form = page.locator('#product-form');
    await form.locator('[name=name]').fill('Canvas backpack');
    await form.locator('[name=sku]').fill('BAG');
    await form.locator('[name=category]').fill('Travel');
    await form.locator('[name=price]').fill('1599.00');
    await form.locator('[name=initial_stock]').fill('4');
    await form.locator('[name=description]').fill('Take the everyday essentials wherever you go.');
    await form.getByRole('button',{name:'Save product'}).click();
    await page.locator('#product-dialog').waitFor({state:'hidden'});
    await page.getByText('Canvas backpack',{exact:true}).waitFor();
    await page.goto('http://127.0.0.1:8012/');
    await page.locator('.product').nth(3).waitFor();
    await page.screenshot({path:path.join(artifacts,'storefront-desktop.png'),fullPage:true});
    await page.locator('#search').fill('backpack');
    assert.equal(await page.locator('.product').count(),1);
    await page.getByRole('button',{name:'Add to bag +'}).click();
    await page.getByRole('button',{name:'Continue to checkout'}).click();
    const checkout = page.locator('#checkout-form');
    for (const [name,value] of Object.entries({name:'Browser Customer',email:'browser@example.com',phone:'+919876543210',address:'123 Test Street',city:'Bengaluru',region:'Karnataka',postal_code:'560001'})) await checkout.locator(`[name=${name}]`).fill(value);
    await checkout.locator('[name=country]').selectOption('IN');
    await page.route('https://checkout.razorpay.com/v1/checkout.js',route => route.fulfill({contentType:'application/javascript',body:`window.Razorpay=class {constructor(options){this.options=options;}async open(){const response=await fetch('/api/__test/payment',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({order_id:this.options.order_id})});this.options.handler(await response.json());}};`}));
    await page.getByRole('button',{name:'Continue to payment'}).click();
    await page.locator('#order-dialog').waitFor({state:'visible'});
    assert.equal(await page.locator('#order-content h2').textContent(),'Paid');
    assert.match(await page.locator('#order-content').textContent(),/1,649/);
    const downloadEvent = page.waitForEvent('download');
    await page.getByRole('button',{name:'Download invoice'}).click();
    const download=await downloadEvent;
    await download.saveAs(path.join(artifacts,'sample-sales-invoice.html'));
    await page.locator('#order-dialog [data-close]').click();
    await page.locator('#search').fill('');
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(artifacts,'storefront-mobile.png'),fullPage:true});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),true,'Mobile page overflows horizontally');
    await page.setViewportSize({width:1440,height:1000});
    await page.goto('http://127.0.0.1:8012/admin');
    await page.locator('#dashboard').waitFor({state:'visible'});
    await page.getByText('₹1,649.00',{exact:true}).first().waitFor();
    await page.screenshot({path:path.join(artifacts,'admin-desktop.png'),fullPage:true});
    await page.getByRole('button',{name:'Orders',exact:false}).click();
    await page.locator('#order-table [data-order]').first().click();
    const adminDownloadEvent=page.waitForEvent('download');
    await page.locator('#admin-download-invoice').click();
    await (await adminDownloadEvent).saveAs(path.join(artifacts,'admin-sales-invoice.html'));
    await page.locator('#fulfilment-form [name=status]').selectOption('SHIPPED');
    await page.locator('#fulfilment-form [name=tracking_url]').fill('https://example.com/tracking');
    await page.getByRole('button',{name:'Update delivery'}).click();
    await page.locator('#admin-order-dialog').waitFor({state:'hidden'});
    await page.locator('#order-table').getByText('SHIPPED',{exact:true}).waitFor();
    assert.deepEqual(errors,[],'Browser JavaScript errors');
    console.log('Passed: admin login, product creation, search, cart, checkout, simulated payment verification, customer/admin invoices, mobile layout, and fulfilment.');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});

