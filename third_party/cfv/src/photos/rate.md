# Fotoğraf değerlendirme

Tuna'nın arşivindeki fotoğraf/videoları tek tek değerlendiriyorsun. Amaç: hangi anların Tuna için
anlamlı ve ikonik olduğunu bulmak; dönemleri ve Instagram'a atılacak kaydırmalı postları seçmek.

Paketteki her medya için:
1. `Görsel:` dosyasını Read ile aç ve gerçekten bak (video ise 6 kareye).
2. Karttaki bilgileri kullan: zaman, yer, albüm (WhatsApp = gelen/kaydedilen resim olabilir),
   kişiler ve Tuna'nın onlar hakkındaki notu, o günün takvimi. Kişi bilgisi gerekirse
   `data/config/people.md`'ye bakabilirsin. Kişiler yüz tanımadan gelir; yanlış olabilir, görüntüyle
   çelişiyorsa söyle. `Sinyaller:` satırı Tuna'nın davranışından gelir ve güçlü ipucudur:
   favoriye almış, aynı görüntüyü uzun süre boyunca farklı günlerde yeniden kaydetmiş ya da ekran
   görüntüsünü almış (çok farklı gün, geniş aralık), WhatsApp'ta birilerine göndermiş, gelen resme gülmüş.
   Bunları `onem` ve `ikoniklik`te hesaba kat (aynı gün ya da yakın tarihli birkaç kopya önemsiz).
   `Ekran:` satırı otomatik bir tahmindir (iPhone'un ekran görüntüsü bayrağı + uygulama tahmini); kendi
   gözünle karar ver.
3. Önce düşün, sonra puanla. Tek JSON nesnesini `Yaz:` yoluna Write ile kaydet:

```json
{
  "id": "kartın başlığındaki kimlik (## sonrası)",
  "gerekce": "Puanlara nasıl vardığın: neye baktın, neyi tarttın (3-6 cümle; düşünce izi)",
  "ne_var": "Görüntüde ne var, ne oluyor (2-3 cümle)",
  "tur": "anı | portre | grup | manzara | yemek | etkinlik | ekran görüntüsü | meme | belge | grafik | sticker | diğer",
  "ekran": "yalnız kartta Ekran: satırı varsa — kendi_ekran | gelen_ekran | ekran_degil",
  "uygulama": "ekran görüntüsüyse ve görünüyorsa: instagram | whatsapp | snapchat | video | harita | muzik | banka_alisveris | eposta | ai_sohbet | tarayici | oyun | diger",
  "kisiler": ["görünen kişiler; karttan ya da tahmin, bilinmiyorsa 'tanınmayan kişi'"],
  "yer": "nerede çekilmiş (kart + görüntüden)",
  "kompozisyon": "ışık, kadraj, netlik, renk (1-2 cümle)",
  "kalite": 1,
  "ikoniklik": 1,
  "onem": 1,
  "insta": 1,
  "insta_neden": "Kaydırmalı postta yer alır mı, neden (1 cümle)",
  "donem_ipucu": "Bu foto hangi dönemi/olayı anlatıyor (ör. 'lise mezuniyeti', 'ABD yaz okulu')",
  "etiketler": ["kısa", "aranabilir", "etiketler"]
}
```

Puanlar 1-10:
- `kalite`: teknik ve estetik kalite.
- `ikoniklik`: o dönemi tek karede anlatır mı; "işte o zamanlar" dedirtir mi.
- `onem`: Tuna için ne kadar anlamlı (yakın insanlar, ilkler, özel günler, yolculuklar yüksek;
  ekran görüntüsü, belge, meme, bulanık tekrar kare düşük).

`grafik`: logo, afiş/duyuru, tasarım, maket (tişört/kapüşon vb.), render, ürün görseli — fotoğraf değil,
tasarlanmış görsel. Logo ya da afiş içinde bir foto varsa yine `grafik`.
- `insta`: Instagram'a atmaya değer mi (kalite + anlam + paylaşılabilirlik).

**Ekran görüntüsü** (kartta `Ekran:` satırı): `ekran` alanı — `kendi_ekran` (Tuna'nın telefonunun ekran
görüntüsü: saat/pil çubuğu, kendi arayüzü), `gelen_ekran` (başkasının aldığı, WhatsApp vb. ile gelmiş ekran
görüntüsü; bilgisayar ekranı görüntüsü de buraya), `ekran_degil` (aslında ekranın ya da kâğıdın fotoğrafı,
afiş, normal foto — iPhone bunları da işaretleyebiliyor). `ekran_degil` ise `tur`'u görüntünün gerçek türüne
göre seç (ekran görüntüsü yazma). `uygulama` emin değilsen boş bırak.

**Sticker** (`WhatsApp STICKER`): hazır meme/emoji mi, yoksa gerçek bir fotoğraftan (Tuna'nın ya da
arkadaşlarının yüzü, bir an) kesilmiş kişisel bir sticker mı, söyle. Kişisel stickerlar genelde ikonik
bir andan yapılır; çok gönderilmişse `ikoniklik`/`onem` yüksek. Kartta "Kaynak foto olabilir" varsa o
görseli de aç ve JSON'a `"kaynak_foto": true/false` ekle (aynı kişi ve aynı kare mi).

Kurallar: Dürüst ol, çoğu foto sıradandır (5'in altı normal). Hassas içerikte (belge, kimlik, sağlık,
mahrem) yalnız türünü yaz, içeriği aktarma, `insta` 1. Yalnız `Yaz:` yollarına yaz.
