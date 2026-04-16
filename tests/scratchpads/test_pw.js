const { firefox } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await firefox.launch();
  const page = await browser.newPage();
  
  // Login first if needed
  await page.goto('http://127.0.0.1:5002/');
  
  await page.fill('input[name="username"]', 'admin');
  await page.fill('input[name="password"]', 'admin');
  await page.click('button[type="submit"]');
  await page.waitForTimeout(2000);
  
  // Execute the data fetch manually to capture exact data 
  const result = await page.evaluate(async () => {
      try {
          const res = await fetch('/api/persona_stats');
          const data = await res.json();
          
          return {
              len: data.length,
              sample: data[0]
          };
      } catch(e) {
          return {error: e.toString()};
      }
  });
  
  console.log(JSON.stringify(result, null, 2));

  await browser.close();
})();
