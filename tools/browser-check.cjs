/* QA opcional: Node + Playwright, independentes do build standalone. */
const {chromium}=require('playwright');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
(async()=>{
  const browser=await chromium.launch({headless:true,channel:'msedge'});
  const page=await browser.newPage();
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const failed=[];
  page.on('response',response=>{if(response.status()>=400)failed.push(response.url())});
  await page.goto('http://127.0.0.1:8002',{waitUntil:'networkidle'});
  await page.evaluate(()=>document.fonts.ready);
  const folder=path.resolve('output/ui-qa');fs.mkdirSync(folder,{recursive:true});
  const contrastFailures=[];
  for(const theme of ['light','dark']){
    await page.locator('[data-theme-control]').selectOption(theme);
    contrastFailures.push(...await page.evaluate(()=>{
      const luminance=color=>{const numbers=color.match(/[\d.]+/g).slice(0,3).map(Number).map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return numbers[0]*.2126+numbers[1]*.7152+numbers[2]*.0722;};
      const pairs=[['text-default','surface-default'],['text-muted','surface-default'],['text-default','surface-subtle'],['accent-text','accent-subtle'],['danger-default','danger-subtle']];
      const css=getComputedStyle(document.querySelector('.df-ui'));const errors=[];
      const rgb=name=>{const probe=document.createElement('span');probe.style.color=css.getPropertyValue('--df-brand-semantic-'+name);document.body.append(probe);const color=getComputedStyle(probe).color;probe.remove();return color;};
      for(const [a,b] of pairs){const x=luminance(rgb(a)),y=luminance(rgb(b));const ratio=(Math.max(x,y)+.05)/(Math.min(x,y)+.05);if(ratio<4.5)errors.push({theme:document.body.dataset.theme,a,b,ratio});}
      for(const node of document.querySelectorAll('.df-button.df-primary:not(:disabled),.df-button.df-destructive:not(:disabled)')){const c=getComputedStyle(node);const x=luminance(c.color),y=luminance(c.backgroundColor);const ratio=(Math.max(x,y)+.05)/(Math.min(x,y)+.05);if(ratio<4.5)errors.push({theme:document.body.dataset.theme,button:node.className,ratio});}
      return errors;
    }));
    for(const width of [390,768,1440]){
      await page.setViewportSize({width,height:900});
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,`Overflow ${width} ${theme}`);
      await page.screenshot({path:path.join(folder,`${theme}-${width}.png`)});
    }
  }
  await page.setViewportSize({width:1440,height:900});
  for(const query of ['button','input','bar chart','line chart','donut chart','board column']){await page.locator('[data-catalog-search]').fill(query);await page.locator('.df-example:visible').first().scrollIntoViewIfNeeded();await page.screenshot({path:path.join(folder,`families-${query.replaceAll(' ','-')}.png`)});}
  await page.locator('[data-catalog-search]').fill('dialog');
  const trigger=page.locator('[data-dialog-open]:visible').first();
  await trigger.click();
  assert.equal(await page.locator('dialog[open]').count(),1);
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(()=>document.querySelector('dialog[open]').contains(document.activeElement)),true);
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('dialog[open]').count(),0);
  assert.equal(await trigger.evaluate(node=>node===document.activeElement),true);
  await page.locator('[data-catalog-search]').fill('tabs');
  const tab=page.locator('[role=tab]:visible').first();await tab.focus();await page.keyboard.press('ArrowRight');
  assert.equal(await page.evaluate(()=>document.activeElement.getAttribute('aria-selected')),'true');
  await page.locator('[data-catalog-search]').fill('select menu');
  await page.locator('[data-menu]:visible summary').first().click();
  await page.keyboard.press('Escape');
  assert.equal(await page.locator('[data-menu][open]').count(),0);
  await page.locator('[data-catalog-search]').fill('checkbox');
  assert.equal(await page.locator('[data-indeterminate]:visible').first().evaluate(node=>node.indeterminate),true);
  await page.locator('[data-catalog-search]').fill('input');
  const field=page.locator('input[type=text]:visible:not(:disabled)').first();await field.fill('Teste de foco');await field.focus();
  await field.evaluate(node=>{document.dispatchEvent(new CustomEvent('htmx:beforeSwap'));const copy=node.cloneNode(true);node.replaceWith(copy);document.dispatchEvent(new CustomEvent('htmx:afterSwap'));});
  assert.equal(await field.evaluate(node=>node===document.activeElement),true);
  await page.locator('[data-catalog-search]').fill('input');
  const font=await page.locator('.df-ui').evaluate(node=>getComputedStyle(node).fontFamily);assert(font.includes('Geist'));
  assert.equal(await page.evaluate(()=>document.fonts.check('400 16px Geist')),true);
  assert.equal(await page.evaluate(()=>document.fonts.check('600 16px Geist')),true);
  assert.equal(await page.evaluate(()=>document.fonts.check('400 16px "Geist Mono"')),true);
  // Idempotência: carregar novamente não duplica os eventos delegados.
  await page.addScriptTag({url:'http://127.0.0.1:8002/assets/library.js'});
  await page.locator('[data-catalog-search]').fill('chip');
  const chip=page.locator('[data-toggle]:visible').first();
  const old=await chip.getAttribute('aria-pressed');await chip.click();assert.notEqual(await chip.getAttribute('aria-pressed'),old);
  assert.deepEqual(errors,[]);assert.deepEqual(failed,[]);assert.deepEqual(contrastFailures,[]);
  console.log('QA browser: Light/Dark, contraste, 390/768/1440, fontes, diálogos, teclado, menus, htmx e idempotência passaram.');
  await browser.close();
})().catch(error=>{console.error(error);process.exit(1)});
