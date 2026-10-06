const table = document.getElementById("people");
const heads = table.querySelectorAll("thead th");

table.querySelectorAll("thead button").forEach((button) => {
  button.addEventListener("click", () => {
    const column = Number(button.dataset.col);
    const th = heads[column];
    const ascending = th.getAttribute("aria-sort") !== "ascending";
    const rows = Array.from(table.tBodies[0].rows);
    rows.sort((a, b) => {
      const x = a.cells[column].textContent.trim();
      const y = b.cells[column].textContent.trim();
      const order = isNaN(x) || isNaN(y) ? x.localeCompare(y) : Number(x) - Number(y);
      return ascending ? order : -order;
    });
    rows.forEach((row) => table.tBodies[0].appendChild(row));
    heads.forEach((head) => head.removeAttribute("aria-sort"));
    th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
  });
});
