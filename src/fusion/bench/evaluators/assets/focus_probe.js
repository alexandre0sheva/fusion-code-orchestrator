// Keyboard smoke check: how many elements can be focused, and does focus show? Returns
// {focusable, visible} where ``visible`` counts focused elements that look different when focused.
() => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [tabindex]:not([tabindex="-1"])';
  const els = Array.from(document.querySelectorAll(sel)).filter((e) => {
    const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return !e.disabled && s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  });
  let visible = 0;
  for (const el of els.slice(0, 30)) {
    const before = getComputedStyle(el);
    const b = [before.outlineStyle, before.outlineWidth, before.boxShadow, before.borderColor, before.backgroundColor].join('|');
    el.focus();
    const after = getComputedStyle(el);
    const a = [after.outlineStyle, after.outlineWidth, after.boxShadow, after.borderColor, after.backgroundColor].join('|');
    const hasOutline = after.outlineStyle !== 'none' && parseFloat(after.outlineWidth) > 0;
    if (document.activeElement === el && (hasOutline || a !== b)) visible += 1;
    el.blur();
  }
  return { focusable: els.length, visible: visible, checked: Math.min(els.length, 30) };
}
