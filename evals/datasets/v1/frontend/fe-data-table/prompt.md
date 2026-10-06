Build a sortable data table of five people (name, team, tickets closed). Use real table semantics: a caption, column
headers and row headers, with a button in each column header that sorts by that column and `aria-sort` reporting the
current sort. On a narrow screen the table must scroll sideways inside its own container instead of breaking
the page, and that container must be reachable by keyboard.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: data table</title>
</head>
<body>
  <!-- TODO: build the data table described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
