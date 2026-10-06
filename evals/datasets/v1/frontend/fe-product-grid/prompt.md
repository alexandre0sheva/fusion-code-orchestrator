Build a product listing for **Hearth & Home**: six products (Blue ceramic mug $14, Linen tea towel $9, Oak serving
board $32, Brass candle holder $21, Wool throw blanket $68, Glass storage jar $12) as a grid of cards. Each card has a
picture (use the tiny inline data-URI image from the starter idea, e.g. `data:image/gif;base64,R0lGODlhAQABAAAAACw=`,
stretched into a square), the product name as a heading, the price and an "Add to cart" button. The grid fits as
many columns as the width allows. Buttons must be distinguishable for screen-reader users.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: product grid</title>
</head>
<body>
  <!-- TODO: build the product grid described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
