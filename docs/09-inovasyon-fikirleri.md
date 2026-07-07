# 09 — İnovasyon Fikirleri: Farklılaştırıcı Katman

Bu doküman, WherUGo temel planının (kişi takibi, dwell time, heatmap, bölge analitiği, kuyruk analitiği, POS entegrasyonu, VLM/LLM içgörü katmanı, KVKK uyumlu anonim mimari) **üzerine** inşa edilecek farklılaştırıcı fikirleri toplar. Buradaki hiçbir madde çekirdek yeteneklerin tekrarı değildir; her biri mevcut mimariye (kenar CV pipeline → anonim olay akışı → bulut analitik → AI içgörü → dashboard) eklenen ince bir katmandır ve yol haritasındaki bir faza etiketlenmiştir. Fikirler bir iç jüri tarafından etki, uygulanabilirlik ve farklılaşma eksenlerinde puanlanmış; örtüşenler birleştirilmiş, kriterleri karşılamayanlar dokümanın sonunda gerekçeleriyle listelenmiştir.

## Özet Tablo

| Fikir | Tek cümle | Etki | Faz |
|---|---|---|---|
| Kayıp Satış Radarı | Yüksek ilgi + sıfır satış noktalarını TL cinsinden kayıp tahminiyle raporlar | 9 | MVP |
| Deneme Kabini Dönüşüm Hunisi | Kabin girişi → satın alma hunisini ölçer, kabin kuyruğunda anlık uyarı verir | 8 | MVP |
| Yarın Radarı | Türkiye takvimine duyarlı trafik tahmini ve vardiya önerisi | 8 | MVP (tahmin) / Faz 2 (optimizasyon) |
| KVKK Kanıt Ajanı | Uyumu imzalı teknik kanıta ve otomatik dokümana çevirir | 8 | MVP |
| Raf-as-Media | Marka standı performansını tedarikçiye satılabilir sertifikalı rapora dönüştürür | 8 | Faz 2 |
| Vardiya Copilotu | 30-60 dk ufukta yoğunluk tahmini yapıp WhatsApp'tan eylem planı öneren ajan | 8 | Faz 2 |
| Alışverişçi DNA'sı | Anonim ziyaret yörüngelerinden davranışsal persona taksonomisi çıkarır | 7 | Faz 2 |
| Raf Sağlığı Nöbetçisi | Kenar-VLM ile boş raf / planogram sapması denetimi, trafikle korelasyon | 8 | Faz 2 |
| WhatsApp Analitik Asistanı | Doğal dilde soru-cevap ve günlük brifingi müdürün yaşadığı kanala taşır | 7 | Faz 2 |
| Uygula ve Ölç Deney Motoru | Mağaza içi değişiklikleri önce/sonra karşılaştırmalı karneye bağlar | 8 | Faz 2 |
| Vitrin Skoru | Capture rate ölçümüyle vitrini 0-100 skorlu bir oyuna çevirir | 7 | Faz 2 |
| Benchmark Havuzu | Ver-al modeliyle anonim sektör kıyaslaması ve veri ağ etkisi | 8 | Faz 2 |
| AVM Ortak Alan Modülü | Aynı pipeline ile GYO/AVM yönetimi segmentine açılım | 8 | Faz 2 |
| Açık Olay API'si | Anonim olay akışını üçüncü taraflara açan platform katmanı | 7 | Faz 2 |
| Türkiye Perakende Trafik Endeksi | Havuz verisinden kamuya açık endeks + finans sektörüne veri ürünü | 7 | Faz 3+ |

---

## MVP — Hızlı Kazanımlar

### 1. Kayıp Satış Radarı: "İlgilendi ama Almadı" Alarmı

