"""life2vec: olay embedding'leri + gün düzeyi GRU ("şimdiki hâl" vektörü) → sonraki gün.

- `data`: gün dizisi (girdi = gün bitince bilinen, bağlam = t başında bilinen), oturum olayları,
  kişi/sohbet sözlükleri ve tazelik (recency) özellikleri. Sözlük kesim gününe göre kurulur.
- `model`: oturum = sohbet + kişiler + saat/uzunluk embedding'i → gün havuzu → GRU → çok başlı
  çıktı (sayılar: negatif binom; kümeler: kişi/sohbet başına sigmoid).
- `__main__`: walk-forward — her ay başında yalnız < ay verisiyle yeniden eğit, ayı tahmin et.
"""
