"""persona: Tuna'nın yerine geçen LLM. t gününü t'den önceki hafızadan yazar.

- `memo`: her geçmiş gün için ham dökümden kısa rapor (d < t olduğundan sızıntı yok; önbellek)
- `jobs`: LLM işleri dosyayla bp Haiku ajanlarına (GPU'da LLM yok; Tuna, 2 Kas 2026)
- `predict`: hayal et (Haiku) + hatırla (CPU embedding) → sayılar/kümeler
"""