**Ne:** Bir raf veya bölgede yüksek dwell time ve yoğun ilgi ölçülmesine rağmen POS tarafında o kategoride satış gerçekleşmiyorsa sistem otomatik "kayıp satış" alarmı üretir. AI içgörü katmanı olası kök nedeni önerir: eksik fiyat etiketi, biten stok, olmayan beden/varyant, rakibe göre yüksek fiyat. Mağaza müdürü sabah raporunda "dün en çok ilgi görüp en az satan 5 nokta"yı tahmini TL kaybıyla görür.

**Neden değerli:** Mağaza sahibi parayı nerede masada bıraktığını doğrudan görür. Düzeltilen her sorunlu raf ölçülebilir dönüşüm artışıdır; "ilgi var, satış yok" açığının %10-20'sinin geri kazanımı bile POS'ta kanıtlanabilir net ciro artışıdır. Satış demosunda sözleşmeyi imzalatan ekran budur.

**Nasıl çalışır:** Dwell time, bölge analitiği ve POS entegrasyonu zaten planda; eklenen tek şey bulut analitikte bir korelasyon/eşik motoru ve LLM tabanlı kök neden önerisi katmanı. Kurulumda bölgelere kategori etiketi girilmesi yeterli; ek donanım yok.

**Faz önerisi:** MVP. Causal ML ile kök neden derinleştirmesi Faz 2'ye bırakılır.

**Riskler / önkoşullar:** Raf-kategori eşlemesinin doğruluğu kurulum kalitesine bağlı; TL kayıp tahmini "tahmin" olarak etiketlenmeli, kesinlik iddiası taşımamalı.

### 2. Deneme Kabini Dönüşüm Hunisi (Moda Dikeyi Paketi)

**Ne:** Kabin bölgesi girişleri, kabin önü bekleme süresi ve doluluk takip edilir; kabin girişleri POS ile eşlenerek "kabine giren → satın alan" huni metriği çıkar. Kabin önünde kuyruk oluştuğunda veya bekleyip vazgeçen müşteri örüntüsü görüldüğünde personele anlık uyarı gider. Kabin içi asla izlenmez; yalnızca bölge giriş/çıkış olayları sayılır.

**Neden değerli:** Modada kabine giren müşterinin dönüşümü mağaza ortalamasının 3-4 katıdır; kabin kuyruğunda vazgeçen her müşteri neredeyse kesin kayıp satıştır. "Kabin kuyruğu kaynaklı aylık X TL kayıp" rakamını gösterip geri kazanımı POS'ta kanıtlamak, moda dikeyi için çok net bir satış argümanıdır.

**Nasıl çalışır:** Mevcut bölge analitiği ve kişi takibiyle yapılır; kabin koridoruna bakan bir kamera açısı ve kabin bölgesinin sanal çizimi yeterli. Anlık uyarı için kenar cihazdan mağaza tabletine/telefonuna basit bildirim kanalı eklenir.

**Faz önerisi:** MVP — aynı zamanda ilk dikey paketleme (moda) stratejisinin çekirdeği.

**Riskler / önkoşullar:** Kabin içine kamera olmadığının ve kimliklendirme yapılmadığının aydınlatma metninde açıkça yazılması; kamera açısının kabin kapılarını görmeyecek şekilde ayarlanması.

### 3. Yarın Radarı: Türkiye Takvimine Duyarlı Trafik Tahmini + Vardiya Optimizasyonu

**Ne:** Saatlik ve bölge bazlı ziyaretçi tahmini yapan zaman serisi temel modeli (TimesFM/TimeGPT sınıfı, mağaza verisiyle fine-tune) dış sinyallerle beslenir: hava durumu, Ramazan/bayram/arefe, maaş ve emekli maaşı günleri, okul takvimi, yerel etkinlikler. Üzerine OR-Tools tabanlı kısıtlı vardiya optimizasyonu personel çizelgesi önerir; LLM gerekçeyi açıklar ("Perşembe arefe, öğleden sonra %60 trafik artışı bekleniyor").

