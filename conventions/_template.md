<!-- Template. Copy to <exact-repo-name>.md (e.g. API-Producer-NODE.md).
     This whole file is injected into context on the first code write of a
     session, so: few rules, all verifiable, all confirmed by a human. A rule
     without a file path does not belong here. -->

## Confirmed patterns

- **Errors**: always via `<Class>` (`path/to/file`). Never a bare `throw`.
- **Dates**: only through `path/to/helper`. Do not add another date library.
- **API responses**: the shape in `path/to/file`.

## Do not do here

- Do not create a new `utils/`: this project uses `<real folder>`.

## Superseded

> ⚠️ **Superseded on YYYY-MM-DD**: <what changed and why>
