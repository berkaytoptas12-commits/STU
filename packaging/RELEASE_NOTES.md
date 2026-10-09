## TechRAG — Windows masaüstü uygulaması

### 0.3.0 — belge klasörü ekleme ve panel düzeni
- **Belge klasörü ekle**: ana klasörü bir kez seçin; alt klasörlerdeki desteklenen belgeler bulunur. Ana klasörün
  doğrudan altındaki klasörler bucket olur (DDR/DDR5/… → "DDR"), kökteki dosyalar "Genel" bucket'ına gider.
  İndekslemeden önce bucket'lar, belge sayıları ve atlanacak dosyalar gösterilir; tek "İndeksle" ile tümü işlenir.
  Belgeler bulundukları yerden okunur, kaynak klasöre yazılmaz. Tek dosya ekleme korunuyor (aynı adlı dosyalar
  artık üzerine yazılmıyor).
- **Yeniden tara**: yeni ve değişen belgeler işlenir; değişmeyen, taşınan veya kopyalanan belgeler model
  API'lerine yeniden gönderilmez. Ayrıştırma/tablo ayarları değiştiyse gereken yeniden işleme algılanır. Kaynağı
  silinen belgeler bildirilir ve cevaplarda kullanılmaz; erişilemeyen klasör (kopuk ağ paylaşımı) belgeleri silinmiş
  saydırmaz. İlerlemede bucket, dosya, sayı ve hatalar görünür; hız sınırında (429) sunucunun bekleme süresine uyulur.
- **Sol panel**: dar, tamamen daraltılabilir; bucket'lar kapalı başlar, belge sayısı, arama kutusu, kapsam satırı.
  Bucket seçimi aramayı o bucket'la sınırlar (standart/sürüm kontrolleri korunur). Durumlar yeniden açılışta korunur.
- **Sağ panel**: sürüklenebilir ve klavyeyle ayarlanabilir genişlik, gizle/göster, kalıcı genişlik, dar pencerede
  çekmece. PDF görüntüsü ve vurgular her genişlikte hizalı.
- Düzeltme: son cevap gelmeden hemen önce çizilen bir taslak karesi son cevabın üzerine yazılabiliyordu.
- İndeks şeması 4: açılışta yerinde güncellenir; yeniden indeksleme gerekmez.

### 0.2.0 — doğruluk ve PDF'de kanıt vurgulama
- **Atfa tıklayınca kanıt PDF'te vurgulanır**: ifadeyi destekleyen cümle ya da tablo değer hücresi (parametre
  adı, sütun başlığı, birim, dipnot ile) sayfa üzerinde işaretlenir; kanıtlar arasında gezinme, vurguyu
  gizleme ve yakınlaştırma. Döndürülmüş/kırpılmış sayfalarda hizalı; fiziksel sayfa ile basılı sayfa etiketi
  ayrı. Konum bulunamazsa bu açıkça yazılır; orijinal PDF'e hiçbir şey yazılmaz.
- **Doğrulama sertleşti**: denetçi hatası, zaman aşımı, bozuk/eksik/tekrarlı karar artık onay sayılmıyor
  ("doğrulanamadı"); sorudaki sayılar kanıt sayılmıyor (ayrı "kullanıcı girdisi"); atıfsız teknik ifadeler
  başlıktan bağımsız denetleniyor; hesap girdileri kaynağa izleniyor; cevap yalnızca doğrulanmış ifadelerden
  kuruluyor; taslak son cevap gibi gösterilmiyor. Durumlar: destekleniyor / desteklenmiyor / doğrulanamadı / uygulanmadı.
- **Kapsam korunuyor**: adı geçen standart/sürüm yüklü değilse başka sürümle cevap verilmiyor ("yüklü değil");
  etiketsiz dokümanlar kesin eşleşme sayılmıyor; araçlar aynı kapsamı ve kullanıcının seçimini uyguluyor;
  karşılaştırmalarda kanıt standart başına toplanıyor; belirsiz kapsamda kullanıcıya soruluyor.
- **Tablolar hücre düzeyinde doğrulanıyor**: değer, parametre satırı + sütun başlığı (+ grup başlığı) kesişiminde,
  birimi ve koşuluyla bulunmalı. Min/max yer değişimi, yanlış birim veya yanlış satır onaylanmıyor; doğrulanmamış
  satırlar cevapta (araçla da) kullanılmıyor. `max(10 ns, 4 tCK)` gibi ifadeler ve bilinmeyen birimler SI'ye
  çevrilmiyor.
- **VLM tablo birleştirme tablo bazında**: aynı sayfadaki başka bir tablo artık kaybolmuyor.
- **Belge önceliği**: doküman serisi/sürüm/revizyon/tür ayrı; Base ile CEM veya tasarım kılavuzu birbirinin
  revizyonu sayılmıyor; guide/appnote spesifikasyonu geçersiz kılmıyor; errata ilgili belgeye (ve mümkünse
  maddeye) bağlanıyor; eski revizyon soruda adlandırılırsa kullanılıyor; çelişen değerler belirtiliyor.
