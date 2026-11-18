Build a photo gallery page for a travel blog that shows the six photos already in the project
(`photos/p1.svg` to `photos/p6.svg`, 800 by 600). Each photo is a `<figure>` holding an `<img>` with `alt`
text that describes the scene (not the file name), `width` and `height` attributes so the page does not
jump while it loads, `loading="lazy"`, and a `<figcaption>` naming the place. The photos sit in a CSS grid:
one column on a phone, two on a tablet and three on a desktop (media queries, or `auto-fit` with `minmax`).

Under each photo a real `<button>` whose text starts with "View larger" opens a viewer: a `<dialog
id="viewer">` with an accessible name (`aria-labelledby` pointing at its heading, or `aria-label`) that
shows the chosen photo large with its caption, and a "Close" button. A script in `app.js` opens it with
`showModal()` so focus stays inside until it is closed. The page has one `<h1>` and a `<main>` landmark.

The photo files, for your reference (do not change them):

- `p1.svg` harbour at dawn, `p2.svg` old town square, `p3.svg` mountain pass,
- `p4.svg` night market, `p5.svg` lighthouse cliffs, `p6.svg` desert road.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: photo gallery</title>
</head>
<body>
  <!-- TODO: build the photo gallery described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
