## Sayısal Tasarım Uzmanı — Windows masaüstü uygulaması

### 0.9.0 — tek API anahtarı, Admin, kişisel dosyalar ve kullanım günlüğü
- **Tek API anahtarı:** sohbet (vLLM), görsel model, embedding ve reranker sunucularının hepsi Ayarlar'ın üstündeki
  tek ortak anahtarı kullanır; servis başına anahtar alanları kaldırıldı. Eski sürümde servis başına girilmiş
  anahtarlar ilk kayıtta ortak anahtara taşınır (sohbet modelininki esas alınır; şifreli hâliyle, yeniden girmeniz
  gerekmez). Kayıtlı anahtar yalnızca ayarlı model sunucularına gönderilir, başka bir adrese gönderilmez.
- **Admin sekmesi** (üst çubuk, herkese açık): *Ortak kütüphane* (ortak dosyaların hangi klasörlerden ve
  bucket'lardan geldiği, belge klasörü ekleme, yeniden tarama, kalite raporu, kütüphane açma/yayımlama — eski
  "Kütüphane" penceresi buraya taşındı), *Düşünme ve aramalar*, *Değerlendirme* (eski "Değerlendirme" penceresi)
  ve *Kullanım günlüğü*.
- **Düşünme ve aranan belgeler sohbetten kalktı:** modelin düşünmesi, kapsam satırı, araç çağrıları ve modele
  verilen kaynaklar Admin › Düşünme ve aramalar'da, bu oturumdaki her soru için açılır-kapanır bölümlerde durur.
  Sohbette cevap, atıflar ve doğrulama kutusu kalır.
- **Kaynaklar: Ortak ve Kişisel.** Sol panelin *Kişisel* bölümünde "Dosya seç…" ile bilgisayarınızdan dosya
  seçersiniz; dosya kişisel klasörünüze kopyalanır (varsayılan `%USERPROFILE%\SayisalTasarimUzmani\Kisisel`,
  "değiştir" ile seçilir) ve o klasördeki ayrı bir kişisel indekste indekslenir. Ortak kütüphane salt-okunur açık
  olsa da çalışır; ortak kütüphaneye hiçbir şey yazılmaz. Sorular ortak ve kişisel kütüphanede birlikte aranır;
  kişisel bucket tek başına da seçilebilir.
