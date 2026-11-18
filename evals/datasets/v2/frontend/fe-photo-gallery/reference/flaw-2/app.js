const viewer = document.getElementById('viewer');
const title = document.getElementById('viewer-title');
const image = document.getElementById('viewer-image');

for (const button of document.querySelectorAll('button.view')) {
  button.addEventListener('click', () => {
    const figure = button.closest('figure');
    const photo = figure.querySelector('img');
    image.src = photo.src;
    image.alt = photo.alt;
    title.textContent = figure.querySelector('figcaption').textContent;
    viewer.showModal();
  });
}

document.getElementById('viewer-close').addEventListener('click', () => viewer.close());
