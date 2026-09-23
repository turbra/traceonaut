# Build the documentation site

The site follows [scrubctl's documentation design](https://turbra.github.io/scrubctl/):
Docusaurus, a task-oriented sidebar, light/dark themes, and GitHub Actions publishing.
Traceonaut uses a text title instead of scrubctl's logo and does not invent a demo
or release downloads. Build dependencies are separate from the Python collector.
The dependency overrides keep the build tools on patched serializer and UUID
versions; retain them until upstream dependencies include those fixes.

## Edit content

- Edit the existing guides in [`references/`](../references). Docusaurus renders
  those files directly; their front matter sets the website routes.
- Edit [`docs/home.mdx`](docs/home.mdx) for the landing page.
- Change [`sidebars.js`](sidebars.js) for navigation and
  [`src/css/custom.css`](src/css/custom.css) for styling.
- The explicit document list in [`docusaurus.config.js`](docusaurus.config.js)
  controls what gets published. Do not replace it with a repository-wide glob.

## Preview and validate

Use Node.js 22 or newer and Python 3. From this directory:

```bash
npm ci --ignore-scripts
npm test
npm run build
python3 check_build.py
npm run serve
```

Open `http://127.0.0.1:3000/traceonaut/`. The preview listens on loopback only.
For live editing, use `npm start`. The build fails on broken document links or
anchors; `check_build.py` also checks the emitted pages, local assets, and routes.
Check desktop/mobile navigation and light/dark themes before publishing.

## Publish

GitHub Pages must use **GitHub Actions** as its source. The
[`pages` workflow](../.github/workflows/pages.yml) validates relevant pull requests
without deploying them. Changes to the site, public guides, or badge on `main`
build and deploy `website/build/`. It can also be run manually on `main`.
No custom domain, deployment key, or extra secret is required.

The public URL is <https://turbra.github.io/traceonaut/>. Build output, dependencies,
local session data, and private work records are not source files and must not be
committed. The site does not contain live metrics, analytics, or dashboard exports.

To roll back, revert the site change on `main` and let the workflow republish the
previous source. This does not change any collector or Grafana deployment.