**Neden değerli:** Yetersiz personelden doğan kuyruk kaybı ile fazla personelden doğan işçilik israfı aynı anda azalır — perakendede işçilik cironun %8-12'sidir, ROI hikayesi güçlüdür. Ramazan saat kayması ve maaş günü zirveleri global rakiplerin hazır modellerinin kör noktasıdır; yerel satış farklılaştırıcısıdır.

**Nasıl çalışır:** Giriş sayım verisi ilk günden birikir; tahmin servisi bulutta çalışır, kenara yük binmez. Zaman serisi foundation modelleri 4-6 haftalık veriyle makul doğruluk verir. Hava/takvim API'leri ucuzdur; personel kısıtları basit bir formla girilir.

**Faz önerisi:** Tahmin katmanı MVP; OR-Tools vardiya optimizasyonu Faz 2; WFM sistemlerine API entegrasyonu geç faz uzantısı.

**Riskler / önkoşullar:** İlk haftalarda soğuk başlangıç doğruluğu düşük olabilir — güven aralığıyla sunulmalı; vardiya önerisi "öneri" olarak konumlanmalı, iş hukuku algısı yönetilmeli.

### 4. KVKK Kanıt Ajanı: Uyumluluğu Ürünleştirme

**Ne:** Pipeline'ı sürekli denetleyen ve uyumu kanıta çeviren ajan: kenar cihazdan imzalı teknik atestasyon üretir ("bu cihazdan hiçbir görüntü karesi dışarı çıkmadı, yüz bölgesi işlenmedi" — hash'li olay loguyla), VERBİS envanterini, aydınlatma metnini ve veri akış şemasını mağazanın gerçek konfigürasyonundan LLM ile otomatik üretir ve her değişiklikte günceller. Denetimde tek tıkla tarih damgalı uyum dosyası çıkar.

**Neden değerli:** Türkiye'de kamera analitiği satışının bir numaralı itirazı KVKK korkusudur; bu ajan itirazı satış avantajına çevirir ve satış döngüsünü kısaltır. Hukuk/uyum ekiplerinin haftalar süren dokümantasyon işi dakikalara iner; "denetlenebilir gizlilik" hiçbir rakipte ürünleşmiş değildir — jüride farklılaşma puanı 10 alan tek fikir.

**Nasıl çalışır:** Kenar ajanına imzalı telemetri/log modülü eklenir (küçük geliştirme); doküman üretimi mevcut LLM katmanının şablonlu uygulamasıdır.

**Faz önerisi:** MVP — ilk sürümden itibaren satış farklılaştırıcısı.

**Riskler / önkoşullar:** Hukuki şablonların bir KVKK danışmanıyla bir kez doğrulanması gerekir; üretilen dokümanların "hukuki tavsiye değildir" çerçevesi net çizilmeli.

---

## Faz 2 — Derinleştirme ve Platformlaşma

### 5. Raf-as-Media: Tedarikçi Standı Performans Sertifikası

**Ne:** Marka standları ve ikincil teşhir alanları için tedarikçiye satılabilir performans raporu: standın önünden geçen kişi, duran kişi, ortalama ilgi süresi ve POS'tan markanın satış artışı. Mağaza, stand alanını doğrulanmış erişim rakamlarıyla "reklam panosu gibi" kiralar; WherUGo raporu bağımsız ölçüm sertifikası işlevi görür.

**Neden değerli:** Mağazaya maliyet düşürme değil **yeni gelir kalemi** açan tek fikir: FMCG markaları doğrulanmış teşhir performansına bugünkü sabit stand bedellerinin üstünde ödemeye isteklidir. Tek bir stand anlaşmasının ek geliri WherUGo aboneliğini karşılayabilir — churn'e karşı en güçlü savunma. Platform için rapor başına revenue share modeli doğar; retail media ölçümünde Türkiye'de boşluk vardır.

