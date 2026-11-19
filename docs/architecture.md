# Mimari

Bu belge sunucunun nasıl kurulduğunu anlatır. Higgsfield'den çıkarılan desenler için
[`higgsfield-architecture.md`](higgsfield-architecture.md) dosyasına bakın.

## Katmanlar

```
MCP istemcisi ── stdio | Streamable HTTP (kapı + Bearer anahtar)
      │
server/      ince MCP cephesi: araç tanımları, açıklamalar, yanıt zarfı, widget
      │      (mcp ve starlette yalnızca burada içe aktarılır)
core/        platform: katalog, job motoru, medya, kredi defteri, projeler, workflow, uygulama kaydı
      │
providers/   Replicate · OpenAI · mock        db/  SQLite (SQLAlchemy Core)        blobs/  yerel dosya deposu
```

`core/`, `db/`, `blobs/` ve `providers/` içinde `mcp` veya `starlette` içe aktarılmaz; bunu
`tests/unit/test_layering.py` zorlar. `build_server()` her çağrıda yeni bir `MCPServer` ve `Platform`
üreten bir fabrikadır.

| Dizin | İçerik |
|---|---|
| `server/registry.py`, `envelope.py`, `build.py` | Bildirimsel araç kaydı, `ok` / `fail` / `guard`, kurulum sırası |
| `server/families/` | Araç aileleri: catalog, generate, jobs, media, billing, transforms, projects, workflows, appreg |
| `server/http/` | `gate.py` (kapı), `signing.py` (imzalı URL), `gateway.py` (dosya geçidi), `app.py` |
| `server/ui/` | `results.html` widget'ı ve kaynak kaydı; `server/visibility.py` yalnızca-widget araçlarını gizler |
| `core/jobs/` | `engine.py` (durum makinesi), `runner.py` (arka plan döngüsü), `service.py` (işleyicilerin gördüğü yüz) |
| `core/catalog/` | YAML katalog, istek normalizasyonu, fiyatlama |
| `core/media/` | Tür koklama, saklama, SSRF korumalı indirme (`fetch.py`), sınırlı yerel okuma (`paths.py`) |
| `core/billing/` | Kredi defteri, idempotensi anahtarları |
| `data/` | `catalog/*.yaml`, `workflows/<ad>/SKILL.md` |

## Yanıt sözleşmesi

- Her araç `Annotated[CallToolResult, Model]` döner. Başarı: null'suz kompakt JSON metni + çıktı şemasına
  uyan `structured_content`. Hata: `is_error` + `{"error": {code, message, hint, recovery_tool, retryable}}`.
- İşleyiciler `ToolError` fırlatmaz (SDK metnin başına ek koyar ve JSON bozulur); `guard` `ApiError`'u zarfa çevirir.
- Bilinmeyen üst düzey argüman reddedilir (`unknown_argument`): SDK bunları sessizce atar, yanlış yazılmış
  bir `max_credits` böylece kaybolurdu.
- Modele özel parametreler `params` içinde geçer; bilinmeyen anahtar, yanlış tip ve bilinmeyen model hatadır.
  Sunucunun yaptığı düzeltmeler (kırpma, yuvarlama, en yakın en-boy oranı) `adjustments` içinde bildirilir.
- Her sonuç 512 KiB'in altındadır; listeler `limit` + opak `cursor` ile sayfalanır.

## Job motoru

```
queued ─sahiplen→ submitting ─tanıtıcı kaydedildi→ running ─çıktı saklandı→ completed
   │                  │  └─eşzamanlı sonuç────────────────────────────────→ completed
   │                  └─hata | sahibi öldü→ failed | moderated
   └─iptal | kuyruk süresi→ canceled | failed        running → failed | canceled | moderated
```

- **Araç işleyicisi sağlayıcıyı çağırmaz.** `generate_*` tek bir yazma işleminde doğrular, fiyatlar, bütçeyi
  denetler, `queued` satırlarını ve harcama kayıtlarını yazar. Sağlayıcı çağrıları yalnızca arka plandaki
  yürütücüde yapılır; işleyiciler her `await` noktasında iptal edilebildiği için böyledir.
- **Doğruluk compare-and-set'e dayanır.** Her geçiş `UPDATE … WHERE status = ? AND lease_owner = ?` ve
  etkilenen satır sayısıdır. Kiralar yalnızca çift gönderimi ve boşa yoklamayı önler.
- **Bir iş en fazla bir kez gönderilir.** Sahibi ölmüş bir `submitting` işi yeniden gönderilmez; `interrupted`
  olarak kapatılır ve iade edilir. Yanıtı belirsiz kalan gönderim `submit_outcome_unknown` olur.
- `completed` dışındaki her terminal durum aynı işlemde iade eder; iade tam bir kez yapılır.
- Aynı SQLite dosyasında birden çok süreç olağandır (her stdio istemcisi kendi sürecini açar). Canlı bir
  sahibe, kirası dolmuş göründüğünde (ör. uykudan dönüş) yenilemesi için süre tanınır.
- Son tarih geçtiyse iş zaman aşımına uğratılmadan önce bir kez daha yoklanır; sağlayıcının bu arada
  ürettiği sonuç atılmaz.

## Bütçe

