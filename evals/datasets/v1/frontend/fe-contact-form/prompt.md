Build a contact form: name, email, a "What is this about?" choice between Sales, Support and Press (radio buttons),
a message box (at most 1000 characters) and a "Send message" button. Use native validation and autocomplete
attributes, group the radio buttons so a screen reader announces the question, and make the form comfortable on a phone.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: contact form</title>
</head>
<body>
  <!-- TODO: build the contact form described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
