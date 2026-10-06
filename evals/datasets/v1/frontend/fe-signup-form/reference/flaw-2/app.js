document.getElementById("signup").addEventListener("submit", (event) => {
  const password = document.getElementById("password");
  if (password.value.length < 8) {
    event.preventDefault();
    password.focus();
  }
});
