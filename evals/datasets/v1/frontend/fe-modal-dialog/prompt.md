Build a newsletter page with a "Subscribe" button that opens a modal dialog containing an email field, a
Subscribe button and a Cancel button. Use the platform's `<dialog>` element so focus is trapped, Escape closes it and the
background is inert. The dialog must be named for screen readers, the opener must say it opens a dialog, the
email field needs a label, and focus should return to the opener when the dialog closes.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: modal dialog</title>
</head>
<body>
  <!-- TODO: build the modal dialog described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
