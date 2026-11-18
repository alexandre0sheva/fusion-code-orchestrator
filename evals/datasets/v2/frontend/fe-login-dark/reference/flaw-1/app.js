const box = document.getElementById("show");
const field = document.getElementById("pass");

box.addEventListener("change", () => {
  field.type = box.checked ? "text" : "password";
});
