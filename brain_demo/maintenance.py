"""Run imported maintenance code against an explicitly selected private vault."""
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "third_party/second-brain/tools"


def module():
    return runpy.run_path(str(TOOLS / "brain/brain.py"))


def run(vault: Path, arguments: list[str]) -> int:
    if not arguments or arguments[0] not in {"add", "inbox", "consolidate"}:
        raise ValueError("Choose add, inbox, or consolidate for the private vault")
    if any(arg.split("=", 1)[0] in {"--repo", "--commit", "--push", "--remote"} for arg in arguments):
        raise ValueError("The memory command uses --vault and files local entries only")
    vault.mkdir(parents=True, exist_ok=True)
    return module()["main"](["--repo", str(vault), *arguments])
