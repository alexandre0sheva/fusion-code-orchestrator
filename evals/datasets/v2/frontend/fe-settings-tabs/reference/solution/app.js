const tabs = Array.from(document.querySelectorAll('[role="tab"]'));

function select(tab) {
  for (const other of tabs) {
    const active = other === tab;
    other.setAttribute('aria-selected', String(active));
    other.tabIndex = active ? 0 : -1;
    document.getElementById(other.getAttribute('aria-controls')).hidden = !active;
  }
  tab.focus();
}

for (const tab of tabs) {
  tab.addEventListener('click', () => select(tab));
  tab.addEventListener('keydown', (event) => {
    const index = tabs.indexOf(tab);
    if (event.key === 'ArrowRight') {
      select(tabs[(index + 1) % tabs.length]);
    } else if (event.key === 'ArrowLeft') {
      select(tabs[(index - 1 + tabs.length) % tabs.length]);
    }
  });
}