**Nasıl çalışır:** Bölge analitiği ve POS korelasyonu mevcut; eklenen, stand bölgelerinin markayla etiketlenmesi ve beyaz-etiketli PDF/portal rapor katmanı. Mağazaya hazır "tedarikçi sunum şablonu" verilir.

**Faz önerisi:** Faz 2.

**Riskler / önkoşullar:** Veriler tamamen anonim ve agrega olduğundan üçüncü tarafla paylaşımda KVKK sorunu yok; raporun "sertifika" güvenilirliği için metodoloji dokümantasyonu şart.

### 6. Vardiya Copilotu: Proaktif Operasyon Ajanı

**Ne:** Canlı anonim olay akışını izleyen, 30-60 dakikalık ufukta kuyruk/yoğunluk tahmini yapan (TFT/N-HiTS sınıfı hafif model) ve mağazanın operasyon kurallarını bilen bir LLM ajanı. Alarm üretmekle kalmaz plan önerir: "Kasa kuyruğu 25 dk içinde 6 kişiyi aşacak; depodaki personeli 14.30'da 2. kasaya yönlendir." Öneriler WhatsApp/Telegram'dan vardiya sorumlusuna gider; sorumlu tek dokunuşla "yaptım/yapamam" der, ajan sonucu ölçüp kendini kalibre eder.

**Neden değerli:** Dashboard'a bakma zorunluluğunu kaldırır; kuyruk kaynaklı terk oranını ölçülebilir şekilde düşürür. Rakiplerin "gerçek zamanlı alarm"ı pasiftir; buradaki fark kapalı döngüdür: tahmin → eylem → sonuç ölçümü.

**Nasıl çalışır:** Kuyruk analitiği ve olay akışı mevcut; bulutta tahmin mikroservisi ve mevcut içgörü katmanının tool-use'lu ajan orkestrasyonu eklenir. Yalnızca sayısal akış kullanılır; KVKK etkisi yoktur.

**Faz önerisi:** İki adım: kendini ayarlayan eşikli anlık uyarılar MVP-yakını; tahmin ufuklu tam ajan Faz 2.

**Riskler / önkoşullar:** WhatsApp Business API onayı; öneri kalitesi düşükse "spam algısı" — kalibrasyon döngüsü ve günlük mesaj limiti şart.

### 7. Alışverişçi DNA'sı: Anonim Davranışsal Persona Motoru

**Ne:** Her ziyaretin yörünge dizisi (giriş → bölgeler → duraklamalar → çıkış) self-supervised bir sekans encoder'la (trajectory2vec / küçük transformer) gömlemeye çevrilir ve kümelenir: "hedefli avcı" (3 dk, tek reyon, satın alma), "gezgin" (20 dk, 8 bölge), "vitrin turisti" gibi kimliksiz personalar. LLM kümeleri adlandırır, agrega POS profiliyle ilişkilendirir, persona karışımının gün/saat/kampanyaya göre değişimini raporlar.

**Neden değerli:** Perakendeci "bugün 1.200 kişi girdi" yerine "gezgin oranı %31'den %44'e çıktı ama dönüşmüyor" düzeyinde içgörü alır. V-Count/Vemcount sayım verir, davranış taksonomisi vermez. Persona çıktısı Deney Motoru ve playbook'ları besleyen ara katmandır.

**Nasıl çalışır:** Girdi (anonim ziyaret yörüngeleri) mimaride zaten üretiliyor; buluta haftalık batch embedding + clustering pipeline'ı ve LLM etiketleme adımı eklenir — düşük maliyet.

**Faz önerisi:** Faz 2 (görece hızlı kazanım).

**Riskler / önkoşullar:** Ziyaret embedding'i oturum sonunda kimliksizleştirilip yalnızca küme istatistiği saklanmalı — kalıcı kimlik tutulmadığı için KVKK'da profilleme riski doğmaz; kişi değil ziyaret segmentlenir.

