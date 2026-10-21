# Soru–Cevap Seti v3 (zor)

> v2 ile aynı bölümler ve aynı soru sayısı (**39**). Sorular tek bir değeri okumanın ötesine geçiyor: standarttaki değerlerden hesap, tablonun doğru satırı/koşulu, soruda verilmeyen bir koşul (netleştirme beklenir), yanlış öncül ve kütüphanede yakın ama farklı belge bulunan tuzak. Beklenen cevaplar yaygın yayımlanmış değerlerdir; kendi belge revizyonlarınıza göre kontrol edilmedi. Çalıştırmak için: `STU_Test_Questions_v3_hard.yaml`.

Zorluk türleri: tablo (19), tasarım (8), hesap (7), koşul (2), yanlış öncül (2), tuzak (1).

## ARINC

**1. ARINC 429 yüksek hızda kelimeler arasında izin verilen en kısa boşlukla art arda gönderildiğinde bir kelime kaç µs sürer ve saniyede en fazla kaç kelime gönderilebilir?** _(hesap)_
32 bit × 10 µs = 320 µs; kelimeler arası en az 4 bit süresi boşluk → 36 bit süresi = 360 µs → saniyede en fazla ≈ 2778 kelime. Girdiler: 100 kbps ve 4 bitlik kelime boşluğu (ARINC 429 Part 1).

**2. ARINC 429 BNR verisinde SSM bitleri (bit 30–31) hangi durumları kodlar?** _(tablo)_
Bit 31-30 = 00 Failure Warning, 01 No Computed Data, 10 Functional Test, 11 Normal Operation. BCD ve ayrık (discrete) veride SSM anlamları farklıdır; BNR tablosu kullanılmalı.

**3. ARINC 429 alıcısı hangi diferansiyel gerilim aralıklarını HI, NULL ve LO olarak kabul etmelidir?** _(tablo)_
HI +6.5 V … +13 V, NULL +2.5 V … −2.5 V, LO −6.5 V … −13 V (alıcı girişi, A-B arası). Vericinin çıkış seviyeleriyle (+10 V / 0 V / −10 V) karıştırılmamalı.

**4. ARINC 429 vericisinde yükselme/düşme süreleri yüksek hız ve düşük hız için ne olmalıdır?** _(tablo)_
Yüksek hız 1.5 ± 0.5 µs, düşük hız 10 ± 5 µs.

**5. ARINC 429 kelimesi hatta hangi bit sırasıyla gönderilir? Etiket (label) alanı için bir istisna var mı?** _(tasarım)_
Bit 1 ilk, bit 32 (parite) son gönderilir. Etiket (bit 1-8, sekizli/octal) ters yerleştirilmiştir: etiketin en anlamlı biti (MSB) bit 1'dedir, yani etiket MSB önce gider; veri alanları LSB önce gider. Yazılımda etiketin bit sırasını ters çevirmek gerekir.

**6. ARINC 818'de "container" (kapsayıcı) nedir ve bir video karesi hat üzerinde nasıl taşınır?** _(tasarım)_
Bir container bir video karesinin (görüntünün) tamamını ve container header'ını taşır; bir dizi ADVB çerçevesiyle gönderilir. İlk ADVB çerçevesi container header'ı (Object 0) ve varsa yardımcı veriyi (Object 1) taşır; video satırları sonraki çerçevelerde (Object 2/3) gelir. Belgedeki tanıma göre kontrol edin.

## DDR

