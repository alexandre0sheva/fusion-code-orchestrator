const dialog = document.getElementById("subscribe");

document.getElementById("open").addEventListener("click", () => {
  dialog.showModal();
});

dialog.addEventListener("close", () => {
  document.getElementById("open").focus();
});
