# FPS1024 Sileo Repo

A personal APT repository hosted on GitHub Pages. GitHub Actions scans the package
directory and publishes fresh package indexes after every package update.

## Add the Source

Open Sileo, go to `Sources`, tap `+`, and add:

```text
https://fps1024.github.io/sileo.github.io/
```

Refresh the source after adding it. The repository generates these indexes:

- `Packages`
- `Packages.bz2`
- `Packages.gz`
- `Packages.xz`

## Supported Architecture

The repository currently targets:

```text
iphoneos-arm64
```

Verify a package before publishing it:

```bash
dpkg-deb -f debs/*.deb Package Version Architecture
```

## Publishing a Package

1. Add the new DEB file to `debs/`.
2. Keep older versions and assign a new package version instead of overwriting one.
3. Verify the package metadata.
4. Commit and push the change.

```bash
git add debs/
git commit -m "Add package version"
git push origin main
```

The GitHub Actions workflow then:

1. Scans `debs/` for DEB files.
2. Generates `Packages`, `Packages.bz2`, `Packages.gz`, and `Packages.xz`.
3. Deploys the static repository files to GitHub Pages.

## Repository Layout

```text
.
├── .github/workflows/repo.yml
├── debs/
├── .nojekyll
├── Release
├── index.html
├── styles.css
└── README.md
```

## Pages URL

```text
https://fps1024.github.io/sileo.github.io/
```
