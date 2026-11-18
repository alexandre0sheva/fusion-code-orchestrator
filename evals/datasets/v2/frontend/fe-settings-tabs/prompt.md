Build an account settings page whose three sections are tabs: Profile, Notifications and Privacy (the
tab labels are exactly these words). Follow the WAI-ARIA tabs pattern: a container with `role="tablist"` and an
accessible name; each tab is a `<button>` with `role="tab"`, an `id`, `aria-controls` naming its panel and
`aria-selected` (`true` on the active tab, `false` on the others); each panel has `role="tabpanel"`, an `id`
and `aria-labelledby` naming its tab, and every panel except the active one has the `hidden` attribute. The
first tab is active when the page loads. A script in `app.js` switches tabs on click and with the Left and
Right arrow keys (`ArrowLeft`, `ArrowRight`), keeping `aria-selected` and `hidden` in step.

The panels hold the settings: Profile has a display name (text field) and a bio (text area); Notifications
has two checkboxes (product news, mentions); Privacy has a select for who can see the profile. Every control
has a label. The active tab is marked by more than colour (an underline or a bold label).

The page starts from this `index.html` (replace it; add `style.css` and `app.js` as you need):

```html
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>TODO: settings tabs</title>
</head>
<body>
  <!-- TODO: build the settings tabs described in the brief -->
</body>
</html>
```

Requirements for every page: valid, semantic HTML with a `lang`, a `<title>` and the viewport meta
tag; one `<h1>` and headings in order; every image has `alt` text and every form control a
label; text with enough contrast (at least 4.5:1); a visible keyboard focus style; a layout that
works from a 390 px phone to a 1440 px desktop; everything offline (no CDN, fonts or images from
other hosts).

A hidden test suite checks the structure, accessibility and responsiveness of what you build.
