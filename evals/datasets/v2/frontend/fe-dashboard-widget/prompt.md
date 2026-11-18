Build a "Revenue" dashboard widget: a card with a heading, a "Range" selector (last 7, 30 or 90 days), a grid of four
key figures (total revenue, orders, average order, refund rate) as labelled values, and a small inline SVG
sparkline of the trend. The card is a named region. The figures reflow from one column on a phone to a
row on a desktop. The sparkline must make sense to a screen-reader user.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: dashboard widget</title>
</head>
<body>
  <!-- TODO: build the dashboard widget described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
