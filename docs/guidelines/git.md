# Git Best Practices

* **Atomic Commits**: Each commit should be a single logical unit of work. Fix one bug or add one feature per commit.
* **Clear Commit Messages**: Use conventional commits (e.g., `feat:`, `fix:`, `refactor:`, `chore:`) with a clear description.
* **Test Before Commit**: Ensure code is tested and verified before pushing.
* **Clean History**: Squash WIP commits before merging to ensure a readable history.
* **No Internal Metadata**: Never mention internal tool names, model names, or internal infrastructure in commit messages.
* **Keep it Concise**: Keep the first line of the commit message under 50 characters.

## In this repository

- Allowed types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`, with an optional scope.
- CI rejects commit subjects of 50 characters or more, or without a conventional type (lint job).
- Run `scripts/run-tests.sh` before pushing.

Check locally before pushing:

```
git log --format=%s origin/main..HEAD | awk 'length >= 50 || !/^(feat|fix|refactor|perf|test|docs|build|ci|chore)(\([a-z0-9-]+\))?: /'
```

No output means every subject is fine.
