# FPS1024 Sileo Repo

A personal APT repository for jailbroken iOS and iPadOS, hosted on GitHub Pages.
The published site contains no binary packages: package files live in the GitHub
Releases of their source repositories, and this site publishes the APT indexes
plus a small package browser.

## Add the Source

Open Sileo, go to `Sources`, tap `+`, and add:

```text
https://fps1024.github.io/sileo.github.io/
```

Refresh the source after adding it.

## How It Works

1. Each package repository publishes a `.deb` file as a GitHub Release asset.
2. A GitHub Actions workflow runs every 15 minutes, or immediately when one of
   our own repositories sends a `repository_dispatch` event. It reads each
   configured repository's newest release, reads the package control metadata
   with HTTP Range requests, and writes `Packages`, the compressed variants, and
   `Release`.
3. The `Filename` field in the package index is the absolute GitHub Release URL
   of the `.deb` asset.
4. Sileo downloads the package directly from GitHub, then installs it from its
   local package cache.

Sileo supports absolute `http://` and `https://` download URLs in `Filename`.
Standard APT and clients that concatenate `Filename` with the repository root do
not, so this source is intended for Sileo.

## Adding a Package Repository

Edit `config/repos.json` and add an entry:

```json
{
  "repo": "owner/name",
  "asset": "*.deb",
  "exclude_assets": ["*iphoneos-arm64e*"]
}
```

The repository must be public and its release assets must include a `.deb` file.
This is an APT repository for Sileo: `.ipa` files cannot be installed through it.

The build filters packages by the `Architecture` field in their control
metadata. With `architectures` set to `iphoneos-arm64`, packages built for
`iphoneos-arm64e` are ignored even when both variants are published in the same
release. `exclude_assets` is an optional filename-level guard for releases that
mix variants.

## Updating Packages

Publish a new GitHub Release in the package's source repository. Repositories we
control can send a `repository_dispatch` for an instant update; all other
repositories are polled every 15 minutes. You can also run the workflow manually
from the Actions tab.

The index lists the newest release for each source repository. Keep published
releases (or at least the tags used by the current index) so existing download
URLs remain valid.

## Instant Updates

A repository cannot watch another repository's releases directly. To update this
site immediately when one of your own repositories publishes a release:

1. Create a fine-grained personal access token with access to
   `FPS1024/sileo.github.io` and **Contents: Read and write** permission.
2. Add it to each source repository as the secret `SILEO_DISPATCH_TOKEN`.
3. Add this step to the source repository's release workflow:

```yaml
      - name: Notify Sileo repository
        env:
          GH_TOKEN: ${{ secrets.SILEO_DISPATCH_TOKEN }}
        run: gh api repos/FPS1024/sileo.github.io/dispatches -f event_type=package-release
```

The receiving workflow listens for `package-release`. Third-party repositories
cannot be modified this way, so they are picked up by the 15-minute schedule.

## Local Build

Requirements: Python 3.10+, `curl`, and optionally `dpkg-deb` and `zstd`.

```bash
GH_TOKEN="$(gh auth token)" python3 scripts/build_repo.py --out public
python3 -m http.server --directory public 8000
```

Open `http://localhost:8000/`. The build refuses to publish an empty index.

## Repository Layout

```text
.
├── .github/workflows/repo.yml
├── config/repos.json
├── scripts/build_repo.py
├── index.html
├── styles.css
├── .nojekyll
└── README.md
```

The workflow writes only generated files into `public/` and deploys that
directory. `public/` is not committed.

## Index Formats

The build generates:

- `Packages`
- `Packages.bz2`
- `Packages.gz`
- `Packages.xz`
- `Release` (with MD5 and SHA-256 checksums)

## Package Storage

Binary packages are not stored in this repository. They are published as GitHub
Release assets in their source repositories. The root `Release` file and the
`Packages` indexes are generated at build time inside `public/`.

## Pages URL

```text
https://fps1024.github.io/sileo.github.io/
```
