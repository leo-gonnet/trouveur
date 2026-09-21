"""Where an exported partition goes.

Two destinations behind one interface, because the useful thing to do before pointing a nightly
job at someone else's servers is to run the whole thing into a local directory and look at what
it produced. The local destination is not a test double -- it is the rehearsal, and the tests
happen to use it too.

The Hub destination is deliberately thin. It uploads one partition per commit rather than
batching a run into one, which makes a run resumable: a night that dies on day seven leaves days
one through six in the repository, and the next run sees them and carries on. A tidier commit
history would cost that, and the corpus is worth more than the history is.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

CARD = """---
configs:
  - config_name: documents
    data_files: documents/*.parquet
  - config_name: jobs
    data_files: jobs/*.parquet
  - config_name: lifecycle
    data_files: lifecycle/*.parquet
---

# Trouveur corpus archive

Nightly append-only export of a self-hosted job radar's corpus. One Parquet file per stream per
day; a file is written once and never rewritten.

| stream | key | what it is |
|---|---|---|
| `documents/<date>.parquet` | day fetched | raw source payloads, exactly as stored |
| `jobs/<date>.parquet` | day first seen | normalised postings with derived facets |
| `lifecycle/<date>.parquet` | day closed | retirements; replay over `jobs` for state at a date |

`documents` is the only stream that cannot be recomputed: `jobs` is a pure function of it, and
embeddings are a pure function of `jobs`. If space ever has to be reclaimed, drop the derived
streams and keep this one.

Contains no account, credential or match-history data.
"""


class Destination(Protocol):
    def existing(self) -> set[str]: ...

    def put(self, local: Path, path_in_repo: str, message: str) -> None: ...

    def describe(self) -> str: ...


class LocalDestination:
    """A directory on this machine. Rehearsal, and what the tests run against."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def existing(self) -> set[str]:
        if not self.root.exists():
            return set()
        return {
            path.relative_to(self.root).as_posix()
            for path in self.root.rglob("*.parquet")
        }

    def put(self, local: Path, path_in_repo: str, message: str) -> None:
        target = self.root / path_in_repo
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(local.read_bytes())

    def describe(self) -> str:
        return str(self.root)


class HubDestination:
    """A private Hugging Face dataset repository."""

    def __init__(self, repo_id: str, token: str) -> None:
        from huggingface_hub import HfApi

        self.repo_id = repo_id
        self._api = HfApi(token=token)
        self._api.create_repo(
            repo_id, repo_type="dataset", private=True, exist_ok=True
        )

    def existing(self) -> set[str]:
        files = set(self._api.list_repo_files(self.repo_id, repo_type="dataset"))
        if "README.md" not in files:
            self._api.upload_file(
                path_or_fileobj=CARD.encode(),
                path_in_repo="README.md",
                repo_id=self.repo_id,
                repo_type="dataset",
                commit_message="describe the archive",
            )
        return {name for name in files if name.endswith(".parquet")}

    def put(self, local: Path, path_in_repo: str, message: str) -> None:
        self._api.upload_file(
            path_or_fileobj=local,
            path_in_repo=path_in_repo,
            repo_id=self.repo_id,
            repo_type="dataset",
            commit_message=message,
        )

    def describe(self) -> str:
        return f"hf://datasets/{self.repo_id}"


def destination(target: str, token: str | None) -> Destination:
    """Resolve a destination string: `local:<path>`, or a `<owner>/<name>` dataset repo."""
    if target.startswith("local:"):
        return LocalDestination(Path(target.removeprefix("local:")).expanduser())
    if not token:
        raise RuntimeError(
            "No Hugging Face token. Set ARCHIVE_TOKEN to a token with write access to "
            f"{target}, or export to `local:<path>` first to rehearse without one."
        )
    return HubDestination(target, token)
