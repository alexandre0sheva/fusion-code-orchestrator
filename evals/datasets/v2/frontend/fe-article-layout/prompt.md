Lay out a long-read article, "A morning on the estuary": a title, a byline with a machine-readable publication date, a
captioned picture, three titled sections of prose and one pull quote. Use semantic elements. The text column
must be comfortable to read (a line length of about 60 to 75 characters and generous line spacing) and the
page must scale from phone to desktop.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: article layout</title>
</head>
<body>
  <!-- TODO: build the article layout described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
