# Website development

The site publishes the landing page in `docs/home.mdx` and selected guides from
[`references/`](../references). The public URL is <https://turbra.github.io/traceonaut/>.

## Edit

- Edit [`docs/home.mdx`](docs/home.mdx) for the landing page and
  [`references/`](../references) for guides.
- Add or remove public guides in the `include` list in
  [`docusaurus.config.js`](docusaurus.config.js). Set each guide's route in its
  front matter.
- Edit [`sidebars.js`](sidebars.js) for navigation and
  [`src/css/custom.css`](src/css/custom.css) for styling.
- Replace [`../assets/traceonaut.png`](../assets/traceonaut.png) to update the
  site image and root README banner.

## Validate locally

Requires Node.js 22 or newer and Python 3. From `website/`:

```bash
npm ci --ignore-scripts
npm test
npm run build
python3 check_build.py
npm run serve
```

Open <http://127.0.0.1:3000/traceonaut/>. Use `npm start` for live editing.
The build checks links and anchors; `check_build.py` checks the generated pages,
routes, and assets. Check desktop and mobile navigation before publishing.

## Publish

Set the repository's GitHub Pages source to **GitHub Actions**. The
[`pages` workflow](../.github/workflows/pages.yml) validates relevant pull
requests and deploys site changes from `main`. It can also be run manually on
`main`.

Do not commit generated `build/`, `node_modules/`, session data, or private records.
To roll back a published site change, revert its source change on `main` and let
the workflow republish it.
