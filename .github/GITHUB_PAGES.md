# GitHub Pages Setup

The landing site lives in docs/index.html and is served via GitHub Pages from the docs/ folder.

## Enable GitHub Pages (one-time, in repo Settings)

1. Go to repository Settings -> Pages
2. Under Source, select Deploy from a branch
3. Branch: main  |  Folder: /docs
4. Click Save

GitHub will publish the site at https://<owner>.github.io/<repo>/ within a minute or two.

## No workflow needed

The docs/ folder approach requires no GitHub Actions workflow.
GitHub Pages reads and serves docs/index.html directly on every push to main.

## Custom domain (optional)

1. Add a CNAME file inside docs/ containing just the domain (e.g. henchmen.dev)
2. Configure DNS: CNAME record pointing to <owner>.github.io
3. Set the custom domain in Settings -> Pages -> Custom domain
