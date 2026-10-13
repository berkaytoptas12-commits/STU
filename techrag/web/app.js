"use strict";
// Sayısal Tasarım Uzmanı - desktop UI. Offline: no external libraries, no requests except to this app's own /api.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TOKEN = new URLSearchParams(location.search).get("token") || "";
const SERVICES = ["llm", "vision", "embedding", "reranker"];
const MASK = "••••••••";

// ------------------------------------------------------------------ i18n
const I18N = {
  tr: {
    newChat: "Yeni sohbet", library: "Kütüphane", settings: "Ayarlar", sources: "Kaynaklar",
    sourcesHint: "Seçim yapmazsanız tüm kaynaklarda aranır. Soruda adı geçen standart (DDR5, PCIe 4.0, ARINC 429 …) aramayı yalnızca o standarda kesin olarak daraltır.",
    addDocs: "Doküman ekle", collection: "Koleksiyon (klasör)", chooseFiles: "Dosya seç…", reindex: "Yeniden indeksle",
    emptyTitle: "Standartlara sorun", emptyText: "Cevaplar yalnızca yüklü dokümanlardan üretilir; her ifade [n] atfıyla kaynağına bağlanır, sayılar ve iddialar cevaptan önce kaynaklara karşı denetlenir.",
    ask: "Sor", tabSource: "Kaynak", viewerEmpty: "Bir [n] atfına veya bir dokümana tıklayın.",
    answering: "Cevaplama", thinking: "Düşünme modu",
    thinkingControl: "Düşünme kontrolü", tools: "Araç kullanımı (tool calling)", judge: "Bağımsız denetçi (her iddia için ayrı LLM kontrolü)",
    regenerate: "Hatalı iddiaları bir kez yeniden ürettir", failedClaims: "Doğrulanamayan iddialar", strip: "Kaldır", flag: "İşaretle",
    visionEnabled: "VLM ile tablo çıkarımı ve sayfa görüntüleri", cancel: "Vazgeç", save: "Kaydet", close: "Kapat",
    currentLibrary: "Açık kütüphane", libraryHint: "Kütüphane klasörü: sources/<koleksiyon>/*.pdf + index.sqlite + cache/. Bir kişi indeksler, diğerleri kopyasını veya ağ paylaşımındaki yayımlanmış sürümü salt-okunur açar.",
    libraryFolder: "Kütüphane klasörü", openReadOnly: "Salt-okunur aç (paylaşılan kütüphane)", openLibrary: "Aç",
    publishTo: "Yayımlanacak hedef klasör (ör. ağ paylaşımı)", publish: "Yayımla", rebuild: "Tamamen yeniden oluştur",
    reasoning: "Model düşünmesi", copy: "Kopyala", copied: "Kopyalandı ✓",
    placeholder: "Sorunuzu yazın (Türkçe veya İngilizce)… Enter: gönder, Shift+Enter: yeni satır",
    stPlanning: "Soru çözümleniyor ve kaynaklar aranıyor…", stAnswering: "Cevap yazılıyor…", stVerifying: "İfadeler kaynaklara karşı doğrulanıyor…",
    stRegenerating: "{0} ifade doğrulanamadı, düzeltiliyor…", stNoTools: "Sunucu araç çağrısını desteklemiyor; araçsız devam ediliyor.",
    scopeEntity: "kapsam: {0} (soruda geçen standart)", scopeUser: "kapsam: seçili bucket / dokümanlar", scopeDomain: "koleksiyon: {0}", scopeAll: "kapsam: tüm kütüphane",
    thinkingOn: "düşünme açık", searchAs: "arama",
    vSupported: "✓ Cevaptaki {0} ifadenin tamamı kaynaklarla doğrulandı (sayı/atıf kontrolü ve bağımsız denetçi).",
    vCorrected: "✓ Kalan {0} ifade doğrulandı; doğrulanamayan {1} ifade cevaptan çıkarıldı.",
    vWarning: "⚠ {0} ifade doğrulanamadı (⚠ ile işaretli) — kesin bilgi değildir, orijinal sayfadan kontrol edin.",
    vNotFound: "Yüklü dokümanlarda bu sorunun cevabı bulunamadı.",
    vUnverified: "⚠ Doğrulama tamamlanamadı; doğrulanamayan ifadeler cevap olarak gösterilmiyor.",
    vNotChecked: "Doğrulama uygulanmadı (Ayarlar'da kapalı): ifadeler kaynaklarla denetlenmedi, doğrulanmış sayılmaz.",
    vNotCheckedJudge: "Bağımsız denetçi kapalı: ifadeler yalnızca sayı/atıf kontrolünden geçti; anlamsal olarak doğrulanmadı.",
    vClarify: "Netleştirme gerekiyor: cevap, soruda belirtilmeyen bir koşula göre değişiyor. Bu bir “bulunamadı” sonucu değildir; koşulu belirtince cevap kaynaklardan verilir.",
    clarifySend: "Yanıtı gönder", dontKnow: "Bilmiyorum", pendingNote: "Sonraki mesajınız şu soruya yanıt olarak birleştirilecek: “{0}”",
    pendingCancel: "Vazgeç", mergedAs: "soru: “{0}”", carried: "sohbetten: {0}",
    qualityReport: "İndeksleme kalite raporu", qShowAll: "Sorunsuz belgeleri de göster",
    qualityHint: "Bu rapor indekslemenin ne yaptığını anlatır (işlenen/atlanan belgeler, VLM sayfaları, tablo ve satır doğrulaması). Bir cevap doğruluğu ölçüsü değildir; her oranın paydası yanında yazılıdır.",
    qNone: "Henüz kalite kaydı yok.", qUnavailable: "Bu kütüphanede kalite bilgisi mevcut değil (eski sürümle oluşturulmuş, salt okunur indeks).",
    qLastRun: "Son indeksleme", qRuns: "Önceki çalıştırmalar", qKind_sync: "ekleme/eşitleme", qKind_rescan: "yeniden tarama", qKind_folder: "klasör ekleme",
    qKind_upload: "dosya yükleme", qKind_add: "dosya ekleme", qKind_retry: "yeniden deneme", qKind_ingest: "indeksleme",
    qRun_done: "tamamlandı", qRun_done_with_errors: "hatalarla tamamlandı", qRun_running: "sürüyor", qRun_interrupted: "yarıda kesildi", qRun_failed: "hatayla bitti",
    qDocsLine: "Belgeler: {0} işlendi ({1} yeni, {2} güncellendi) · {3} değişmediği için atlandı · {4} yeniden kullanıldı (taşınmış/kopya) · {5} kısmen işlendi · {6} başarısız · {7} erişilemedi · {8} kaynağı yok",
    qVlmOff: "Görsel model (VLM) bu çalıştırmada kapalıydı: tablolar yalnızca PDF metin katmanından okundu, hücre hücre tablo doğrulaması yapılmadı.",
    qVlmLine: "VLM: {0} belgede {1} sayfa incelendi ({2} önbellekten) — {3} sayfada tablo bulundu, {4} sayfa incelendi ama tablo yok, {5} sayfada VLM başarısız. {6} sayfa tablo filtresinin altında kaldı (incelenmedi), {7} sayfa sayfa sınırı yüzünden incelenmedi.",
    qTablesLine: "Tablolar: {0} çıkarıldı · {1} PDF metniyle hücre hücre eşleşti · {2} eşleşmedi (orijinal PDF metni kanıt, VLM sürümü yalnızca aramada).",
    qRowsLine: "Parametre satırları: {0}/{1} doğrulandı{2} — payda: VLM'in çıkardığı satırlar. Doğrulanmamış {3} satır cevaplarda kullanılmaz.",
    qStages: "Aşama hataları: okuma {0} · ayrıştırma {1} · embedding {2} · indeks yazımı {3} · diğer {4}", qEmb: "Embedding: {0} parça gönderildi, {1} değişmemiş parça mevcut vektörünü korudu.",
    qUnreachableFolders: "Erişilemeyen klasörler: {0}",
    qRetryTitle: "Başarısız işlemleri yeniden dene", qRetryNone: "Yeniden denenecek başarısız işlem yok.",
    qRetryScope: "{0} belge yeniden denenecek: {1} belgede yalnızca başarısız VLM sayfaları ({2} sayfa; başarılı sayfalar önbellekten, değişmeyen parçalar mevcut vektörüyle), {3} belge belge düzeyinde yeniden işlenecek.",
    qRetryFolders: "{0} erişilemeyen klasör yeniden taranacak.", qRetryKeep: "Başarısız bir deneme mevcut indeksi silmez; daha kötü sonuç veren deneme yazılmaz.",
    qRetryRunning: "Yeniden deneniyor", qRetryDone: "Yeniden deneme: {0}",
    qRes_ok: "tamamlandı", qRes_partial: "hâlâ kısmi", qRes_failed: "yine başarısız (mevcut indeks korundu)", qRes_kept_existing: "sonuç daha iyi değil, mevcut indeks korundu", qRes_file_missing: "kaynak dosya yok",
    qSt_ok: "sorunsuz", qSt_partial: "kısmen işlendi", qSt_failed: "başarısız", qSt_none: "kalite bilgisi yok",
    qNoRecord: "Kalite bilgisi mevcut değil: bu belge rapor özelliğinden önce indekslendi. Tahmin yapılmaz; yeniden taramada belge değişmişse veya yeniden işlenirse bilgi oluşur.",
    qProcessed: "son işlem: {0}", qNeeds: "yeniden işleme gerekli", qKept: "Önceki indeks korunuyor ve cevaplarda kullanılıyor (son başarılı işlem: {0}).", qNotIndexed: "Bu belge hiç indekslenemedi; cevaplarda kullanılmıyor.",
    qMissingFile: "Kaynak dosya bulunamadı: belge cevaplardan çıkarıldı (indeks korunuyor).",
    qVlmDoc: "VLM: {0} sayfa incelendi — {1} tablolu, {2} tablosuz, {3} başarısız; {4} sayfa filtre altında, {5} sayfa sınır dışı.",
    qVlmDocOff: "VLM kapalıydı: tablolar yalnızca PDF metninden.", qVlmDocNotPdf: "PDF değil: VLM tablo çıkarımı uygulanmaz.",
    qTablesDoc: "Tablolar: {0} ({1} eşleşti) · satırlar {2}/{3} doğrulandı.", qRetried: "Son yeniden deneme: {0}.",
    qGoto: "Sayfaya git", qAction: "Önerilen:",
    qi_vlm_failed: "Sayfa {page}: VLM tablo çıkarımı başarısız ({detail}).", qa_vlm_failed: "Görsel model sunucusunu kontrol edip yeniden deneyin.", qa_vlm_cut: "vision.max_tokens değerini artırıp yeniden deneyin.",
    qi_metadata_failed: "Belge bilgisi (standart/revizyon) modelle okunamadı; dosya adı ve ön sayfalardan çıkarıldı ({detail}).", qa_metadata_failed: "Model sunucusu erişilebilir olduğunda yeniden deneyin.",
    qi_no_text: "Metin çıkarılamadı (taranmış PDF olabilir).", qa_no_text: "Önce OCR uygulayıp belgeyi yeniden ekleyin.",
    qi_vlm_disabled: "VLM kapalıydı: tablolar yalnızca PDF metninden okundu.", qa_vlm_disabled: "Tablo doğrulaması için Ayarlar'dan görsel modeli etkinleştirip yeniden tarayın.",
    qi_vlm_over_limit: "{count} tablo adayı sayfa, belge başına sayfa sınırı nedeniyle incelenmedi.", qa_vlm_over_limit: "vision.max_pages_per_doc değerini artırıp yeniden tarayın.",
    qi_table_not_grounded: "Sayfa {page}, {table}: tablo PDF metniyle hücre hücre eşleştirilemedi ({status}); orijinal PDF metni kanıt olarak kullanılıyor, VLM sürümü yalnızca aramada.", qa_table_not_grounded: "Sayfayı kontrol edin.",
    qi_rows_unverified: "Sayfa {page}, {table}: {count}/{total} parametre satırı doğrulanamadı; bu satırlar cevaplarda kullanılmaz.", qa_rows_unverified: "Sayfayı kontrol edin.",
    qi_table_match_uncertain: "Sayfa {page}, {table}: VLM tablosu PDF'teki tabloyla güvenle eşleştirilemedi; orijinali korundu.", qa_table_match_uncertain: "",
    qi_read_failed: "Dosya okunamadı ({detail}).", qa_read_failed: "Dosyanın açık/kilitli olmadığını ve erişim izni olduğunu kontrol edip yeniden deneyin.",
    qi_parse_failed: "Dosya ayrıştırılamadı ({detail}).", qa_parse_failed: "Dosyanın bozuk olmadığını kontrol edin (yeniden kaydedin/indirin) ve yeniden deneyin.",
    qi_embedding_failed: "Embedding servisi hata verdi ({detail}).", qa_embedding_failed: "Embedding sunucusunu kontrol edip yeniden deneyin.",
    qi_index_write_failed: "İndekse yazılamadı ({detail}).", qa_index_write_failed: "Disk alanını ve yazma iznini kontrol edip yeniden deneyin.",
    qi_other_failed: "İşlenemedi ({detail}).", qa_other_failed: "Yeniden deneyin; sorun sürerse günlüğe bakın.",
    qStage_read: "okuma", qStage_parse: "ayrıştırma", qStage_embedding: "embedding", qStage_index_write: "indeks yazımı", qStage_other: "diğer", qStage_vlm: "VLM", qStage_metadata: "belge bilgisi",
    qOpenReport: "Kalite raporu",
    vRegenerated: "Cevap bir kez düzeltilerek yeniden üretildi.",
    judgeDone: "bağımsız denetçi tamamlandı", judgeFailed: "bağımsız denetçi tamamlanamadı", judgeOff: "bağımsız denetçi kapalı", judgeNone: "denetçi gerekmedi",
    removedList: "Çıkarılan / işaretlenen ifadeler", claimDetails: "İfade ayrıntıları", usesUser: "kullanıcı girdisi içerir", usesCalc: "hesap içerir",
    st_supported: "destekleniyor", st_unsupported: "desteklenmiyor", st_unverified: "doğrulanamadı", st_not_checked: "uygulanmadı",
    prev: "‹ Önceki", next: "Sonraki ›", zoom: "Yakınlaştır", zoomIn: "Yakınlaştır", zoomOut: "Uzaklaştır", openOriginal: "Orijinal dosyayı aç",
    inspectText: "Kaynak metnini incele", noPageImage: "Bu kaynak PDF değil, bu yüzden sayfa görüntüsü yok.",
    pageUnavailable: "Sayfa görüntülenemiyor: kaynak dosya bulunamadı, taşınmış veya okunamıyor.",
    highlight: "Vurgu", prevEv: "‹ Önceki kanıt", nextEv: "Sonraki kanıt ›", printed: "basılı", verifStatus: "Doğrulama", locStatus: "Konum",
    loc_located: "kanıt sayfada işaretlendi", loc_partial: "kanıtın bir kısmı işaretlendi",
    loc_not_located: "Kaynak sayfası bulundu, kesin konum eşleştirilemedi.", loc_ambiguous: "Kaynak sayfası bulundu, kesin konum eşleştirilemedi (metin sayfada birden fazla yerde geçiyor).",
    loc_no_text_layer: "Bu sayfada metin katmanı yok (taranmış olabilir); konum gösterilemiyor.",
    loc_legacy_index: "Bu doküman eski sürümle indekslenmiş; vurgulama için Kütüphane > 'Konum verisini oluştur'.",
    loc_not_pdf: "PDF değil: kanıt aşağıdaki metinde işaretlendi.", loc_none: "Bu kaynak için konum bilgisi yok (cevapta atıf almadı).",
    loc_calc: "Hesap sonucu PDF'de yer almaz; girdileri ve kaynakları:", loc_user: "Kullanıcı girdisi — sorudan alınan değerler, bir doküman değildir.",
    locNote: "Vurgu kanıtın sayfadaki yerini gösterir; ifadenin doğru olup olmadığını 'Doğrulama' durumu belirtir.",
    role_value: "değer", role_label: "parametre", role_header: "sütun başlığı", role_unit: "birim", role_condition: "koşul", role_footnote: "dipnot", role_text: "metin", role_row: "tablo satırı",
    inputs: "Girdiler", noSource: "kaynak yok", draftProgress: "Taslak yazılıyor — henüz doğrulanmadı", showDraft: "Taslağı göster (doğrulanmamış)",
    legacyNotice: "{0} doküman eski indeks biçiminde: PDF vurgulama ve hücre düzeyi tablo doğrulaması için geçiş gerekir.",
    migrate: "Konum verisini oluştur (hızlı geçiş)", legacyTag: "eski indeks", userKind: "kullanıcı girdisi",
    scopeMissing: "yüklü değil: {0}",
    toggleLeft: "Kaynak panelini aç/kapat", collapseLeft: "Paneli daralt", toggleRight: "Kaynak paneli", hideRight: "Kaynak panelini gizle",
    splitter: "Sürükleyerek veya ← → tuşlarıyla kaynak panelinin genişliğini ayarlayın",
    addFolder: "Belge klasörü ekle", rescan: "Yeniden tara", filterPh: "Belge veya bucket ara…",
    scopeLine: "Kapsam: {0}", scopeLibrary: "tüm kütüphane", scopeDocsN: "{0} belge", clearScope: "Temizle",
    generalBucket: "Genel (ana klasör)", docsShort: "belge", missingNotice: "{0} belgenin kaynak dosyası bulunamadı; cevaplarda kullanılmıyor.",
    showMissing: "Listele", purgeMissing: "İndeksten kaldır", confirmPurge: "Kaynağı bulunamayan {0} belge indeksten kaldırılsın mı? (Dosyalarınıza dokunulmaz.)",
    missingTag: "kaynak yok", jobLog: "Ayrıntılı günlük", indexNow: "İndeksle", reindexAll: "Tümünü yeniden tara",
    folderPrompt: "Belge klasörünün yolu:", folderTitle: "Belge klasörü: {0}", rescanTitle: "Yeniden tara",
    folderIntro: "Belgeler bulundukları yerden okunur; klasöre hiçbir şey yazılmaz, dosyalar taşınmaz veya yeniden adlandırılmaz.",
    colBucket: "Bucket", colDocs: "Belge", colFolders: "Alt klasörler", totalDocs: "Toplam {0} desteklenen belge",
    unsupportedN: "{0} desteklenmeyen dosya atlanacak", emptyDirsN: "{0} boş klasör", linksN: "{0} bağlantı/junction izlenmeyecek",
    unreadableN: "{0} okunamayan öğe", notReachable: "Klasöre erişilemiyor: {0}", nothingFound: "Desteklenen belge bulunamadı.",
    planTitle: "Yapılacaklar", act_new: "yeni", act_changed: "değişmiş", act_settings: "ayar değişikliği nedeniyle yeniden", act_moved: "taşınmış (yeniden kullanılır)",
    act_duplicate: "kopya (yeniden kullanılır)", act_touched: "aynı içerik", act_restored: "geri gelen", act_unchanged: "değişmemiş",
    act_missing: "kaynağı silinmiş", act_unreachable: "erişilemeyen (dokunulmaz)", apiNote: "Yalnızca yeni/değişmiş belgeler model API'lerine gönderilir.",
    jobScan: "Taranıyor…", jobIndex: "İndeksleniyor", jobDoneTitle: "İndeksleme bitti", jobFailedTitle: "İndeksleme hatalarla bitti",
    jobErrors: "{0} hata", reportLine: "{0} yeni, {1} güncellendi, {2} değişmemiş, {3} yeniden kullanıldı, {4} kaynağı yok, {5} erişilemedi, {6} hata",
    relPath: "Konum",

    page: "Sayfa", pages: "sayfa", chunks: "parça", superseded: "eski revizyon", toc: "İçindekiler", noToc: "Bu dokümanda yer imi yok.",
    readOnly: "salt-okunur", docs: "doküman", params: "parametre", modelsLoad: "Modelleri getir", test: "Test et",
    sameAsChat: "Sohbet modeliyle aynı", baseUrl: "API adresi (…/v1)", apiKey: "API anahtarı", model: "Model",
    svc_llm: "Sohbet modeli (LLM)", svc_vision: "Görsel model (VLM)", svc_embedding: "Embedding", svc_reranker: "Reranker",
    enabled: "etkin", notOnServer: "(sunucuda yok)", savedSettings: "Ayarlar kaydedildi.", noModels: "Model listesi alınamadı",
    errorPrefix: "Hata", needToken: "Bu sunucu bir API anahtarı istiyor:",
    newChatStarted: "Yeni sohbet başladı.", calc: "hesap", parameter: "parametre", table: "tablo", pageKind: "sayfa", unverified: "doğrulanmamış çıkarım",
    tlsTitle: "Bağlantı güvenliği (HTTPS sertifikaları)",
    tlsSystem: "Windows sertifika deposuna güven (BT'nin dağıttığı şirket CA sertifikaları)",
    tlsProxy: "Sistem proxy ayarlarını kullan (model sunucuları yerel ağdaysa kapalı bırakın)",
    tlsCa: "Ek CA / sunucu sertifika dosyaları (.pem, .crt, .cer — birden fazlaysa ; ile ayırın)",
    tlsHint: "Bu bölümdeki değişiklikler hemen uygulanır. 'Bu sunucuya güven' ile kaydedilen sertifikalar da bu listeye eklenir.",
    tlsApplied: "TLS ayarları uygulandı.", insecure: "SSL doğrulamasını kapat (güvensiz — yalnızca test için)",
    trustServer: "Bu sunucuya güven…", trust: "Güven", certTitle: "Sunucunun sunduğu sertifika",
    certSubject: "Konu", certIssuer: "Veren", certValid: "Geçerlilik", certFp: "SHA-256 parmak izi", certNames: "Adlar",
    certSelf: "kendinden imzalı", certCheck: "Bu parmak izini sunucu yöneticisinden doğrulayın; aynıysa güvenin.",
    certAlreadyOk: "Mevcut ayarlarla bu sertifika zaten doğrulanıyor.", trusted: "Sertifika kaydedildi ve güvenilenlere eklendi: {0}",
    details: "Ayrıntı",
    err_tls_untrusted: "Sunucu sertifikasına güvenilmiyor (kendinden imzalı ya da bu bilgisayarın tanımadığı bir şirket CA'sı). Şirket kök CA'sını Windows'a yükletin, CA dosyasını aşağıdaki TLS bölümüne ekleyin ya da parmak izini kontrol edip 'Bu sunucuya güven'i kullanın.",
    err_tls_hostname: "Sertifika başka bir ad/IP için verilmiş. API adresinde sertifikadaki adı kullanın (ör. IP yerine https://vllm.sirket.local:8000/v1).",
    err_tls_expired: "Sunucu sertifikasının süresi dolmuş ya da henüz geçerli değil (bu bilgisayarın saatini de kontrol edin).",
    err_tls_protocol: "TLS el sıkışması başarısız: sunucu büyük olasılıkla düz HTTP konuşuyor. https:// yerine http:// deneyin.",
    err_tls_other: "TLS bağlantısı kurulamadı.", err_connect: "Bağlanılamadı: adresi/portu, sunucunun çalıştığını ve güvenlik duvarını kontrol edin.",
    err_model: "Seçilen model bu sunucuda yok.",
    published: "Yayımlandı: {0}", opened: "Kütüphane açıldı.", pickFolderPrompt: "Klasör yolu:",
    ex: ["DDR4 ile DDR5 arasında VDD ve burst length farkları nelerdir?", "PCIe 5.0 LTSSM Polling alt durumları ve geçiş koşulları nelerdir?",
         "ARINC 429 kelimesinde SSM bitleri hangi bitlerdir ve BNR verisi için anlamları nedir?", "I2C Fast-mode Plus için maksimum yükselme süresi ve bus kapasitansı nedir?"],
  },
  en: {
    newChat: "New chat", library: "Library", settings: "Settings", sources: "Sources",
    sourcesHint: "With nothing selected, all sources are searched. A standard named in the question (DDR5, PCIe 4.0, ARINC 429 …) hard-restricts the search to that standard.",
    addDocs: "Add documents", collection: "Collection (folder)", chooseFiles: "Choose files…", reindex: "Re-index",
    emptyTitle: "Ask the standards", emptyText: "Answers come only from the loaded documents; every statement is tied to its source with an [n] citation, and numbers and claims are checked against the sources before you see the answer.",
    ask: "Ask", tabSource: "Source", viewerEmpty: "Click an [n] citation or a document.",
    answering: "Answering", thinking: "Thinking mode",
    thinkingControl: "Thinking control", tools: "Tool calling", judge: "Independent judge (separate LLM check per claim)",
    regenerate: "Regenerate failing claims once", failedClaims: "Unverifiable claims", strip: "Remove", flag: "Flag",
    visionEnabled: "VLM table extraction and page images", cancel: "Cancel", save: "Save", close: "Close",
    currentLibrary: "Open library", libraryHint: "A library folder holds sources/<collection>/*.pdf + index.sqlite + cache/. One person indexes; others open a copy or a published version on a network share read-only.",
    libraryFolder: "Library folder", openReadOnly: "Open read-only (shared library)", openLibrary: "Open",
    publishTo: "Publish to folder (e.g. a network share)", publish: "Publish", rebuild: "Full rebuild",
    reasoning: "Model reasoning", copy: "Copy", copied: "Copied ✓",
    placeholder: "Type your question (English or Turkish)… Enter: send, Shift+Enter: new line",
    stPlanning: "Analysing the question and searching…", stAnswering: "Writing the answer…", stVerifying: "Checking statements against the sources…",
    stRegenerating: "{0} statement(s) failed verification, correcting…", stNoTools: "The server does not support tool calls; continuing without tools.",
    scopeEntity: "scope: {0} (named in the question)", scopeUser: "scope: selected buckets / documents", scopeDomain: "collection: {0}", scopeAll: "scope: whole library",
    thinkingOn: "thinking on", searchAs: "search",
    vSupported: "✓ All {0} statements of the answer were verified against the sources (number/citation check and independent judge).",
    vCorrected: "✓ The remaining {0} statements were verified; {1} unverifiable statement(s) were removed.",
    vWarning: "⚠ {0} statement(s) could not be verified (marked ⚠) — not established facts; check the original page.",
    vNotFound: "The loaded documents do not answer this question.",
    vUnverified: "⚠ Verification could not be completed; unverified statements are not shown as the answer.",
    vNotChecked: "Verification not applied (switched off in Settings): statements were not checked against the sources.",
    vNotCheckedJudge: "Independent judge switched off: statements passed only the number/citation check and are not semantically verified.",
    vClarify: "Clarification needed: the answer depends on a condition the question does not state. This is not a “not found” result; once you specify it, the answer comes from the sources.",
    clarifySend: "Send answer", dontKnow: "I don't know", pendingNote: "Your next message will be merged as a reply to: “{0}”",
    pendingCancel: "Cancel", mergedAs: "question: “{0}”", carried: "from the conversation: {0}",
    qualityReport: "Indexing quality report", qShowAll: "Also show documents without problems",
    qualityHint: "This report describes what indexing did (processed/skipped documents, VLM pages, table and row verification). It is not a measure of answer accuracy; every ratio states its denominator.",
    qNone: "No quality records yet.", qUnavailable: "No quality information in this library (an index created by an older version, opened read-only).",
    qLastRun: "Last indexing", qRuns: "Earlier runs", qKind_sync: "add/sync", qKind_rescan: "rescan", qKind_folder: "folder added",
    qKind_upload: "upload", qKind_add: "files added", qKind_retry: "retry", qKind_ingest: "indexing",
    qRun_done: "finished", qRun_done_with_errors: "finished with errors", qRun_running: "running", qRun_interrupted: "interrupted", qRun_failed: "ended with an error",
    qDocsLine: "Documents: {0} processed ({1} new, {2} updated) · {3} skipped as unchanged · {4} reused (moved/copies) · {5} partially processed · {6} failed · {7} unreachable · {8} source missing",
    qVlmOff: "The vision model (VLM) was off for this run: tables were read from the PDF text layer only, no cell-level table verification was done.",
    qVlmLine: "VLM: {1} pages examined in {0} documents ({2} from the cache) — tables on {3} pages, {4} pages examined without a table, VLM failed on {5} pages. {6} pages stayed below the table filter (not examined), {7} pages were not examined because of the page limit.",
    qTablesLine: "Tables: {0} extracted · {1} matched the PDF text cell by cell · {2} did not (the original PDF text is the evidence, the VLM version is search-only).",
    qRowsLine: "Parameter rows: {0}/{1} verified{2} — denominator: rows extracted by the VLM. The {3} unverified rows are not used in answers.",
    qStages: "Stage failures: read {0} · parse {1} · embedding {2} · index write {3} · other {4}", qEmb: "Embedding: {0} chunks sent, {1} unchanged chunks kept their vector.",
    qUnreachableFolders: "Unreachable folders: {0}",
    qRetryTitle: "Retry failed operations", qRetryNone: "Nothing failed that could be retried.",
    qRetryScope: "{0} documents will be retried: {1} only their failed VLM pages ({2} pages; successful pages from the cache, unchanged chunks keep their vectors), {3} processed again as a whole.",
    qRetryFolders: "{0} unreachable folder(s) will be scanned again.", qRetryKeep: "A failed retry never deletes the existing index; a retry with a worse result is not written.",
    qRetryRunning: "Retrying", qRetryDone: "Retry: {0}",
    qRes_ok: "done", qRes_partial: "still partial", qRes_failed: "failed again (existing index kept)", qRes_kept_existing: "not better, existing index kept", qRes_file_missing: "source file missing",
    qSt_ok: "no problems", qSt_partial: "partially processed", qSt_failed: "failed", qSt_none: "no quality info",
    qNoRecord: "No quality information: this document was indexed before the report existed. Nothing is guessed; the information appears once it is processed again.",
    qProcessed: "last processed: {0}", qNeeds: "needs reprocessing", qKept: "The earlier index is kept and used in answers (last success: {0}).", qNotIndexed: "This document was never indexed; it is not used in answers.",
    qMissingFile: "Source file not found: the document is excluded from answers (its index is kept).",
    qVlmDoc: "VLM: {0} pages examined — {1} with tables, {2} without, {3} failed; {4} pages below the filter, {5} over the limit.",
    qVlmDocOff: "VLM was off: tables from the PDF text only.", qVlmDocNotPdf: "Not a PDF: no VLM table extraction.",
    qTablesDoc: "Tables: {0} ({1} matched) · rows {2}/{3} verified.", qRetried: "Last retry: {0}.",
    qGoto: "Go to page", qAction: "Suggested:",
    qi_vlm_failed: "Page {page}: VLM table extraction failed ({detail}).", qa_vlm_failed: "Check the vision model server and retry.", qa_vlm_cut: "Raise vision.max_tokens and retry.",
    qi_metadata_failed: "Document info (standard/revision) could not be read by the model; taken from the file name and front pages ({detail}).", qa_metadata_failed: "Retry when the model server is reachable.",
    qi_no_text: "No text could be extracted (scanned PDF?).", qa_no_text: "Run OCR first and add the document again.",
    qi_vlm_disabled: "VLM was off: tables were read from the PDF text only.", qa_vlm_disabled: "Enable the vision model in Settings and rescan to verify tables.",
    qi_vlm_over_limit: "{count} table-like pages were not examined because of the per-document page limit.", qa_vlm_over_limit: "Raise vision.max_pages_per_doc and rescan.",
    qi_table_not_grounded: "Page {page}, {table}: the table could not be matched to the PDF text cell by cell ({status}); the original PDF text is the evidence, the VLM version is search-only.", qa_table_not_grounded: "Check the page.",
    qi_rows_unverified: "Page {page}, {table}: {count}/{total} parameter rows could not be verified; they are not used in answers.", qa_rows_unverified: "Check the page.",
    qi_table_match_uncertain: "Page {page}, {table}: the VLM table could not be matched to a PDF table reliably; the original is kept.", qa_table_match_uncertain: "",
    qi_read_failed: "The file could not be read ({detail}).", qa_read_failed: "Check that the file is not open/locked and readable, then retry.",
    qi_parse_failed: "The file could not be parsed ({detail}).", qa_parse_failed: "Check that the file is not damaged (save/download it again), then retry.",
    qi_embedding_failed: "The embedding service failed ({detail}).", qa_embedding_failed: "Check the embedding server and retry.",
    qi_index_write_failed: "The index could not be written ({detail}).", qa_index_write_failed: "Check disk space and write permission, then retry.",
    qi_other_failed: "Could not be processed ({detail}).", qa_other_failed: "Retry; if it keeps failing, see the log.",
    qStage_read: "read", qStage_parse: "parse", qStage_embedding: "embedding", qStage_index_write: "index write", qStage_other: "other", qStage_vlm: "VLM", qStage_metadata: "document info",
    qOpenReport: "Quality report",
    vRegenerated: "The answer was corrected and regenerated once.",
    judgeDone: "independent judge completed", judgeFailed: "independent judge NOT completed", judgeOff: "independent judge off", judgeNone: "judge not needed",
    removedList: "Removed / flagged statements", claimDetails: "Statement details", usesUser: "uses user input", usesCalc: "uses a calculation",
    st_supported: "supported", st_unsupported: "unsupported", st_unverified: "unverified", st_not_checked: "not applied",
    prev: "‹ Prev", next: "Next ›", zoom: "Zoom", zoomIn: "Zoom in", zoomOut: "Zoom out", openOriginal: "Open original file",
    inspectText: "Inspect source text", noPageImage: "This source is not a PDF, so there is no page image.",
    pageUnavailable: "The page cannot be shown: the source file is missing, moved or unreadable.",
    highlight: "Highlight", prevEv: "‹ Previous evidence", nextEv: "Next evidence ›", printed: "printed", verifStatus: "Verification", locStatus: "Location",
    loc_located: "evidence marked on the page", loc_partial: "part of the evidence marked",
    loc_not_located: "Source page found; the exact location could not be matched.", loc_ambiguous: "Source page found; the exact location could not be matched (the text occurs in several places).",
    loc_no_text_layer: "This page has no text layer (possibly scanned); no location can be shown.",
    loc_legacy_index: "This document was indexed by an older version; for highlights use Library > 'Build location data'.",
    loc_not_pdf: "Not a PDF: the evidence is marked in the text below.", loc_none: "No location for this source (not cited in the answer).",
    loc_calc: "A calculated result is not in any PDF; its inputs and their sources:", loc_user: "User input — values taken from the question, not a document.",
    locNote: "The highlight shows where the evidence is on the page; whether the statement is right is given by its 'Verification' status.",
    role_value: "value", role_label: "parameter", role_header: "column header", role_unit: "unit", role_condition: "condition", role_footnote: "footnote", role_text: "text", role_row: "table row",
    inputs: "Inputs", noSource: "no source", draftProgress: "Writing a draft — not verified yet", showDraft: "Show draft (unverified)",
    legacyNotice: "{0} document(s) use an older index format: PDF highlights and cell-level table checks need a migration.",
    migrate: "Build location data (quick migration)", legacyTag: "legacy index", userKind: "user input",
    scopeMissing: "not loaded: {0}",
    toggleLeft: "Show/hide the sources panel", collapseLeft: "Collapse panel", toggleRight: "Source panel", hideRight: "Hide the source panel",
    splitter: "Drag, or use ← →, to resize the source panel",
    addFolder: "Add document folder", rescan: "Rescan", filterPh: "Filter documents or buckets…",
    scopeLine: "Scope: {0}", scopeLibrary: "whole library", scopeDocsN: "{0} document(s)", clearScope: "Clear",
    generalBucket: "General (top folder)", docsShort: "docs", missingNotice: "{0} document(s) have no source file any more; they are not used in answers.",
    showMissing: "List", purgeMissing: "Remove from index", confirmPurge: "Remove the {0} document(s) without a source file from the index? (Your files are not touched.)",
    missingTag: "source missing", jobLog: "Detailed log", indexNow: "Index", reindexAll: "Rescan everything",
    folderPrompt: "Path of the document folder:", folderTitle: "Document folder: {0}", rescanTitle: "Rescan",
    folderIntro: "Documents are read where they are; nothing is written into the folder and no file is moved or renamed.",
    colBucket: "Bucket", colDocs: "Docs", colFolders: "Sub-folders", totalDocs: "{0} supported document(s) in total",
    unsupportedN: "{0} unsupported file(s) will be skipped", emptyDirsN: "{0} empty folder(s)", linksN: "{0} link(s)/junction(s) not followed",
    unreadableN: "{0} unreadable item(s)", notReachable: "The folder is not reachable: {0}", nothingFound: "No supported document found.",
    planTitle: "What will happen", act_new: "new", act_changed: "changed", act_settings: "re-processed (settings changed)", act_moved: "moved (index reused)",
    act_duplicate: "copy (index reused)", act_touched: "same content", act_restored: "back again", act_unchanged: "unchanged",
    act_missing: "source deleted", act_unreachable: "unreachable (left as is)", apiNote: "Only new or changed documents are sent to the model APIs.",
    jobScan: "Scanning…", jobIndex: "Indexing", jobDoneTitle: "Indexing finished", jobFailedTitle: "Indexing finished with errors",
    jobErrors: "{0} error(s)", reportLine: "{0} new, {1} updated, {2} unchanged, {3} reused, {4} missing, {5} unreachable, {6} failed",
    relPath: "Location",

    page: "Page", pages: "pages", chunks: "chunks", superseded: "superseded", toc: "Contents", noToc: "This document has no outline.",
    readOnly: "read-only", docs: "documents", params: "parameters", modelsLoad: "Load models", test: "Test",
    sameAsChat: "Same as chat model", baseUrl: "API base URL (…/v1)", apiKey: "API key", model: "Model",
    svc_llm: "Chat model (LLM)", svc_vision: "Vision model (VLM)", svc_embedding: "Embedding", svc_reranker: "Reranker",
    enabled: "enabled", notOnServer: "(not on server)", savedSettings: "Settings saved.", noModels: "Could not load models",
    errorPrefix: "Error", needToken: "This server requires an API token:",
    newChatStarted: "New chat started.", calc: "calculation", parameter: "parameter", table: "table", pageKind: "page", unverified: "unverified extraction",
    tlsTitle: "Connection security (HTTPS certificates)",
    tlsSystem: "Trust the Windows certificate store (company CA certificates deployed by IT)",
    tlsProxy: "Use system proxy settings (leave off when the model servers are on the LAN)",
    tlsCa: "Extra CA / server certificate files (.pem, .crt, .cer — separate several with ;)",
    tlsHint: "Changes in this section apply immediately. Certificates saved with 'Trust this server' are added to this list.",
    tlsApplied: "TLS settings applied.", insecure: "Disable SSL verification (insecure — testing only)",
    trustServer: "Trust this server…", trust: "Trust", certTitle: "Certificate presented by the server",
    certSubject: "Subject", certIssuer: "Issuer", certValid: "Valid", certFp: "SHA-256 fingerprint", certNames: "Names",
    certSelf: "self-signed", certCheck: "Confirm this fingerprint with the server administrator; trust it only if it matches.",
    certAlreadyOk: "This certificate already verifies with the current settings.", trusted: "Certificate saved and trusted: {0}",
    details: "Details",
    err_tls_untrusted: "The server certificate is not trusted (self-signed, or issued by a company CA this PC does not know). Have IT install the company root CA, add the CA file in the TLS section below, or check the fingerprint and use 'Trust this server'.",
    err_tls_hostname: "The certificate was issued for a different name/IP. Use the name in the certificate in the base URL (e.g. https://vllm.company.local:8000/v1 instead of the IP).",
    err_tls_expired: "The server certificate has expired or is not yet valid (also check this PC's clock).",
    err_tls_protocol: "TLS handshake failed: the server probably speaks plain HTTP. Try http:// instead of https://.",
    err_tls_other: "TLS connection failed.", err_connect: "Cannot connect: check the address/port, that the server is running and the firewall.",
    err_model: "The selected model is not served here.",
    published: "Published: {0}", opened: "Library opened.", pickFolderPrompt: "Folder path:",
    ex: ["What are the VDD and burst length differences between DDR4 and DDR5?", "What are the PCIe 5.0 LTSSM Polling substates and their exit conditions?",
         "Which bits are the SSM in an ARINC 429 word and what do they mean for BNR data?", "What is the maximum rise time and bus capacitance for I2C Fast-mode Plus?"],
  },
};
let LANG = "tr";
function t(key, ...args) {
  let s = (I18N[LANG] && I18N[LANG][key]) ?? I18N.en[key] ?? key;
  args.forEach((a, i) => { s = String(s).replace(`{${i}}`, a); });
  return s;
}
function applyI18n() {
  document.documentElement.lang = LANG;
  $$("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  $$("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); el.setAttribute("aria-label", el.title); });
  $("#question").placeholder = t("placeholder");
  $("#doc-filter").placeholder = t("filterPh");
  $$(".lang").forEach((b) => b.classList.toggle("active", b.dataset.lang === LANG));
  const ex = $("#examples");
  if (ex) {
    ex.innerHTML = "";
    for (const q of t("ex")) {
      const b = document.createElement("button");
      b.className = "example";
      b.textContent = q;
      b.addEventListener("click", () => ask(q));
      ex.append(b);
    }
  }
}

