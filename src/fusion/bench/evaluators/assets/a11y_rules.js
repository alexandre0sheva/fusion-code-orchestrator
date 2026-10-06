// Accessibility rules that run in the page. Used when no axe-core file is present beside this one
// (see docs/BENCHMARKING.md, "Accessibility"). The result has axe-core's shape so the two are
// interchangeable: [{id, impact, help, nodes: [{target, html}]}]. The rules are a deliberately
// small subset of axe's; a pass here is not a certificate of accessibility.
(() => {
  const violations = [];
  const add = (id, impact, help, els) => {
    const nodes = els.slice(0, 10).map((el) => ({
      target: el.tagName.toLowerCase() + (el.id ? '#' + el.id : ''),
      html: el.outerHTML.slice(0, 120),
    }));
    if (nodes.length) violations.push({ id, impact, help, nodes });
  };
  const all = (sel) => Array.from(document.querySelectorAll(sel));
  const visible = (el) => {
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const text = (el) => (el.textContent || '').trim();
  const hasName = (el) =>
    !!(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') ||
       el.getAttribute('title') || text(el) || (el.querySelector('img[alt]:not([alt=""])')));

  add('image-alt', 'critical', 'Images must have alternate text',
      all('img').filter((i) => !i.hasAttribute('alt') && i.getAttribute('role') !== 'presentation'));

  const controls = all('input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]):not([type=image]), select, textarea');
  add('label', 'critical', 'Form elements must have labels', controls.filter((c) => {
    if (c.getAttribute('aria-label') || c.getAttribute('aria-labelledby') || c.getAttribute('title')) return false;
    if (c.id && document.querySelector('label[for="' + CSS.escape(c.id) + '"]')) return false;
    return !c.closest('label');
  }));

  add('button-name', 'critical', 'Buttons must have discernible text',
      all('button, [role=button], input[type=submit], input[type=button]').filter(
        (b) => !(b.value || hasName(b))));
  add('link-name', 'serious', 'Links must have discernible text',
      all('a[href]').filter((a) => !hasName(a)));

  if (!document.documentElement.getAttribute('lang'))
    add('html-has-lang', 'serious', '<html> must have a lang attribute', [document.documentElement]);
  if (!document.title.trim())
    add('document-title', 'serious', 'Documents must have a <title>', [document.documentElement]);
  if (!document.querySelector('main, [role=main]'))
    add('landmark-one-main', 'moderate', 'Page must have one main landmark', [document.body]);
  if (!document.querySelector('h1'))
    add('page-has-heading-one', 'moderate', 'Page must contain a level-one heading', [document.body]);

  const headings = all('h1,h2,h3,h4,h5,h6');
  add('heading-order', 'moderate', 'Heading levels must only increase by one',
      headings.filter((h, i) => i > 0 && Number(h.tagName[1]) > Number(headings[i - 1].tagName[1]) + 1));
  add('tabindex', 'serious', 'Elements must not have a tabindex greater than zero',
      all('[tabindex]').filter((e) => Number(e.getAttribute('tabindex')) > 0));

  const seen = new Set(); const dupes = [];
  all('[id]').forEach((e) => { if (seen.has(e.id)) dupes.push(e); seen.add(e.id); });
  add('duplicate-id', 'minor', 'id attributes must be unique', dupes);

  const rgb = (c) => { const m = c.match(/[\d.]+/g); return m ? m.slice(0, 4).map(Number) : [0, 0, 0, 1]; };
  const lum = ([r, g, b]) => {
    const f = (v) => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
  };
  const backdrop = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const c = rgb(getComputedStyle(e).backgroundColor);
      if ((c[3] === undefined ? 1 : c[3]) > 0.5) return c;
    }
    return [255, 255, 255, 1];
  };
  const low = all('body *').filter((el) => {
    if (!visible(el) || !Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim())) return false;
    const s = getComputedStyle(el);
    const big = parseFloat(s.fontSize) >= 24 || (parseFloat(s.fontSize) >= 18.66 && Number(s.fontWeight) >= 700);
    const a = lum(rgb(s.color)); const b = lum(backdrop(el));
    return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05) < (big ? 3 : 4.5);
  });
  add('color-contrast', 'serious', 'Text must have sufficient color contrast', low);
  return violations;
})()