- **Kullanım günlüğü:** her soru Windows kullanıcı adı, bilgisayar, soru, sonuç, süre ve token sayılarıyla
  (istem / üretilen / düşünme / toplam, model çağrısı sayısı) Admin'de seçilen klasörde kullanıcı başına bir CSV
  dosyasına yazılır (UTF-8, `;` ayraçlı, Excel'de açılır). Cevap metni yazılmaz. Ağ klasörü kullanılabilir; klasöre
  yazılamazsa kayıt yerel klasöre düşer ve Admin uyarır. Admin'de kullanıcı başına soru/token özeti görünür.
- Yeniden indeksleme gerekmez; ortak kütüphane, ayarlar ve anahtarlar aynen kullanılır.
- Doğrulanan: birim/arayüz testleri (anahtar taşıma ve yalnız ayarlı sunuculara gönderilmesi, kişisel indeksin
  ortak kütüphaneyle birlikte aranması ve salt-okunur ortak kütüphanede çalışması, kimliklerin karışmaması,
  günlük satırları ve yerel yedek, Admin sekmeleri, sohbette düşünme/arama gösterilmemesi). Gerçek model
  sunucusunda ve Windows ağ klasöründe denenmedi.

### 0.8.1 — düzeltmede yalnızca hatalı cümleler yeniden yazılır
- Doğrulamada desteklenmeyen bir ifade bulununca model artık cevabın **tamamını** yeniden yazmıyor; yalnızca o
  cümleleri düzeltiyor ya da kaldırıyor. Diğer cümlelere dokunulmuyor. Düzeltme isteği kısa bir JSON cevap
  bekliyor (hatalı ifade başına en fazla ~250 token) ve cevabın kendi konuşmasının devamı olarak gönderiliyor:
  aynı kaynaklar, aynı istem başlangıcı (model sunucusunda önek önbelleği açıksa yeniden kullanılır).
  Örnek ölçümde tam yeniden yazma 2.132 token / 3 dakika sürüyordu; birkaç hatalı cümle için artık birkaç yüz
  token beklenir. Gerçek sunucuda ölçülmedi; "Süre" dökümündeki "düzeltme" satırında görünür.
- Doğrulama gevşetilmedi: düzeltilen cümleler ilk taslak gibi yeniden doğrulanır, denetçiye yalnızca değişen
  cümleler gider (önceden onaylanan cümlelerin kararı korunur). Kullanılamayan bir düzeltme (bozuk JSON, tablo
  satırı yerine düz cümle) yok sayılır; hatalı ifade önceki gibi çıkarılır veya işaretlenir.
- Ayardaki adı: "Doğrulanamayan ifadeleri bir kez düzelttir (yalnızca o cümleler yeniden yazılır)".
- Yeniden indeksleme gerekmez.

### 0.8.0 — durdur, bildirim, süre dökümü ve hızlandırmalar
- **Durdur:** model düşünürken veya cevap yazarken gönder düğmesinin yerinde kırmızı **Durdur** düğmesi ve geçen
  süre görünür. Durdur yalnızca ekranı değil, modele giden isteği de keser; sunucu o cevap için token üretmeyi
  bırakır, kuyruktaki diğer işler beklemez. Yarım kalan taslak doğrulanmadığı için cevap olarak gösterilmez.
  "Yeni sohbet" de süren cevabı aynı şekilde durdurur. Pencereyi kapatmak (gizlemek) cevabı durdurmaz; cevap
  arka planda tamamlanır ve bildirim gelir.
- **Cevap hazır bildirimi:** pencere küçültülmüş, çip simgesinin arkasına gizlenmiş veya başka programların
  arkasındayken cevap ya da netleştirme sorusu hazır olunca Windows bildirimi çıkar (başlık: "Cevap hazır",
  "Cevap hazır: kaynaklarda bulunamadı", "Netleştirme sorusu var" veya hata; metin: sorulan soru). Bildirime
  tıklamak sohbeti açar. Küçültülmüş pencerenin görev çubuğu düğmesi de yanıp söner. Pencere öndeyken bildirim
  çıkmaz; Windows'un odak yardımı / sessiz saatleri geçerlidir. Ayarlar'dan kapatılabilir.
- **Süre dökümü:** her cevabın altındaki "Süre: …" düğmesi sürenin nereye gittiğini gösterir: soru çözümleme,
  arama, şekil sayfaları, cevap yazma (model çağrısı sayısı, düşünme, araç çağrılarının payı), doğrulama,
  düzeltme, kanıt konumu; her aşamanın model çağrıları için üretilen token (düşünme kısmı ayrı), token/sn ve ilk
  token'a kadar bekleme. Sunucu token sayısını bildirmiyorsa metin uzunluğundan tahmin edilir (≈). Döküm günlüğe
  de yazılır. 15 dakika süren bir cevabın hangi aşamada beklediğini buradan görüp ona göre ayar yapabilirsiniz.
- **Hızlandırmalar (doğrulama gevşetilmedi):**
  - Hatalı ifadeleri düzeltme turu artık düşünmeden çalışır (sorunlar zaten tek tek listeleniyor; düzeltilen
    cevap ilk taslak gibi yeniden doğrulanır). Düşünme açıkken bu adım tek başına dakikalar sürebiliyordu.
  - Görsel model açıkken şekil sayfası dökümleri sırayla ve sınırsız yapılıyordu (bulunan parçalar arasında
    şekil sayfası olan her sayfa için ayrı, uzun bir model çağrısı). Artık amaçlandığı gibi en fazla 2 sayfa ve
    aynı anda; dökümler önbelleğe alındığı için sonraki sorularda tekrar yapılmaz.
  - Gelişmiş ayarlara **en fazla araç turu** eklendi (varsayılan 4; her tur ayrı bir model çağrısı).
- Yeniden indeksleme gerekmez. Ayarlar, API anahtarları ve kütüphane aynen kullanılır.
- Doğrulanan: birim/arayüz testleri (durdurmanın modele giden isteği akış ortasında kapattığı, durdurulan
  cevabın doğrulanmadığı ve bildirilmediği, süre dökümü, bildirimin yalnızca pencere görünmezken çıktığı),
  Windows'ta gerçek bildirim ve tıklama testi ve GUI smoke testi. Gerçek model sunucusunda hızın ne kadar arttığı
  ölçülmedi; bu, sürenin sizde hangi aşamada geçtiğine bağlıdır (döküme bakın).

### 0.7.3 — değerlendirmede soru soru cevaplar, yeni örnek set
- **Sorular ve cevaplar:** Değerlendirme penceresinde her raporun altında her soru bir satır: sonuç (doğru,
  yanlış, cevap yok, netleştirme sordu, bulunamadı dedi, hata). Açınca beklenen değer, belge, sayfa ve kontrol
  notu ile sistemin cevabı, atıf yaptığı kaynaklar, doğrulamanın çıkardığı ifadeler (Türkçe gerekçeleriyle) ve
  olgu sonuçları yan yana görünür. "Yalnızca sorunlu olanları göster" süzgeci ve soruyu sohbette (kaynaklar, PDF
  vurgusu, 👍/👎 ile) soran "Sohbette sor" düğmesi var. Önceki raporlar da bu ayrıntılarla açılır (yeni
  çalıştırmalarda beklenen değerler ve kaynaklar rapora yazılır).
- **Yeni örnek set (30 soru):** kütüphanedeki konulara göre — ARINC-429, ARINC-818, COM Express, DDR, LPDDR4,
  DisplayPort, Ethernet, genel tasarım, I2C, PCIe, RS-422, USB. Karışık türler: değer (13), tasarım dilinde soru
  (12; "length matching gerekir mi", "pull-up nasıl seçilir", "I2C hattı ne kadar uzun olabilir"), koşula bağlı
  (2; netleştirme sorması doğru), karşılaştırma (2) ve kütüphanede cevabı olmayan (1). Değerler taslaktır;
  "Dosyayı indir" ile indirip belgenize göre kontrol edin (belge, sayfa, değer), `status: expert_verified` yapın ve
  kendi dosyanız olarak yükleyin.
- **Simge artık kaybolmuyor:** sohbet penceresi açılınca (etkin pencere olunca) simge onun arkasına geçiyordu;
  pencere simgenin köşesini kapladığında (ekranı kapladığında veya dizüstü ölçeğinde büyük açıldığında) simge
  görünmez oluyordu. Simge artık sohbet penceresinin de üzerinde kalır. Simge gönder düğmesinin üstüne gelirse
  soru kutusu ve gönder düğmesi simgenin soluna kayar; pencere taşındığında, boyutlandırıldığında veya simge
  sürüklendiğinde bu kendiliğinden güncellenir.
- PDF okuyucu kütüphanesi (PyMuPDF) 1.x sürümüne sabitlendi: yeni çıkan 2.0, PDF tablolarını farklı okuyor
  (kelime arası boşluklar kayboluyor, boş sütunlar ekleniyor, "7.8" değeri "78" ve "." diye bölünüyor). Önceki
  sürümlerin exe'leri 1.x ile derlenmişti, etkilenmedi.
- Değerlendirme raporunda netleştirme ayrı ölçülür: koşula bağlı sorularda netleştirme sorma oranı ve diğer
  sorularda gereksiz netleştirme oranı (pay/payda ile); koşula bağlı soruda netleştirme sormak "gereksiz cevap
  vermeme" sayılmaz.
- Yeniden indeksleme gerekmez.

### 0.7.2 — tasarım sorularını standardın gereksinimine bağlama
Standartlar gereksinimleri kendi terimleriyle yazar; kullanıcı ise çoğu zaman tasarım diliyle sorar. Örneğin
"length matching gerekir mi?" sorusunun cevabı standartta intra/inter-pair skew sınırı olarak yazar. Önceki
sürümde arama sorudaki kelimeyle yapıldığı için bu bağ kurulamıyor, model "bulunamadı" diyordu. Bu sürümde bağı
soru veya standart bazında bir eşleme tablosuyla değil, LLM kurar:
- **Planlayıcı:** sorulan kavramı standartta hangi gereksinimin (sınır, zamanlama, elektriksel parametre, zorunlu
  prosedür) belirlediğini düşünür ve onlar için de standardın terimleriyle arama sorgusu üretir. Ayrıca
  "standart bunu nasıl yazardı" diye, sayıların yerine X konan bir cümle yazar; bu cümle yalnızca anlamsal
  aramada kullanılır, kanıt olmaz ve gösterilmez. Ek model çağrısı yok.
- **İkinci şans:** cevap modeli "bulunamadı" derse bir kez, ilgili gereksinimi araçlarla standardın kendi
  terimleriyle araması istenir (araç çağırma açıksa; yalnızca bu cevaplarda bir model turu daha). Sohbette
  "Doğrudan bulunamadı — standardın ilgili gereksinimi aranıyor…" görünür. Ayarlar → Gelişmiş'ten kapatılabilir.
- **Cevap kuralı:** gereksinim atıfla verilir, tasarım sonucu "Mühendislik yorumu" bölümünde açıklanır.
  Kaynakta olmayan bir değere dayanan sayısal dönüşüm (ör. ps → mm için kartın mm başına gecikmesi) sayı olarak
  verilmez, neye bağlı olduğu söylenir; o değeri soruda verirseniz hesap sizin varsayımınız olarak yapılır.
- Doğrulama gevşetilmedi: yorum cümleleri dayandıkları kaynağa atıf yapmak zorunda, kaynaksız sayılar çıkarılır.
  Cevabı kütüphanede gerçekten olmayan sorular yine "bulunamadı" ile biter.
- Yeniden indeksleme gerekmez.
- Doğrulanan: sahte model sunucusuyla mekanizma testleri (hatırlatmanın yalnızca bir kez ve yalnızca araç
  çağırma açıkken yapılması, değersiz arama cümlesi, cevapsız soruların "bulunamadı" kalması). Gerçek modelin
  bu bağı ne sıklıkta kurduğu ölçülmedi; Değerlendirme penceresinde birkaç tasarım-dili sorusuyla önce/sonra
  karşılaştırılabilir.

### 0.7.1 — tablo eşleştirme ve okuma yardımı
Gerçek bir kütüphanede VLM tablolarının yalnızca %14'ü, parametre satırlarının %10'u PDF metniyle hücre hücre
eşleşebiliyordu; eşleşmeyenler cevapta kullanılamıyordu ve aramanın bulduğu temiz tablo yerine modele karışık
PDF metni gidiyordu ("rerank %98 ama bulunamadı"). Bu sürüm standartların üç yaygın dizgi biçimini tanıyor ve
eşleşmeyen tabloyu okuma yardımı olarak veriyor. Doğrulama kuralı gevşetilmedi.
- **Alt simgeli semboller:** "t" + alt simge "RFC" (metin katmanında iki kelime) artık "tRFC" olarak bulunuyor.
- **İki satıra taşan parametre adı:** ad satır satır, aynı hücrede alt alta aranıyor; ikinci satır doğru satırı
  belirliyor ("… for 8Gb devices" ile "… for 16Gb devices" karışmıyor).
- **Dipnot işaretleri:** değerden sonra yükseltilmiş işaret ("295" + üst simge "1") metin katmanında "2951" diye
  birleşiyordu; artık ayrılıyor ve değer sayılmıyor. Metinde ve tablolarda "295⁽1⁾", on üzeri gösterimler
  "10^-12" olarak yazılıyor — model ve doğrulama 2951 okumuyor. "2951" artık basılı değer kabul edilmiyor.
- **Okuma yardımı:** arama PDF metniyle eşleşmeyen bir VLM tablosu bulursa model sayfanın PDF metnini ve yanında
  o tabloyu "atıf yapılamaz okuma yardımı" olarak alıyor. Doğrulama, denetçi ve kanıt konumu yalnızca PDF metnine
  bakıyor; yalnızca okuma yardımında geçen değer "doğrulanamadı" sayılıyor.
- **Rerank puanı:** gösterilen puan artık modele verilen metnin puanı (önceden modelin hiç görmediği temiz
  tablonun puanı gösterilebiliyordu).
- **Doğrulama, boş hücreli tablo satırlarını okuyor:** "| tRFC | 295 | - | ns |" gibi satırlarda (Max boş, birim
  ayrı sütunda) 295 değeri ns birimiyle eşleşmiyordu; doğru "tRFC 295 ns" ifadesi kaldırılabiliyordu. Artık "-",
  "n/a" gibi boş hücreler değerle birimi ayırmıyor; "295⁽1⁾" 295 olarak okunuyor. Bu düzeltme yeniden
  indeksleme gerektirmez.
- **Mevcut kütüphane için:** iyileştirmeler yeni indekslenen belgelere uygulanır. Hepsine uygulamak için
  Kütüphane → "yeniden oluştur" işaretli → "Tümünü yeniden tara". VLM cevapları önbellekten gelir (yeniden VLM
  çağrısı yapılmaz), metni değişmeyen parçaların embedding'i korunur. Önce ve sonra kalite raporundaki tablo ve
  parametre satırı oranlarını, Değerlendirme penceresinde aynı soru setinin sonuçlarını karşılaştırabilirsiniz.
- Doğrulanan: sentetik PDF'lerle (alt simge, taşan ad, dipnot işareti, I²C/1st/mm² gibi değişmemesi gerekenler,
  min/max yer değişimi ve yanlış satır gibi olumsuz durumlar) ve sahte model sunucusuyla testler. Gerçek standart
  PDF'lerinde eşleşme oranının ne kadar artacağı ölçülmedi.

