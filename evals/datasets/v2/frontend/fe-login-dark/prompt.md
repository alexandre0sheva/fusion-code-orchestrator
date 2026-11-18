Build a dark-theme sign-in card, centred on the page: a heading, Username and Password fields, a "Show password"
checkbox that reveals the password (a few lines of script), a Sign in button and a "Forgot your password?" link.
The theme is dark, but every piece of text must still have a contrast ratio of at least 4.5:1 against the
colour behind it, and the keyboard focus ring must be clearly visible on the dark background.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: login dark</title>
</head>
<body>
  <!-- TODO: build the login dark described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
