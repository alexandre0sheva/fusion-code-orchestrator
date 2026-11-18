function show(name) {
  for (const id of ['profile', 'notifications', 'privacy']) {
    document.getElementById(id).style.display = id === name ? 'block' : 'none';
  }
}