### 0.7.0 — doğruluk ve ölçüm iyileştirmeleri
**Doğruluk / güvenilirlik**
- **Denetçi istekleri bağlama sığıyor:** denetçiye gönderilen istekler modelin bağlam uzunluğuna (ayardaki değer
  veya sunucunun bildirdiği `max_model_len`, hangisi küçükse) yanıt payıyla birlikte sığacak şekilde gruplanıyor;
  uzun kaynaklarda ifade başına alıntı kısaltılıyor. Başarısız bir istek ifade ifade, bağlam aşımı veya token
  sınırında kesilen yanıtta bir kez daha kısa alıntılarla yeniden deneniyor. Kesik yanıt hiçbir zaman karar
  sayılmıyor. Teknik denetçi hatası ("doğrulanamadı") denetçinin "desteklenmiyor" kararından ayrı gösteriliyor.
- **VLM sayfa filtresi:** VLM'e yalnızca gerçek bir tablo işareti olan sayfalar gidiyor — satır başında tablo
  başlığı, PDF'in bulduğu çizgili tablo veya en az üç ortak sütunda hizalı satırlar (çizgisiz ve döndürülmüş
  tablolar dahil). Değer ve "min/max" kelimeleriyle dolu düz metin sayfaları artık tek başına seçilmiyor (önceki
  sürümde bu tür sayfalar eşiği rahatça geçiyordu). Mevcut indeks yeniden işlenmez; kural yeni/değişen belgelere
  ve yeniden indekslemeye uygulanır.
