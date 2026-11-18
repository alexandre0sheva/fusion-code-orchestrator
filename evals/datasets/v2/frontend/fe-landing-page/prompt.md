Build the landing page of **Tidepool**, a tide-forecast service for coastal crews. It needs: a header
with the brand and a main navigation of three links (Features, Pricing, Contact); a hero with a
headline, a one-sentence subheading and a call-to-action link whose text mentions the free trial; a
features section with a heading and three feature cards, each with its own heading and a sentence; and
a footer with a contact line and a copyright line. Calm blues, generous spacing, readable type.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: landing page</title>
</head>
<body>
  <!-- TODO: build the landing page described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