- **Değerlendirme**: olgu tabanlı şema, cevapsız sorular, hatalı cevap / cevap vermeme oranları, kaynak/sayfa/
  konum doğruluğu ayrı; örnek set "doğrulanmış benchmark değil" olarak işaretli.
- **Eski kütüphaneler**: açılışta şema güncellenir; eski sürümün "doğrulanmış" saydığı tablo satırları güvenilmez
  kabul edilir. Kütüphane → "Konum verisini oluştur" (veya `TechRAG.exe migrate`) yeniden embedding ve VLM çağrısı
  yapmadan konum verisini ekler ve tabloları yeniden doğrular. Gerekirse `ingest --rebuild` önerilir.

### 0.1.2 — kapalı ağda HTTPS sertifika hatası (`SSL: CERTIFICATE_VERIFY_FAILED`)
- Uygulama artık **Windows sertifika deposuna** güveniyor: BT'nin dağıttığı şirket CA'sıyla imzalı model
  sunucuları ek ayar gerektirmeden çalışır (önceden yalnızca Python'un kendi genel CA listesi kullanılıyordu).
- **Ayarlar → Bağlantı güvenliği**: ek CA / sunucu sertifika dosyaları (.pem, .crt, .cer), sistem proxy anahtarı.
- Kendinden imzalı sunucular için **"Bu sunucuya güven…"**: sertifika konusu, geçerliliği ve SHA-256 parmak izi
  gösterilir, onaydan sonra kaydedilir.
- Hata türü Türkçe açıklanır: güvenilmeyen sertifika, ad/IP uyuşmazlığı, süresi dolmuş sertifika, http/https karışıklığı.
- `TechRAG.exe cert <url>`: komut satırından sertifika teşhisi ve güvenme. Servis başına "SSL doğrulamasını kapat" (yalnızca test).
- Model sunucularına istekler varsayılan olarak sistem/şirket proxy'sini kullanmıyor (Ayarlar'dan açılabilir).

### 0.1.1'deki düzeltme
- **0.1.0 açılışta donuyordu** ("yanıt vermiyor" / "çalışmayı durdurdu"). pywebview, JavaScript köprüsünü
  kurarken pencere nesnesinin içindeki .NET formuna inip nesne ağacında sonsuz özyinelemeye giriyordu.
  Düzeltildi; derleme artık CI'da gerçek WebView2 penceresi açılarak (sayfa + JS köprüsü) test ediliyor.
- Başlangıç günlüğü: `%APPDATA%\TechRAG\techrag.log` (her aşama, hatalar, çökme izi).
- Tarayıcıyla indirilen zip'ten çıkarılan DLL'lerin "internetten indirildi" işareti açılışta kaldırılıyor
  (.NET'in bu dosyaları yüklemeyi reddetmesini önler).

Arayüz ve tasarım standartları (ARINC, DDR, PCIe, Ethernet, DisplayPort, USB, RS-422, I²C, genel) için
kaynaklara bağlı, cevabı göstermeden önce doğrulayan asistan. Modeller kurum içi vLLM / SGLang
(OpenAI uyumlu) sunucularından kullanılır; exe içinde model yoktur.

### İndirme
| Dosya | Ne zaman |
|---|---|
| `TechRAG-portable.exe` | Tek dosya, kopyala-çalıştır (ilk açılış birkaç saniye sürer) |
| `TechRAG-win64.zip` | Klasör sürümü, daha hızlı açılır — zip'i açıp `TechRAG\TechRAG.exe` çalıştırın |

### Gereksinimler
- Windows 10/11 x64, **Microsoft Edge WebView2 Runtime** (Windows 11'de hazır; Windows 10'da yoksa
  "Evergreen Standalone Installer" çevrimdışı kurulumu).
- Erişilebilir vLLM uç noktaları: sohbet (tool calling açık), isteğe bağlı ayrı görsel model, embedding, reranker.

### İlk adımlar
1. `TechRAG.exe` → **Ayarlar**: her servis için API adresi (`http://sunucu:8000/v1`), API anahtarı, modeli
   açılır listeden seçin, **Test et**.
2. **Doküman ekle**: koleksiyonu (ör. `ddr`, `pcie`) seçip PDF'leri ekleyin; indeksleme ilerlemesi görünür.
3. Soru sorun. Komut satırı da aynı exe ile çalışır: `TechRAG.exe doctor`, `TechRAG.exe ingest`, `TechRAG.exe docs`.

Kütüphane varsayılan olarak `%USERPROFILE%\TechRAG\library` altındadır; ayarlar `%APPDATA%\TechRAG\settings.json`.
Ayrıntılar: depodaki `README.md`.

### Bilinen sınırlamalar
- Bu sürüm gerçek model sunucularına ve gerçek standart PDF'lerine karşı henüz denenmedi; testler sahte bir
  OpenAI uyumlu sunucu ve üretilmiş PDF'lerle yapıldı. VLM tablo eşiği ve rerank ayarlarının kendi
  dokümanlarınızla `TechRAG.exe eval --retrieval-only` kullanılarak ayarlanması gerekir.
- Exe imzasızdır; Windows SmartScreen ilk açılışta uyarı verebilir ("Daha fazla bilgi → Yine de çalıştır").
