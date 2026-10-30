# ADR 003 — Flat package layout: `frigilunch/` at the repo root

**Status:** Accepted

## Context

The original codebase used a flat `src/` folder as the Python package name. All imports read `from src.models import ...`, `from src.constraints import ...`, and so on. The name `src` is semantically meaningless — every project's code is "source". It also prevented giving the package a name that identifies what it does.

After adding the domain/application/adapters three-layer structure, the package was renamed to `frigilunch` using a Strangler Fig migration. The migration created the new package incrementally alongside `src/`, replaced all import sites, and then deleted `src/` once the bridges were gone.

The question then was: should `frigilunch/` sit directly at the repo root (flat layout), or inside a `src/` wrapper directory (src-layout)?

## Decision

Flat layout: `frigilunch/` sits directly at the repo root, alongside `tests/`, `data/`, and `output/`.

```
frigilunch-avn/
├── frigilunch/       ← the package
├── tests/
├── data/
├── output/
└── create_plan.py
```

The package name `frigilunch` matches the product name. All imports read `from frigilunch.domain.models import Order`, which is self-documenting.

## Consequences

**No installation step required.** Running `python -m frigilunch.cli` or `python create_plan.py` works from the repo root without `pip install -e .`. Python finds `frigilunch/` because the current directory is on `sys.path`.

**Import shadowing risk.** If a `frigilunch` package were installed elsewhere in the environment and the repo root is on `sys.path`, the local folder takes precedence. In practice this is not a concern — `frigilunch` is not published to PyPI and is never installed globally.

**Alternative not taken (src-layout).** Wrapping the package as `src/frigilunch/` would prevent the import-shadowing risk and is what the Python Packaging Authority recommends for distributed packages. It would require `pip install -e .` for a working import. For an internal tool never published to PyPI, this setup friction is not justified. The decision can be revisited if the tool is ever packaged for distribution.