Oluşturmada `spend`, tamamlanmayan her terminal durumda `refund`. Sıra: çağrı tavanı → `max_credits` →
`auto_approve_credits` → bakiye ve günlük tavan. Kullanıcı kararı gereken durum hata değil veridir:
`status: "approval_required"`, hiçbir şey oluşturulmamıştır. Aynı `idempotency_key` + aynı istek, bütçe
veya sağlayıcı durumu değişmiş olsa bile ilk çağrının işlerini döndürür (`replayed: true`).

## Medya

- Dosya türü her giriş yolunda içeriğin ilk baytlarından belirlenir; yalnızca görsel / video / ses kabul edilir.
- stdio'da sonuçlar `local_path`, HTTP'de kısa ömürlü imzalı `url` taşır. Model bir görseli
  `media_get(include_preview=true)` ile görür.
- `media_import_url` her sıçramada adı çözer, **bütün** adreslerin genel olduğunu doğrular, doğrulanan IP'ye
  özgün `Host` ve SNI ile bağlanır ve yönlendirmeleri elle izler. Ortam vekili kullanılıyorsa IP sabitlenemez;
  yalnızca URL metni denetlenir ve `doctor` bunu bildirir.
- `media_import_path` yalnızca yapılandırılmış köklerin altındaki, gizli olmayan normal dosyaları okur.

## HTTP güvenlik modeli

En dışta varsayılanı ret olan saf ASGI bir kapı durur:

1. `Host` izin listesinde değilse 421. Aynı liste SDK'nın kendi denetimine de verilir.
2. Yol tablosu kesindir (`/mcp`, `/healthz`, `/files/med_…`, `/upload/med_…`); gerisi 404.
3. İzin listesinde olmayan `Origin` ve `Sec-Fetch-Site: cross-site` reddedilir; CORS başlığı gönderilmez.
4. `/mcp` için tek bir `Authorization: Bearer` başlığı gerekir; sunulan anahtarın özeti saklanan özetle
   sabit zamanlı karşılaştırılır.

Dosya bağlantıları amaç, medya kimliği, bitiş zamanı, bayt sınırı ve (yüklemede) tek kullanımlık değeri
kapsayan HMAC ile imzalanır; ömrü en fazla 15 dakikadır. Yükleme bağlantısı bir kez kullanılır. Yanıtlar
`nosniff`, `Content-Security-Policy: sandbox` ve koklanmış içerik türüyle gönderilir.

## Widget

`results.html` statiktir ve içerik özetiyle adreslenir (`ui://mcp-tools/results.<özet>.html`). Yalnızca
salt-okunur araçları çağırır (`jobs_wait`, `media_previews`), küçük resimleri `data:` URL olarak alır ve ağa
hiç çıkmaz. DOM yalnızca `createElement` + `textContent` ile kurulur. `tests/widget_harness/run.cjs` widget'ı
Chromium'da, korumalı (opak origin) bir çerçevede ve MCP Apps varsayılan CSP'si altında çalıştırır.

## Araçlar ve Higgsfield karşılıkları

| Bu sunucu | Higgsfield |
|---|---|
| `models_explore` | `models_explore` |
| `generate_image` / `generate_video` / `generate_audio` | aynı adlar |
| `generate_batch` | `generate_image_batch`, `generate_video_batch`, `generate_audio_batch` |
| `estimate_cost` | `estimate_image_cost`, `estimate_video_cost`, `estimate_audio_cost` |
| `jobs_wait`, `job_cancel` | `jobs_wait` |
| `show_jobs`, `show_generations` | `show_generation_by_ids` / `job_display`, `show_generations` |
| `media_upload`, `media_confirm`, `media_import_url` | aynı adlar |
| `media_import_path`, `media_get`, `media_list` | — / `show_medias` |
| `balance`, `transactions`, `preferences_get`, `preferences_update` | `balance`, `transactions`, `get_preferences`, `update_preferences` |
| `upscale_image`, `remove_background` | aynı adlar |
| `projects_*`, `folders_*`, `assets_*` | `create_project`, `list_folders`, `attach_project_asset`, … |
| `workflow_instructions`, `workflow_file` | `get_workflow_instructions`, `get_workflow_bundle_file` |
| `apps_search`, `apps_describe`, `apps_invoke` | aynı adlar |

## Plandan sapmalar

- **Canlı sağlayıcı doğrulaması yapılmadı** (anahtar ve ağ izni yoktu). Katalogdaki Replicate model adları
  ve tüm fiyatlar doğrulanmamış tahminlerdir (`verified_at: null`).
- **`tools/list` boyutu** planlanan 40 KB yerine yaklaşık 60 KB'dir: çıktı şemaları araç başına ~1,7 KB tutar.
  Bütçe testi 80 KB'ye ayarlandı; şemalar başlıklar ve null varyantları atılarak küçültülür.
- **Tek widget:** üretim ve galeri görünümleri tek `results` widget'ında birleşti. Yükleme widget'ı ve
  parçalı yükleme araçları yazılmadı (yerine `media_upload` / `media_import_*`).
- **Uygunluk denemesi:** widget resmî `@modelcontextprotocol/ext-apps` 2.0.4 `AppBridge`'i ile bir kez elle
  doğrulandı; depoda kalan test, protokolün ana uygulama tarafını elle yazılmış bir sayfayla oynatır.
- **`catalog sync` yok;** yerine `doctor --live` katalog alanlarını sağlayıcının model şemasıyla karşılaştırır.
- Workflow'lar MCP prompt'u olarak ayrıca kaydedilmez.