// ------------------------------------------------------------------ state + api
const state = { info: null, docs: [], selected: new Set(), selectedBuckets: new Set(), history: [], busy: false,
  token: TOKEN, filter: "", ui: loadUi(), chatId: 0, abort: null, viewSeq: 0, pendingClarify: null };

// Panel layout preferences (per user, kept across restarts; never affect what is searched).
function loadUi() {
  const d = { leftCollapsed: false, rightHidden: false, rightW: 440, openBuckets: [] };
  try { return Object.assign(d, JSON.parse(localStorage.getItem("techrag.ui") || "{}")); } catch { return d; }
}
function saveUi() { safeSet("techrag.ui", JSON.stringify(state.ui)); }

function safeGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function safeSet(k, v) { try { localStorage.setItem(k, v); } catch { /* unavailable */ } }
const desk = () => (window.pywebview && window.pywebview.api) || null;

// The desktop app hides this window instead of closing it: the page, the chat and running jobs stay as they are. While
// it is hidden nothing is polled (indexing goes on in the backend); progress is fetched again once it is shown.
const host = { hidden: false, waiters: [] };
const pageHidden = () => host.hidden || document.hidden;
function wakeUp() { if (!pageHidden()) host.waiters.splice(0).forEach((r) => r()); }
const whenShown = () => (pageHidden() ? new Promise((r) => host.waiters.push(r)) : Promise.resolve());
document.addEventListener("visibilitychange", wakeUp);
window.techragHost = {
  setVisible(v) { host.hidden = !v; wakeUp(); return !host.hidden; },
  openSettings() { if (!$("#dlg-settings").open) openSettings().catch((e) => alert(e.message)); return true; },
};

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers.Authorization = "Bearer " + state.token;
  const res = await fetch(path, Object.assign({}, opts, { headers }));
  if (res.status === 401 && !TOKEN) {
    const tok = prompt(t("needToken"));
    if (tok) { state.token = tok.trim(); return api(path, opts); }
  }
  if (!res.ok) {
    let msg = `${res.status} ${res.statusText}`;
    try { const j = await res.json(); msg = j.detail || msg; } catch { /* not json */ }
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (p) => (await api(p)).json();
const postJSON = async (p, body, method = "POST") =>
  (await api(p, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
const tokenQ = () => (state.token ? `?token=${encodeURIComponent(state.token)}` : "");
const pageUrl = (docId, page, dpi) => `/api/documents/${docId}/page/${page}.png${tokenQ()}${dpi ? (tokenQ() ? "&" : "?") + "dpi=" + dpi : ""}`;

// ------------------------------------------------------------------ markdown
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function expandCites(inner) {
  const out = [];
  for (const part of inner.split(/\s*,\s*/)) {
    const r = part.match(/^(\d+)\s*[–-]\s*(\d+)$/);
    // Same rule as the server (verify.parse_citations): button i must match cite_claims[i].
    if (r) { const a = +r[1], b = +r[2]; if (b > a && b - a < 50) for (let i = a; i <= b; i++) out.push(i); }
    else if (/^\d+$/.test(part)) out.push(+part);
  }
  return out;
}
function inline(s, cites) {
  const codes = [];
  s = s.replace(/`([^`]+)`/g, (m, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s.,;:)]|$)/g, "$1<em>$2</em>");
  if (cites) {
    s = s.replace(/\[(\d+(?:\s*[,–-]\s*\d+)*)\]/g, (m, inner) =>
      expandCites(inner).map((n) => `<button class="cite" data-n="${n}">${n}</button>`).join(""));
    s = s.replace(/⚠/g, '<span class="flag">⚠</span>');
  }
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[+i]}</code>`);
}
const LIST_RE = /^\s*([-*•]|\d+[.)])\s+/;
function renderTable(rows, cites) {
  const cells = (r) => r.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());
  const body = rows.filter((r) => !/^\s*\|?\s*:?-{2,}/.test(r));
  if (!body.length) return "";
  let h = "<table><thead><tr>" + cells(body[0]).map((c) => `<th>${inline(c, cites)}</th>`).join("") + "</tr></thead><tbody>";
  for (const r of body.slice(1)) h += "<tr>" + cells(r).map((c) => `<td>${inline(c, cites)}</td>`).join("") + "</tr>";
  return h + "</tbody></table>";
}
function renderMarkdown(md, cites = true) {
  const lines = esc(md).split("\n");
  const inl = (x) => inline(x, cites);
  let html = "";
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      html += `<pre><code>${buf.join("\n")}</code></pre>`;
      continue;
    }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) rows.push(lines[i++]);
      html += renderTable(rows, cites);
      continue;
    }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { const lvl = Math.min(h[1].length + 1, 4); html += `<h${lvl}>${inl(h[2])}</h${lvl}>`; i++; continue; }
    if (LIST_RE.test(line)) {
      const ordered = /^\s*\d+[.)]/.test(line);
      const items = [];
      while (i < lines.length && LIST_RE.test(lines[i])) {
        items.push(lines[i].replace(LIST_RE, ""));
        i++;
        while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !LIST_RE.test(lines[i])) { items[items.length - 1] += " " + lines[i].trim(); i++; }
      }
      const tag = ordered ? "ol" : "ul";
      html += `<${tag}>${items.map((x) => `<li>${inl(x)}</li>`).join("")}</${tag}>`;
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const para = [line];
    i++;
    while (i < lines.length && lines[i].trim() && !/^(\s*```|\s*\||#{1,6}\s)/.test(lines[i]) && !LIST_RE.test(lines[i])) para.push(lines[i++]);
    html += `<p>${inl(para.join("<br>"))}</p>`;
  }
  return html;
}

// ------------------------------------------------------------------ info + sources panel
async function loadInfo() {
  state.info = await getJSON("/api/info");
  if (!safeGet("techrag.lang")) LANG = state.info.language || LANG;
  renderInfo();
  const ids = new Set((state.info.buckets || []).map((b) => b.id));
  for (const id of [...state.selectedBuckets]) if (!ids.has(id)) state.selectedBuckets.delete(id);
}
function renderInfo() {
  const i = state.info;
  if (!i) return;
  const ro = i.read_only ? ` · ${t("readOnly")}` : "";
  $("#lib-info").textContent = `${i.library} · ${i.stats.documents} ${t("docs")} · ${i.stats.parameters || 0} ${t("params")}${ro}`;
  $("#add-box").hidden = !i.allow_upload;
  $("#src-actions").hidden = !i.allow_upload;
  const banner = $("#banner");
  const missing = SERVICES.filter((s) => s !== "vision" && !i.models[s]);
  if (i.error) { banner.hidden = false; banner.textContent = `${t("errorPrefix")}: ${i.error}`; }
  else if (i.legacy_documents && !i.read_only) { banner.hidden = false; banner.textContent = `${t("legacyNotice", i.legacy_documents)} → ${t("library")} › ${t("migrate")}`; }
  else if (missing.length) { banner.hidden = false; banner.textContent = `${t("settings")}: ${missing.map((s) => t("svc_" + s)).join(", ")} — ${t("model")}?`; }
  else banner.hidden = true;
  $("#collection-list").innerHTML = (i.buckets || []).filter((b) => b.name).map((b) => `<option value="${esc(b.name)}"></option>`).join("");
}

async function loadDocs() {
  try { state.docs = await getJSON("/api/documents"); } catch { state.docs = []; }
  for (const k of Object.keys(docCache)) delete docCache[k];  // re-indexed documents get new page info
  const ids = new Set(state.docs.map((d) => d.id));
  for (const id of [...state.selected]) if (!ids.has(id)) state.selected.delete(id);
  renderCollections();
}

function docTags(d) {
  const tags = (d.entities || []).slice(0, 2).map((e) => `<span class="tag">${esc(e)}</span>`);
  if (d.doc_type && d.doc_type !== "base") tags.push(`<span class="tag errata">${esc(d.doc_type.toUpperCase())}</span>`);
  if (d.revision) tags.push(`<span class="tag">rev ${esc(d.revision)}</span>`);
  if (d.superseded_by) tags.push(`<span class="tag">${t("superseded")}</span>`);
  if (d.legacy) tags.push(`<span class="tag errata" title="${esc(t("loc_legacy_index"))}">${t("legacyTag")}</span>`);
  if (d.missing) tags.push(`<span class="tag errata">${t("missingTag")}</span>`);
  return tags.join("");
}

const bucketName = (b) => (b && b.name) || t("generalBucket");
const fold = (x) => String(x || "").toLocaleLowerCase("tr").normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/ı/g, "i");
const docLocation = (d) => [d.bucket || t("generalBucket"), d.subpath, (d.rel_path || d.path).split("/").pop()].filter(Boolean).join(" / ");

function renderCollections() {
  const wrap = $("#collections");
  wrap.innerHTML = "";
  const buckets = (state.info && state.info.buckets) || [];
  const q = fold(state.filter.trim());
  let shown = 0;
  for (const b of buckets) {
    const docs = state.docs.filter((x) => (x.bucket_id || "") === b.id);
    const bucketHit = !q || fold(bucketName(b)).includes(q);
    const visibleDocs = bucketHit ? docs : docs.filter((d) => fold(`${d.title} ${d.rel_path || d.path}`).includes(q));
    if (!visibleDocs.length && !bucketHit) continue;
    shown += visibleDocs.length;
    const open = q ? visibleDocs.length > 0 : state.ui.openBuckets.includes(b.id);
    const bsel = state.selectedBuckets.has(b.id);
    const sec = document.createElement("div");
    sec.className = "bucket" + (open ? " open" : "") + (bsel ? " selected" : "");
    sec.dataset.bucket = b.id;
    const miss = b.missing ? ` <span class="tag errata" title="${esc(t("missingNotice", b.missing))}">⚠${b.missing}</span>` : "";
    sec.innerHTML = `<div class="bucket-head"><button type="button" class="chev" aria-expanded="${open}">${open ? "▾" : "▸"}</button>
      <input type="checkbox" class="bucket-cb" ${bsel ? "checked" : ""} title="${esc(t("scopeLine", bucketName(b)))}">
      <span class="bucket-name" title="${esc(bucketName(b))}">${esc(bucketName(b))}</span>${miss}<span class="count">${docs.length}</span></div>`;
    const list = document.createElement("div");
    list.className = "bucket-docs";
    list.hidden = !open;
    for (const doc of visibleDocs) {
      const row = document.createElement("div");
      row.className = "doc" + (doc.superseded_by ? " superseded" : "") + (doc.missing ? " missing" : "");
      const warn = doc.warnings && doc.warnings.length ? ` <span class="tag errata" title="${esc(doc.warnings.join("\n"))}">⚠</span>` : "";
      const checked = bsel || state.selected.has(doc.id);
      row.innerHTML = `<input type="checkbox" ${checked ? "checked" : ""} ${bsel ? "disabled" : ""}>
        <a href="#" class="doc-name"></a><span class="doc-tags">${docTags(doc)}${warn}</span>`;
      const a = $("a", row);
      a.textContent = doc.title;
      a.title = `${doc.title}\n${t("relPath")}: ${docLocation(doc)}`;
      $("input", row).addEventListener("change", (e) => { e.target.checked ? state.selected.add(doc.id) : state.selected.delete(doc.id); renderCollections(); });
      a.addEventListener("click", (e) => { e.preventDefault(); showDocument(doc.id); });
      list.append(row);
    }
    $(".chev", sec).addEventListener("click", () => toggleBucket(b.id));
    $(".bucket-name", sec).addEventListener("click", () => toggleBucket(b.id));
    $(".bucket-cb", sec).addEventListener("change", (e) => {
      if (e.target.checked) { state.selectedBuckets.add(b.id); docs.forEach((d) => state.selected.delete(d.id)); }
      else state.selectedBuckets.delete(b.id);
      renderCollections();
    });
    sec.append(list);
    wrap.append(sec);
  }
  if (!wrap.children.length) wrap.innerHTML = `<p class="muted small">${esc(q ? t("nothingFound") : t("viewerEmpty").replace(/\s*\[n\].*$/, ""))}</p>`;
  $("#doc-count").textContent = state.docs.length ? `${state.docs.length} ${t("docsShort")}` : "";
  renderScopeLine();
  const missing = state.docs.filter((d) => d.missing);
  const mb = $("#missing-box");
  mb.hidden = !missing.length;
  if (missing.length) {
    mb.innerHTML = `⚠ ${esc(t("missingNotice", missing.length))} <a href="#" class="show-missing">${t("showMissing")}</a>
      ${state.info && !state.info.read_only ? `· <a href="#" class="purge-missing">${t("purgeMissing")}</a>` : ""}
      <ul class="missing-list" hidden>${missing.map((d) => `<li title="${esc(d.rel_path || d.path)}">${esc(docLocation(d))}</li>`).join("")}</ul>`;
    $(".show-missing", mb).addEventListener("click", (e) => { e.preventDefault(); $(".missing-list", mb).hidden = !$(".missing-list", mb).hidden; });
    const purge = $(".purge-missing", mb);
    if (purge) purge.addEventListener("click", async (e) => {
      e.preventDefault();
      if (!confirm(t("confirmPurge", missing.length))) return;
      try { await postJSON("/api/documents/purge-missing", {}); await loadInfo(); await loadDocs(); } catch (err) { alert(err.message); }
    });
  }
}

function toggleBucket(id) {
  const open = new Set(state.ui.openBuckets);
  open.has(id) ? open.delete(id) : open.add(id);
  state.ui.openBuckets = [...open];
  saveUi();
  renderCollections();
}

function renderScopeLine() {
  const buckets = ((state.info && state.info.buckets) || []).filter((b) => state.selectedBuckets.has(b.id));
  const parts = buckets.map((b) => bucketName(b));
  if (state.selected.size) parts.push(t("scopeDocsN", state.selected.size));
  const line = $("#scope-line");
  line.innerHTML = `${esc(t("scopeLine", parts.length ? parts.join(", ") : t("scopeLibrary")))}`
    + (parts.length ? ` <a href="#" class="clear-scope">${t("clearScope")}</a>` : "");
  line.classList.toggle("active", parts.length > 0);
  const c = $(".clear-scope", line);
  if (c) c.addEventListener("click", (e) => { e.preventDefault(); state.selected.clear(); state.selectedBuckets.clear(); renderCollections(); });
  $("#btn-toggle-left").classList.toggle("scoped", parts.length > 0);
  $("#btn-toggle-left").title = `${t("toggleLeft")} — ${t("scopeLine", parts.length ? parts.join(", ") : t("scopeLibrary"))}`;
}

const scope = () => {
  const out = {};
  if (state.selected.size) out.doc_ids = [...state.selected];
  if (state.selectedBuckets.size) out.buckets = [...state.selectedBuckets];
  return out;
};

// ------------------------------------------------------------------ layout: left panel, splitter, right panel
const MIN_RIGHT = 300, MIN_CHAT = 380, NARROW = 980;
const isNarrow = () => window.innerWidth < NARROW;
function leftWidth() {
  const el = $("#sources-panel");
  return state.ui.leftCollapsed || isNarrow() || !el ? 0 : el.getBoundingClientRect().width;
}
function maxRight() { return Math.max(MIN_RIGHT, window.innerWidth - leftWidth() - MIN_CHAT - 48); }
function setRightWidth(w, persist) {
  const clamped = Math.round(Math.min(Math.max(w, MIN_RIGHT), maxRight()));
  document.documentElement.style.setProperty("--right-w", `${clamped}px`);
  const sp = $("#splitter");
  sp.setAttribute("aria-valuenow", clamped);
  sp.setAttribute("aria-valuemin", MIN_RIGHT);
  sp.setAttribute("aria-valuemax", maxRight());
  if (persist) { state.ui.rightW = clamped; saveUi(); }
  return clamped;
}
function applyLayout() {
  // Wide window: panels follow the saved preferences. Narrow window: they become drawers, closed by default.
  const narrow = isNarrow();
  const b = document.body;
  b.classList.toggle("narrow", narrow);
  b.classList.toggle("left-collapsed", narrow ? !state.narrowLeft : !!state.ui.leftCollapsed);
  b.classList.toggle("right-hidden", narrow ? !state.narrowRight : !!state.ui.rightHidden);
  $("#btn-toggle-left").setAttribute("aria-expanded", String(!b.classList.contains("left-collapsed")));
  $("#btn-toggle-right").setAttribute("aria-pressed", String(!b.classList.contains("right-hidden")));
  setRightWidth(state.ui.rightW || 440, false); // a width saved on a larger screen is clamped, not overwritten
}
function setLeftCollapsed(v) {
  if (isNarrow()) state.narrowLeft = !v; else { state.ui.leftCollapsed = v; saveUi(); }
  applyLayout();
}
function setRightHidden(v) {
  if (isNarrow()) state.narrowRight = !v; else { state.ui.rightHidden = v; saveUi(); }
  applyLayout();
}
const leftOpen = () => !document.body.classList.contains("left-collapsed");
const rightOpen = () => !document.body.classList.contains("right-hidden");
function showRight() { if (!rightOpen()) setRightHidden(false); }

function bindSplitter() {
  const sp = $("#splitter");
  let startX = 0, startW = 0;
  const move = (e) => setRightWidth(startW + (startX - e.clientX), false);
  const up = (e) => {
    sp.releasePointerCapture(e.pointerId);
    document.body.classList.remove("resizing");
    sp.removeEventListener("pointermove", move);
    sp.removeEventListener("pointerup", up);
    state.ui.rightW = parseInt(getComputedStyle(document.documentElement).getPropertyValue("--right-w"), 10) || state.ui.rightW;
    saveUi();
  };
  sp.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault(); // no text selection while dragging
    startX = e.clientX;
    startW = $("#viewer-panel").getBoundingClientRect().width;
    sp.setPointerCapture(e.pointerId);
    document.body.classList.add("resizing");
    sp.addEventListener("pointermove", move);
    sp.addEventListener("pointerup", up);
  });
  sp.addEventListener("keydown", (e) => {
    const cur = $("#viewer-panel").getBoundingClientRect().width;
    const step = e.shiftKey ? 80 : 20;
    const keys = { ArrowLeft: cur + step, ArrowRight: cur - step, Home: maxRight(), End: MIN_RIGHT };
    if (e.key in keys) { e.preventDefault(); setRightWidth(keys[e.key], true); }
    else if (e.key === "Enter") { e.preventDefault(); setRightHidden(true); }
  });
  sp.addEventListener("dblclick", () => setRightWidth(440, true));
  window.addEventListener("resize", () => applyLayout());
}

// ------------------------------------------------------------------ viewer
const ZOOMS = [1, 1.5, 2, 3];
const docCache = {};
async function docInfo(id) {
  if (!docCache[id]) docCache[id] = getJSON(`/api/documents/${id}`).catch(() => ({ page_labels: {} }));
  return docCache[id];
}

// Page image + highlight overlay. Region rects are fractions (0..1) of the rendered page (rotation and
// CropBox already applied server-side), so the overlay stays aligned at any zoom level, DPI or screen scaling.
function pageViewer(container, docId, page, nPages, opts = {}) {
  const regions = (opts.regions || []).filter((r) => r.doc_id === undefined || r.doc_id === docId);
  const st = { p: page, zoom: 0, show: true, cur: regions.length ? Math.max(0, Math.min(opts.focus || 0, regions.length - 1)) : -1, labels: {} };
  if (st.cur >= 0) st.p = regions[st.cur].page;
  const box = document.createElement("div");
  box.className = "pv-box";
  const evCtl = regions.length ? `<span class="pv-ev"><label class="small"><input type="checkbox" class="pv-hl" checked> ${t("highlight")}</label>
      <button type="button" class="ghost small pv-evprev">${t("prevEv")}</button><span class="pv-evlabel small"></span>
      <button type="button" class="ghost small pv-evnext">${t("nextEv")}</button></span>` : "";
  box.innerHTML = `<div class="page-nav"><button type="button" class="ghost small pv-prev" title="${t("prev")}">‹</button>
    <span class="pv-label small"></span><button type="button" class="ghost small pv-next" title="${t("next")}">›</button>
    <button type="button" class="ghost small pv-zout" title="${t("zoomOut")}">−</button><span class="pv-zl small"></span>
    <button type="button" class="ghost small pv-zin" title="${t("zoomIn")}">+</button>
    ${state.info && state.info.desktop ? `<button type="button" class="ghost small pv-open">${t("openOriginal")}</button>` : ""}</div>
    ${evCtl}<div class="pv-role small muted"></div><div class="pv-fail" hidden></div>
    <div class="page-view"><div class="page-wrap"><img alt=""><div class="hl-layer"></div></div></div>`;
  const img = $("img", box);
  const wrap = $(".page-wrap", box);
  const layer = $(".hl-layer", box);
  const draw = () => {
    layer.innerHTML = "";
    layer.hidden = !st.show;
    regions.forEach((r, i) => {
      if (r.page !== st.p) return;
      for (const rc of r.rects || []) {
        const d = document.createElement("div");
        d.className = `hl role-${r.role || "text"}${i === st.cur ? " current" : ""}`;
        d.style.left = `${rc[0] * 100}%`;
        d.style.top = `${rc[1] * 100}%`;
        d.style.width = `${Math.max(rc[2] - rc[0], 0.002) * 100}%`;
        d.style.height = `${Math.max(rc[3] - rc[1], 0.002) * 100}%`;
        d.title = `${t("role_" + (r.role || "text"))}${r.quote ? ": " + r.quote : ""}`;
        layer.append(d);
      }
    });
    const cur = regions[st.cur];
    const role = $(".pv-role", box);
    role.textContent = cur && cur.page === st.p ? `${t("role_" + (cur.role || "text"))}${cur.quote ? " — " + cur.quote.slice(0, 160) : ""}` : "";
    const el = $(".evlabel, .pv-evlabel", box);
    if (el) el.textContent = `${st.cur + 1}/${regions.length}`;
  };
  const show = () => {
    const z = ZOOMS[st.zoom];
    wrap.style.width = `${z * 100}%`;
    img.src = pageUrl(docId, st.p, Math.min(220, Math.round(110 * z)));
    const lab = st.labels[st.p] || (regions.find((r) => r.page === st.p && r.page_label) || {}).page_label || "";
    $(".pv-label", box).textContent = `${t("page")} ${st.p}${nPages ? " / " + nPages : ""}${lab && lab !== String(st.p) ? ` (${t("printed")}: ${lab})` : ""}`;
    $(".pv-zl", box).textContent = `${Math.round(z * 100)}%`;
    $(".pv-prev", box).disabled = st.p <= 1;
    $(".pv-next", box).disabled = nPages ? st.p >= nPages : false;
    draw();
  };
  img.addEventListener("load", () => {
    $(".pv-fail", box).hidden = true;
    $(".page-view", box).hidden = false;
    const c = $(".hl.current", box);
    if (c && st.show) c.scrollIntoView({ block: "center", inline: "center" });
  });
  img.addEventListener("error", () => {  // file moved/missing: say so instead of an empty frame
    const fail = $(".pv-fail", box);
    fail.innerHTML = `<div class="small muted">${esc(t("pageUnavailable"))}</div>${opts.fallback ? opts.fallback() : ""}`;
    fail.hidden = false;
    $(".page-view", box).hidden = true;
  });
  const go = (i) => { st.cur = (i + regions.length) % regions.length; if (regions[st.cur].page !== st.p) { st.p = regions[st.cur].page; show(); } else { draw(); const c = $(".hl.current", box); if (c) c.scrollIntoView({ block: "center", inline: "center" }); } };
  $(".pv-prev", box).addEventListener("click", () => { if (st.p > 1) { st.p--; show(); } });
  $(".pv-next", box).addEventListener("click", () => { st.p++; show(); });
  $(".pv-zin", box).addEventListener("click", () => { if (st.zoom < ZOOMS.length - 1) { st.zoom++; show(); } });
  $(".pv-zout", box).addEventListener("click", () => { if (st.zoom > 0) { st.zoom--; show(); } });
  if (regions.length) {
    $(".pv-hl", box).addEventListener("change", (e) => { st.show = e.target.checked; draw(); });
    $(".pv-evprev", box).addEventListener("click", () => go(st.cur - 1));
    $(".pv-evnext", box).addEventListener("click", () => go(st.cur + 1));
  }
  const open = $(".pv-open", box);
  if (open) open.addEventListener("click", () => postJSON(`/api/documents/${docId}/open`, {}).catch((e) => alert(e.message)));
  container.append(box);
  docInfo(docId).then((d) => { st.labels = d.page_labels || {}; show(); });
  show();
  return box;
}

function kindLabel(s) {
  return { parameter: t("parameter"), table: t("table"), page: t("pageKind"), calc: t("calc"), user: t("userKind") }[s.kind] || "";
}

// Location records of the claims that cite source s (or of one claim) in an answer message.
function evidenceOf(msg, s, claimId) {
  const v = msg && msg._verification;
  if (!v) return [];
  const out = [];
  for (const c of v.details || []) {
    if (claimId ? c.id !== claimId : !(c.citations || []).includes(s.n)) continue;
    if (c.outcome !== "kept" && c.outcome !== "flagged") continue;
    for (const e of c.evidence || []) if (e.n === s.n) out.push({ claim: c, rec: e });
  }
  return out;
}

function markQuotes(html, quotes) {
  let out = html;
  for (const q of quotes || []) {
    const e = esc(q.replace(/\[\d+(?:\s*[,–-]\s*\d+)*\]/g, "").trim());
    if (e.length < 4) continue;
    const i = out.indexOf(e);
    if (i >= 0) out = out.slice(0, i) + `<mark>${e}</mark>` + out.slice(i + e.length);
  }
  return out;
}

function showSource(s, msg, claimId, override) {
  state.viewSeq++;
  showRight();
  const pane = $("#tab-source");
  const ev = override ? [] : evidenceOf(msg, s, claimId);
  const recs = override ? [override] : ev.map((x) => x.rec);
  const claims = ev.map((x) => x.claim);
  const regions = [];
  for (const r of recs) for (const g of r.regions || []) if (!regions.some((x) => x.page === g.page && JSON.stringify(x.rects) === JSON.stringify(g.rects))) regions.push(g);
  const quotes = recs.flatMap((r) => r.quotes || []);
  const label = s.page_label && s.page_label !== String(s.page_start) ? ` (${t("printed")}: ${s.page_label})` : "";
  const pages = s.page_start === s.page_end ? `${t("page")} ${s.page_start}${label}` : `${t("page")} ${s.page_start}–${s.page_end}`;
  const meta = [(s.entities || []).join(", "), s.doc_type && s.doc_type !== "base" ? s.doc_type.toUpperCase() : "", s.section, s.doc_id ? pages : "",
    kindLabel(s), s.verified === false ? t("unverified") : ""].filter(Boolean).map(esc).join(" · ");
  // Verification (is the statement right?) and location (where is the evidence?) are shown separately.
  let status = "";
  if (claims.length) {
    const st = [...new Set(claims.map((c) => c.status))];
    status += `<div class="small"><b>${t("verifStatus")}:</b> ${st.map((x) => `<span class="badge st-${x}">${esc(t("st_" + x))}</span>`).join(" ")}</div>`;
  }
  const locKeys = [...new Set(recs.map((r) => (r.partial ? "partial" : r.status)))];
  let locText = "";
  if (s.kind === "calc") locText = t("loc_calc");
  else if (s.kind === "user") locText = t("loc_user");
  else if (!recs.length) locText = t("loc_none");
  else locText = locKeys.map((k) => t("loc_" + k)).join(" ");
  status += `<div class="small"><b>${t("locStatus")}:</b> ${esc(locText)}</div>`;
  if (regions.length) status += `<div class="small muted">${esc(t("locNote"))}</div>`;
  let inputs = "";
  if (s.kind === "calc") {
    const recInputs = (recs[0] && recs[0].inputs) || (s.extra && s.extra.inputs) || [];
    inputs = `<div class="calc-inputs"><b>${t("inputs")}:</b><ul>${recInputs.map((inp, i) => `<li><code>${esc(inp.value)}</code> — ${
      (inp.sources || []).length ? inp.sources.map((n) => `<button type="button" class="cite" data-input="${i}" data-n="${n}">${n}</button>`).join("") : `<span class="flag">${t("noSource")}</span>`}</li>`).join("")}</ul></div>`;
  }
  const title = s.kind === "calc" ? t("calc") : s.kind === "user" ? t("userKind") : s.doc_title;
  // A PDF source shows its page (with the highlights) and nothing else: the extracted text stays in the index for
  // search, verification and evidence matching, it is just not repeated under the page. A source without a page
  // image (Markdown, DOCX, text) keeps its document info and the passage behind an "inspect" toggle.
  const doc = s.doc_id ? state.docs.find((d) => d.id === s.doc_id) : null;
  const isPdf = !!s.doc_id && (!doc || /\.pdf$/i.test(doc.rel_path || doc.path || ""));
  const inspect = (open) => `<details class="source-excerpt"${open ? " open" : ""}><summary>${t("inspectText")}</summary>
    <div class="source-text markdown">${markQuotes(renderMarkdown(s.text, false), quotes)}</div></details>`;
  let body = "";
  if (isPdf) body = `<div class="pv"></div>`;
  else if (s.doc_id) {
    body = `<div class="small muted no-page">${esc(t("noPageImage"))}</div>${state.info && state.info.desktop
      ? `<button type="button" class="ghost small src-open">${t("openOriginal")}</button>` : ""}${inspect(false)}`;
  } else body = `<div class="source-text markdown">${renderMarkdown(s.text, false)}</div>`;  // calculation / user input
  pane.innerHTML = `<div class="source-card${isPdf ? " pdf" : ""}"><h3>[${s.n}] ${esc(title)}${s.revision ? ` <span class="tag">rev ${esc(s.revision)}</span>` : ""}</h3>
    <div class="source-meta">${meta}</div>${s.relation ? `<div class="small relation">${esc(s.relation)}</div>` : ""}
    <div class="loc-status">${status}</div>${inputs}${body}</div>`;
  const srcOpen = $(".src-open", pane);
  if (srcOpen) srcOpen.addEventListener("click", () => postJSON(`/api/documents/${s.doc_id}/open`, {}).catch((e) => alert(e.message)));
  if (s.kind === "calc") {
    $$(".calc-inputs .cite", pane).forEach((b) => b.addEventListener("click", () => {
      const src = (msg._sources || []).find((x) => x.n === +b.dataset.n);
      const inp = ((recs[0] && recs[0].inputs) || [])[+b.dataset.input] || {};
      const loc = (inp.locations || []).find((l) => l.n === +b.dataset.n);
      if (src) showSource(src, msg, null, loc || { n: src.n, status: "not_located", regions: [], quotes: [inp.value] });
    }));
  }
  if (isPdf) {
    pageViewer($(".pv", pane), s.doc_id, s.page_start || 1, doc ? doc.n_pages : 0, { regions, fallback: () => inspect(true) });
  }
  $$(".cite-chip.active").forEach((c) => c.classList.remove("active"));
}

async function showDocument(id, page) {
  const seq = ++state.viewSeq;
  showRight();
  const d = await getJSON(`/api/documents/${id}`);
  if (seq !== state.viewSeq) return;  // the user opened something else meanwhile
  docCache[id] = Promise.resolve(d);
  const pane = $("#tab-source");
  const toc = (d.toc || []).slice(0, 500).map(([lvl, title, page]) =>
    `<li style="padding-left:${(lvl - 1) * 12}px"><a data-page="${page}">${esc(title)}</a><span class="pg">${page}</span></li>`).join("");
  const warn = (d.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  pane.innerHTML = `<div class="source-card"><h3>${esc(d.title)} ${docTags(d)}</h3>
    <div class="source-meta">${esc(d.domain)} · ${d.n_pages} ${t("pages")} · ${d.n_chunks} ${t("chunks")} · ${esc(d.doc_date || "")}</div>
    ${warn ? `<div class="verify warning"><ul>${warn}</ul></div>` : ""}<div class="pv"></div>
    <h3 style="margin-top:10px">${t("toc")}</h3>${toc ? `<ul class="toc">${toc}</ul>` : `<p class="muted">${t("noToc")}</p>`}</div>`;
  const pv = $(".pv", pane);
  if (!/\.pdf$/i.test(d.rel_path || d.path || "")) {  // no page image: document info + open, never an empty frame
    pv.innerHTML = `<div class="small muted no-page">${esc(t("noPageImage"))}</div>${state.info && state.info.desktop
      ? `<button type="button" class="ghost small src-open">${t("openOriginal")}</button>` : ""}`;
    const o = $(".src-open", pv);
    if (o) o.addEventListener("click", () => postJSON(`/api/documents/${d.id}/open`, {}).catch((e) => alert(e.message)));
    return;
  }
  pageViewer(pv, d.id, page || 1, d.n_pages);
  $$(".toc a", pane).forEach((a) => a.addEventListener("click", () => { pv.innerHTML = ""; pageViewer(pv, d.id, +a.dataset.page, d.n_pages); }));
}

// ------------------------------------------------------------------ chat
const scrollDown = () => { const m = $("#messages"); m.scrollTop = m.scrollHeight; };

function renderChips(el) {
  const wrap = $(".cites", el);
  wrap.innerHTML = "";
  const list = [...el._sources].sort((a, b) => (b.cited === true) - (a.cited === true) || a.n - b.n);
  for (const s of list) {
    if (el._final && s.cited === false && list.some((x) => x.cited)) continue;
    const b = document.createElement("button");
    b.className = "cite-chip" + (s.cited === false ? " uncited" : "");
    const where = s.kind === "calc" ? s.text.split("\n")[0] : s.kind === "user" ? t("loc_user").split(" — ")[0]
      : `${s.doc_title}${s.section ? " — " + s.section.split(" > ").pop() : ""} — ${t("page")} ${s.page_start}`;
    b.innerHTML = `<b>[${s.n}]</b> ${kindLabel(s) ? `<i>${esc(kindLabel(s))}</i> ` : ""}${esc(where)}`;
    b.title = s.text.slice(0, 400);
    b.addEventListener("click", () => { showSource(s, el); b.classList.add("active"); });
    wrap.append(b);
  }
}

function renderVerification(el, v, confidence) {
  const box = $(".verify", el);
  if (!v) { box.hidden = true; return; }
  box.hidden = false;
  box.className = "verify " + v.status;
  const j = v.judge || {};
  let html = "";
  if (v.status === "supported") html = t("vSupported", v.kept);
  else if (v.status === "corrected") html = t("vCorrected", v.kept, (v.removed || []).length);
  else if (v.status === "not_found") html = t("vNotFound");
  else if (v.status === "clarify") html = t("vClarify");
  else if (v.status === "unverified") html = t("vUnverified");
  else if (v.status === "not_checked") html = (v.deterministic || {}).enabled === false ? t("vNotChecked") : t("vNotCheckedJudge");
  else html = t("vWarning", (v.flagged || []).length);
  const notes = [];
  if (v.status !== "clarify") {
    notes.push(!j.enabled ? t("judgeOff") : j.completed ? t("judgeDone") : j.called ? t("judgeFailed") : t("judgeNone"));
    if (v.regenerated) notes.push(t("vRegenerated"));
    if (confidence != null) notes.push(`rerank ${(confidence * 100).toFixed(0)}%`);
  }
  html += `<div class="small">${notes.map(esc).join(" · ")}</div>`;
  if ((j.errors || []).length) html += `<div class="small muted">${esc(j.errors.join(" | "))}</div>`;
  const failed = (v.details || []).filter((c) => c.outcome === "removed" || c.outcome === "flagged");
  if (failed.length) {
    html += `<details><summary>${t("removedList")} (${failed.length})</summary><ul>${failed.map((c) =>
      `<li><span class="badge st-${c.status}">${esc(t("st_" + c.status))}</span> ${esc(c.text)}<br><span class="small">${esc((c.reasons || []).concat(c.judge && c.judge.error ? [c.judge.error] : []).join("; "))}</span></li>`).join("")}</ul></details>`;
  }
  const kept = (v.details || []).filter((c) => c.outcome === "kept" || c.outcome === "flagged");
  if (kept.length) {
    html += `<details><summary>${t("claimDetails")} (${kept.length})</summary><ul>${kept.map((c) => {
      const extra = [c.uses_user_input ? t("usesUser") : "", c.uses_calculation ? t("usesCalc") : ""].filter(Boolean).join(", ");
      return `<li><span class="badge st-${c.status}">${esc(t("st_" + c.status))}</span> ${esc(c.text)}${extra ? ` <span class="small muted">(${esc(extra)})</span>` : ""}</li>`;
    }).join("")}</ul></details>`;
  }
  box.innerHTML = html;
}

function stageText(ev) {
  return { planning: t("stPlanning"), answering: t("stAnswering"), verifying: t("stVerifying"),
    regenerating: t("stRegenerating", ev.failed || ""), tools_unsupported: t("stNoTools") }[ev.stage] || "";
}

// A clarifying question waits for the user's reply: the next message is merged into the question it belongs to
// (the server decides; a new question of its own drops it). Shown above the input so it is never a surprise.
function setPending(p) {
  state.pendingClarify = p || null;
  const box = $("#pending-clarify");
  box.hidden = !state.pendingClarify;
  if (state.pendingClarify) $("#pending-text").textContent = t("pendingNote", state.pendingClarify.question);
}

function renderClarify(el, body, clar) {
  if (clar.questions && clar.questions.length) {
    body.innerHTML = `<p>${esc(clar.intro || "")}</p>`;
    const picks = {};
    const multi = clar.questions.length > 1;
    const send = document.createElement("button");
    const reply = () => clar.questions.map((q) => picks[q.kind]).filter(Boolean).join(", ");
    clar.questions.forEach((q, i) => {
      const block = document.createElement("div");
      block.className = "clarify-q";
      block.innerHTML = `<div>${multi ? `${i + 1}. ` : ""}${esc(q.text)}</div>`;
      if (q.options.length) {
        const row = document.createElement("div");
        row.className = "clarify";
        for (const o of q.options) {
          const b = document.createElement("button");
          b.className = "example";
          b.textContent = o.label;
          b.title = o.value;
          b.addEventListener("click", () => {
            if (!multi) { ask(o.value, clar.pending || null); return; }
            picks[q.kind] = picks[q.kind] === o.value ? null : o.value;
            $$("button", row).forEach((x) => x.classList.toggle("active", x === b && picks[q.kind] === o.value));
            send.disabled = !reply();
          });
          row.append(b);
        }
        block.append(row);
      }
      body.append(block);
    });
    const acts = document.createElement("div");
    acts.className = "clarify clarify-actions";
    if (multi && clar.questions.some((q) => q.options.length)) {
      send.className = "example";
      send.textContent = t("clarifySend");
      send.disabled = true;
      send.addEventListener("click", () => { const r = reply(); if (r) ask(r, clar.pending || null); });
      acts.append(send);
    }
    const dk = document.createElement("button");
    dk.className = "example";
    dk.textContent = t("dontKnow");
    dk.addEventListener("click", () => ask(clar.dont_know || t("dontKnow"), clar.pending || null));
    acts.append(dk);
    body.append(acts);
    const outro = document.createElement("p");
    outro.className = "small muted";
    outro.textContent = clar.outro || "";
    body.append(outro);
    return;
  }
  if (clar.options) {  // an older server: one list of ready-made questions
    const box = document.createElement("div");
    box.className = "clarify";
    for (const o of clar.options) {
      const b = document.createElement("button");
      b.className = "example";
      b.textContent = o.label;
      b.title = o.question;
      b.addEventListener("click", () => ask(o.question));
      box.append(b);
    }
    body.append(box);
  }
}

function newChat(note) {
  state.chatId++;  // events of an answer still streaming no longer touch the new chat
  if (state.abort) state.abort.abort();
  state.history = [];
  setPending(null);
  $("#messages").innerHTML = `<div class="empty" id="empty-state"><p>${esc(note)}</p></div>`;
}

// `reply`: the clarifying question this message answers (an option button of that question); by default the one
// pending above the input.
async function ask(question, reply) {
  if (state.busy || !question.trim()) return;
  state.busy = true;
  const chat = state.chatId;
  const ctrl = new AbortController();
  state.abort = ctrl;
  const pendingClarify = reply !== undefined ? reply : state.pendingClarify;
  setPending(null);
  let asked = question;
  $("#btn-send").disabled = true;
  const empty = $("#empty-state");
  if (empty) empty.remove();
  const u = document.createElement("div");
  u.className = "msg user";
  u.textContent = question;
  $("#messages").append(u);
  const el = $("#tpl-answer").content.firstElementChild.cloneNode(true);
  $$("[data-i18n]", el).forEach((x) => { x.textContent = t(x.dataset.i18n); });
  $("#messages").append(el);
  el._sources = [];
  el._final = false;
  const body = $(".body", el);
  const status = $(".status", el);
  let text = "";
  let confidence = null;
  let pending = false;
  // The streamed draft is progress only: it is shown collapsed and greyed, never as the answer.
  const render = () => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (el._final) return; // a draft frame queued before the final answer must not overwrite it
      let d = $(".draft-box", body);
      if (!d) {
        body.innerHTML = `<details class="draft-box"><summary>${t("showDraft")} <span class="small muted draft-len"></span></summary><div class="markdown draft"></div></details>`;
        d = $(".draft-box", body);
      }
      $(".draft", d).innerHTML = renderMarkdown(text, false);
      $(".draft-len", d).textContent = text ? `(${text.length})` : "";
      scrollDown();
    });
  };
  scrollDown();

  const handle = (ev) => {
    switch (ev.type) {
      case "status": status.textContent = stageText(ev); break;
      case "plan": {
        const s = ev.scope || {};
        const parts = [];
        if (s.missing && s.missing.length) parts.push(t("scopeMissing", s.missing.join(", ")));
        if (s.reason === "entity") parts.push(t("scopeEntity", (s.entities || []).join(", ")));
        else if (s.reason === "user" || (s.buckets || []).length) parts.push(t("scopeUser"));
        else if (s.reason === "domain") parts.push(t("scopeDomain", (s.domains || []).join(", ")));
        else parts.push(t("scopeAll"));
        if (ev.merged_question) { asked = ev.merged_question; parts.push(esc(t("mergedAs", ev.merged_question))); }
        if ((ev.plan.carried_entities || []).length) parts.push(esc(t("carried", ev.plan.carried_entities.join(", "))));
        if (ev.plan.english && ev.plan.english !== ev.plan.question) parts.push(`${t("searchAs")}: “${esc(ev.plan.english)}”`);
        if (ev.thinking) parts.push(t("thinkingOn"));
        const p = $(".plan", el);
        p.innerHTML = parts.join(" · ");
        p.hidden = false;
        break;
      }
      case "sources": el._sources = ev.sources; confidence = ev.confidence; renderChips(el); break;
      case "sources_add": el._sources = el._sources.concat(ev.sources); renderChips(el); break;
      case "reasoning": { const d = $(".reasoning", el); d.hidden = false; $("pre", d).textContent += ev.text; break; }
      case "tool": {
        const ul = $(".tool-log", el);
        ul.hidden = false;
        const li = document.createElement("li");
        li.textContent = `${ev.name}: ${ev.summary}`;
        ul.append(li);
        break;
      }
      case "draft": status.textContent = t("draftProgress"); text += ev.text; render(); break;
      case "draft_reset": text = ""; render(); break;
      case "clarify": el._clarify = ev; break;
      case "final": {
        el._final = true;
        status.textContent = "";
        text = ev.answer;
        body.innerHTML = renderMarkdown(text);
        $$("button.cite", body).forEach((b, i) => { const c = (ev.cite_claims || [])[i]; if (c != null) b.dataset.claim = c; });
        el._sources = ev.sources || el._sources;
        el._verification = ev.verification;
        renderChips(el);
        renderVerification(el, ev.verification, confidence);
        const clar = ev.clarify || el._clarify;
        if (clar) {
          renderClarify(el, body, clar);
          if (clar.pending && chat === state.chatId) setPending(clar.pending);
        }
        if (ev.notice) {
          const n = document.createElement("div");
          n.className = "notice";
          n.textContent = ev.notice;
          body.prepend(n);
        }
        const acts = $(".msg-actions", el);
        acts.hidden = false;
        $(".timing", acts).textContent = ev.timings && ev.timings.total ? `${ev.timings.total.toFixed(1)} s` : "";
        $(".btn-copy", acts).addEventListener("click", (e) => {
          const refs = el._sources.filter((s) => s.cited).map((s) => `[${s.n}] ${s.doc_title} — ${s.section} — p.${s.page_start}`).join("\n");
          navigator.clipboard && navigator.clipboard.writeText(`${text}\n\n${refs}`);
          e.target.textContent = t("copied");
        });
        if (chat === state.chatId) {
          state.history.push({ role: "user", content: asked }, { role: "assistant", content: text });
          state.history = state.history.slice(-12);
        }
        break;
      }
      case "error": el._final = true; status.textContent = ""; body.innerHTML = `<div class="verify warning">${t("errorPrefix")}: ${esc(ev.message)}</div>`; break;
      default: break;
    }
  };

  try {
    const res = await api("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, signal: ctrl.signal,
      body: JSON.stringify(Object.assign({ question, history: state.history.slice(-6), stream: true,
        clarification: pendingClarify }, scope())) });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const chunk = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        const data = chunk.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join("");
        if (data) handle(JSON.parse(data));
      }
    }
  } catch (e) {
    el._final = true;  // a draft frame still queued must not replace the error
    status.textContent = "";
    body.innerHTML = `<div class="verify warning">${t("errorPrefix")}: ${esc(e.message)}</div>`;
  } finally {
    if (state.abort === ctrl) state.abort = null;
    state.busy = false;
    $("#btn-send").disabled = false;
    scrollDown();
  }
}

// ------------------------------------------------------------------ jobs / adding documents
// Indexing runs on the server in the background; this only polls, so the window never freezes.
let jobChain = Promise.resolve();
function pollJob(job, title) {
  const run = jobChain.then(() => followJob(job, title));
  jobChain = run.catch(() => {});
  return run;
}
async function followJob(job, title) {
  const box = $("#job-box");
  box.hidden = false;
  $("#job-title").textContent = title || t("jobIndex");
  const log = $("#job-log");
  let j = null;
  for (;;) {
    await new Promise((r) => setTimeout(r, 900));
    await whenShown();
    j = await getJSON(`/api/jobs/${job}`);
    const p = j.progress || {};
    const pct = p.total ? Math.round((100 * (p.done || 0)) / p.total) : (j.status === "done" ? 100 : 5);
    $("#job-bar").style.width = `${pct}%`;
    $("#job-now").textContent = p.phase === "scan" ? t("jobScan")
      : p.file ? `${p.bucket || t("generalBucket")} · ${p.file} (${(p.done || 0) + 1}/${p.total})` : (p.total ? `${p.done}/${p.total}` : "");
    $("#job-errors").textContent = p.errors ? t("jobErrors", p.errors) : "";
    log.textContent = j.messages.slice(-300).join("\n");
    log.scrollTop = log.scrollHeight;
    if (j.status === "done" || j.status === "failed" || j.status === "cancelled") break;
  }
  const r = j.report || {};
  $("#job-title").textContent = j.status === "done" && !Object.keys(r.failed || {}).length ? t("jobDoneTitle") : t("jobFailedTitle");
  $("#job-bar").style.width = "100%";
  if (r.summary) {
    $("#job-now").textContent = t("reportLine", (r.added || []).length, (r.updated || []).length, r.unchanged || 0,
      (r.reused || []).length, (r.missing || []).length, (r.unreachable || []).length, Object.keys(r.failed || {}).length);
    const failed = Object.entries(r.failed || {});
    $("#job-errors").innerHTML = failed.slice(0, 20).map(([k, v]) => `<div title="${esc(v)}">✕ ${esc(k.replace(/^@\d+\//, ""))}</div>`).join("")
      + `<button type="button" class="ghost small" id="btn-job-quality">${esc(t("qOpenReport"))}</button>`;
    $("#btn-job-quality").addEventListener("click", openQuality);
  } else if (j.status === "failed") {
    $("#job-errors").textContent = j.messages.slice(-1)[0] || "";
  }
  await loadInfo();
  await loadDocs();
  return j;
}

async function addFiles() {
  const collection = $("#add-collection").value.trim() || "general";
  try {
    if (desk()) {
      const paths = await desk().pick_files();
      if (!paths || !paths.length) return;
      const r = await postJSON("/api/sources/add", { paths, collection });
      await pollJob(r.job);
    } else {
      $("#add-file-input").click();
    }
  } catch (e) { alert(`${t("errorPrefix")}: ${e.message}`); }
}
async function uploadFiles(files) {
  const collection = $("#add-collection").value.trim() || "general";
  for (const f of files) {
    const fd = new FormData();
    fd.append("file", f);
    fd.append("domain", collection);
    try { const r = await (await api("/api/upload", { method: "POST", body: fd })).json(); await pollJob(r.job); }
    catch (e) { alert(`${t("errorPrefix")}: ${e.message}`); }
  }
}

function planHtml(plan) {
  const counts = (plan && plan.counts) || {};
  const order = ["new", "changed", "settings", "moved", "duplicate", "restored", "touched", "unchanged", "missing", "unreachable"];
  const rows = order.filter((k) => counts[k]).map((k) => `<li><b>${counts[k]}</b> ${esc(t("act_" + k))}</li>`).join("");
  return rows ? `<h3>${t("planTitle")}</h3><ul class="plan-list">${rows}</ul><p class="small muted">${t("apiNote")}</p>` : "";
}

function scanHtml(sc) {
  if (!sc.reachable) return `<div class="verify warning">${esc(t("notReachable", sc.error || sc.root))}</div>`;
  const rows = (sc.buckets || []).map((b) => `<tr><td>${esc(bucketName(b))}</td><td class="num">${b.documents}</td>
    <td class="small muted">${esc((b.folders || []).slice(0, 6).join(", "))}${(b.folders || []).length > 6 ? " …" : ""}</td></tr>`).join("");
  const notes = [];
  if (sc.unsupported_count) notes.push(`<li title="${esc((sc.unsupported || []).join("\n"))}">${esc(t("unsupportedN", sc.unsupported_count))}</li>`);
  if ((sc.empty_dirs || []).length) notes.push(`<li title="${esc(sc.empty_dirs.join("\n"))}">${esc(t("emptyDirsN", sc.empty_dirs.length))}</li>`);
  if ((sc.skipped_links || []).length) notes.push(`<li title="${esc(sc.skipped_links.join("\n"))}">${esc(t("linksN", sc.skipped_links.length))}</li>`);
  if ((sc.unreadable || []).length) notes.push(`<li class="bad" title="${esc(sc.unreadable.join("\n"))}">${esc(t("unreadableN", sc.unreadable.length))}</li>`);
  return `${rows ? `<table class="bucket-table"><thead><tr><th>${t("colBucket")}</th><th>${t("colDocs")}</th><th>${t("colFolders")}</th></tr></thead><tbody>${rows}</tbody></table>`
    : `<p>${t("nothingFound")}</p>`}<p class="small"><b>${esc(t("totalDocs", sc.documents))}</b></p>${notes.length ? `<ul class="small scan-notes">${notes.join("")}</ul>` : ""}`;
}

function confirmDialog(title, html, canGo) {
  return new Promise((resolve) => {
    const dlg = $("#dlg-folder");
    $("#folder-title").textContent = title;
    $("#folder-body").innerHTML = html;
    $("#folder-status").textContent = "";
    $("#btn-folder-go").disabled = !canGo;
    $$("#dlg-folder [data-i18n]").forEach((x) => { x.textContent = t(x.dataset.i18n); });
    const done = () => { dlg.removeEventListener("close", done); resolve(dlg.returnValue === "default"); };
    dlg.addEventListener("close", done);
    dlg.returnValue = "";
    dlg.showModal();
  });
}

async function addFolder() {
  let path = null;
  try {
    if (desk()) path = await desk().pick_folder();
    else path = prompt(t("folderPrompt"), "");
    if (!path) return;
    const pv = await postJSON("/api/folders/preview", { path });
    const ok = await confirmDialog(t("folderTitle", path), `<p class="small muted">${t("folderIntro")}</p>${scanHtml(pv.scan)}${pv.known ? planHtml(pv.plan) : ""}`,
      pv.scan.reachable && pv.scan.documents > 0);
    if (!ok) return;
    const r = await postJSON("/api/folders", { path });
    await pollJob(r.job, `${t("jobIndex")}: ${path}`);
  } catch (e) { alert(`${t("errorPrefix")}: ${e.message}`); }
}

async function rescan(rebuild) {
  try {
    const pv = await postJSON("/api/rescan/preview", {});
    const roots = pv.roots.filter((r) => !r.library || r.scan.documents || r.plan.counts.missing);
    const body = roots.map((r) => `<div class="rescan-root"><div class="small"><b>${esc(r.root)}</b></div>${r.scan.reachable ? planHtml(r.plan) : scanHtml(r.scan)}</div>`).join("");
    const ok = await confirmDialog(t("rescanTitle"), body || `<p>${t("nothingFound")}</p>`, true);
    if (!ok) return;
    const r = await postJSON("/api/rescan", { rebuild: !!rebuild });
    await pollJob(r.job, t("rescan"));
  } catch (e) { alert(`${t("errorPrefix")}: ${e.message}`); }
}

// ------------------------------------------------------------------ indexing quality report
// What the last indexing runs did and, per document, what its last processing produced. Counts only: no answer
// accuracy is claimed, and every ratio names its denominator.
const QFAIL = { failed: 0, partial: 1, none: 2, ok: 3 };
function fmtTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString(LANG === "tr" ? "tr-TR" : "en-GB");
}
function qIssue(it) {
  const fill = (s) => String(s).replace(/\{(\w+)\}/g, (_, k) => (it[k] ?? "") === "" ? "?" : (k === "pages" ? it[k].join(", ") : it[k]));
  const text = fill(t("qi_" + it.code));
  const act = it.code === "vlm_failed" && it.cut_off ? t("qa_vlm_cut") : t("qa_" + it.code);
  return { text, act: act && act !== "qa_" + it.code ? act : "" };
}
function qRunHtml(run) {
  const q = run.summary || {};
  const v = q.vlm || {}, tb = q.tables || {}, st = q.stage_failures || {}, em = q.embedding || {};
  const pct = tb.rows ? ` (%${Math.round((100 * tb.rows_verified) / tb.rows)})` : "";
  const lines = [t("qDocsLine", q.processed || 0, q.added || 0, q.updated || 0, q.skipped_unchanged || 0, q.reused || 0,
    q.partial || 0, q.failed || 0, q.unreachable || 0, q.missing || 0)];
  if (v.enabled === false) lines.push(t("qVlmOff"));
  else lines.push(t("qVlmLine", v.documents || 0, v.selected || 0, v.cached || 0, v.with_tables || 0, v.no_table || 0,
    v.failed || 0, v.filtered_out || 0, v.over_limit || 0));
  lines.push(t("qTablesLine", tb.extracted || 0, tb.grounded || 0, tb.not_grounded || 0));
  if (tb.rows) lines.push(t("qRowsLine", tb.rows_verified || 0, tb.rows, pct, tb.rows_unverified || 0));
  if (Object.values(st).some((n) => n)) lines.push(t("qStages", st.read || 0, st.parse || 0, st.embedding || 0, st.index_write || 0, st.other || 0));
  if (em.embedded || em.reused) lines.push(t("qEmb", em.embedded || 0, em.reused || 0));
  if ((q.unreachable_folders || []).length) lines.push(t("qUnreachableFolders", q.unreachable_folders.join("; ")));
  if (q.retry) {
    const res = Object.entries(q.retry).map(([k, r]) => `${k.replace(/^@\d+\//, "")}: ${t("qRes_" + (r.result || "ok"))}`);
    if (res.length) lines.push(t("qRetryDone", res.join(" · ")));
  }
  return lines.map((l) => `<p>${esc(l)}</p>`).join("");
}
function qDocHtml(d) {
  const q = d.quality;
  const st = q ? q.status : "none";
  const data = (q && q.data) || {};
  const head = `<div class="q-head"><span class="q-st ${st}">${esc(t("qSt_" + st))}</span><b>${esc(d.title)}</b>
    <span class="muted small">${esc(d.bucket || t("generalBucket"))} · ${esc(d.rel_path)}</span>
    ${q ? `<span class="muted small">${esc(t("qProcessed", fmtTime(q.processed_at)))}</span>` : ""}
    ${q && q.needs_reprocess ? `<span class="pill">${esc(t("qNeeds"))}</span>` : ""}</div>`;
  const items = [];
  if (!q) items.push(`<li class="muted">${esc(t("qNoRecord"))}</li>`);
  if (d.missing) items.push(`<li>${esc(t("qMissingFile"))}</li>`);
  if (q && q.status === "failed") items.push(`<li>${esc(q.index_kept ? t("qKept", fmtTime(q.last_ok_at)) : t("qNotIndexed"))}</li>`);
  const v = data.vlm || (data.previous || {}).vlm;
  if (q && v) {
    if (v.enabled === false) items.push(`<li class="muted">${esc(v.off_reason === "not_pdf" ? t("qVlmDocNotPdf") : t("qVlmDocOff"))}</li>`);
    else items.push(`<li class="muted">${esc(t("qVlmDoc", (v.selected || []).length, (v.with_tables || []).length, (v.no_table || []).length,
      Object.keys(v.failed || {}).length, v.filtered_out || 0, (v.over_limit || []).length))}</li>`);
  }
  const tb = data.tables || (data.previous || {}).tables;
  if (q && tb && tb.extracted) items.push(`<li class="muted">${esc(t("qTablesDoc", tb.extracted, tb.grounded || 0, tb.rows_verified || 0, tb.rows || 0))}</li>`);
  if (data.retry) items.push(`<li class="muted">${esc(t("qRetried", fmtTime(data.retry.at)))}</li>`);
  for (const it of (data.issues || []).filter((x) => x.code !== "vlm_disabled")) {
    const { text, act } = qIssue(it);
    const go = d.doc_id && d.pdf && !d.missing && it.page ? `<button type="button" class="ghost small goto" data-doc="${d.doc_id}" data-page="${it.page}">${esc(t("qGoto"))}</button>` : "";
    items.push(`<li>${esc(text)}${go}${act ? `<br><span class="q-act small">${esc(t("qAction"))} ${esc(act)}</span>` : ""}</li>`);
  }
  return `<div class="q-doc ${st}">${head}<ul>${items.join("")}</ul></div>`;
}
let QUALITY = null;
function renderQuality() {
  const r = QUALITY;
  if (!r) return;
  const run = $("#q-run");
  if (!r.available) {
    run.innerHTML = `<p>${esc(t("qUnavailable"))}</p>`;
  } else if (!r.runs.length) {
    run.innerHTML = `<p>${esc(t("qNone"))}</p>`;
  } else {
    const last = r.runs[0];
    run.innerHTML = `<h3>${esc(t("qLastRun"))}: ${esc(t("qKind_" + last.kind) !== "qKind_" + last.kind ? t("qKind_" + last.kind) : last.kind)} · ${esc(fmtTime(last.finished_at || last.started_at))} · ${esc(t("qRun_" + last.status))}</h3>${qRunHtml(last)}`
      + (r.runs.length > 1 ? `<details class="q-runs"><summary class="small">${esc(t("qRuns"))}</summary><ul>${r.runs.slice(1).map((x) =>
        `<li>${esc(fmtTime(x.finished_at || x.started_at))} · ${esc(t("qKind_" + x.kind))} · ${esc(t("qRun_" + x.status))} · ${esc(t("qDocsLine", ...["processed", "added", "updated", "skipped_unchanged", "reused", "partial", "failed", "unreachable", "missing"].map((k) => (x.summary || {})[k] || 0)))}</li>`).join("")}</ul></details>` : "");
  }
  const box = $("#q-retry");
  const plan = r.retry || [];
  box.hidden = !r.available;
  if (r.available) {
    const docs = plan.filter((p) => p.scope !== "folder");
    const pages = docs.filter((p) => p.scope === "pages");
    const lines = plan.length ? [t("qRetryScope", docs.length, pages.length, pages.reduce((a, p) => a + p.pages.length, 0), docs.length - pages.length)] : [t("qRetryNone")];
    const folders = plan.filter((p) => p.scope === "folder").length;
    if (folders) lines.push(t("qRetryFolders", folders));
    if (plan.length) lines.push(t("qRetryKeep"));
    box.innerHTML = `<h3>${esc(t("qRetryTitle"))}</h3>${lines.map((l) => `<p>${esc(l)}</p>`).join("")}
      <div class="row"><button type="button" id="btn-q-retry"${!plan.length || r.read_only ? " disabled" : ""}>${esc(t("qRetryTitle"))}</button></div>`;
    $("#btn-q-retry").addEventListener("click", retryFailed);
  }
  const all = $("#q-all").checked;
  const docs = (r.documents || []).slice().sort((a, b) => QFAIL[a.quality ? a.quality.status : "none"] - QFAIL[b.quality ? b.quality.status : "none"]
    || String(a.title).localeCompare(String(b.title)));
  const shown = docs.filter((d) => all || !d.quality || d.quality.status !== "ok" || d.missing || (d.quality.data.issues || []).some((i) => i.code !== "vlm_disabled"));
  $("#q-count").textContent = `${shown.length}/${docs.length}`;
  $("#q-docs").innerHTML = shown.map(qDocHtml).join("");
  $$("#q-docs .goto").forEach((b) => b.addEventListener("click", () => {
    $("#dlg-quality").close();
    showDocument(+b.dataset.doc, +b.dataset.page);
  }));
}
async function openQuality() {
  const dlg = $("#dlg-quality");
  if (!dlg.open) dlg.showModal();
  $("#q-status").textContent = "…";
  try {
    QUALITY = await getJSON("/api/quality");
    renderQuality();
    $("#q-status").textContent = "";
  } catch (e) { $("#q-status").textContent = `${t("errorPrefix")}: ${e.message}`; }
}
async function retryFailed() {
  try {
    $("#btn-q-retry").disabled = true;
    const r = await postJSON("/api/quality/retry", {});
    await pollJob(r.job, t("qRetryRunning"));
    if ($("#dlg-quality").open) await openQuality();
  } catch (e) { alert(`${t("errorPrefix")}: ${e.message}`); }
}

// ------------------------------------------------------------------ settings dialog
let SETTINGS = null;
function svcCard(svc) {
  const div = document.createElement("div");
  div.className = "svc";
  div.dataset.svc = svc;
  const s = SETTINGS[svc];
  const extra = svc === "vision" ? `<label><input type="checkbox" class="same"> ${t("sameAsChat")}</label>`
    : svc === "reranker" ? `<label><input type="checkbox" class="enabled"> ${t("enabled")}</label>` : "";
  div.innerHTML = `<h3>${t("svc_" + svc)}</h3>${extra}
    <label>${t("baseUrl")}<input class="url" value="${esc(s.base_url)}" placeholder="http://server:8000/v1"></label>
    <label>${t("apiKey")}<input class="key" type="password" value="${esc(s.api_key)}" autocomplete="off"></label>
    <label>${t("model")}<div class="row"><select class="model"></select><button type="button" class="ghost load">${t("modelsLoad")}</button><button type="button" class="ghost test">${t("test")}</button></div></label>
    <label class="insecure-row"><input type="checkbox" class="insecure"> ${t("insecure")}</label>
    <div class="result"></div>`;
  $(".insecure", div).checked = s.verify_ssl === false;
  const same = $(".same", div);
  if (same) {
    same.checked = !s.base_url && !s.model;
    const sync = () => $$(".url,.key,.model,.load,.test,.insecure", div).forEach((x) => { x.disabled = same.checked; });
    same.addEventListener("change", sync);
    sync();
  }
  const en = $(".enabled", div);
  if (en) en.checked = !!s.enabled;
  const sel = $(".model", div);
  const setOptions = (models) => {
    const cur = s.model;
    const opts = [...new Set([...(models || []), ...(cur ? [cur] : [])])];
    sel.innerHTML = `<option value=""></option>` + opts.map((m) =>
      `<option value="${esc(m)}" ${m === cur ? "selected" : ""}>${esc(m)}${models && !models.includes(m) ? " " + t("notOnServer") : ""}</option>`).join("");
  };
  setOptions(null);
  const load = async () => {
    const res = $(".result", div);
    res.className = "result";
    res.textContent = "…";
    try {
      const r = await postJSON("/api/settings/models", probe());
      s.model = sel.value || s.model;
      if (r.ok) { setOptions(r.models); res.textContent = `${r.models.length} model`; }
      else showError(res, r, t("noModels"));
    } catch (e) { res.className = "result bad"; res.textContent = e.message; }
  };
  const probe = (extra) => Object.assign({ service: svc, base_url: $(".url", div).value, api_key: $(".key", div).value,
    verify_ssl: !$(".insecure", div).checked }, extra || {});
  const showError = (res, r, prefix) => {
    res.className = "result bad";
    const hint = I18N[LANG]["err_" + r.code] || I18N.en["err_" + r.code];
    res.innerHTML = `<div class="hint-text">✕ ${esc(hint || prefix || "")}</div>` +
      (r.error ? `<details><summary>${t("details")}</summary>${esc(r.error)}</details>` : "");
    if (r.code === "tls_untrusted" && /^https:/i.test($(".url", div).value.trim())) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "ghost small";
      b.textContent = t("trustServer");
      b.addEventListener("click", () => trustFlow(res));
      res.append(b);
    }
  };
  const trustFlow = async (res) => {
    const base = $(".url", div).value.trim();
    const info = await postJSON("/api/settings/certificate", { base_url: base });
    if (!info.ok) { showError(res, info); return; }
    const leaf = info.chain[0];
    const panel = document.createElement("div");
    panel.className = "cert-panel";
    const rows = [[t("certSubject"), leaf.subject + (leaf.self_signed ? ` (${t("certSelf")})` : "")], [t("certIssuer"), leaf.issuer],
      [t("certNames"), (leaf.names || []).join(", ")], [t("certValid"), `${leaf.not_before} → ${leaf.not_after}`]];
    panel.innerHTML = `<b>${t("certTitle")}</b> — ${esc(info.host)}:${info.port}
      <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v || "—")}</dd>`).join("")}
      <dt>${t("certFp")}</dt><dd><code>${esc(leaf.sha256)}</code></dd></dl>
      <div>${info.verify.ok ? t("certAlreadyOk") : t("certCheck")}</div>
      <div class="row" style="margin-top:6px"><button type="button" class="do-trust">${t("trust")}</button><button type="button" class="ghost no-trust">${t("cancel")}</button></div>`;
    $(".no-trust", panel).addEventListener("click", () => panel.remove());
    $(".do-trust", panel).addEventListener("click", async () => {
      try {
        const r = await postJSON("/api/settings/trust", { base_url: base, sha256: leaf.sha256 });
        SETTINGS.tls = r.settings.tls;
        $("#set-tls-ca").value = SETTINGS.tls.ca_bundle;
        panel.remove();
        $("#settings-status").textContent = t("trusted", r.path);
        await load();
      } catch (e) { panel.innerHTML = `<span class="hint-text">${esc(e.message)}</span>`; }
    });
    res.append(panel);
  };
  $(".load", div).addEventListener("click", load);
  $(".insecure", div).addEventListener("change", load);
  $(".url", div).addEventListener("change", load);
  $(".key", div).addEventListener("change", load);
  $(".test", div).addEventListener("click", async () => {
    const res = $(".result", div);
    res.className = "result";
    res.textContent = "…";
    const r = await postJSON("/api/settings/test", probe({ model: sel.value }));
    if (r.ok) { res.className = "result ok"; res.textContent = `✓ ${r.detail || ""}${r.latency_ms ? ` (${r.latency_ms} ms)` : ""}`; }
    else showError(res, r, r.error || r.detail);
  });
  if (s.base_url && !(same && same.checked)) load();
  return div;
}
async function openSettings() {
  SETTINGS = await getJSON("/api/settings");
  const grid = $("#svc-grid");
  grid.innerHTML = "";
  for (const svc of SERVICES) grid.append(svcCard(svc));
  $("#set-thinking").value = SETTINGS.llm.thinking;
  $("#set-thinking-control").value = SETTINGS.llm.thinking_control;
  $("#set-tools").value = SETTINGS.llm.tools;
  $("#set-judge").checked = SETTINGS.answer.judge;
  $("#set-regenerate").checked = SETTINGS.answer.regenerate;
  $("#set-failed").value = SETTINGS.answer.failed_claims;
  $("#set-vision-enabled").checked = SETTINGS.vision.enabled;
  $("#set-tls-system").checked = SETTINGS.tls.system_store;
  $("#set-tls-proxy").checked = SETTINGS.tls.use_system_proxy;
  $("#set-tls-ca").value = SETTINGS.tls.ca_bundle || "";
  $("#settings-status").textContent = "";
  $$("#dlg-settings [data-i18n]").forEach((x) => { x.textContent = t(x.dataset.i18n); });
  $("#dlg-settings").showModal();
}
async function saveSettings(e) {
  e.preventDefault();
  const patch = { answer: { judge: $("#set-judge").checked, regenerate: $("#set-regenerate").checked, failed_claims: $("#set-failed").value } };
  for (const card of $$(".svc")) {
    const svc = card.dataset.svc;
    const same = $(".same", card);
    if (same && same.checked) { patch[svc] = { base_url: "", model: "", api_key: "" }; continue; }
    patch[svc] = { base_url: $(".url", card).value.trim(), api_key: $(".key", card).value, model: $(".model", card).value,
      verify_ssl: !$(".insecure", card).checked };
    const en = $(".enabled", card);
    if (en) patch[svc].enabled = en.checked;
  }
  Object.assign(patch.llm, { thinking: $("#set-thinking").value, thinking_control: $("#set-thinking-control").value, tools: $("#set-tools").value });
  patch.vision = Object.assign(patch.vision || {}, { enabled: $("#set-vision-enabled").checked });
  patch.tls = tlsPatch();
  try {
    const r = await postJSON("/api/settings", patch, "PUT");
    $("#settings-status").textContent = r.error ? `${t("errorPrefix")}: ${r.error}` : t("savedSettings");
    await loadInfo();
    await loadDocs();
    if (!r.error) setTimeout(() => $("#dlg-settings").close(), 500);
  } catch (err) { $("#settings-status").textContent = `${t("errorPrefix")}: ${err.message}`; }
}

function tlsPatch() {
  return { system_store: $("#set-tls-system").checked, use_system_proxy: $("#set-tls-proxy").checked,
    ca_bundle: $("#set-tls-ca").value.trim() };
}
async function applyTls() {
  try {
    const r = await postJSON("/api/settings", { tls: tlsPatch() }, "PUT");
    SETTINGS.tls = r.settings.tls;
    $("#settings-status").textContent = r.error ? `${t("errorPrefix")}: ${r.error}` : t("tlsApplied");
  } catch (e) { $("#settings-status").textContent = `${t("errorPrefix")}: ${e.message}`; }
}
async function pickCaFiles() {
  let paths = [];
  if (desk()) paths = (await desk().pick_files()) || [];
  else { const p = prompt(t("tlsCa"), ""); if (p) paths = [p]; }
  if (!paths.length) return;
  const cur = $("#set-tls-ca").value.split(/[;\n]/).map((x) => x.trim()).filter(Boolean);
  $("#set-tls-ca").value = [...new Set([...cur, ...paths])].join(";");
  await applyTls();
}

// ------------------------------------------------------------------ library dialog
async function pickFolder(input) {
  if (desk()) { const p = await desk().pick_folder(); if (p) input.value = p; }
  else { const p = prompt(t("pickFolderPrompt"), input.value); if (p) input.value = p; }
}
function openLibrary() {
  const i = state.info;
  $("#lib-path").textContent = i ? i.library : "";
  $("#lib-ro").hidden = !(i && i.read_only);
  $("#lib-new-path").value = i ? i.library : "";
  $("#lib-readonly").checked = !!(i && i.read_only);
  $("#lib-status").textContent = "";
  const legacy = (i && i.legacy_documents) || 0;
  $("#lib-legacy").hidden = !legacy || (i && i.read_only);
  $("#lib-legacy-text").textContent = legacy ? t("legacyNotice", legacy) : "";
  $$("#dlg-library [data-i18n]").forEach((x) => { x.textContent = t(x.dataset.i18n); });
  $("#dlg-library").showModal();
}

// ------------------------------------------------------------------ wiring
function bind() {
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const q = $("#question").value.trim();
    if (q && !state.busy) { $("#question").value = ""; ask(q); }  // while answering, the typed question stays
  });
  $("#question").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("#composer").requestSubmit(); } });
  $("#messages").addEventListener("click", (e) => {
    const c = e.target.closest("button.cite");
    if (!c || c.closest(".source-card")) return;
    const msg = c.closest(".msg");
    const s = (msg && msg._sources || []).find((x) => x.n === +c.dataset.n);
    if (s) showSource(s, msg, c.dataset.claim ? +c.dataset.claim : null);
  });
  $("#btn-new-chat").addEventListener("click", () => newChat(t("newChatStarted")));
  $("#btn-pending-cancel").addEventListener("click", () => setPending(null));
  $$("dialog [data-close]").forEach((b) => b.addEventListener("click", () => b.closest("dialog").close("cancel")));
  $$(".lang").forEach((b) => b.addEventListener("click", () => {
    LANG = b.dataset.lang;
    safeSet("techrag.lang", LANG);
    applyI18n();
    renderInfo();
    renderCollections();
    postJSON("/api/settings", { ui: { language: LANG } }, "PUT").catch(() => {});
  }));
  $("#btn-settings").addEventListener("click", () => openSettings().catch((e) => alert(e.message)));
  $("#btn-save-settings").addEventListener("click", saveSettings);
  ["#set-tls-system", "#set-tls-proxy", "#set-tls-ca"].forEach((id) => $(id).addEventListener("change", applyTls));
  $("#btn-pick-ca").addEventListener("click", pickCaFiles);
  $("#btn-library").addEventListener("click", openLibrary);
  $("#btn-pick-lib").addEventListener("click", () => pickFolder($("#lib-new-path")));
  $("#btn-pick-publish").addEventListener("click", () => pickFolder($("#lib-publish-path")));
  $("#btn-open-lib").addEventListener("click", async () => {
    try {
      const r = await postJSON("/api/library/open", { path: $("#lib-new-path").value, read_only: $("#lib-readonly").checked });
      $("#lib-status").textContent = r.error ? `${t("errorPrefix")}: ${r.error}` : t("opened");
      if (!r.error) {  // document ids belong to a library: nothing selected or cited in the old one carries over
        state.selected.clear();
        state.selectedBuckets.clear();
        newChat(t("newChatStarted"));
        $("#tab-source").innerHTML = `<p class="muted">${esc(t("viewerEmpty"))}</p>`;
        state.viewSeq++;
      }
      await loadInfo();
      await loadDocs();
      $("#lib-path").textContent = state.info.library;
    } catch (e) { $("#lib-status").textContent = `${t("errorPrefix")}: ${e.message}`; }
  });
  $("#btn-publish").addEventListener("click", async () => {
    try { const r = await postJSON("/api/library/publish", { dest: $("#lib-publish-path").value }); $("#lib-status").textContent = t("published", r.path); }
    catch (e) { $("#lib-status").textContent = `${t("errorPrefix")}: ${e.message}`; }
  });
  $("#btn-reindex-all").addEventListener("click", () => { $("#dlg-library").close(); rescan($("#lib-rebuild").checked); });
  $("#btn-migrate").addEventListener("click", async () => {
    $("#dlg-library").close();
    try { const r = await postJSON("/api/library/migrate", {}); await pollJob(r.job, t("migrate")); }
    catch (e) { alert(e.message); }
  });
  $("#btn-add-files").addEventListener("click", addFiles);
  $("#add-file-input").addEventListener("change", (e) => {
    const files = [...e.target.files];
    e.target.value = "";  // choosing the same file again (e.g. after a failed upload) must work
    uploadFiles(files);
  });
  $("#lib-new-path").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); $("#btn-open-lib").click(); } });
  $("#btn-add-folder").addEventListener("click", addFolder);
  $("#btn-rescan").addEventListener("click", () => rescan(false));
  $("#btn-quality").addEventListener("click", openQuality);
  $("#q-all").addEventListener("change", renderQuality);
  $("#btn-job-close").addEventListener("click", () => { $("#job-box").hidden = true; });
  $("#doc-filter").addEventListener("input", (e) => { state.filter = e.target.value; renderCollections(); });
  $("#btn-toggle-left").addEventListener("click", () => setLeftCollapsed(leftOpen()));
  $("#btn-collapse-left").addEventListener("click", () => setLeftCollapsed(true));
  $("#btn-toggle-right").addEventListener("click", () => setRightHidden(rightOpen()));
  $("#btn-close-right").addEventListener("click", () => setRightHidden(true));
  bindSplitter();
}

(async function init() {
  LANG = safeGet("techrag.lang") || LANG;
  bind();
  applyLayout();
  applyI18n();
  try {
    await loadInfo();
    applyI18n();
    await loadDocs();
  } catch (e) {
    const b = $("#banner");
    b.hidden = false;
    b.textContent = `${t("errorPrefix")}: ${e.message}`;
  }
})();
