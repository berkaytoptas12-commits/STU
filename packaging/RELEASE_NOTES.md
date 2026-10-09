## TechRAG — Windows masaüstü uygulaması

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