- **Yanlış atıf düzeltmesi:** değeri doğru ama atfı yanlış bir ifade — değerlerin hepsi cevapla verilen başka bir
  doğrulanmış kaynakta yazıyorsa — o kaynağın atfı eklenerek yeniden kontrol ediliyor; denetçinin onayı yine
  gerekiyor ve düzeltme "atıf düzeltildi" olarak gösteriliyor. Ayarlardan kapatılabilir.
- **Kaldırma gerekçeleri Türkçe:** doğrulama kutusundaki gerekçeler artık arayüz dilinde ("'1.25 V' değeri atıf
  yapılan kaynaklarda bulunamadı", "değer [4] kaynağında geçiyor (yanlış atıf)", "Bağımsız denetleyici: kaynaklar
  bu ifadeyi desteklemiyor").
- **Tablo / bölüm başvuruları izleniyor:** bulunan pasaj "Table 4-12", "Section 3.2", "clause 5.4.1" veya "§ 4.5"
  diyorsa o tablo/bölüm aynı belgeden ayrı bir kaynak olarak ekleniyor (en fazla 3; kaynak listesinde
  "↪ başvurulan Tablo 4-12"). Bütçe doluysa yer, başvuru yapılmamış en düşük sıralı pasajdan açılıyor.

**Ölçüm**
- **Cevap geri bildirimi:** her cevabın altında 👍 / 👎; 👎 doğru değer, birim, parametre, belge ve sayfa için kısa
  bir form açar. Kayıtlar yalnızca bu bilgisayarda, kullanıcı klasöründe tutulur (kütüphaneye yazılmaz; salt
  okunur paylaşılan kütüphanede de çalışır); kaynak metni, kütüphane yolu, sunucu adresi veya anahtar saklanmaz.
- **Değerlendirme penceresi:** örnek soru seti (artık exe'nin içinde), geri bildirimden çıkan aday maddeler veya
  kendi `.yaml` dosyanız arka planda çalıştırılır (ilerleme, durdurma). Her oran pay/payda ile gösterilir; paydası
  0 olan oran "ölçülmedi" yazar. Raporlar kullanıcı klasörüne kaydedilir ve listelenir; hata metinlerinden sunucu
  adresi ve anahtarlar silinir. Aday maddeler `status: candidate` olarak dışa aktarılır; bir mühendis kontrol
  edene kadar benchmark sayılmaz. Komut satırında `eval` artık varsayılan olarak gömülü seti kullanır.
- **Gelişmiş ayarlar:** model bağlam uzunluğu (sunucunun bildirdiği değerle), denetçi grup boyutu, atıf
  düzeltmesi, izlenecek başvuru sayısı, VLM sayfa eşiği ve sayfa sınırı — aralık denetimiyle.

- Yeniden indeksleme gerekmez. Ayarlar, API anahtarları ve kütüphane aynen kullanılır.
- Doğrulanan: tüm birim/arayüz testleri (sahte model sunucusuyla), self-test. Gerçek model sunucusuyla denetçi
  istek boyutları, gerçek standart PDF'lerinde tablo işareti/başvuru takibi ve atıf düzeltmesinin sıklığı
  ölçülmedi.

### 0.6.1 — tablodaki değerler doğrulamada bulunuyor
- **Düzeltme:** birimi ayrı sütunda yazan tablolardaki değerler ("Min Typ Max Unit", "VDD | 1.14 | 1.2 | 1.26 | V",
  hücreleri ayrı satırda gelen tablolar, "Min (V)" gibi başlıkta birim) ve doğrulanmış parametre satırları
  doğrulamada kaynakta bulunamıyordu; bu yüzden özellikle gerilim değerleri içeren doğru ifadeler "kaynaklarla
  doğrulanamadı" diye cevaptan çıkarılıyordu. Artık değer, satırındaki veya sütun başlığındaki birimle okunuyor.
  "800 mVpp", "5 VDC", "3.3Vaux", "1.2-V" yazımları da tanınıyor; 1200 mV = 1,2 V gibi birim çevrimleri eskisi
  gibi geçerli.
- Doğrulama kuralı gevşetilmedi: değer yine atıf yapılan kaynakta, aynı birimle (veya eşdeğer çevrimiyle) yazıyor
  olmalı ve bağımsız denetçi ifadeyi ayrıca onaylamalı. Kaynakta olmayan değer (ör. tabloda 1.26 V varken 1.25 V),
  başka birimle yazılmış değer veya bölüm numarası ("3.2") değer kanıtı sayılmıyor.
- Yeniden indeksleme gerekmez; düzeltme cevap doğrulamasında.

### 0.6.0 — netleştirici sorular, kalite raporu, mikroçip simgesi, paragraf cevaplar
- **Netleştirici sorular:** cevap, soruda belirtilmeyen bir koşula göre gerçekten değişiyorsa cevap yerine en
  fazla üç kısa soru sorulur — kanıt birden çok standart/sürümden geliyorsa sürüm; doğrulanmış tablo satırları
  hız sınıfına, yoğunluğa veya sıcaklığa göre farklı değer veriyorsa o koşul; elektriksel uyumluluk sorularında
  gerilim seviyeleri, TX/RX rolleri ve sonlandırma. Genel sorular doğrudan cevaplanır; soruda, sohbette veya
  seçili belgelerde zaten olan bilgi yeniden sorulmaz (seçili bucket sürüm seçimi sayılmaz). Seçenekler yalnızca
  kütüphaneden gelir (yüklü standartlar, doğrulanmış satırlardaki koşullar) ve düğme olarak sunulur; serbest metin
  de yazılabilir. Kısa cevap ("DDR4, 2400 MT/s") bekleyen soruyla birleştirilir; yeni soru veya yeni sohbet bekleyen
  soruyu bırakır; "bilmiyorum" koşullara bağlı, kaynaklarla doğrulanan bir açıklama verir (kesin değer veya
  uyumluluk kararı uydurulmaz). "Netleştirme gerekiyor" durumu "kaynakta bulunamadı"dan ayrı gösterilir.
  Netleştirme sorusu sabit bir şablondur; teknik iddia taşımaz, doğrulamayı atlatan bir yol değildir.
- Sohbette daha önce adı geçen standart, standart belirtmeyen değer sorularında korunur ("Peki tREFI?").
  Yeniden üretim (regeneration) artık ilk taslağın kapsam notlarını da görür.
- **Paragraf cevaplar:** önce doğrudan cevap, sonra kısa paragraflar; liste yalnızca adımlar, bağımsız koşullar,
  seçenekler veya kontrol listesi için, tablo karşılaştırma ve çok sayıda sayısal değer için; istenen biçime
  uyulur. Atıflar desteklediği ifadenin yanındadır. Doğrulama gevşetilmedi ve ek model çağrısı eklenmedi: her
  cümle yine ayrı ayrı denetlenir (paragraf sonundaki tek atıf önceki ifadeleri doğrulamaz). Kaynakta yazan
  bilgi ile mühendislik yorumu ayrı bölümlerde kalır.
- **İndeksleme kalite raporu (📋):** her indeksleme çalıştırmasının ve her belgenin son işlenişinin kalıcı kaydı —
  işlenen/atlanan/yeniden kullanılan/kısmen işlenen/başarısız/erişilemeyen belgeler; VLM için seçilen, tablo
  bulunan, incelenip tablo bulunmayan, başarısız, filtre altında kalan ve sayfa sınırı yüzünden incelenmeyen
  sayfalar (VLM kapalıysa açıkça yazar); tablo ve parametre satırı doğrulama sayıları (her oran paydasıyla);
  embedding ve indeks yazma hataları. Belge başına aşama, sayfa, tablo, neden, önerilen işlem ve "Sayfaya git".
  Rapora API anahtarı, sunucu adresi veya erişim anahtarı yazılmaz. Rapor bir cevap doğruluğu ölçüsü değildir.
- **Başarısız işlemleri yeniden dene:** kısmen işlenen belgede yalnızca başarısız VLM sayfaları yeniden denenir
  (başarılı sayfalar önbellekten, değişmeyen parçalar mevcut embedding'iyle); diğer hatalarda belge düzeyinde
  yeniden işlenir ve bu kapsam önceden gösterilir. Başarısız bir deneme mevcut indeksi silmez; daha kötü sonuç
  veren deneme yazılmaz. Belgeler kalite kaydıyla birlikte tek işlemde yazılır. Komut satırı: `quality`, `retry`.
- Yeniden işlenen belgelerde (ayar değişikliği, yeniden deneme) metni değişmeyen parçalar embedding'lerini korur:
  embedding servisine yalnızca yeni/değişen parçalar gönderilir.
- **Mikroçip simgesi:** başlatıcı simgesi küçük boyutta da okunan, saydam köşeli bir mikroçip oldu; uygulamanın
  içinde her monitörün ölçeğinde çizilir (internetten bir şey indirilmez).
- **Simgeyle aç/gizle:** sol tık — gizliyse göster ve öne al; görünür ve öndeyse gizle; küçültülmüş veya başka
  pencerelerin arkasındaysa öne getir. Gizlemek çıkmak değildir (sohbet, seçili bucket'lar, açık PDF, panel
  genişlikleri ve süren indeksleme korunur). Sürükleme aç/kapat tetiklemez; çift tık pencereyi açık bırakır; hızlı
  tıklamalar sırayla işlenir. Sağ tık menüsü, tek örnek ve çoklu monitör davranışı aynı.
- **Kaynak paneli:** PDF kaynaklarında sayfanın altındaki çıkarılmış metin alanı kaldırıldı; boşalan yer sayfa
  görüntüsüne verildi. Vurgu, atıf→konum, yakınlaştırma, sayfa/kanıt gezinme, orijinali açma ve durum mesajları
  aynı. Metin indekste kalır (arama, doğrulama ve kanıt eşleştirme onu kullanır). Sayfa görüntülenemezse bu
  yazılır ve alıntılanan metin incelenebilir; PDF olmayan kaynaklarda belge bilgisi, "Orijinal dosyayı aç" ve
  "Kaynak metnini incele" gösterilir.
- **Yükseltme:** 0.5 kütüphanesi açılışta şema 5'e yükseltilir — yalnızca kalite raporu için iki tablo eklenir;
  belgeler, embedding'ler, tablolar, ayarlar ve API anahtarları değişmez. Eski belgeler için raporda "kalite
  bilgisi mevcut değil" yazar (tahmin yapılmaz).
- Doğrulanan: tüm birim/arayüz testleri (Python 3.11 ve 3.13, Playwright ile arayüz), Windows'ta testler, iki
  derleme, self-test ve gerçek pencerelerle GUI smoke testi (simgenin öndeyken gizlemesi, gizliyken göstermesi,
  küçültülmüş ve örtülmüş pencereyi öne getirmesi dahil). Gerçek model sunucusuyla netleştirme sıklığı, paragraf
  biçimine uyum ve gerçek VLM hataları ölçülmedi; gerçek çoklu monitörlü makinede denenmedi.

### 0.5.0 — yeni ad: Sayısal Tasarım Uzmanı
- Uygulamanın adı **Sayısal Tasarım Uzmanı** oldu (pencere başlığı, simge menüsü, iletiler, belgeler). Dosyalar:
  `SayisalTasarimUzmani-portable.exe` ve `SayisalTasarimUzmani-win64.zip` (içinde `SayisalTasarimUzmani.exe`);
  Görev Yöneticisi ve dosya özellikleri uygulamayı "Sayısal Tasarım Uzmanı" olarak gösterir.
- Önceki sürümden yükseltme: `%APPDATA%\TechRAG` ve `%USERPROFILE%\TechRAG\library` klasörleri varsa aynen
  kullanılır — ayarlar, API anahtarları ve kütüphane yerinde kalır. Yeni kurulumlar `SayisalTasarimUzmani`
  klasörlerini kullanır.
- **Proje genelinde gözden geçirme ve düzeltmeler**:
  - *Doğrulama*: "bulunamadı" cümlesi, selamlama/giriş ifadesi ("Tabii, …", "Kısaca: …"), başlık veya kalın
    satır ve tablo başlığı/kısa tablo satırı içindeki ifadeler artık denetimden kaçamıyor. Sayfa/bölüm numarası
    değer kanıtı sayılmıyor. Hesap etiketi kanıt değil; kaynaktan hiç girdi almayan hesap ve sorudaki değerle
    yapılan hesap belgelenmiş değer gibi onaylanmıyor. Denetçi uzun kaynakta ifadenin dayandığı satırları görüyor.
  - *Kapsam*: sorudaki standart belge seçiminin içinde de uygulanıyor (yalnız DDR4 seçiliyken DDR5 sorusu
    DDR4'ten cevaplanmıyor); bir revizyonun adı yalnızca kendi standardını sabitliyor ("PCIe 4.0 vs PCIe 5.0
    r0.9" artık PCIe 4.0'ı yüklü değil saymıyor); araç çağrıları kapsamı genişletmiyor.
  - *İndeksleme*: aynı taramada değişen bir dosyanın kopyası yanlış içerikle indekslenmiyor; eksi işareti (−40)
    ve "1,200"/"1.200" ayrımı tablo doğrulamasında korunuyor; tek okunamayan dosya klasörü durdurmuyor; kesik VLM
    cevabı "tablo yok" diye önbelleğe yazılmıyor; BOM'lu/Windows-1254 metin, Türkçe "İ" araması ve DOCX bölümleri
    düzeldi; farklı yazılmış revizyonlar taramayı çökertmiyor.
  - *Sunucu ve ayarlar*: salt-okunur kütüphaneye dosya yazılmıyor; ayar kaydetmek komut satırı/ortam ayarlarını
    geri almıyor; kütüphanenin içine/kendisine yayımlama engellendi; BOM'lu veya bozuk settings.json sessizce
    silinmiyor (yedeklenir); çözülemeyen API anahtarı üzerine yazılmıyor, şifrelenemeyen anahtar düz metin
    kaydedilmiyor; kayıtlı anahtar yalnızca kendi sunucusuna gönderiliyor; belge açma yalnız belge türleri için;
    aynı klasör iki kez eklenmiyor; `serve` her zaman erişim anahtarıyla açılıyor.
  - *Masaüstü*: "Uygulamadan çık" süreci gerçekten sonlandırıyor (arka planda süren model çağrıları beklenmiyor);
    iptal edilen oturum kapatma uygulamayı bozmuyor; başka bir programın kapatma isteği uygulamayı düzgün
    kapatıyor; sohbet penceresi simgenin bulunduğu monitöre göre boyutlanıyor.
  - *Arayüz*: panel düzeni, dil ve açık bucket'lar yeniden açılışta korunuyor (her açılışta değişen port bunları
    siliyordu); hata mesajı taslakla örtülmüyor; cevap beklenirken yazılan soru kaybolmuyor; "Yeni sohbet" eski
    cevabı yeni sohbete taşımıyor; diyalogda Enter "İptal" sayılmıyor; atıf düğmeleri doğru ifadeye bağlanıyor;
    başka kütüphane açılınca eski seçim/sohbet temizleniyor; güncellemeden sonra eski arayüz dosyası veya silinmiş
    belgenin sayfa görüntüsü önbellekten gösterilmiyor.

### 0.4.0 — sohbet simgesiyle açılış, Notlar kaldırıldı
- **Yuvarlak sohbet simgesi**: uygulama ekranın sağ alt köşesinde küçük bir simge olarak açılır (normal
  pencerelerin üzerinde, klavye odağını almaz). Tıklayınca sohbet penceresi açılır; sürüklemek simgeyi taşır ve
  sohbeti açmaz. Yeri monitör ve köşe uzaklığıyla hatırlanır; çözünürlük/DPI/görev çubuğu değişince ve monitör
  çıkarılınca görünür alana alınır. Sağ tık: "Sohbeti aç", "Ayarlar", "Uygulamadan çık".
- **Kapat düğmesi pencereyi gizler**: sohbet, bucket seçimi, açık PDF sayfası ve panel genişlikleri korunur,
  indeksleme arka planda sürer, gizliyken arayüz sorgulama yapmaz. İlk seferde kısa bilgi gösterilir.
- **Tek uygulama**: exe ikinci kez açılırsa yeni arka uç başlatılmaz, çalışan uygulamanın penceresi öne gelir.
- **Güvenli çıkış**: "Uygulamadan çık" süren indekslemeyi belge sınırında durdurur, yazılmakta olan kaydı bitirir
  ve sonrasında yazma başlatmaz; kalan belgeler sonraki taramada işlenir. Model önbellek dosyaları artık yarım
  yazılamaz. Windows oturum kapatmayı engellenmez. Uygulama Windows başlangıcına eklenmez.
- **Notlar özelliği kaldırıldı** (Notlar sekmesi, "Not olarak kaydet", dışa aktarma). Daha önce kaydedilmiş notlar
  silinmedi (tarayıcı deposunda olduğu gibi duruyor). PDF dipnotları ve tablo notları etkilenmez.

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
| `SayisalTasarimUzmani-portable.exe` | Tek dosya, kopyala-çalıştır (ilk açılış birkaç saniye sürer) |
| `SayisalTasarimUzmani-win64.zip` | Klasör sürümü, daha hızlı açılır — zip'i açıp `SayisalTasarimUzmani\SayisalTasarimUzmani.exe` çalıştırın |

### Gereksinimler
- Windows 10/11 x64, **Microsoft Edge WebView2 Runtime** (Windows 11'de hazır; Windows 10'da yoksa
  "Evergreen Standalone Installer" çevrimdışı kurulumu).
- Erişilebilir vLLM uç noktaları: sohbet (tool calling açık), isteğe bağlı ayrı görsel model, embedding, reranker.

### İlk adımlar
1. `SayisalTasarimUzmani.exe` açılınca ekranın sağ altında mikroçip simgesi belirir; tıklayınca sohbet penceresi
   açılır. **Ayarlar**: her servis için API adresi (`http://sunucu:8000/v1`), API anahtarı, modeli açılır
   listeden seçin, **Test et**.
2. **Belge klasörü ekle**: belgelerinizin ana klasörünü seçin; alt klasörler bucket olur, belgeler yerinde okunur.
   İndeksleme sonucu 📋 kalite raporunda görünür.
3. Soru sorun; cevap yanlış veya eksikse 👎 ile doğru değeri ve sayfasını ekleyin. **Değerlendirme**
   penceresinden örnek seti veya kendi soru dosyanızı çalıştırabilirsiniz. Komut satırı da aynı exe ile çalışır:
   `SayisalTasarimUzmani.exe doctor`, `... rescan`, `... quality`, `... docs`, `... eval`.

Kütüphane varsayılan olarak `%USERPROFILE%\SayisalTasarimUzmani\library` altındadır; ayarlar
`%APPDATA%\SayisalTasarimUzmani\settings.json` (eski `TechRAG` klasörleri varsa onlar kullanılır).
Ayrıntılar: depodaki `README.md`.

### Bilinen sınırlamalar
- Testler sahte bir OpenAI uyumlu sunucu ve üretilmiş PDF'lerle yapıldı; gerçek model sunucularına ve gerçek
  standart PDF'lerine karşı doğruluk ölçülmedi. VLM tablo eşiği ve rerank ayarlarının kendi dokümanlarınızla
  (Değerlendirme penceresi, "yalnızca erişim" veya `SayisalTasarimUzmani.exe eval --retrieval-only`)
  ayarlanması gerekir. Kaynaklara bağlı doğruluk
  garanti edilmez; doğrulama yanlışların bir kısmını yakalar.
- Exe imzasızdır; Windows SmartScreen ilk açılışta uyarı verebilir ("Daha fazla bilgi → Yine de çalıştır").
