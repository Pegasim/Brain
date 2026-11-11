"""LLM işleri dosyayla Haiku ajanlarına (GPU'da LLM yok).

Her iş türünün klasörü kendi içinde (ajanın çalışma klasörü, dar): <gün>.in.md = tam istem,
<gün>.md = çıktı (önbellek: varsa iş yeniden yazılmaz), h<i>.txt = ajan listeleri
("<gün>.in.md -> <gün>.md"), CLAUDE.md = ajan talimatı (prompts/agent.md). Walk-forward istemi
yazan kodda kalır; ajan yalnız bu klasördeki girdiyi okur, MCP ve Bash'i kapalıdır.
"""

from __future__ import annotations

from pathlib import Path

AGENT = (Path(__file__).parent / "prompts" / "agent.md").read_text()


def write(d: Path, day: str, prompt: str) -> str:
    (d / f"{day}.in.md").write_text(prompt)
    return day


def assign(d: Path, days: list[str], n: int) -> list[Path]:
    """Ardışık parçalara böl (her ajan bir dönem); eski listeleri ez."""
    (d / "CLAUDE.md").write_text(AGENT)
    for old in d.glob("h*.txt"):
        old.unlink()
    size = -(-len(days) // n) if days else 1
    lists = []
    for i in range(0, len(days), size):
        lists.append(d / f"h{len(lists) + 1}.txt")
        lists[-1].write_text("".join(f"{x}.in.md -> {x}.md\n" for x in days[i:i + size]))
    return lists
