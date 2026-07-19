# Releasing irsync

Releases are **tag-driven**. Pushing a `vX.Y.Z` tag to GitHub triggers
`.github/workflows/release.yml`, which builds the sdist + wheel and publishes
them to PyPI via **Trusted Publishing** (OpenID Connect — no API tokens stored
in the repo).

## One-time setup: PyPI Trusted Publisher

Before the first release, register this repo as a trusted publisher on PyPI:

1. Go to <https://pypi.org/manage/account/publishing/> (PyPI account needs 2FA).
2. Add a **pending publisher** with:
   - PyPI Project Name: `irsync`
   - Owner: `killett`
   - Repository name: `irsync`
   - Workflow name: `release.yml`
   - Environment name: `pypi`

## Cutting a release

1. Bump the version in `src/irsync/__init__.py` (`__version__`) — this is the
   single source of truth; hatchling reads it as the package version.
2. Commit and push to `main`; confirm CI (`test.yml`) is green.
3. Tag and push — the tag **must** match `__version__`:

   ```bash
   git tag -a v0.1.0 -m "Release v0.1.0"
   git push origin v0.1.0
   ```

4. Watch the release workflow (`gh run watch`). It publishes to PyPI.
5. Verify: `pip install irsync==0.1.0` in a clean environment.
6. Create the GitHub Release: `gh release create v0.1.0 --generate-notes`.

## Immutability warning

PyPI filenames are **permanent** — once a version is uploaded it can never be
re-uploaded, even after deletion. If a release fails *before* any file uploads,
it is safe to delete the tag, fix, and re-tag the same version. If any file
uploaded, that version is burned — release the next patch version instead.
Never delete-and-reuse a version that shipped a file.

## conda-forge

After the PyPI release, the conda-forge feedstock's autotick bot opens a PR for
each new version; a maintainer just approves it. The initial feedstock is
created by submitting a recipe to
[conda-forge/staged-recipes](https://github.com/conda-forge/staged-recipes).
