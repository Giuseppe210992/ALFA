"""Costruisce lo stesso dizionario di core.github_repo.fetch_repo() a partire da una cartella locale."""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import github_repo as g  # noqa: E402


def info_from_dir(path, owner="o", repo="r", description="", ref="main"):
    root = Path(path)
    items = []
    for dp, dn, fn in os.walk(root):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", "__pycache__")]
        for f in fn:
            p = Path(dp, f)
            items.append({"path": p.relative_to(root).as_posix(), "type": "blob", "size": p.stat().st_size})
    tree, trunc = g._prune_tree(items)
    paths = [t["path"] for t in tree]
    rp = next((p for p in paths if "/" not in p and p.lower().startswith("readme")), None)
    readme = (root / rp).read_text(encoding="utf-8", errors="replace")[:g.README_CHARS] if rp else ""
    wanted = [p for p in paths if "/" not in p and re.match(
        r"^(requirements[\w.-]*\.txt|pyproject\.toml|setup\.py|setup\.cfg|environment\.ya?ml|Dockerfile|Makefile)$", p)]
    wanted += g.readme_scripts(readme, paths)
    wanted += [p for p in paths if p.count("/") <= 1 and g.ENTRY_RE.search(p) and p.endswith(".py") and p not in wanted][:6]
    files = {p: (root / p).read_text(encoding="utf-8", errors="replace")[:g.FILE_CHARS] for p in wanted[:12]}
    return {"owner": owner, "repo": repo, "ref": ref, "url": f"https://github.com/{owner}/{repo}",
            "description": description, "stars": 0, "license": "", "size_kb": 0, "language": "Python",
            "archived": False, "tree": paths, "tree_truncated": trunc, "readme": readme, "files": files, "fetched": 0}
