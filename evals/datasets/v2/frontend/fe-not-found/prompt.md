Build the "page not found" (404) page of a documentation site. It says in its one heading that the page
was not found, explains in a sentence what happened, and helps the reader on: a search form that sends a
GET request to `/search` with a labelled search field and a submit button, a link back to the home page
(`/`), and a short list of at least three popular pages inside a navigation landmark. No script is needed.
Keep it calm and readable: a narrow column, a clear search row that wraps on a phone.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: not found page</title>
</head>
<body>
  <!-- TODO: build the not found page described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