### 8. Raf Sağlığı Nöbetçisi: Kenar-VLM ile Uygulama Denetimi

**Ne:** Kenar cihazdaki quantize kompakt VLM (MiMo-VL sınıfı), müşteri yokken raf bölgelerinin anlık karesini alıp yapılandırılmış JSON üretir: boş raf gözü, devrik ürün, planogram sapması, yerde koli. Raf durumu olayları trafik/dönüşüm verisiyle korele edilir: "X rafı bugün 3 saat boş kaldı, o saatlerde bölge dönüşümü %18 düştü." Görüntü buluta gitmez; yalnızca metin/JSON çıkar, insan içeren kareler atlanır.

**Neden değerli:** Zincir merkezine mağaza uygulama denetimini insansız ve sürekli hale getirir; sektörde ciro kaybının ~%4'ü raf boşluğu (OOS) kaynaklıdır ve burada kayıp, trafik verisiyle fiyatlandırılır. Saha denetçi maliyetini düşürür; trafik ağırlıklı önceliklendirme gerçek farktır.

**Nasıl çalışır:** Kameralar ve kenar cihaz mevcut; Jetson Orin sınıfı cihazda 2-4B parametreli quantize VLM, CV pipeline'la zaman paylaşımlı dakikada birkaç kare işler. Planogram referansı için basit raf tanımlama arayüzü gerekir.

**Faz önerisi:** Faz 2.

