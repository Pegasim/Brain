# Yazıcı ajan talimatı (cfv-predict-persona)

Sana bu klasördeki bir liste dosyası (h<i>.txt) verilir. Her satırı bir iştir:
`<girdi>.in.md -> <çıktı>.md`, ikisi de BU klasörde.

Her iş için, sırayla:
1. Çıktı dosyası zaten varsa atla (bağlam sıkıştırmasından sonra kaldığın yeri böyle bulursun).
2. Girdiyi Read ile SONUNA KADAR oku (uzunsa offset/limit ile parça parça).
3. Girdideki talimata harfiyen uyarak metni yaz ve Write ile çıktı dosyasına kaydet. Yalnız istenen
   metin: önüne/arkasına açıklama, kod bloğu koyma.
4. Hemen sonraki işe geç. Ara özet yazma, onay sorma.

Kurallar:
- YALNIZ listendeki girdi dosyalarını oku. Bu klasördeki başka dosyaları, üst klasörleri, repoyu,
  veritabanını okuma: tahmin işlerinde gelecek bilgisi sızar ve deney geçersiz olur.
- İnternet yok. Veri kişiseldir: içeriği çıktı dosyası dışında hiçbir yere aktarma.
- Liste bitince "<liste> bitti: <yazılan> iş" yaz ve dur.
