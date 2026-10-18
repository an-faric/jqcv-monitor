"""Persistencia del estado en una rama separada, sin modificar la rama de código."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

STATE_NAME = "last_content.txt"
TRACKING_REF = "refs/remotes/origin/jqcv-monitor-state-storage"


class StateStoreError(Exception):
    """La persistencia no se pudo realizar sin sobrescribir trabajo remoto."""


def git(*args: str, allowed: tuple[int, ...] = (0,), **kwargs) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", *args], capture_output=True, text=True, timeout=60, **kwargs
    )
    if result.returncode not in allowed:
        # Las respuestas remotas pueden contener URLs o información de autenticación.
        raise StateStoreError(f"git {args[0]} falló (código {result.returncode}); estado no publicado.")
    return result


def validate_branch(branch: str, default_branch: str) -> str:
    if not branch or branch == default_branch:
        raise StateStoreError("STATE_BRANCH debe ser una rama distinta de la rama de código.")
    ref = f"refs/heads/{branch}"
    git("check-ref-format", ref)
    return ref


def load(branch: str, default_branch: str) -> None:
    ref = validate_branch(branch, default_branch)
    if git("status", "--porcelain", "--", STATE_NAME).stdout.strip():
        raise StateStoreError("Ya hay cambios locales de estado; no se sobrescriben.")
    remote = git("ls-remote", "--exit-code", "--heads", "origin", ref, allowed=(0, 2))
    if remote.returncode == 2:
        # Primer arranque: reutiliza el archivo de la rama de código si lo hubiera.
        print("La rama de estado aún no existe; se conserva la referencia del checkout.")
        return
    git("fetch", "--no-tags", "origin", f"{ref}:{TRACKING_REF}")
    content = git("show", f"{TRACKING_REF}:{STATE_NAME}").stdout
    if not content.strip():
        raise StateStoreError("La rama remota contiene un estado vacío; no se reemplaza.")
    from check_results import _atomic_write

    _atomic_write(Path(STATE_NAME), content)
    print("Estado recuperado de la rama de persistencia.")


def save(branch: str, default_branch: str) -> None:
    ref = validate_branch(branch, default_branch)
    if not Path(STATE_NAME).is_file() or not Path(STATE_NAME).read_text(encoding="utf-8").strip():
        raise StateStoreError("El monitor no ha producido un estado no vacío.")
    parent = git("rev-parse", "--verify", TRACKING_REF, allowed=(0, 128)).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="jqcv-state-index-") as directory:
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        git("read-tree", parent if parent else "--empty", env=environment)
        git("add", "--", STATE_NAME, env=environment)
        tree = git("write-tree", env=environment).stdout.strip()
    if parent and tree == git("rev-parse", f"{parent}^{{tree}}").stdout.strip():
        print("Sin cambios en el estado: no se crea commit ni se hace push.")
        return
    environment = os.environ.copy()
    environment.update({
        "GIT_AUTHOR_NAME": "github-actions[bot]",
        "GIT_COMMITTER_NAME": "github-actions[bot]",
        "GIT_AUTHOR_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
        "GIT_COMMITTER_EMAIL": "41898282+github-actions[bot]@users.noreply.github.com",
    })
    parents = ["-p", parent] if parent else []
    commit = git(
        "commit-tree", tree, *parents,
        "-m", "Actualizar estado de resultados C1 JQCV", env=environment,
    ).stdout.strip()
    # Un push normal rechaza conflictos/cambios concurrentes. No se fuerza historia.
    git("push", "origin", f"{commit}:{ref}")
    print("Estado publicado en su rama; la rama de código no se ha modificado.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("load", "save"))
    parser.add_argument("--branch", default=os.environ.get("STATE_BRANCH", "jqcv-monitor-state"))
    parser.add_argument("--default-branch", default=os.environ.get("DEFAULT_BRANCH", "main"))
    args = parser.parse_args()
    try:
        (load if args.mode == "load" else save)(args.branch, args.default_branch)
    except (StateStoreError, OSError, subprocess.SubprocessError) as exc:
        detail = str(exc) if isinstance(exc, StateStoreError) else type(exc).__name__
        print(f"ERROR de persistencia: {detail}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
