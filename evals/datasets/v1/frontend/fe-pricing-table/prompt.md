Build a pricing page with three plans: Starter ($9/month), Pro ($29/month, the recommended plan, marked with a
"Recommended" badge) and Business ($99/month). Each plan is a card with its name, price, a list of at least three
features and a button-style link to choose it. Links must make sense out of context: a screen reader listing all
links should be able to tell them apart. Cards sit side by side on desktop and stack on phones.

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: pricing table</title>
</head>
<body>
  <!-- TODO: build the pricing table described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