**Riskler / önkoşullar:** Kenar cihazda VLM inference kapasitesi ve olası ek kamera maliyeti (jüri feasibility'yi bu yüzden 7 verdi); insan tespitinde kare atlama kuralı KVKK riskini sıfırlar.

### 9. WhatsApp Analitik Asistanı

**Ne:** LLM katmanına bağlı WhatsApp Business botu: doğal dilde soru ("dün öğlen kaç kişi girdi?", "bu haftanın en ölü saati ne?"), mini grafik görselleriyle yanıt, her sabah zamanlanmış "Günaydın Brifingi" ve kritik uyarılar — hepsi aynı kanaldan.

**Neden değerli:** Türkiye'de mağaza müdürünün fiilen yaşadığı kanal WhatsApp'tır; dashboard açma bariyerini sıfırlar, çok şubeli KOBİ zincirlerinde benimsemeyi hızlandırır ve churn'ü düşürür. Hedef: kanalı açan hesaplarda haftalık etkileşimin 3 katına çıkması.

**Nasıl çalışır:** Doğal dil sorgu yeteneği AI içgörü katmanında zaten var; eklenen WhatsApp Business API entegrasyonu, NL-to-query köprüsü ve grafik render servisidir. Yalnızca agrega anonim veri paylaşılır; KVKK riski yok, mesaj başı maliyet düşük.

**Faz önerisi:** Faz 2 — Vardiya Copilotu ile aynı kanal altyapısını paylaşır.

**Riskler / önkoşullar:** Meta API onay süreci tek operasyonel risk; alternatif olarak Twilio benzeri sağlayıcı yedeklenmeli.

### 10. "Uygula ve Ölç" Deney Motoru (Mağaza İçi A/B Koçu)

**Ne:** Müdür veya merchandising ekibi bir değişikliği (masa yeri, yeni tabela, vitrin yenileme) tek ekranla kaydeder: hangi bölge, ne değişti, ne zamandan itibaren. Platform öncesi/sonrası dwell, uğrama, dönüşüm ve POS satışını kontrol bölgeleriyle karşılaştırıp 7-14 gün sonra net karne verir: "İşe yaradı: bölge uğrama +%23." Sonuç kartlarında "Ne-Yapmalı" playbook önerileri sunum katmanı olarak yer alır.

**Neden değerli:** "Değişiklik yaptık ama işe yaradı mı bilmiyoruz" problemini çözer; ürünü rapor aracından karar motoruna yükseltir. Her deney sonucu geri dönmek için doğal neden yarattığından retention motorudur; hedef: aktif mağaza başına ayda 2+ deney.

**Nasıl çalışır:** Ölçüm tamamen mevcut bölge analitiği + POS verisiyle; yeni olan deney metadata modeli, fark-içinde-fark benzeri basit istatistik ve sonuç bildirimi. CV pipeline'da değişiklik yok. Kampanya Karnesi aynı altyapının bir şablonudur.

**Faz önerisi:** Faz 2. Otonom deney ajanı ve sentetik kontrol, bu motorun geç faz evrimleri olarak beklemede.

**Riskler / önkoşullar:** İstatistiksel gücün düşük olduğu küçük mağazalarda sonuçların güven aralığıyla sunulması; deney disiplinini teşvik edecek UX.

### 11. Vitrin Skoru ve Vitrin Değişiklik Günlüğü

**Ne:** Girişe bakan kameradan, önünden geçen anonim kişi sayısına karşı içeri giren oranı (capture rate) ölçülür; her vitrin düzenine 0-100 Vitrin Skoru verilir. Müdür vitrin değişikliklerini fotoğrafla günlüğe kaydeder; platform düzenleri karşılaştırır, skor düşünce uyarır. Zincirlerde en iyi skorlu vitrin fotoğrafları şubeler arası paylaşılır.

**Neden değerli:** Vitrin, perakendenin en çok içgüdüyle yönetilen alanıdır; skor onu ölçülebilir bir oyuna çevirir. Cadde/AVM mağazasında capture rate doğrudan ciro kolonudur; zincir içi en iyi pratik yayılımı bonus değerdir.

**Nasıl çalışır:** Mevcut YOLO+ByteTrack pipeline'ı giriş hattı sayımı için yeterli; geçen-giren ayrımı için giriş kamerası açı ayarı veya ilave ucuz bir kamera gerekebilir. Deney Motoru ile doğal entegrasyon: her vitrin değişikliği bir deney kaydıdır.

**Faz önerisi:** Faz 2.

**Riskler / önkoşullar:** KVKK açısından kritik kural: dış alan görüntüsü işlenmez, yalnızca anonim geçiş sayısı kenarda çıkarılır ve görüntü cihazı terk etmez; vitrin fotoğrafları insansız çekim kuralıyla yüklenir.

### 12. WherUGo Benchmark Havuzu: "Ver-Al" Modeliyle Anonim Sektör Kıyaslama

**Ne:** Opt-in mağazaların agrega metrikleri (trafik, dönüşüm, dwell, kuyruk süresi) kategori + şehir + mağaza büyüklüğü kırılımında yüzdelik dilimlere dönüştürülür: "Dönüşümün %2,1; benzer AVM içi giyim mağazalarında medyan %3,4." Erişim şartı veri katkısıdır (give-to-get); katılmayan kıyas göremez. Çoklu şube karnesi aynı altyapının zincir içi görünümüdür.

**Neden değerli:** Mağaza sahibine "iyi miyim, kötü müyüm?" sorusunun ilk objektif cevabı; WherUGo'ya klasik veri ağ etkisi — her yeni müşteri ürünü herkes için değerli kılar, rakibe geçişi zorlaştırır. Kopyalanamaz veri hendeği; stratejik olarak en değerli platform fikri. Benchmark erişimi premium katman olarak fiyatlanabilir.

**Nasıl çalışır:** Bulut analitikteki mevcut agrega metrikler üzerine bir toplama servisi; ham olay verisi paylaşılmaz, yalnızca mağaza-düzeyi agregalar havuzlanır. Mağaza taksonomisi (kategori/lokasyon etiketi) ve katılım sözleşmesi maddesi gerekir.

**Faz önerisi:** Faz 2 — veri birikimi MVP'den itibaren başlamalı.

**Riskler / önkoşullar:** Kritik kütle riski (feasibility bu yüzden 7); soğuk başlangıç için veri katkısına abonelik indirimi. KVKK/rekabet açısından k-anonimlik kuralı şart: kırılımda 5-10'dan az mağaza varsa o dilim gösterilmez.

### 13. AVM Ortak Alan Modülü: GYO/AVM Segmentine Açılım

**Ne:** Aynı kenar cihaz + CV pipeline AVM ortak alanlarına (girişler, koridorlar, yemek katı, etkinlik alanı) kurulur. AVM yönetimi kat/koridor trafiği, etkinlik çekim gücü, giriş kapısı dağılımı ve "ölü koridor" analizini görür; kiralama ekibi vitrin önü trafiğini kira pazarlığında veri olarak kullanır.

**Neden değerli:** Mağaza başına değil AVM başına fiyatlanan, çok daha yüksek sepetli yeni segment (Türkiye'de 450+ AVM). Ciro kirası doğrulaması ve boş dükkan kiralama sunumlarında gerçek trafik verisi — bugün tahminle yapılan iş. Ayrıca her AVM anlaşması, içindeki onlarca kiracıya mağaza-içi ürün satışı için sıcak kanal açar.

**Nasıl çalışır:** CV pipeline değişmeden çalışır; yeni olan "ortak alan" bölge tipolojisi ve AVM'ye özel dashboard görünümleridir. Kiracı-AVM veri takası geç faz uzantısıdır.

**Faz önerisi:** Faz 2.

**Riskler / önkoşullar:** Kamera sayısı mağazadan yüksek olduğundan kenar donanım maliyeti planlanmalı (kamera başı maliyet optimizasyonu); KVKK tarafı mağazayla aynıdır: anonim takip + aydınlatma levhaları.

### 14. Açık Olay API'si ve Entegrasyon Pazarı

**Ne:** Anonim olay akışı (giriş sayımı, bölge doluluk, kuyruk uzunluğu) webhook ve REST/stream API olarak üçüncü taraflara açılır. İlk senaryolar: dijital tabela/DOOH içeriğini yoğunluğa göre değiştirme, ERP'ye trafik beslemesi, klima/aydınlatma otomasyonu, kuyruk uzayınca kasiyer çağrısı. Onaylı entegrasyonlar zamanla bir marketplace'te listelenir, gelir paylaşılır.

**Neden değerli:** WherUGo'yu üründen platforma taşır: her entegrasyon geçiş maliyetini yükseltir, yerel yazılımevleri ve sistem entegratörleri satış kanalına dönüşür. Müşteri tek kamerayla üç ayrı sistemin (sayaç, sensör, tabela tetikleyici) yerini alır — donanım konsolidasyonu somut tasarruftur. Elenen enerji optimizasyonu fikrinin değeri de sıfır ürün yüküyle bu API üzerinden ortaklara devredilir.

**Nasıl çalışır:** Mimarideki anonim olay akışı zaten API'nin kendisidir; gereken API gateway, anahtar yönetimi, oran limitleme ve geliştirici dokümantasyonudur — düşük efor.

**Faz önerisi:** API çekirdeği Faz 2; marketplace sonraki aşama.

**Riskler / önkoşullar:** Kritik tasarım kuralı: API yalnızca agrega/sayım olayları taşır, track-ID'ler dışarı sızmaz — yeniden kimliklendirme vektörü kapalı kalır.

---

## Faz 3+ — Vizyon

### 15. Türkiye Perakende Trafik Endeksi: Kamuya Açık Veri Ürünü

**Ne:** Benchmark havuzundaki anonim trafik verisinden aylık kamuya açık bir endeks yayınlanır ("Mart'ta AVM içi giyim trafiği yıllık %4 düştü"); il/kategori/hafta kırılımlı detaylar bankalara, GYO'lara, yatırım fonlarına ve pazar araştırma şirketlerine ücretli rapor/API olarak satılır.

**Neden değerli:** Mağaza sahibi olmayan tamamen yeni bir müşteri segmenti (finans, GYO, medya) ve sıfır marjinal maliyetli gelir. Kamuya açık endeks aynı zamanda en ucuz pazarlama kanalıdır: her ay ekonomi basınında marka görünürlüğü. Placer.ai bu modeli ABD'de kanıtladı; Türkiye'de boşluk var.

**Nasıl çalışır:** Benchmark havuzu altyapısı üzerine yayınlama katmanı; LLM içgörü katmanı endeks yorumunu otomatik yazar.

**Faz önerisi:** Faz 3+ — benchmark havuzu kurulduysa doğal devamı; bugünden vizyon slaytında durmalı.

**Riskler / önkoşullar:** İstatistiksel temsiliyet için muhtemelen 200+ mağaza tabanı (feasibility'nin düşük olma nedeni), mevsimsellik düzeltmesi, veri satış sözleşmelerinde "agrega, geri çözülemez" garantisi. Bireysel mağaza verisi asla ifşa edilmez.

---

## Elenen Fikirler ve Nedenleri

Şeffaflık için jüri değerlendirmesinde elenen fikirler ve gerekçeleri:

- **Kayıp Önleme Anomali Uyarıları:** Kimliksiz de olsa "şüpheli davranış" tespiti fiilen bireyi hedefler; "asla bireysel takip" konumlandırmasını çelişkiye sokar, yanlış pozitifler masum müşteriye müdahaleye yol açar. Marka riski değerinden büyük.
- **Dijital İkiz + What-If Motoru:** Kalibre edilmemiş simülasyonun ciro tahmini doğrulanamaz vaat; ağır R&D. Deney Motoru aynı soruyu gerçek veriyle daha ucuz cevaplar.
- **Bağlam Zenginleştirici (VLM nitelikleri):** Çocuklu aile / tekerlekli sandalye / grup çıkarımı KVKK'da özel nitelikli veri ve profilleme sınırına dayanır. Güvenli alt parçalar (ör. showrooming sinyali) ileride tek tek değerlendirilebilir.
- **Otonom Deney Ajanı:** Prematüre; manuel deney alışkanlığı oturmadan otonom hipotez ajanı hayaldir. Deney Motoru'nun doğal evrimi olarak beklemede.
- **Sentetik Kontrol Ağı:** Benchmark havuzu + deney motoru önkoşullu geç faz uzantısı; ayrı fikir olarak taşınmadı, yol haritasına not düşüldü.
- **Enerji/İklimlendirme Optimizasyonu:** Çekirdek değer öneriden uzak scope creep; BMS entegrasyon yükü yüksek, uzman rakipler var. Doluluk verisi Açık API üzerinden ortaklara açılarak aynı değer sıfır ürün yüküyle yakalanır.
- **Mağaza Ligi (Gamification):** Tek başına satın alma nedeni değil; "çalışan performans izleme" algısı iş hukuku/sendika riski taşır. İleride WhatsApp özetine küçük bir kıyas satırı olarak eklenebilir.
- **İçgörü Şablon Pazarı:** İki taraflı pazar, arz tarafı (danışman ekosistemi) yokken kurulamaz; platform olgunluğundan yıllar uzakta.
- **Lokasyon Karnesi:** Güçlü gelir fikri ama benchmark havuzu + 200'lü mağaza tabanı önkoşullu; dış veri lisans maliyeti belirsiz. Havuz kritik kütleye ulaşınca yeniden değerlendirilecek.
- **Bölge Müdürü Ziyaret Brifingi:** Lig, deney ve playbook olgunlaşmadan içi boş; kendi başına fikir değil, mevcut özelliklerin rol-bazlı görünümü.
- **Ne-Yapmalı Playbook Kartları:** İyi UX fikri ama bağımsız ürün değil; Deney Motoru'nun sunum katmanı olarak birleştirildi.
