Build the header of **Harbour Books**, an independent bookshop: the brand on the left and a main
navigation of five links (New arrivals, Fiction, Children, Events, Visit us). On a wide screen the links sit in a
row. On a phone (under 700 px) they collapse behind a "Menu" button that opens and closes the list. The button
must tell assistive technology whether the menu is open and which element it controls. Below the header,
a headline and one sentence.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: responsive nav</title>
</head>
<body>
  <!-- TODO: build the responsive nav described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
