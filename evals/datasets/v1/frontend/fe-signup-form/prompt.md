Build a sign-up form for a web app. Fields: email address, password (at least 8 characters) and a checkbox
to accept the terms of service, then a "Create account" button. Use native HTML validation attributes
(types, `required`, `minlength`), attach each field's hint text to the field for screen readers,
add autocomplete hints so password managers help, and keep the form usable on a phone. A small
script may stop submission when the password is too short.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: signup form</title>
</head>
<body>
  <!-- TODO: build the signup form described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