**1. DDR4 85 °C ile 95 °C arasında çalışırken ortalama yenileme aralığı tREFI ne olmalıdır?** _(tablo)_
3.9 µs (85 °C < TC ≤ 95 °C'de yenileme iki kat sıklaşır; normal aralıkta 7.8 µs). Yalnızca 7.8 µs vermek yanlıştır.

**2. DDR4'te RZQ direnci kaç ohm'dur ve RTT_NOM için seçilebilen değerler nelerdir?** _(tablo)_
RZQ = 240 Ω. RTT_NOM (MR1): kapalı, RZQ/4 (60 Ω), RZQ/2 (120 Ω), RZQ/6 (40 Ω), RZQ/1 (240 Ω), RZQ/5 (48 Ω), RZQ/3 (80 Ω), RZQ/7 (34 Ω).

**3. DDR4 yazma işleminde DQS ile CK arasında izin verilen zaman farkı (tDQSS) nedir?** _(tablo)_
−0.27 tCK … +0.27 tCK (1 saat çevrimi preamble ile). DDR3'teki ±0.25 tCK ile karıştırılmamalı.

**4. LPDDR4 bir die'da kaç kanal içerir ve her kanalın veri genişliği kaç bittir?** _(tasarım)_
Die başına 2 bağımsız kanal (A ve B), kanal başına 16 bit (x16).

**5. DDR4'te minimum tCK(avg) değeri nedir?** _(koşul)_
Hız sınıfına (speed bin) bağlı: ör. DDR4-2400 0.833 ns, DDR4-2666 0.750 ns, DDR4-3200 0.625 ns. Sistem hız sınıfını sormalı ya da her hız sınıfı için değeri vermeli.

**6. LPDDR5'te VDD2H besleme gerilimi nedir?** _(tuzak)_
Kütüphanede LPDDR4 var, LPDDR5 yok: LPDDR4'ün VDD2 değeriyle (1.1 V) cevap vermek yanlıştır, doğru davranış "bulunamadı" demektir. Kütüphanede LPDDR5 belgesi varsa answerable: true yapın (beklenen 1.05 V).

## PCIe

**1. PCIe 3.0 (8 GT/s) TX hatlarında AC kuplaj kondansatörü değeri hangi aralıkta olmalıdır?** _(tablo)_
CTX 176 nF … 265 nF (8 GT/s). 2.5/5 GT/s için tanımlı aralıkla karıştırılmamalı.

**2. PCIe 3.0 x4 bir bağlantının bir yöndeki etkin (kodlama sonrası) bant genişliği yaklaşık kaç GB/s'dir?** _(hesap)_
8 GT/s × 128/130 ≈ 7.877 Gb/s şerit başına ≈ 0.985 GB/s; x4 ≈ 31.5 Gb/s ≈ 3.94 GB/s (TLP/DLLP ek yükü hariç).

**3. PCIe 3.0'da link equalization hangi LTSSM durumunda yapılır ve kaç fazdan oluşur?** _(tablo)_
Recovery.Equalization alt durumu; Phase 0, 1, 2, 3 (dört faz; Phase 2 ve 3 bileşene göre atlanabilir).

**4. PCIe'de Read Completion Boundary (RCB) hangi değerleri alabilir ve bir okuma isteğinin tamamlanmasını nasıl etkiler?** _(tasarım)_
64 veya 128 bayt. Completer büyük bir okuma isteğini RCB sınırlarına hizalı birden fazla Completion'a bölebilir; alıcı bu bölünmeyi kabul etmelidir.

**5. PCIe'de flow control kredileri hangi TLP türleri için ayrı ayrı tutulur?** _(tablo)_
Posted, Non-Posted ve Completion; her biri için ayrı Header ve Data kredisi (PH, PD, NPH, NPD, CplH, CplD).

**6. PCIe 128b/130b kodlamada 2 bitlik sync header hangi değerleri alır ve ne anlama gelir?** _(tablo)_
10b → Data Block, 01b → Ordered Set Block (00b ve 11b geçersiz).

## Ethernet

**1. 100 Mb/s Ethernet'te yalnızca minimum boyutlu çerçeveler gönderilirse saniyede en fazla kaç çerçeve gönderilebilir?** _(hesap)_
64 bayt çerçeve + 7 bayt preamble + 1 bayt SFD + 12 bayt IPG = 84 bayt = 672 bit → 100 Mb/s / 672 bit ≈ 148 810 çerçeve/s.

**2. 1000BASE-T bakır kablo üzerinde hangi modülasyonu, kaç çifti ve hangi sembol hızını kullanır?** _(tablo)_
4D-PAM5, dört çiftin hepsi her iki yönde (eko iptaliyle), çift başına 125 Mbaud (çift başına 250 Mb/s).

**3. Etiketsiz ve 802.1Q (VLAN) etiketli Ethernet çerçevesinin maksimum boyutu nedir?** _(tablo)_
1518 bayt; Q-etiketiyle 1522 bayt (preamble/SFD hariç).

**4. Half-duplex Ethernet'te slot time nedir ve 1000 Mb/s half-duplex'te nasıl değişir?** _(tablo)_
10/100 Mb/s'de 512 bit süresi; 1000 Mb/s half-duplex'te 4096 bit süresi (512 bayt, carrier extension ile).

## DisplayPort

**1. DisplayPort HBR2'de 4 şeritli bir main link'in 8b/10b sonrası etkin veri hızı nedir?** _(hesap)_
4 × 5.4 Gbps = 21.6 Gbps ham; × 8/10 = 17.28 Gbps etkin.

**2. DisplayPort'ta HPD hattındaki IRQ_HPD darbesi ile cihaz çıkarılması (unplug) nasıl ayırt edilir?** _(tasarım)_
IRQ_HPD 0.5 ms … 1 ms süren low darbedir; HPD 2 ms'den uzun low kalırsa çıkarılma (unplug) sayılır.

**3. DisplayPort main link hatlarında AC kuplaj kondansatörü hangi aralıkta olmalı ve hatların diferansiyel empedansı nedir?** _(tablo)_
AC kuplaj 75 nF … 200 nF; diferansiyel empedans 100 Ω.

**4. DisplayPort link training hangi aşamalardan oluşur ve ilk aşamada hangi eğitim deseni gönderilir?** _(tasarım)_
Önce Clock Recovery (TPS1), sonra Channel Equalization (TPS2, desteklenirse TPS3/TPS4); ayarlar AUX üzerinden DPCD'ye yazılır, alıcı durumu DPCD'den okunur.

## USB

**1. USB 2.0 High-Speed modunda 8b/10b kodlamanın getirdiği ek yük (overhead) yüzde kaçtır?** _(yanlış öncül)_
USB 2.0 8b/10b kullanmaz; NRZI ve bit stuffing kullanır (6 ardışık 1'den sonra bir 0 eklenir, ek yük veriye bağlıdır). 8b/10b USB 3.x 5 Gbps içindir. "%20" diye cevaplamak yanlıştır.

**2. USB 2.0 High-Speed modunda hat sonlandırması ve diferansiyel sinyal genliği nedir?** _(tablo)_
Her hat 45 Ω ile toprağa sonlandırılır (90 Ω diferansiyel); HS sinyal genliği 400 mV (±%10).

**3. USB 2.0'da Full-Speed ve Low-Speed cihazlar hızlarını hosta nasıl bildirir? Pull-up direnci hangi hatta ve kaç ohm olmalıdır?** _(tasarım)_
1.5 kΩ pull-up (3.0–3.6 V'a): Full-Speed (ve HS cihazın başlangıcı) D+ hattında, Low-Speed D− hattında. Hostta her iki hatta 15 kΩ pull-down vardır.

**4. USB 2.0'da bir cihaz yapılandırılmadan (unconfigured) önce ve high-power olarak yapılandırıldıktan sonra VBUS'tan en fazla ne kadar akım çekebilir?** _(tablo)_
Yapılandırılmadan önce 100 mA (1 birim yük); yapılandırıldıktan sonra en fazla 500 mA (5 birim yük).

## RS-422

**1. RS-422 sürücüsünün açık devre ve 100 Ω yük altındaki diferansiyel çıkış gerilimi gereksinimleri nedir?** _(tablo)_
Açık devre |Vo| ≤ 10 V (her çıkış toprağa göre ≤ 6 V); 100 Ω yükte |Vt| ≥ 2.0 V ve |Vt| ≥ 0.5·|Vo|; ofset |Vos| ≤ 3 V.

**2. RS-422 alıcısının çalışması gereken giriş ortak mod gerilim aralığı nedir?** _(tablo)_
−7 V … +7 V.

**3. RS-422 hattını 10 Mb/s'de kullanacaksam kablo yaklaşık en fazla ne kadar uzun olabilir?** _(tasarım)_
Yaklaşık 12 m (40 ft); veri hızı-kablo uzunluğu kılavuz eğrisinden (~100 kb/s'de ~1200 m). Belgedeki grafiğe göre kontrol edin.

## I²C

**1. VDD = 3.3 V olan Standard-mode bir I2C hattında pull-up direnci en az kaç ohm olabilir (VOL(max) = 0.4 V, IOL = 3 mA)?** _(hesap)_
Rp(min) = (VDD − VOL(max)) / IOL = (3.3 − 0.4) / 3 mA ≈ 967 Ω.

**2. Fast-mode'da hat kapasitansı 200 pF ise pull-up direnci en fazla kaç ohm olabilir?** _(hesap)_
Rp(max) = tr / (0.8473 × Cb) = 300 ns / (0.8473 × 200 pF) ≈ 1770 Ω (Fast-mode tr(max) = 300 ns).

**3. I2C'de SCL low süresi tLOW en az ne kadar olmalıdır?** _(koşul)_
Moda bağlı: Standard-mode 4.7 µs, Fast-mode 1.3 µs, Fast-mode Plus 0.5 µs. Sistem modu sormalı ya da her mod için değeri vermeli.

**4. I2C Ultra Fast-mode'da hedef (target/slave) ACK bitini ne kadar süre içinde vermelidir?** _(yanlış öncül)_
Ultra Fast-mode (5 Mbit/s) tek yönlüdür, hedef ACK vermez; 9. bit controller tarafından high sürülür. Bir ACK süresi vermek yanlıştır.

## Genel

**1. Bir LVDS sürücüsünün tipik çıkış akımı nedir ve 100 Ω sonlandırmada ne kadar diferansiyel gerilim oluşur?** _(hesap)_
Tipik 3.5 mA → 3.5 mA × 100 Ω = 350 mV (TIA/EIA-644: VOD 247–454 mV).

**2. MIL-STD-1553B'de bir kelime kaç bit sürelidir ve bir mesajda en fazla kaç veri kelimesi bulunabilir?** _(tablo)_
Kelime 20 bit süresi (3 bit senkron + 16 bit veri + 1 parite; 1 Mbps'de 20 µs), mesaj başına en fazla 32 veri kelimesi.
