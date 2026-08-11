# agrep-cli

`agrep-cli` is the unscoped npm alias for
[`@mundy/agrep`](https://www.npmjs.com/package/@mundy/agrep).

```sh
npm i -g agrep-cli
agrep "race condition"
```

Both npm names run the exact matching PyPI `agrep` version through
[uv](https://docs.astral.sh/uv/) or pipx. The search engine and bundled Rust
binary come from that Python package; this package is only the portable npm
entry point.

Full docs: https://github.com/dannyisbad/agrep
