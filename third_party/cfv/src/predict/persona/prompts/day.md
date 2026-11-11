Sen bir kişinin günlük hayat kayıtlarından öğrenen bir hafıza modelisin. Aşağıda bu kişinin
geçmiş günlerinin raporları ve {day} ({dow}) günü için ÖNCEDEN bilinenler var. {day} gününün
kayıtları sende YOK; görevin bu günün nasıl geçeceğini tahmin edip, gün bittikten sonra
yazılacak raporu ŞİMDİDEN yazmak.

Kurallar:
- Geçmişteki örüntülere (haftanın günü, süren projeler, açık işler, düzenli görüşülen kişiler) ve
  takvime dayan. Belirsiz şeylerde "muhtemelen" de; takvimde olan ama gerçekleşmeyebilecekler için de.
- Nötr anlatım ("toplantıya gidildi"); duygu/sağlık yorumu yapma.
- `projects` yalnızca şu etiketlerden: {labels}
- `people` için aşağıdaki aday listesindeki adları aynen kullan.

## {day} için bilinenler
Haftanın günü: {dow}
Takvim (önceden girilmiş):
{calendar}

## Son günlerin sayıları (NULL = kayıt yok)
{numbers}

## Geçen hafta aynı gün ({prev_week})
{prev_week_memo}

## Son 7 günün raporları (eskiden yeniye)
{memos}

## Aday kişiler (birebir yazışılanlar; son 28 günde kaç gün)
{people}

Bu günün {k} FARKLI olası raporunu yaz (her biri kendi içinde tutarlı ayrı bir senaryo; olası
alternatifleri dağıt, aynı raporu tekrarlama). Raporların arasına tek başına `===` satırı koy.
Her rapor tam olarak bu biçimde (ilk satır `---`, kod bloğu yok, Türkçe):

---
projects: [etiketler]
tags: [en fazla 6 kısa etiket]
people: [en fazla 8 aday kişi]
---
# {day} — <3-6 kelimelik başlık>

<2-4 cümle özet>

## Neler oldu
- <saat sırasıyla 4-10 madde; kaynak: [takvim] [foto] [wp] [ödev] [not] [mail]>
